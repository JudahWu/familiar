"""Milvus collection 的定义与读写。

只做向量库这一层：建 collection、写入分块、按知识库过滤检索、按文档/库删除。
业务语义（什么是知识库、文档状态怎么流转）在 `service.py`。

schema 的一个关键选择：**稀疏（BM25）是必有的，稠密由配置决定**。
- 稀疏靠 Milvus 2.5+ 的 BM25 Function + jieba 分词器，服务端自己算，不需要任何
  embedding 服务就能跑通检索（阶段 ② 的目标：先验证"搜不搜得到"）；
- 稠密要外部 embedding 模型，配了 EMBEDDING_DIM 才建这个字段。

**Milvus 不允许给已有集合补一个可用的向量字段。** 实测（2.6.25）：事后 add 一个
`nullable=True` 的 FLOAT_VECTOR 字段能建索引，但一检索就报
`field dense is not loaded, please reload the collection`——加字段后集合的
加载状态是残的。结论是 dense 必须在建集合时就有，换 embedding 模型只能重建集合
（`scripts/rebuild_knowledge_collection.py`）。所以 ensure_collection() 会校验
已有集合的字段是否与当前配置一致，不一致就报错并给出重建命令，而不是继续写
进一个半残的集合。

文本块与图片条目落在**同一个 dense 字段**里，用 `category` 区分（'text'/'image'）。
这样两种向量处在同一个空间，才谈得上"用文字搜到图""用图搜到图"。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pymilvus import DataType, Function, FunctionType, MilvusClient

from familiar.core.settings import MILVUS_URI
from familiar.env_utils import get_env

logger = logging.getLogger("familiar.knowledge.milvus")

COLLECTION = "knowledge_chunks"

# 输出字段：检索回来要拼上下文和引用，text 之外还需要出处信息。
# image_path 是图片条目在 MinIO 里的 object key（文本条目为空）。
_OUTPUT_FIELDS = ["text", "title", "doc_title", "doc_id", "chunk_index", "page", "category", "image_path"]

_client: MilvusClient | None = None


def client() -> MilvusClient:
    """进程内复用同一个连接。MilvusClient 内部自带连接池，重复建开销大。"""

    global _client
    if _client is None:
        _client = MilvusClient(uri=MILVUS_URI)
        logger.info("Milvus 连接已建立：%s", MILVUS_URI)
    return _client


def embedding_dim() -> int | None:
    """稠密向量维度；未配置 embedding 时返回 None（只做稀疏检索）。"""

    value = get_env("EMBEDDING_DIM", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("EMBEDDING_DIM 不是整数，按未配置处理：%s", value)
        return None


def collection_exists() -> bool:
    return COLLECTION in client().list_collections()


def _describe_fields() -> dict[str, dict[str, Any]]:
    desc = client().describe_collection(collection_name=COLLECTION)
    return {field["name"]: field for field in desc.get("fields", [])}


def _schema_mismatch(reason: str) -> RuntimeError:
    return RuntimeError(
        f"Milvus 集合 {COLLECTION} 与当前配置不匹配：{reason}。"
        "Milvus 不支持给已有集合补一个可用的向量字段（实测 2.6.25：加 nullable dense 后"
        "检索会报 field dense is not loaded），所以只能重建集合。"
        "重建命令：python scripts/rebuild_knowledge_collection.py"
    )


def _verify_collection(dim: int | None) -> None:
    """校验已有集合的字段是否与当前配置对得上。

    对不上就报错并给出重建命令。宁可让用户显式重建，也不能继续往一个半残的
    集合里写数据——那种状态更糟：看着能写，检索时才炸。
    """

    fields = _describe_fields()
    missing = [name for name in ("sparse", "text", "category", "image_path") if name not in fields]
    if missing:
        raise _schema_mismatch("缺少字段 " + "、".join(missing))

    if "dense" in fields:
        actual = int((fields["dense"].get("params") or {}).get("dim") or 0)
        if dim is None:
            raise _schema_mismatch(f"集合带 dense({actual})，但当前没有配置 EMBEDDING_DIM")
        if actual != dim:
            raise _schema_mismatch(f"dense 维度 {actual} ≠ 配置的 {dim}（换了 embedding 模型）")
    elif dim:
        raise _schema_mismatch(f"配置了 EMBEDDING_DIM={dim}，但集合里没有 dense 字段")


def ensure_collection() -> None:
    """按当前配置建 collection；已存在则校验字段是否匹配。

    维度不匹配说明换过 embedding 模型，此时继续写入会污染已有数据，
    直接报错让用户重新导入，比悄悄写错向量好。
    """

    dim = embedding_dim()
    c = client()
    if collection_exists():
        _verify_collection(dim)
        return

    schema = c.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("chunk_index", DataType.INT64)
    # text 同时是 BM25 的输入字段，所以必须开 analyzer
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=8000,
        enable_analyzer=True,
        analyzer_params={"tokenizer": "jieba"},
    )
    schema.add_field("title", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_title", DataType.VARCHAR, max_length=512)
    schema.add_field("page", DataType.INT64)
    # category 区分文本块与图片条目；两者共用 dense 字段，才能互相检索
    schema.add_field("category", DataType.VARCHAR, max_length=16)
    # 图片条目在 MinIO 里的 object key（文本条目为空）
    schema.add_field("image_path", DataType.VARCHAR, max_length=512)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    if dim:
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)

    schema.add_function(
        Function(
            name="text_bm25",
            input_field_names=["text"],
            output_field_names=["sparse"],
            function_type=FunctionType.BM25,
        )
    )

    index_params = c.prepare_index_params()
    index_params.add_index(
        field_name="sparse",
        index_name="sparse_inverted_index",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={
            "inverted_index_algo": "DAAT_MAXSCORE",
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
        },
    )
    if dim:
        index_params.add_index(
            field_name="dense",
            index_name="dense_index",
            index_type="AUTOINDEX",
            metric_type="IP",
        )

    c.create_collection(collection_name=COLLECTION, schema=schema, index_params=index_params)
    logger.info("已创建 Milvus collection：%s（dense=%s）", COLLECTION, dim or "未启用")


def insert_chunks(rows: list[dict[str, Any]]) -> int:
    """写入分块。rows 里不要带 sparse——它由 BM25 Function 服务端生成。"""

    if not rows:
        return 0
    ensure_collection()
    client().insert(collection_name=COLLECTION, data=rows)
    return len(rows)


def search_sparse(query: str, *, kb_id: str, top_k: int) -> list[dict[str, Any]]:
    """BM25 检索。query 直接传文本，Milvus 用同一个 analyzer 编码。"""

    if not collection_exists():
        return []
    hits = client().search(
        collection_name=COLLECTION,
        data=[query],
        anns_field="sparse",
        limit=top_k,
        filter=f'kb_id == "{kb_id}"',
        output_fields=_OUTPUT_FIELDS,
        search_params={"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
    )
    return _flatten(hits)


def search_dense(vector: list[float], *, kb_id: str, top_k: int) -> list[dict[str, Any]]:
    """稠密检索；collection 没有 dense 字段时直接返回空。"""

    if not collection_exists():
        return []
    try:
        hits = client().search(
            collection_name=COLLECTION,
            data=[vector],
            anns_field="dense",
            limit=top_k,
            filter=f'kb_id == "{kb_id}"',
            output_fields=_OUTPUT_FIELDS,
            search_params={"metric_type": "IP", "params": {"nprobe": 10}},
        )
    except Exception:  # noqa: BLE001 - 没有 dense 字段时 Milvus 会报错，按"不支持"处理
        logger.debug("dense 检索不可用（collection 可能没有 dense 字段）", exc_info=True)
        return []
    return _flatten(hits)


def _flatten(hits: Any) -> list[dict[str, Any]]:
    """把 Milvus 的 [[{id, distance, entity}]] 结构拍平成普通 dict 列表。"""

    rows: list[dict[str, Any]] = []
    for group in hits or []:
        for hit in group:
            entity = dict(hit.get("entity") or {})
            entity["score"] = float(hit.get("distance") or 0.0)
            rows.append(entity)
    return rows


def delete_document(doc_id: str) -> None:
    if not collection_exists():
        return
    expr = f'doc_id == "{doc_id}"'
    client().delete(collection_name=COLLECTION, filter=expr)
    wait_until_deleted(expr)


def delete_base(kb_id: str) -> None:
    if not collection_exists():
        return
    expr = f'kb_id == "{kb_id}"'
    client().delete(collection_name=COLLECTION, filter=expr)
    wait_until_deleted(expr)


def wait_until_deleted(expr: str, timeout: float = 5.0) -> bool:
    """等 Milvus 的异步删除生效。

    Milvus 的 delete 是异步的：不等的话紧接着查还能查到刚删掉的行——用户删完
    一篇文档立刻搜索，会看到"删了还搜得到"，像删除没生效。

    这里用轮询而不是 flush：实测这套 standalone 部署的 flush 每次要 10.5 秒
    （空集合也一样，是 MQ 刷盘间隔决定的），而删除本身几十毫秒就生效。
    等一个真正会完成的条件，比等一个固定 10 秒的空操作划算。
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = client().query(
                collection_name=COLLECTION, filter=expr, output_fields=["id"], limit=1
            )
            if not rows:
                return True
        except Exception:  # noqa: BLE001 - 查询失败就当删干净了，别卡住删除
            logger.debug("确认删除状态时查询失败", exc_info=True)
            return True
        time.sleep(0.05)
    logger.warning("等待 Milvus 删除生效超时：%s", expr)
    return False


