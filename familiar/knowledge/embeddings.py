"""向量与视觉模型的调用封装。

这个模块只管"怎么调外部模型"，没有业务语义；谁该调、失败了怎么办，由
`service.py` 决定。

两条链路：
- `embed_texts` / `embed_image`：多模态向量。**文本和图片必须用同一个模型**，
  它们落在同一个 dense 字段里，不同模型产生的向量之间距离没有意义。
- `describe_image`：给书里的插图写一段中文描述，走 OpenAI 兼容端点。

为什么 embedding 不能直接走现有的 OpenAI 兼容客户端：实测阿里的多模态
embedding（`multimodal-embedding-v1`）在兼容模式的 `/embeddings` 上返回 404，
只有 DashScope 原生 SDK 能调。所以这里按 `EMBEDDING_PROVIDER` 分派。
"""

from __future__ import annotations

import base64
import logging
import threading
import time

from familiar.core.settings import (
    ALIBABA_API_KEY,
    ALIBABA_BASE_URL,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    VISION_MODEL,
)
from familiar.env_utils import get_env

logger = logging.getLogger("familiar.knowledge.embeddings")

# 限流：外部 embedding 接口有 RPM 限制，打满了会 429。
# 固定窗口计数，够用且好懂——不是滑动窗口那种精确算法，但足够避免被打爆。
_RPM = 60

# 单次请求的两个硬限制，都是 DashScope 多模态 embedding 的实测值：
#   - 条数不超过 20（"batch size is invalid, it should not be larger than 20"）
#   - 所有文本总长度不超过 10240 字符（"text total input length is invalid"）
# 800 字一段的 chunk，20 段就是 16000 字符，所以两个限制都得管。
_MAX_BATCH = 20
_MAX_TOTAL_CHARS = 10000


def _batches(texts: list[str]) -> list[list[str]]:
    """按条数和总字符数切批。两个限制任一触发就开新批。"""

    batches: list[list[str]] = []
    current: list[str] = []
    length = 0
    for text in texts:
        size = len(text)
        if current and (len(current) >= _MAX_BATCH or length + size > _MAX_TOTAL_CHARS):
            batches.append(current)
            current = []
            length = 0
        current.append(text)
        length += size
    if current:
        batches.append(current)
    return batches


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._count = 0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self.window_seconds:
                self._window_start = now
                self._count = 0
            if self._count >= self.limit:
                wait = self.window_seconds - (now - self._window_start)
                if wait > 0:
                    logger.info("达到 %d 次/分钟限速，等待 %.1fs", self.limit, wait)
                    time.sleep(wait)
                self._window_start = time.monotonic()
                self._count = 0
            self._count += 1


_limiter = RateLimiter(_RPM)


class EmbeddingError(RuntimeError):
    """向量生成失败。调用方决定是让整篇文档失败，还是降级只走 BM25。"""


class VisionError(RuntimeError):
    """图片描述生成失败。"""


def is_configured() -> bool:
    """向量服务是否可用。False 表示只做 BM25 检索。"""

    if EMBEDDING_PROVIDER == "dashscope":
        return bool(ALIBABA_API_KEY and EMBEDDING_MODEL)
    return bool(EMBEDDING_BASE_URL and EMBEDDING_API_KEY and EMBEDDING_MODEL)


