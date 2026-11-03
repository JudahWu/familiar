"""跨会话记忆的语义索引。

背景：知识库问答原来是**会话级**记忆（`chat.py`，按 session_id 读最近几轮），
解决的是"那它呢""上面那个方法"这类指代。但真实用法里还有另一种提问：

    上周我在哪本书里看过有界流和无界流的对比？

这类问题跨会话、跨知识库，按 session_id 查表是查不到的。RAG 项目用
`t_context_collection` 解决它——把每条 AIMessage 向量化，按 user 过滤后做
语义召回（tools.py:50-56）。

存储选择：**PG 是唯一事实来源，Milvus 只是这张表的一个可重建索引。**

- 事实来源只有 `knowledge_chat_turns` 一张表，不会出现"两个版本的历史"；
- 索引丢了可以按 PG 全量重建（`rebuild()`），不需要备份；
- 删除时先删 PG 行、再删索引；召回拿到 turn_id 后回 PG 取正文，**取不到的直接
  丢弃**——索引脏了最多少召回一条，不会把已删的记忆当成资料引用；
- 没走 pgvector：这台 PG 是 postgres:15-alpine，没有 vector 扩展，换镜像要重启
  整个库（checkpointer、业务 store、langfuse 共用），而 Milvus 已经在跑。

召回用 hybrid（dense + BM25），和 RAG 项目里 `search_context` 的实际调用一致
（tools.py:50 虽然是 hybrid，注释里写着"只要语义检索"）。BM25 对"有界流"这种
专有名词有用，dense 对"上次问的那个流处理概念"有用，两路都要。

**索引写入不放在 `chat.append_turn` 里**：那样 chat 要依赖 memory，而 memory 要
依赖 chat 的 Turn 结构，形成环。写入方（`answer.py`）在落库之后调 `index_turn`，
索引失败只记日志——记忆检索变差，不该让用户已经看到的答案变成 500。
"""

from __future__ import annotations

import logging
from typing import Any

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, WeightedRanker

from familiar.knowledge import embeddings, schema

logger = logging.getLogger("familiar.knowledge.memory")

COLLECTION = "knowledge_memory"

# 固定权重：两条路同等重要。RAG 项目用的是 WeightedRanker(1.0, 1.0)。
_RANKER = WeightedRanker(1.0, 1.0)

_OUTPUT_FIELDS = ["turn_id", "session_id", "kb_id", "role"]


def _client() -> Any:
    return schema.client()


def available() -> bool:
    """有没有语义召回能力。没配 embedding 就只能按 session 读表。"""

    return schema.embedding_dim() is not None and embeddings.is_configured()


def collection_exists() -> bool:
    return COLLECTION in _client().list_collections()


def ensure_collection() -> None:
    """建索引表。没配 embedding 就直接跳过——没有向量就没有索引。"""

    if not available():
        logger.info("未配置 embedding，跨会话记忆索引不可用")
        return
    if collection_exists():
        return

    dim = schema.embedding_dim()
    c = _client()
    spec = c.create_schema(auto_id=False, enable_dynamic_field=False)
    # 主键就是 PG 里的 turn id，两边一一对应；auto_id=False 才能让 upsert 幂等
    spec.add_field("turn_id", DataType.INT64, is_primary=True)
    spec.add_field("session_id", DataType.VARCHAR, max_length=64)
    spec.add_field("kb_id", DataType.VARCHAR, max_length=64)
    # owner 是邮箱。**必须有**：跨会话召回是跨库的，没有这个字段，
    # 甲的历史会出现在乙的检索结果里。RAG 项目也是这么做的（tools.py:35）。
    spec.add_field("owner", DataType.VARCHAR, max_length=128)
    spec.add_field("role", DataType.VARCHAR, max_length=16)
    # text 同时是 BM25 的输入字段
    spec.add_field(
        "text",
        DataType.VARCHAR,
        max_length=8000,
        enable_analyzer=True,
        analyzer_params={"tokenizer": "jieba"},
    )
    spec.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    spec.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)

    spec.add_function(
        Function(
            name="memory_bm25",
            input_field_names=["text"],
            output_field_names=["sparse"],
            function_type=FunctionType.BM25,
        )
    )

    index_params = c.prepare_index_params()
    index_params.add_index(
        field_name="sparse",
        index_name="memory_sparse",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
    )
    index_params.add_index(
        field_name="dense",
        index_name="memory_dense",
        index_type="AUTOINDEX",
        metric_type="IP",
    )
    c.create_collection(collection_name=COLLECTION, schema=spec, index_params=index_params)
    logger.info("已创建记忆索引 collection：%s", COLLECTION)


