"""PDF → 文本 → 分块（阶段 ② 只做"有文字层"的部分）。

设计原则：**能直接抽就不猜**。PyMuPDF 对电子书这类 PDF 抽出来的文字通常比任何
OCR 都准，所以先按页抽，抽不到字的页记下来交给 ③ 的版面识别处理，不在这里
硬凑内容。

分块策略（两级）：
1. **按页抽文本、页内按空行拆段**，顺手记下"段前最近的标题行"，检索结果里能显示
   「第六章 > 6.3 数据流与算子」这种出处，比只给页码有用。
2. **语义合并**（`semantic=True` 且配了 embedding 时）：把同一页的段落各自向量化，
   相邻段相似度骤降的位置就是天然边界，据此把段落合成语义块，再按 800 字上限装箱。

为什么不照 demo 用句级 `SemanticChunker`：它要按句子算向量，一本 300 页的书是几千次
调用；而 PyMuPDF 抽出来的段落本身就是语义单元，按段算只要几百次，效果差别不大、
成本差一个数量级。相似度阈值用百分位而不是固定值——不同书的段落间基线相似度差很多，
固定阈值在一本书上刚好、换一本就全切断或全不切。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf  # PyMuPDF 1.24+ 的新模块名，fitz 已弃用

logger = logging.getLogger("familiar.knowledge.extract")

# 一页文字少于这个长度就认为"没有文字层"，交给版面识别。
_MIN_TEXT_CHARS = 30

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# 语义合并只在"这页文字超过 CHUNK_SIZE"时做：短页本来就是一个完整语义单元，
# 为一页两段话算两次向量纯属浪费。
SEMANTIC_PERCENTILE = 70.0
# 切出来的段太短就没意义（等于没合并），并进下一段。
MIN_SEGMENT_CHARS = 120
# 一行。太长的页改用固定切分——那种页多是密集正文，语义边界本来就不明显，
# 而句级 embedding 的次数会翻好几倍。
SEMANTIC_MAX_SENTENCES = 40
# 语义分段会切出很短的碎片（实测有 7 字的块）。这种块检索时只会当噪音，
# 同页内合并回上一块；跨页不合并——那会把两页的内容混在一个出处里。
MIN_CHUNK_CHARS = 80

# 形如「第六章 xxx」「6.3 xxx」「3 数据流」的行，视为标题
_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百零]+[章节]|\d+(?:\.\d+){0,3})[\s、.．]?\S")


@dataclass
class PageText:
    page: int
    text: str

    @property
    def has_text_layer(self) -> bool:
        return len(self.text.strip()) >= _MIN_TEXT_CHARS


@dataclass
class Chunk:
    chunk_index: int
    text: str
    page: int
    title: str


def extract_pages(source: Path | bytes) -> list[PageText]:
    """逐页抽文本。加密或损坏的 PDF 直接抛错，由上层转成文档的失败态。

    接受 bytes 是因为原件现在存在 MinIO 里：先落盘再读纯属多余，
    PyMuPDF 本来就能直接吃内存里的流（`stream=` + `filetype=`）。
    """

    pages: list[PageText] = []
    doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes) else pymupdf.open(source)
    with doc:
        for index, page in enumerate(doc, start=1):
            # "text" 会保留换行，但会把同段内的硬换行也带出来，后面按空行拆段前
            # 先做一次规整。
            raw = page.get_text("text") or ""
            pages.append(PageText(page=index, text=_normalize_newlines(raw)))
    return pages


def _normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 行尾空白去掉；连续 3 个以上空行压成 2 个（保留段落分隔的语义）
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text)


def page_stats(pages: list[PageText]) -> tuple[int, int]:
    """返回 (有文字层的页数, 需要版面识别的页数)。"""

    text_pages = sum(1 for page in pages if page.has_text_layer)
    return text_pages, len(pages) - text_pages


def _split_paragraphs(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text)]
    return [block for block in blocks if block]


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and len(stripped) <= 40 and bool(_HEADING_RE.match(stripped))


def semantic_segments(text: str) -> list[str] | None:
    """给外部用的语义分段入口（`chunk.py` 复用）。

    返回 None 表示"这次没做语义分段"（句子太少/太多、没配 embedding、调用失败），
    调用方退回按字数硬切。这个约定和 `_semantic_units` 一致。
    """

    block = (text or "").strip()
    if not block:
        return None
    return _semantic_units([block])


def chunk_pages(pages: list[PageText], *, semantic: bool = True) -> list[Chunk]:
    """把逐页文本切成检索单元。

    没有文字层的页会被跳过（它们的正文要等 ③ 的版面识别补上），
    但页码信息保留，方便以后回填。

    `semantic=True` 时会先做语义分段再装箱；embedding 不可用或调用失败都退回
    按段落直接合并——切分质量下降总比导入失败好。
    """

    chunks: list[Chunk] = []
    current_title = ""
    index = 0

    for page in pages:
        if not page.has_text_layer:
            continue
        blocks = _split_paragraphs(page.text)
        for block in blocks:
            first_line = block.split("\n", 1)[0]
            if _is_heading(first_line):
                current_title = first_line.strip()

        segments = (
            _semantic_units(blocks) if semantic and len(page.text) > CHUNK_SIZE else None
        )

        if segments:
            # 语义边界是**硬边界**：每个段单独成块，不再往回合并。
            # 之前这里复用了下面的装箱逻辑，结果 60 字的语义段全被合并回 800 字一块，
            # 语义信息一点没起作用（实测块数只从 29 变 28）。想清楚这件事的关键是：
            # 装箱的目标是"不超上限"，而语义分段的目标是"在意思变了的地方断开"，
            # 后者不该为了填满上限而让步。
            for segment in segments:
                pieces = _hard_split(segment) if len(segment) > CHUNK_SIZE else [segment]
                for piece in pieces:
                    if piece.strip():
                        chunks.append(
                            Chunk(
                                chunk_index=index,
                                text=piece.strip(),
                                page=page.page,
                                title=current_title,
                            )
                        )
                        index += 1
            continue

        buffer = ""
        # 单段就超长时直接硬切，避免一个巨型段落把整块撑爆
        for block in blocks:
            pieces = _hard_split(block) if len(block) > CHUNK_SIZE else [block]
            for piece in pieces:
                if buffer and len(buffer) + len(piece) + 2 > CHUNK_SIZE:
                    chunks.append(
                        Chunk(chunk_index=index, text=buffer.strip(), page=page.page, title=current_title)
                    )
                    index += 1
                    buffer = _tail(buffer)
                buffer = f"{buffer}\n\n{piece}" if buffer else piece
        if buffer.strip():
            chunks.append(Chunk(chunk_index=index, text=buffer.strip(), page=page.page, title=current_title))
            index += 1

    return _merge_tiny(chunks)


def _merge_tiny(chunks: list[Chunk]) -> list[Chunk]:
    """把同页里过短的块并回上一块，然后重新编号。

    语义分段会切出很短的碎片（实测有 7 字的块），那种块检索时只会当噪音。
    只并同一页：跨页合并会让出处变错——用户点开引用看到的是另一页的内容。
    """

    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk.text) < MIN_CHUNK_CHARS
            and merged[-1].page == chunk.page
            and len(merged[-1].text) + len(chunk.text) + 2 <= CHUNK_SIZE
        ):
            previous = merged[-1]
            merged[-1] = Chunk(
                chunk_index=previous.chunk_index,
                text=f"{previous.text}\n\n{chunk.text}",
                page=previous.page,
                title=previous.title,
            )
        else:
            merged.append(chunk)
    return [
        Chunk(chunk_index=index, text=chunk.text, page=chunk.page, title=chunk.title)
        for index, chunk in enumerate(merged)
    ]


def _semantic_units(blocks: list[str]) -> list[str] | None:
    """按语义把相邻单元合成更大的块。

    两级降级，原因很实际：**PyMuPDF 抽出来的"段落"经常不可靠**——实测这本 Flink
    教材每页只有 1 个段落（正文里没有空行），按段算等于什么都没合并。所以：
    - 一页有 3 段以上 → 按段落算（便宜，一本书几百次调用）；
    - 只有 1-2 段 → 退到句子级，否则这个功能在这类 PDF 上等于不存在。

    句子太多（> SEMANTIC_MAX_SENTENCES）就放弃语义、退回固定合并：那种页通常是
    密集正文，切不切语义差别不大，但 embedding 次数会翻好几倍。

    返回 `None` 表示"这次没做语义分段"（调用方退回普通装箱）；返回列表表示分段成功，
    每个段就是一个硬边界。

    任何异常都返回 None：切分是"让检索更准"，不该成为导入失败的原因。
    """

    joiner = "\n\n"
    if len(blocks) >= 3:
        units = blocks
    else:
        units = _split_sentences("\n\n".join(blocks))
        joiner = ""
        if len(units) < 4 or len(units) > SEMANTIC_MAX_SENTENCES:
            return None

    try:
        # 延迟导入：extract 在没配 embedding 的环境里也要能用来抽文本。
        from familiar.knowledge import embeddings

        if not embeddings.is_configured():
            return None
        vectors = embeddings.embed_texts(units)
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("语义分段失败，退回按段落合并", exc_info=True)
        return None

    if len(vectors) != len(units):
        return None

    distances = [1.0 - _cosine(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    # 距离全都一样（比如整页是重复的模板文字）时百分位没有意义，阈值会退化成 0，
    # 于是每个位置都"该断"——那不是语义切分，是乱切。这种页直接走普通装箱。
    if not distances or max(distances) - min(distances) < 1e-6:
        return None
    threshold = _percentile(distances, SEMANTIC_PERCENTILE)

    segments: list[str] = []
    start = 0
    size = len(units[0])
    for i, distance in enumerate(distances):
        # 只在"确实该断"且"当前段已经够长"时断开，避免切出一堆两行的小段
        if distance >= threshold and size >= MIN_SEGMENT_CHARS:
            segments.append(joiner.join(units[start : i + 1]))
            start = i + 1
            size = 0
        size += len(units[i + 1])
    segments.append(joiner.join(units[start:]))
    return [segment for segment in segments if segment.strip()]


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点和换行切句。保留标点，拼回去时不用补。"""

    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [part.strip() for part in parts if part and part.strip()]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _percentile(values: list[float], percent: float) -> float:
    """线性插值的百分位。数据量很小（一页几段），不值得为它引入 numpy。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _tail(text: str) -> str:
    """取上一块结尾的一小段作为重叠，保证跨块句子两边都能命中。"""

    return text[-CHUNK_OVERLAP:] if len(text) > CHUNK_OVERLAP else text


def _hard_split(text: str) -> list[str]:
    return [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP)]
