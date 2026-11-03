"""每页 Markdown → 检索块。

对齐 `splitters/splitter_md.py` 的思路，但没用 `langchain_text_splitters`：
这个环境里没装它，而我们要的逻辑只有三条——按 `#` 级别切、把标题层级继承下来、
超长块再语义细分。自己写四十行比引一个会牵动 langchain-core 版本的包划算。

三件事，顺序不能换：

1. **抽图**：dots_ocr 把插图裁出来转成 base64 内联在 md 里
   （`format_transformer.py:171`），正文里留一个 `[图片]` 占位。图要单独成条目
   才好检索——"用文字搜图"靠的就是图的描述而不是它所在的那段正文。
2. **按标题切**：`#`/`##`/`###` 是硬边界。段落级切分不认标题，会把"A 小节最后
   一段"和"B 小节第一段"拼成一个块，检索出来出处就错了。
3. **超长再语义切**：只有超过 `SEMANTIC_TRIGGER` 的块才动用 embedding 细切。
   全量跑语义切分的调用次数随页数线性涨，而收益只体现在长块上。

标题层级是**跨页继承**的：一章的第一页给出 `# 第二章`，后面十几页不会重复写，
所以标题状态要在页与页之间滚动（`splitter_md.py:131` 的 `add_title_hierarchy`
做的就是这件事）。不继承的话，一本书九成的块 `title` 都是空的。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field

from familiar.knowledge.extract import CHUNK_SIZE, Chunk, semantic_segments

logger = logging.getLogger("familiar.knowledge.chunk")

# 超过这个长度才动用语义切分。原项目也是 1000（splitter_md.py:20）。
SEMANTIC_TRIGGER = 1000

# 语义切分之后仍可能超长（一整段没标点的代码），最后再按字数硬切兜底。
HARD_LIMIT = 1200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# dots_ocr 输出的图片是标准 markdown 内联 base64：![](data:image/png;base64,xxx)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(data:image/([a-zA-Z0-9.+-]+);base64,([^)\s]+)\)")


@dataclass
class PageImage:
    page: int
    index: int
    data: bytes
    ext: str = "png"


@dataclass
class SplitResult:
    chunks: list[Chunk] = field(default_factory=list)
    images: list[PageImage] = field(default_factory=list)
    # 每页去掉图片占位后的正文，给"图片描述"当上下文用
    page_texts: dict[int, str] = field(default_factory=dict)


def split_pages(pages: list, *, semantic: bool = True) -> SplitResult:  # noqa: ANN001
    """把解析结果切成检索块，同时把插图抽出来。

    `semantic=False` 关掉超长块的语义细分，只按字数硬切。`KNOWLEDGE_SEMANTIC_CHUNK`
    落到这里——关掉能省掉导入时的大部分 embedding 调用，代价是长块边界变成
    "每 800 字切一刀"。
    """

    result = SplitResult()
    header_state = {1: "", 2: "", 3: ""}
    chunk_index = 0
    image_index = 0

    for page in pages:
        if not getattr(page, "ok", True):
            continue  # 解析失败的页没有内容可用

        text, images = _take_images(getattr(page, "markdown", ""), page.page)
        image_index = _append_images(result.images, images, page.page, image_index)
        result.page_texts[page.page] = text

        for body, headings in _blocks(text, header_state):
            for piece in _fit(body, semantic):
                result.chunks.append(
                    Chunk(
                        chunk_index=chunk_index,
                        text=piece,
                        page=page.page,
                        title=" > ".join(h for h in headings if h),
                    )
                )
                chunk_index += 1

    return result


def _take_images(markdown: str, page: int) -> tuple[str, list[PageImage]]:
    """把内联 base64 图抠出来，原位留 `[图片]`。"""

    images: list[PageImage] = []

    def _replace(match: re.Match[str]) -> str:
        ext = match.group(1).lower()
        if ext not in {"png", "jpg", "jpeg"}:
            ext = "png"
        try:
            data = base64.b64decode(match.group(2))
        except Exception:  # noqa: BLE001 - 坏图直接丢掉占位即可，不要因此整页失败
            logger.debug("第 %d 页有一张图解不出来，跳过", page)
            return ""
        images.append(PageImage(page=page, index=0, data=data, ext=ext))
        return "[图片]"

    return _IMAGE_RE.sub(_replace, markdown or ""), images


def _append_images(
    bucket: list[PageImage], images: list[PageImage], page: int, start: int
) -> int:
    for offset, image in enumerate(images):
        image.page = page
        image.index = start + offset
        bucket.append(image)
    return start + len(images)


def _blocks(markdown: str, header_state: dict[int, str]) -> list[tuple[str, list[str]]]:
    """按 `#` 级标题切块，返回 [(正文, [H1, H2, H3])]。

    `header_state` 是跨页滚动的：遇到高一级标题就把低级的清空，和
    `splitter_md.add_title_hierarchy` 的规则一致。
    """

    blocks: list[tuple[str, list[str]]] = []
    buffer: list[str] = []

    def _flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            blocks.append((body, [header_state[1], header_state[2], header_state[3]]))

    for line in (markdown or "").split("\n"):
        match = _HEADING_RE.match(line)
        if not match:
            buffer.append(line)
            continue
        level = min(3, len(match.group(1)))
        _flush()
        header_state[level] = match.group(2).strip()
        for lower in range(level + 1, 4):
            header_state[lower] = ""

    _flush()
    return blocks


def _fit(text: str, semantic: bool = True) -> list[str]:
    """超长块的处理：先语义切，再按字数硬切兜底。"""

    body = (text or "").strip()
    if not body:
        return []
    if not semantic or len(body) <= SEMANTIC_TRIGGER:
        return _hard_split(body)

    segments = semantic_segments(body)
    if not segments:
        return _hard_split(body)

    pieces: list[str] = []
    for segment in segments:
        pieces.extend(_hard_split(segment))
    return [piece for piece in pieces if piece.strip()]


def _hard_split(text: str) -> list[str]:
    body = text.strip()
    if len(body) <= HARD_LIMIT:
        return [body] if body else []
    step = max(1, CHUNK_SIZE)
    return [body[i : i + step].strip() for i in range(0, len(body), step) if body[i : i + step].strip()]