def _data_uri(image_bytes: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


# ---------------------------------------------------------------- 文本向量


def embed_texts(texts: list[str]) -> list[list[float]]:
    """给一批文本生成向量。

    失败直接抛 EmbeddingError —— 导入场景下宁可整篇失败重试，也不要写进去
    一堆没有向量的行（那样检索会静默变差，很难发现）。
    """

    if not texts:
        return []
    vectors: list[list[float]] = []
    for batch in _batches(texts):
        if EMBEDDING_PROVIDER == "dashscope":
            vectors.extend(_embed_dashscope([{"text": text} for text in batch]))
        else:
            vectors.extend(_embed_openai(batch))
    return vectors


def embed_image(image_bytes: bytes, caption: str) -> list[float]:
    """给一张图生成向量。

    `caption` 是这张图的文字描述（由 qwen3-vl-plus 生成）。图和描述一起送进
    模型，这样向量里同时含视觉信息和语义信息——纯图像向量在"用文字搜图"
    时命中率很低，纯描述向量又丢了视觉细节。
    """

    uri = _data_uri(image_bytes)
    if EMBEDDING_PROVIDER == "dashscope":
        vectors = _embed_dashscope([{"image": uri, "text": caption}])
    else:
        # OpenAI 兼容模式的多模态 embedding 格式各家不同，这里只支持纯文本
        # provider 下的图片输入——本地部署的多模态 embedding 服务按各自的
        # 约定填，这里保守地退化成"只描述文本"。
        vectors = _embed_openai([caption])
    if not vectors:
        raise EmbeddingError("图片向量为空")
    return vectors[0]


def embed_query_image(image_bytes: bytes) -> list[float]:
    """给"用户上传的查询图片"生成向量。

    和 `embed_image` 的差别：那个是给书里的插图编码，图 + 视觉模型写的描述一起送，
    向量里带语义；这里是一张随手截的图，没有描述可带，只送图本身。

    只送图在 DashScope 上可用（demo 的 `retrieve()` 就是这样传的）。而 OpenAI 兼容
    模式没有统一的多模态输入格式，所以那边直接报错——不支持就明确说不支持，别
    偷偷退化成"用文件名搜"这种假功能。
    """

    if EMBEDDING_PROVIDER != "dashscope":
        raise EmbeddingError(
            "图片检索需要多模态 embedding（EMBEDDING_PROVIDER=dashscope），"
            f"当前是 {EMBEDDING_PROVIDER or '未配置'}"
        )
    vectors = _embed_dashscope([{"image": _data_uri(image_bytes)}])
    if not vectors:
        raise EmbeddingError("图片查询向量为空")
    return vectors[0]


def _embed_dashscope(inputs: list[dict[str, str]]) -> list[list[float]]:
    import dashscope

    _limiter.acquire()
    try:
        response = dashscope.MultiModalEmbedding.call(
            model=EMBEDDING_MODEL,
            input=inputs,
            api_key=ALIBABA_API_KEY,
        )
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"DashScope 调用失败：{type(exc).__name__}: {exc}") from exc

    if getattr(response, "status_code", None) != 200:
        raise EmbeddingError(
            f"DashScope 返回 {getattr(response, 'status_code', '?')}："
            f"{getattr(response, 'code', '')} {getattr(response, 'message', '')}"
        )
    try:
        return [item["embedding"] for item in response.output["embeddings"]]
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"解析向量失败：{exc}") from exc


def _embed_openai(texts: list[str]) -> list[list[float]]:
    import httpx

    payload = {"model": EMBEDDING_MODEL, "input": texts}
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"},
                json=payload,
            )
        response.raise_for_status()
        data = response.json()["data"]
        # OpenAI 兼容接口不保证顺序，按 index 排一次
        data.sort(key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"embedding 接口调用失败：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- 图片描述


def describe_image(image_bytes: bytes, context: str = "", *, max_chars: int = 300) -> str:
    """用视觉模型给插图写一段中文描述。

    `context` 是这张图所在页的正文。提示词允许模型结合上下文推断"这张图在讲什么"，
    这样描述里会带上图的用途（而不只是画面内容），检索时命中率更高——代价是
    模型可能顺着上下文编造图里没有的细节，所以要求它把"看到的"和"推断的"
    分开说。

    长度上限 300 汉字，和原项目一致（`db_operator.py:126`）。太短描述不出图表
    结构，太长会把图片条目的 text 撑得比正文块还长，BM25 权重失衡。

    失败抛 VisionError。
    """

    if not ALIBABA_API_KEY or not ALIBABA_BASE_URL:
        raise VisionError("未配置 ALIBABA_API_KEY / ALIBABA_BASE_URL，无法生成图片描述")

    context_block = f"\n图片前后的正文：\n{context.strip()}\n" if context.strip() else ""
    prompt = (
        "你是技术书籍的插图标注助手。下面给你一张书里的插图，以及它所在页的正文。\n"
        f"{context_block}\n"
        "请写一段中文描述，要求：\n"
        "1. 先客观描述图里有什么（界面、图表、流程节点、代码等），这是事实部分；\n"
        "2. 再结合正文，说明这张图在讲什么、起什么作用，这是推断部分，"
        "不确定的地方用「推测」二字开头；\n"
        "3. 总长度不超过 300 个汉字；\n"
        "4. 只用中文，不要输出 Markdown 语法。"
    )

    import httpx

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_uri(image_bytes)},
                    },
                ],
            }
        ],
        "max_tokens": 512,
    }
    try:
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{ALIBABA_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {ALIBABA_API_KEY}"},
                json=payload,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise VisionError(f"视觉模型调用失败：{type(exc).__name__}: {exc}") from exc

    text = (content or "").strip()
    if not text:
        raise VisionError("视觉模型返回空描述")
    return text[:max_chars]


def ocr_available() -> bool:
    """版面识别服务是否可用。没配就走降级路径。"""

    return bool(get_env("OCR_BASE_URL", "").strip())
