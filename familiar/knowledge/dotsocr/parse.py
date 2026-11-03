"""PDF → 每页 Markdown。整条解析链路只有这一个入口。

流程和原项目一致（`parser.py:370-426`）：

    每页渲染成图（200dpi）→ 版面模型出布局 JSON → post_process 换 bbox
    → layoutjson2md 转 Markdown（图片裁出来转 base64 内联）

为什么逐页并发、而且每线程各开一个 PDF 句柄：
- 逐页是因为版面模型的输入就是"一页的图"，整本喂不进去；
- 并发是因为实测单页 2.9 秒，一本 300 页的书串行要 15 分钟；
- 每线程各开一个 `pymupdf.Document` 是因为 PyMuPDF 的 Document 不是线程安全的，
  共享会偶发崩溃或读到错页。`threading.local()` 拿到的就是本线程私有的句柄。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from io import BytesIO, StringIO
from typing import Callable, Iterator

import pymupdf
from PIL import Image

from familiar.env_utils import get_env
from familiar.knowledge.dotsocr import client as ocr_client
from familiar.knowledge.dotsocr.format_transformer import layoutjson2md
from familiar.knowledge.dotsocr.layout_utils import post_process_output
from familiar.knowledge.dotsocr.prompts import dict_promptmode_to_prompt

logger = logging.getLogger("familiar.knowledge.dotsocr.parse")

PROMPT_MODE = "prompt_layout_all_en"

# 解析失败重试次数。版面模型偶尔会把 JSON 写成半截，重发一次基本能过。
_RETRY = 2

# 进度回调：(已完成页数, 总页数)
ProgressHook = Callable[[int, int], None]

_thread_local = threading.local()
_pdf_bytes: bytes | None = None


@dataclass
class ParsedPage:
    page: int
    markdown: str
    ok: bool = True
    error: str = ""


def _dpi() -> int:
    try:
        return max(72, int(get_env("DOTS_OCR_DPI", "200")))
    except ValueError:
        return 200


def _concurrency() -> int:
    """并发数。默认 6：实测单页 2.9 秒，6 路并发对一张卡比较温和。"""

    try:
        return max(1, min(16, int(get_env("DOTS_OCR_CONCURRENCY", "6"))))
    except ValueError:
        return 6


def _document() -> pymupdf.Document:
    """本线程私有的 PDF 句柄。"""

    doc = getattr(_thread_local, "doc", None)
    if doc is None:
        if _pdf_bytes is None:
            raise RuntimeError("PDF 内容未初始化")
        doc = pymupdf.open(stream=_pdf_bytes, filetype="pdf")
        _thread_local.doc = doc
    return doc


def page_count(content: bytes) -> int:
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return len(doc)


def parse_pdf(
    content: bytes,
    *,
    progress: ProgressHook | None = None,
) -> list[ParsedPage]:
    """解析整本 PDF，返回按页码排序的结果。

    单页失败不影响其他页：那一页标 `ok=False`，上游据此决定是整篇失败还是
    先收下能用的部分（`service.run_import` 会记失败页数）。
    """

    if not ocr_client.available():
        raise ocr_client.DotsOcrError("未配置 DOTS_OCR_BASE_URL，无法解析 PDF")

    global _pdf_bytes
    _pdf_bytes = content
    _thread_local.__dict__.clear()  # 新任务开始，丢掉上一本残留的句柄

    try:
        total = page_count(content)
        logger.info("开始解析 PDF：%d 页，并发 %d，dpi %d", total, _concurrency(), _dpi())

        results: dict[int, ParsedPage] = {}
        done = 0
        # 整个池子外面套一层，不要在每个线程里各套一次：`sys.stdout` 是进程级的，
        # 多线程各自 redirect 会互相踩（一个线程把另一个的 stdout 关掉）。
        with _quiet(), ThreadPoolExecutor(max_workers=_concurrency()) as pool:
            futures = {pool.submit(_parse_page, index): index for index in range(1, total + 1)}
            for future in as_completed(futures):
                page = future.result()
                results[page.page] = page
                done += 1
                if progress:
                    progress(done, total)
        return [results[index] for index in sorted(results)]
    finally:
        # 每个线程的句柄由线程自己结束生命周期；主动清一次，避免长驻进程攒内存
        _thread_local.__dict__.clear()
        _pdf_bytes = None


def _parse_page(page_no: int) -> ParsedPage:
    """解析一页。页码从 1 开始，和 PDF 的实际页序一致。"""

    try:
        doc = _document()
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=_dpi())
        image = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 渲染失败只影响这一页
        logger.warning("第 %d 页渲染失败：%s", page_no, exc)
        return ParsedPage(page=page_no, markdown="", ok=False, error=f"渲染失败：{exc}")

    last_error = ""
    for attempt in range(_RETRY):
        try:
            raw = ocr_client.parse_image(image, prompt_mode=PROMPT_MODE)
            cells, filtered = post_process_output(raw, PROMPT_MODE, image, image)

            if filtered:
                # 模型把 JSON 写坏了。原项目的做法是直接把清洗后的原始输出当正文
                # 写进 md（parser.py:258-282），那种 md 既没有标题也没有图片标记，
                # 后面按标题切分时会被当成一大块纯文本。这里改成重发一次。
                last_error = "布局 JSON 解析失败"
                logger.info("第 %d 页首次解析失败，重试（%d/%d）", page_no, attempt + 1, _RETRY)
                continue

            markdown = layoutjson2md(image, cells, text_key="text").strip()
            if not markdown:
                last_error = "版面识别没有产出内容"
                continue
            return ParsedPage(page=page_no, markdown=markdown)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("第 %d 页解析异常（%d/%d）：%s", page_no, attempt + 1, _RETRY, exc)

    return ParsedPage(page=page_no, markdown="", ok=False, error=last_error)


def prompt_modes() -> list[str]:
    """可用的提示模式，排查时用。"""

    return list(dict_promptmode_to_prompt.keys())


@contextmanager
def _quiet() -> Iterator[None]:
    """吞掉移植代码的 print。

    `output_cleaner` 在 JSON 清洗失败时会把原始输出整段打出来（实测一次 26K 字符），
    一本 300 页的书能刷出几十兆日志，把真正的告警埋掉。这些信息对我们没用——
    失败已经被翻译成 `ParsedPage.error` 和一条 warning。
    """

    buffer = StringIO()
    with redirect_stdout(buffer):
        yield