def wait_until_searchable(kb_id: str, probe: str, timeout: float = 30.0) -> bool:
    """等 BM25 索引生效，也就是新插入的数据能被搜到。

    Milvus 的全文索引不支持在 growing segment 上搜索，数据写进去之后要等
    segment 被索引才能被 BM25 命中。表现是"导入成功、立刻搜却是 0 条"。

    同样用轮询而不是 flush：flush 在这套部署上固定 10.5 秒，而实际索引生效
    也就这个量级。轮询的好处是**等到真的能搜到就停**，索引快就少等。
    """

    text = (probe or "").strip()
    if not text or not collection_exists():
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            hits = client().search(
                collection_name=COLLECTION,
                data=[text],
                anns_field="sparse",
                limit=1,
                filter=f'kb_id == "{kb_id}"',
                output_fields=["id"],
                search_params={"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
            )
            if hits and hits[0]:
                return True
        except Exception:  # noqa: BLE001
            logger.debug("确认索引状态时搜索失败", exc_info=True)
        time.sleep(0.5)
    logger.warning("等待 BM25 索引生效超时：kb=%s", kb_id)
    return False


def stats(kb_id: str | None = None) -> dict[str, int]:
    """统计段落数，用于页面上的"段落 1284"。空库或未建表时返回 0。"""

    if not collection_exists():
        return {"chunks": 0}
    expr = f'kb_id == "{kb_id}"' if kb_id else ""
    try:
        rows = client().query(
            collection_name=COLLECTION,
            filter=expr or "id >= 0",
            output_fields=["count(*)"],
        )
        count = int(rows[0].get("count(*)", 0)) if rows else 0
    except Exception:  # noqa: BLE001
        logger.debug("统计段落数失败", exc_info=True)
        count = 0
    return {"chunks": count}
