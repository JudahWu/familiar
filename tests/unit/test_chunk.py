"""知识库切分（chunk）的单元测试。

只测确定性逻辑：抽内联图、按标题切块、跨页标题继承、超长硬切。
语义切分（`semantic_segments`）依赖 embedding，这里用 `semantic=False` 避开。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

from familiar.knowledge.chunk import (
    HARD_LIMIT,
    _blocks,
    _hard_split,
    _take_images,
    split_pages,
)


def _page(page: int, markdown: str, ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(page=page, markdown=markdown, ok=ok)


def test_take_images_extracts_base64_and_placeholder() -> None:
    payload = base64.b64encode(b"fake-image-bytes").decode()
    md = f"正文\n![](data:image/png;base64,{payload})\n继续"
    text, images = _take_images(md, page=3)
    assert "base64" not in text
    assert "[图片]" in text
    assert len(images) == 1
    assert images[0].page == 3
    assert images[0].ext == "png"
    assert images[0].data == b"fake-image-bytes"


def test_blocks_split_by_heading() -> None:
    header = {1: "", 2: "", 3: ""}
    md = "# 第一章\n正文 A\n## 1.1 小节\n正文 B\n正文 C"
    blocks = _blocks(md, header)
    bodies = [body for body, _ in blocks]
    assert "正文 A" in bodies[0]
    assert "正文 B" in bodies[1]
    # 第二个块继承 H1 + H2
    assert blocks[1][1] == ["第一章", "1.1 小节", ""]


def test_blocks_heading_clears_lower_levels() -> None:
    header = {1: "第一章", 2: "1.1", 3: "1.1.1"}
    md = "# 第二章\n新正文"
    blocks = _blocks(md, header)
    # 遇到新的 H1，H2/H3 被清空
    assert blocks[0][1] == ["第二章", "", ""]


def test_split_pages_inherits_heading_across_pages() -> None:
    pages = [
        _page(1, "# 第二章\n第一页正文"),
        _page(2, "第二页正文（没写标题）"),
    ]
    result = split_pages(pages, semantic=False)
    assert len(result.chunks) == 2
    assert result.chunks[0].title == "第二章"
    # 第二页继承了第一页的 H1
    assert result.chunks[1].title == "第二章"
    assert result.chunks[1].page == 2


def test_split_pages_skips_failed_page() -> None:
    pages = [_page(1, "正文", ok=False), _page(2, "# 标题\n正常正文")]
    result = split_pages(pages, semantic=False)
    assert len(result.chunks) == 1
    assert result.chunks[0].page == 2


def test_hard_split_short_text() -> None:
    assert _hard_split("短文本") == ["短文本"]


def test_hard_split_long_text() -> None:
    long = "字" * (HARD_LIMIT + 500)
    pieces = _hard_split(long)
    assert len(pieces) > 1
    assert all(len(piece) <= HARD_LIMIT for piece in pieces)
    # 拼接回去内容不丢
    assert "".join(pieces) == long