def index_turn(
    *,
    turn_id: int,
    session_id: str,
    kb_id: str,
    owner: str,
    role: str,
    text: str,
) -> bool:
    """把一条问答写进索引。幂等：同一个 turn_id 重复写只会覆盖。

    只索引助手轮。用户那句原话价值有限——追问召回要的是"上次答了什么"，
    而且用户句往往是"那它呢"这种半句，索引进去只增加噪音。RAG 项目也只写
    AIMessage（workflow.py:303）。
    """

    if not available():
        return False
    body = (text or "").strip()
    if not body:
        return False

    try:
        ensure_collection()
        vector = embeddings.embed_texts([body])[0]
        _client().upsert(
            collection_name=COLLECTION,
            data=[
                {
                    "turn_id": int(turn_id),
                    "session_id": session_id,
                    "kb_id": kb_id,
                    "owner": owner or "",
                    "role": role,
                    "text": body[:8000],
                    "dense": vector,
                }
            ],
        )
        return True
    except Exception:  # noqa: BLE001 - 索引是附加能力，失败只影响下次召回
        logger.warning("记忆索引写入失败：turn=%s", turn_id, exc_info=True)
        return False


def _expr(*, owner: str = "", kb_id: str = "", session_id: str = "") -> str:
    """拼 filter 表达式。空条件直接不出现——Milvus 不认 `owner == ""` 这种占位。

    只拼三个固定字段的等值条件，值都来自服务端（owner 取当前登录用户、
    kb_id/session_id 取数据库行），不接用户自由输入，没有注入面。
    """

    parts = []
    if owner:
        parts.append(f'owner == "{owner}"')
    if kb_id:
        parts.append(f'kb_id == "{kb_id}"')
    if session_id:
        parts.append(f'session_id == "{session_id}"')
    return " and ".join(parts)


def recall_ids(
    query: str, *, owner: str = "", top_k: int = 6, kb_id: str = ""
) -> list[tuple[int, float]]:
    """按语义召回历史轮次，返回 [(turn_id, score)]。

    两个过滤条件含义不同，不能混：
    - `owner`：**安全边界**，传了就只在这个用户的历史里找。不传表示本地单用户
      环境（还没登录），此时退回全库——生产环境不该出现这种情况。
    - `kb_id`：**范围收窄**，可选。不传就是跨库回忆（"我上周在哪本书里看过"），
      传了就只在某个库的历史里找。默认跨库，因为这才是跨会话回忆的用处。
    """

    text = (query or "").strip()
    if not text or not available() or not collection_exists():
        return []

    expr = _expr(owner=owner, kb_id=kb_id)
    try:
        vector = embeddings.embed_texts([text])[0]
        dense_req = AnnSearchRequest(
            [vector],
            "dense",
            {"metric_type": "IP", "params": {"nprobe": 10}},
            limit=top_k,
            expr=expr,
        )
        sparse_req = AnnSearchRequest(
            [text],
            "sparse",
            {"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
            limit=top_k,
            expr=expr,
        )
        hits = _client().hybrid_search(
            collection_name=COLLECTION,
            reqs=[sparse_req, dense_req],
            ranker=_RANKER,
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
    except Exception:  # noqa: BLE001 - 召回失败退化成"这次没有历史"
        logger.warning("记忆召回失败，本次按无历史处理", exc_info=True)
        return []

    result: list[tuple[int, float]] = []
    for hit in hits[0] if hits else []:
        entity = dict(hit.get("entity") or {})
        try:
            result.append((int(entity.get("turn_id")), float(hit.get("distance") or 0.0)))
        except (TypeError, ValueError):
            continue
    return result


def forget(*, kb_id: str = "", session_id: str = "") -> None:
    """删索引。调用方负责先删 PG 行，这里只清派生数据。

    两个条件都给时按 kb 删——一个库没了，它的所有会话索引都没意义了。
    """

    if not collection_exists():
        return
    expr = _expr(kb_id=kb_id, session_id=session_id)
    if not expr:
        return
    try:
        _client().delete(collection_name=COLLECTION, filter=expr)
    except Exception:  # noqa: BLE001
        logger.warning("清理记忆索引失败：%s", expr, exc_info=True)


def rebuild(rows: list[dict[str, Any]]) -> int:
    """从 PG 全量重建索引。rows 每项要含 turn_id/session_id/kb_id/role/text。

    索引是可以随时丢掉重来的派生数据，所以重建就是"删表再灌一遍"，
    不需要增量对账。脚本见 `scripts/rebuild_knowledge_memory.py`。
    """

    if not available():
        return 0
    try:
        c = _client()
        if collection_exists():
            c.drop_collection(COLLECTION)
        ensure_collection()

        written = 0
        batch: list[dict[str, Any]] = []
        for row in rows:
            body = (row.get("text") or "").strip()
            if not body:
                continue
            batch.append(row)
            if len(batch) >= 20:
                written += _write_batch(batch)
                batch = []
        if batch:
            written += _write_batch(batch)
        return written
    except Exception:  # noqa: BLE001
        logger.exception("重建记忆索引失败")
        return 0


def _write_batch(batch: list[dict[str, Any]]) -> int:
    vectors = embeddings.embed_texts([(row.get("text") or "")[:8000] for row in batch])
    data = [
        {
            "turn_id": int(row["turn_id"]),
            "session_id": str(row["session_id"]),
            "kb_id": str(row["kb_id"]),
            "owner": str(row.get("owner") or ""),
            "role": str(row.get("role") or "assistant"),
            "text": (row.get("text") or "")[:8000],
            "dense": vector,
        }
        for row, vector in zip(batch, vectors)
    ]
    _client().insert(collection_name=COLLECTION, data=data)
    return len(data)
