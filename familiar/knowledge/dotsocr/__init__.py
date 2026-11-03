"""dots_ocr 版面解析：PDF → 每页 Markdown。

`utils/` 下的六个文件是从 `Multimodal_RAG/dots_ocr/utils/` 原样搬过来的
（consts / image_utils / doc_utils / layout_utils / format_transformer /
output_cleaner / prompts），只改了导入路径。没重写的原因很实际：里面是
bbox 换算、LaTeX/HTML 清洗、跨页表格拼接这些细活，共 1350 行，重写一遍只会
引入新 bug。

入口只有两个：`parse_pdf` 和 `available`。
"""

from __future__ import annotations

from familiar.knowledge.dotsocr.client import DotsOcrError, available, health
from familiar.knowledge.dotsocr.parse import ParsedPage, parse_pdf, page_count

__all__ = [
    "DotsOcrError",
    "ParsedPage",
    "available",
    "health",
    "page_count",
    "parse_pdf",
]
