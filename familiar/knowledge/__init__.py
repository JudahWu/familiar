"""知识库：把 PDF 变成可检索的段落。

和 `app/tools/scm.py` 一样，这是一个**独立于 Agent 运行时的能力层**：
建库、导入、检索都不需要模型参与，HTTP 接口和 Agent 工具都调它。

三处存储各管一段：

    PostgreSQL   knowledge_bases / knowledge_documents
                 库名、文档、状态、进度、段落数 —— 唯一事实来源
    MinIO        <kb_id>/<doc_id>/source.pdf
                 PDF 原件，以及 ③ 阶段切出来的插图
    Milvus       knowledge_chunks
                 段落文本 + BM25 稀疏向量（配了 EMBEDDING_* 才有稠密向量）

删除顺序永远是「先派生物、后事实来源」（Milvus → MinIO → PostgreSQL）。
反过来的话，删到一半失败就再也找不到该清哪些对象了。

历史：元信息最初是 `<workspace>/knowledge/<kb_id>/meta.json` 的文件方案，
2026-10-29 搬到 PostgreSQL。原因是 ③ 的逐页进度会频繁写状态，文件方案每次
都要重写整份 JSON，并发下必丢更新。
"""

from __future__ import annotations

from familiar.knowledge.service import (
    create_base,
    delete_base,
    delete_document,
    get_base_detail,
    import_pdf,
    list_bases,
    search,
)

__all__ = [
    "create_base",
    "delete_base",
    "delete_document",
    "get_base_detail",
    "import_pdf",
    "list_bases",
    "search",
]
