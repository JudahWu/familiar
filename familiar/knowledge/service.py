"""知识库用例层：建库、导入 PDF、检索、删除。

上层（HTTP 接口、Agent 工具）只调这里，不直接碰 PostgreSQL / MinIO / Milvus。

三处存储的角色：
- PostgreSQL：元信息，**唯一事实来源**；
- MinIO：PDF 原件（`objects.py`）；
- Milvus：能搜到的段落（`schema.py`）。

所以删除顺序永远是「先派生物、后事实来源」：Milvus → MinIO → PostgreSQL。
中途失败时库里还留着记录，用户能看出哪个库/文档没删干净；反过来先删库的话，
删到一半失败就再也找不到该清哪些对象了。

一个刻意的取舍：**导入是同步的**。阶段 ② 的目标是把"搜不搜得到"跑通，同步导入
让链路最短、问题最容易定位；③ 再换成后台任务 + SSE 进度（那时才有"解析 42%"
这种中间状态）。所以现在 `status` 只会是 pending → parsing → indexed/failed，
中间的百分比一律 0。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from familiar.core.events import record_event
from familiar.core.telemetry import record_kb_import
from familiar.env_utils import get_env
from familiar.knowledge import chunk as chunking
from familiar.knowledge import dotsocr, embeddings, images, memory, objects, schema, store, tasks
from familiar.knowledge.extract import Chunk, chunk_pages, extract_pages, page_stats

logger = logging.getLogger("familiar.knowledge.service")

# 进度回调：(阶段, 已完成, 总数)。阶段是 "text" 或 "image"。
ProgressHook = Callable[[str, int, int], None]

# 一次 insert 的行数。太大容易顶到 Milvus 的 gRPC 消息上限，太小又慢。
_INSERT_BATCH = 200

# 稠密与稀疏结果的融合参数（Reciprocal Rank Fusion）。
_RRF_K = 60


def list_bases(owner: str | None = None) -> list[dict[str, Any]]:
    """列表页要的数据：库 + 每个库的文档 + 段落总数。"""

    result: list[dict[str, Any]] = []
    for base in store.list_bases(owner=owner):
        result.append(
            {
                "id": base.id,
                "name": base.name,
                "created_at": base.created_at,
                "chunks": store.chunk_total(base.id),
                "documents": [_doc_payload(doc) for doc in base.documents],
            }
        )
    return result


def get_base_detail(kb_id: str) -> dict[str, Any]:
    base = store.get_base(kb_id)
    return {
        "id": base.id,
        "name": base.name,
        "created_at": base.created_at,
        "chunks": store.chunk_total(base.id),
        "documents": [_doc_payload(doc) for doc in base.documents],
    }


def _doc_payload(doc: store.DocumentMeta) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "filename": doc.filename,
        "size_bytes": doc.size_bytes,
        "pages": doc.pages,
        "status": doc.status,
        "progress": doc.progress,
        "paragraphs": doc.paragraphs,
        "images": doc.images,
        "text_pages": doc.text_pages,
        "vision_pages": doc.vision_pages,
        "error": doc.error,
        "updated_at": doc.updated_at,
    }


def create_base(name: str, *, owner: str = "") -> dict[str, Any]:
    base = store.create_base(name, owner=owner)
    # 建库时就把 collection 建好，避免第一次导入才发现 Milvus 连不上。
    schema.ensure_collection()
    return get_base_detail(base.id)


def delete_base(kb_id: str) -> None:
    """删一个知识库。

    **库里有文档时不允许删**——整个库连带删掉是不可逆的，而用户往往只是想
    清掉其中一本书。让人先把文档删干净，误操作的成本就小得多。
    前端也按同一条规则隐藏删除按钮（见 KnowledgeSidebar），两边一致。
    """

    base = store.get_base(kb_id)  # 不存在就先 404
    if base.documents:
        raise ValueError(
            f"知识库「{base.name}」里还有 {len(base.documents)} 篇文档，请先删除它们再删除知识库。"
        )
    # 走到这里库一定是空的，所以没有在跑的导入要取消（原来这里有一段
    # `for doc in base.documents: tasks.cancel(...)`，但 documents 已经为空，
    # 那段永远执行不到，是死代码）。
    schema.delete_base(kb_id)
    objects.delete_prefix(f"{kb_id}/")
    # 记忆索引也是派生物：库都没了，它名下的问答历史不该还能被召回。
    # 顺序和别处一致——先派生物、后事实来源（PG 里 sessions 靠外键 CASCADE 带走）。
    memory.forget(kb_id=kb_id)
    store.delete_base(kb_id)


def delete_document(kb_id: str, doc_id: str) -> None:
    """删一篇文档：Milvus 向量、MinIO 对象、PG 元信息三处都清。

    顺序是有讲究的：**先取消后台任务**。否则导入线程还在跑，会把刚删掉的
    向量和图片又写回去，用户看到的是"删了又冒出来"。

    MinIO 删的是**整个文档前缀** `<kb_id>/<doc_id>/`，不只删原件——插图也在
    同一个前缀下，只删原件会留下一堆插图孤儿。
    """

    store.get_document(kb_id, doc_id)  # 文档不存在会抛 FileNotFoundError，先校验再动手
    tasks.cancel(doc_id)
    schema.delete_document(doc_id)
    objects.delete_prefix(f"{kb_id}/{doc_id}/")
    store.delete_document(kb_id, doc_id)
    logger.info("已删除文档：kb=%s doc=%s", kb_id, doc_id)


def _register_document(kb_id: str, filename: str, content: bytes) -> tuple[store.DocumentMeta, bool]:
    """登记一篇文档：原件存 MinIO + 在 PG 里建行。

    返回 (文档, 是否命中重复)。重复时不会新建，直接返回已有那条。
    """

    checksum = hashlib.sha256(content).hexdigest()
    duplicate = store.find_by_checksum(kb_id, checksum)
    if duplicate:
        return duplicate, True
    doc = store.add_document(kb_id, filename=filename, content=content, checksum=checksum)
    return doc, False


def run_import(
    kb_id: str,
    doc_id: str,
    filename: str,
    content: bytes,
    *,
    progress: ProgressHook | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """解析一篇已登记的文档并入库。

    任何一步失败都会把文档标成 failed 并记录原因，同时清掉已写入的向量，
    避免留下"库里查得到但文档显示失败"的半成品。

    **失败时不删 MinIO 里的原件**：那是用户上传的唯一一份，删掉他本地若也没了
    就彻底没了。原件留在桶里，文档记录上也留着 object_key，排查和重试都用得上；
    真正的清理发生在用户删这篇文档时。

    `cancel_event` 用于响应"用户把文档删了"：置位后在关键节点抛
    `tasks.ImportCancelled`，由调用方清理已经写入的中间产物。
    """

    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise tasks.ImportCancelled(f"导入被取消：{doc_id}")

    doc = store.get_document(kb_id, doc_id)
    store.update_document(kb_id, doc.id, status="parsing")

    # 导入是最慢的操作（一本 18 页的书 3 分钟），但以前除了最后一行日志什么都看不到。
    # 事件让前端运行时间线能显示进度，指标让"识别质量下降"能在图上被发现。
    thread = f"kb-{kb_id}"
    event_key = f"kb-import-{doc.id}"
    started = time.perf_counter()
    record_event(
        thread, event_key, f"导入文档：{filename}", kind="fetch", status="in_progress"
    )
    try:
        _check_cancel()
        # 直接从内存解析，不用先落盘再读——原件已经在 MinIO 里了。
        parsed = _parse_document(content, progress=progress, cancel_event=cancel_event)
        chunks = parsed.chunks

        rows = [
            {
                "kb_id": kb_id,
                "doc_id": doc.id,
                "chunk_index": chunk.chunk_index,
                "text": _with_title(chunk.text, chunk.title),
                "title": chunk.title[:500],
                "doc_title": doc.title[:500],
                "page": chunk.page,
                "category": "text",
                "image_path": "",
            }
            for chunk in chunks
        ]
        # 集合里建了 dense 字段就必须填，否则 Milvus 拒收。生成失败就让整篇
        # 文档失败（会走 except 分支标 failed），不能写进去一批空向量——那样
        # 检索会静默变差，比导入失败更难发现。
        if schema.embedding_dim() is not None:
            vectors = embeddings.embed_texts([row["text"] for row in rows])
            for row, vector in zip(rows, vectors):
                row["dense"] = vector

        if progress:
            progress("text", len(rows), len(rows))

        # 插图：每张要一次视觉描述 + 一次向量，比较慢，所以单独走一遍
        image_rows = _process_page_images(
            kb_id,
            doc,
            parsed.images,
            parsed.page_texts,
            start_index=len(rows),
            progress=progress,
            cancel_event=cancel_event,
        )
        rows.extend(image_rows)

        _check_cancel()
        for start in range(0, len(rows), _INSERT_BATCH):
            schema.insert_chunks(rows[start : start + _INSERT_BATCH])
        # 等 BM25 索引生效，否则导入完马上搜会是 0 条（Milvus 的全文索引要等
        # segment 被索引，见 schema.wait_until_searchable 的注释）。
        schema.wait_until_searchable(kb_id, rows[0]["text"][:40] if rows else "")

        store.update_document(
            kb_id,
            doc.id,
            status="indexed",
            progress=100,
            pages=parsed.total_pages,
            paragraphs=len(chunks),
            images=len(image_rows),
            text_pages=parsed.text_pages,
            vision_pages=parsed.vision_pages,
        )
        logger.info(
            "导入完成：kb=%s doc=%s 页=%d 段落=%d 插图=%d 解析失败页=%d",
            kb_id,
            doc.id,
            parsed.total_pages,
            len(chunks),
            len(image_rows),
            parsed.vision_pages,
        )
        record_kb_import(
            status="ok",
            pages_failed=parsed.vision_pages,
            duration_s=time.perf_counter() - started,
        )
        record_event(
            thread,
            event_key,
            f"导入文档：{filename}",
            kind="fetch",
            status="completed",
            detail=(
                f"{parsed.total_pages} 页 → {len(chunks)} 段 + {len(image_rows)} 图"
                + (f"（{parsed.vision_pages} 页没取到正文）" if parsed.vision_pages else "")
            ),
        )
    except tasks.ImportCancelled:
        # 用户删了这篇文档：把已经写进去的东西撤掉，别让它冒出来
        logger.info("导入被取消，清理中间产物：kb=%s doc=%s", kb_id, doc.id)
        schema.delete_document(doc.id)
        objects.delete_prefix(f"{kb_id}/{doc.id}/")
        record_event(
            thread, event_key, f"导入文档：{filename}", kind="fetch", status="failed",
            detail="用户取消了这次导入",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落到文档状态里
        logger.exception("导入失败：kb=%s doc=%s", kb_id, doc.id)
        schema.delete_document(doc.id)
        _mark_failed(kb_id, doc.id, exc)
        record_kb_import(status="failed", pages_failed=0, duration_s=time.perf_counter() - started)
        record_event(
            thread, event_key, f"导入文档：{filename}", kind="fetch", status="failed",
            detail=str(exc)[:200],
        )
        raise

    return _doc_payload(store.get_document(kb_id, doc.id))


def _mark_failed(kb_id: str, doc_id: str, exc: Exception) -> None:
    """把文档标成 failed。

    文档可能已经被用户删了，这时 PG 里没有行，更新会抛 FileNotFoundError——
    不能让这个异常盖掉真正的失败原因。
    """

    try:
        store.update_document(kb_id, doc_id, status="failed", error=str(exc)[:500])
    except FileNotFoundError:
        logger.info("文档已被删除，不再更新状态：kb=%s doc=%s", kb_id, doc_id)


def import_pdf(
    kb_id: str,
    *,
    filename: str,
    content: bytes,
    progress: ProgressHook | None = None,
) -> dict[str, Any]:
    """同步导入。脚本与测试用这个；HTTP 接口走 `start_import`。"""

    if not filename.lower().endswith(".pdf"):
        raise ValueError("目前只支持 PDF")

    doc, duplicate = _register_document(kb_id, filename, content)
    if duplicate:
        logger.info("内容重复，跳过解析：kb=%s 已存在 doc=%s", kb_id, doc.id)
        return {**_doc_payload(doc), "duplicate": True}
    return run_import(kb_id, doc.id, filename, content, progress=progress)


def start_import(kb_id: str, *, filename: str, content: bytes) -> dict[str, Any]:
    """登记文档并起后台任务，**立刻返回**。

    返回的 payload 里 status 是 "parsing"，真实进度用
    `GET /dashboard/api/knowledge/documents/{doc_id}/progress` 查。
    """

    if not filename.lower().endswith(".pdf"):
        raise ValueError("目前只支持 PDF")

    doc, duplicate = _register_document(kb_id, filename, content)
    if duplicate:
        logger.info("内容重复，跳过解析：kb=%s 已存在 doc=%s", kb_id, doc.id)
        return {**_doc_payload(doc), "duplicate": True}

    tasks.spawn_import(kb_id, doc.id, filename, content)
    return {**_doc_payload(doc), "status": "parsing"}


@dataclass
class ParsedDocument:
    """一篇文档解析完的中间结果。两条解析路径都归到这一个结构。"""

    chunks: list[Chunk]
    images: list[Any]  # chunking.PageImage 或 images.ExtractedImage，字段同形
    page_texts: dict[int, str]
    total_pages: int
    text_pages: int
    vision_pages: int


def _parse_document(
    content: bytes,
    *,
    progress: ProgressHook | None = None,
    cancel_event: threading.Event | None = None,
) -> ParsedDocument:
    """解析 PDF。配了 dots_ocr 就走版面模型，没配就退回 PyMuPDF 抽文字层。

    两条路的产物刻意做成同一个结构，后面的切分、向量化、入库完全共用——
    否则"有没有 OCR"会把后面整条链路复制成两份。

    降级路径的缺陷是明确的：扫描版（没有文字层）的页面**整页没有正文**，
    只能靠 PDF 内嵌位图凑几张图出来，见 `extract.py` 开头的说明。
    """

    if dotsocr.available():
        def _page_progress(done: int, total: int) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise tasks.ImportCancelled("导入被取消")
            if progress:
                progress("page", done, total)

        # dots_ocr 的取消只能按页检查：单页请求发出去就得等它返回
        parsed = dotsocr.parse_pdf(content, progress=_page_progress)
        split = chunking.split_pages(parsed, semantic=_semantic_chunking())
        ok_pages = sum(1 for page in parsed if page.ok)
        return ParsedDocument(
            chunks=split.chunks,
            images=split.images,
            page_texts=split.page_texts,
            total_pages=len(parsed),
            text_pages=ok_pages,
            vision_pages=len(parsed) - ok_pages,
        )

    logger.info("未配置 DOTS_OCR_BASE_URL，退回 PyMuPDF 抽文字层（扫描页不会有正文）")
    pages = extract_pages(content)
    text_pages, vision_pages = page_stats(pages)
    return ParsedDocument(
        chunks=chunk_pages(pages, semantic=_semantic_chunking()),
        images=images.extract_images(content),
        page_texts={page.page: page.text for page in pages},
        total_pages=len(pages),
        text_pages=text_pages,
        vision_pages=vision_pages,
    )


def _with_title(text: str, title: str) -> str:
    """把标题拼到正文前面。

    为什么值得多这几个字：BM25 只索引 `text` 字段（`schema.py` 里的
    `text_bm25`），标题不进正文的话，"第 6.3 节 数据流"这种只有标题里才有的词
    就永远搜不到。原项目也是这么干的（`db_operator.py:75`）。
    """

    chain = (title or "").strip()
    if not chain:
        return text
    head = chain[:80]
    if text.startswith(head):
        return text
    return f"{head} ：{text}"


def _process_page_images(
    kb_id: str,
    doc: store.DocumentMeta,
    page_images: list[Any],
    page_texts: dict[int, str],
    *,
    start_index: int,
    progress: ProgressHook | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """插图存 MinIO、写描述、算向量，返回可以直接入库的行。

    插图在 Milvus 里是**独立的行**（category='image'），不是挂在文本块上。
    它的 text 字段存的是视觉模型写的描述——这样 BM25 也能搜到图片（靠描述
    里的词），dense 存的是(图 + 描述)的向量。

    描述带该页正文当上下文：模型看到"这一页在讲什么"，写出来的描述会带上图的
    用途而不只是画面内容，检索命中率明显不同。代价是可能顺着上下文编造图里
    没有的细节，所以提示词要求把"看到的"和"推断的"分开说。
    """

    if not page_images:
        return []

    rows: list[dict[str, Any]] = []
    for position, image in enumerate(page_images):
        if cancel_event is not None and cancel_event.is_set():
            raise tasks.ImportCancelled(f"导入被取消：{doc.id}")
        context = (page_texts.get(image.page) or "")[:600]
        key = objects.document_key(
            kb_id, doc.id, f"images/p{image.page}_{image.index}.{image.ext}"
        )
        objects.put_document(key, image.data, content_type=f"image/{image.ext}")

        try:
            description = embeddings.describe_image(image.data, context)
        except embeddings.VisionError as exc:
            # 描述失败不丢图：退化成一句占位描述，图照样能被检索到（靠页码和
            # 上下文关键词），总比整张图从知识库里消失好。
            logger.warning("图片描述生成失败，降级处理：%s", exc)
            description = f"第 {image.page} 页的插图"

        row: dict[str, Any] = {
            "kb_id": kb_id,
            "doc_id": doc.id,
            "chunk_index": start_index + position,
            "text": description,
            "title": f"第 {image.page} 页插图",
            "doc_title": doc.title[:500],
            "page": image.page,
            "category": "image",
            "image_path": key,
        }
        if schema.embedding_dim() is not None:
            row["dense"] = embeddings.embed_image(image.data, description)
        rows.append(row)

        if progress:
            progress("image", position + 1, len(page_images))

    return rows


def _semantic_chunking() -> bool:
    """导入时要不要做语义分段。默认开；任一非 true 值就退回按段落直接合并。

    关掉它主要为了省 embedding 调用——一本大书的导入时间里有相当一部分花在这。
    """

    return get_env("KNOWLEDGE_SEMANTIC_CHUNK", "true").strip().lower() not in {"0", "false", "no", "off"}


def search(
    kb_id: str,
    query: str = "",
    *,
    top_k: int = 5,
    image_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    """检索段落。

    三种情况：
    - 给了 `image_bytes`：把图编成向量，**只走稠密**。BM25 对图片无从下手，
      混进去只会稀释结果。
    - 只有文字且配了 embedding：BM25 + 稠密，用 RRF 合并。
    - 只有文字且没配 embedding：纯 BM25。
    """

    if image_bytes:
        # 图片查询必须有多模态向量；没配就明确报错，别退化成"什么都搜不到"
        vector = embeddings.embed_query_image(image_bytes)
        return [_hit_payload(hit) for hit in schema.search_dense(vector, kb_id=kb_id, top_k=top_k)]

    text = (query or "").strip()
    if not text:
        return []

    sparse_hits = schema.search_sparse(text, kb_id=kb_id, top_k=top_k * 2)
    dense_hits: list[dict[str, Any]] = []
    vector = _embed_query(text)
    if vector:
        dense_hits = schema.search_dense(vector, kb_id=kb_id, top_k=top_k * 2)

    if not dense_hits:
        return [_hit_payload(hit) for hit in sparse_hits[:top_k]]

    # RRF：名次越靠前分越高，两路取长补短。比直接加权原始分数稳，
    # 因为 BM25 和向量相似度的量纲完全不同。
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for hits in (sparse_hits, dense_hits):
        for rank, hit in enumerate(hits, start=1):
            key = (hit.get("doc_id", ""), int(hit.get("chunk_index") or 0))
            entry = merged.setdefault(key, {**hit, "_rrf": 0.0})
            entry["_rrf"] += 1.0 / (_RRF_K + rank)
    ordered = sorted(merged.values(), key=lambda item: item["_rrf"], reverse=True)
    return [_hit_payload(hit) for hit in ordered[:top_k]]


def _hit_payload(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": hit.get("text", ""),
        "title": hit.get("title", ""),
        "doc_title": hit.get("doc_title", ""),
        "doc_id": hit.get("doc_id", ""),
        "page": int(hit.get("page") or 0),
        "chunk_index": int(hit.get("chunk_index") or 0),
        "score": round(float(hit.get("score") or hit.get("_rrf") or 0.0), 4),
        # category 区分文本块与插图条目；image_path 是插图在 MinIO 里的 key
        "category": hit.get("category") or "text",
        "image_path": hit.get("image_path") or "",
    }


def _embed_query(text: str) -> list[float] | None:
    """把查询文本转成向量。未配置或调用失败时返回 None（本次只走 BM25）。

    查询侧的失败降级、导入侧的失败中止，是刻意不同：查询失败只影响这一次的
    排序质量；导入失败写进去的是一批没有向量的行，会让检索静默变差。
    """

    if not embeddings.is_configured():
        return None
    try:
        vectors = embeddings.embed_texts([text])
        return vectors[0] if vectors else None
    except embeddings.EmbeddingError:
        logger.warning("embedding 调用失败，本次只走 BM25", exc_info=True)
        return None
