"""知识库会话记忆（PostgreSQL）。

在此之前知识库问答是**完全无状态**的：每次 `/answer` 都是「拿这一句问题去检索 →
生成」。后果很具体：

    你：有界流是什么？
    它：有界流是有明确开始和结束的数据流……
    你：那它和无界流有什么区别？
    它：这个知识库里没有检索到相关内容。

第二句里的"它"没有指代对象，整句被原样丢进 BM25 和向量检索，"那它和……"这些词
对两种召回都是噪音。

补记忆要分两步，缺一不可：

1. **存**：问答历史落 PG（这里），会话按 `session_id` 归集。
2. **改写**：检索之前先用最近几轮把追问改写成独立问题，再拿去检索。
   只存不改写的话，历史只是给生成模型看的，检索照样拿原句去搜，追问依旧搜不到。

这两步只解决**会话内**的指代。跨会话的回忆（"上周我在哪本书里看过……"）要靠
语义召回，索引在 `memory.py`：PG 仍是唯一事实来源，Milvus 只是可重建的派生索引，
召回拿到 turn_id 之后回到这张表取正文。

刻意不做的两件事：
- **不用 `InMemorySaver` / `InMemoryStore`**：进程一重启就没了，而且那套是给
  LangGraph 图准备的。这里的问答是同步函数调用，没有图、没有中断，直接读写表最简单。
- **不把问答正文只放进向量库**：RAG demo 的 `t_context_collection` 就是只进向量库，
  结果是引用快照、改写句、分数都没地方放，删库还得两处对账。这里反着来——
  结构化事实只落 PG，向量库只当索引，随时可以丢掉重建。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from familiar.knowledge.store import Base, _session

logger = logging.getLogger("familiar.knowledge.chat")

# 改写与生成看多少轮历史。4 轮（8 条）足够覆盖"它/那个/刚才说的"这类指代，
# 再多主要是重复内容，只是把提示词撑长。
HISTORY_TURNS = 8
# 每个会话最多留多少条，超了删最老的。没有会话列表界面，留太多只是攒垃圾。
MAX_TURNS_KEPT = 60


class ChatSessionRow(Base):
    __tablename__ = "knowledge_chat_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kb_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 首条问题截一段当标题。现在没有会话列表，先存着，将来要列历史就不用回填。
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_knowledge_chat_sessions_kb", "kb_id", "updated_at"),)


class ChatTurnRow(Base):
    __tablename__ = "knowledge_chat_turns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge_chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 检索实际用的那句。用户轮才有意义；历史为空时等于原文，改写后可能完全不同。
    # 存下来是为了排查"为什么搜到这个段落"——改写出错时这是唯一线索。
    search_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 助手轮：命中段落快照。存成 JSON 是为了刷新页面后引用还能点开——
    # 只存编号的话，恢复出来的答案有一堆点不动的 [1][2]。
    citations: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    score: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[str] = mapped_column(Text, nullable=False, default="")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_knowledge_chat_turns_session", "session_id", "id"),)


@dataclass
class Turn:
    role: str
    content: str
    # 自增主键。召回时先拿 id 再回表，所以它要跟着 Turn 走
    id: int = 0
    search_query: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    score: float | None = None
    coverage: str = ""
    needs_review: bool = False
    created_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "search_query": self.search_query,
            "citations": self.citations,
            "score": self.score,
            "coverage": self.coverage,
            "needs_review": self.needs_review,
            "created_at": self.created_at,
        }


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def ensure_session(session_id: str | None, *, kb_id: str, owner: str = "", question: str = "") -> str:
    """拿到可用的 session_id：给了就用（不存在则补建），没给就新建。

    不校验 session 是否属于这个 kb——会话和库是弱关联，用户切库时前端会换一个新 id。
    真要严格校验，反而会让"同一个会话继续问另一个库"这种合理操作报错。
    """

    with _session() as session:
        if session_id:
            row = session.get(ChatSessionRow, session_id)
            if row is not None:
                row.updated_at = datetime.now(UTC)
                session.commit()
                return row.id
        new_id = session_id or f"chat-{uuid.uuid4().hex[:12]}"
        session.add(
            ChatSessionRow(
                id=new_id,
                kb_id=kb_id,
                owner=owner,
                title=(question or "").strip()[:60],
            )
        )
        session.commit()
        return new_id


def recent_turns(session_id: str, *, limit: int = HISTORY_TURNS) -> list[Turn]:
    """取最近的若干条，按时间正序返回（最早的在前，便于拼进提示词）。"""

    if not session_id:
        return []
    with _session() as session:
        rows = session.execute(
            select(ChatTurnRow)
            .where(ChatTurnRow.session_id == session_id)
            .order_by(ChatTurnRow.id.desc())
            .limit(limit)
        ).scalars()
        return [_to_turn(row) for row in reversed(list(rows))]


def list_turns(session_id: str, *, limit: int = 40) -> list[Turn]:
    """给前端恢复界面用。和 `recent_turns` 的差别只是条数上限更大。"""

    if not session_id:
        return []
    with _session() as session:
        rows = session.execute(
            select(ChatTurnRow)
            .where(ChatTurnRow.session_id == session_id)
            .order_by(ChatTurnRow.id.desc())
            .limit(limit)
        ).scalars()
        return [_to_turn(row) for row in reversed(list(rows))]


def append_turn(
    session_id: str,
    *,
    role: str,
    content: str,
    search_query: str = "",
    citations: list[dict[str, Any]] | None = None,
    score: float | None = None,
    coverage: str = "",
    needs_review: bool = False,
) -> int | None:
    """写一条，返回新行的 id（供记忆索引使用）。

    失败只打日志，不往上抛。返回 None 表示没写进去——调用方据此跳过索引，
    不必自己判断。

    记忆是附加能力：写不进去最多是下次追问失忆，不该让用户已经看到的答案变成 500。
    """

    if not session_id or not (content or "").strip():
        return None
    try:
        with _session() as session:
            row = ChatTurnRow(
                session_id=session_id,
                role=role,
                content=content,
                search_query=search_query or "",
                citations=json.dumps(citations or [], ensure_ascii=False),
                score=score,
                coverage=coverage or "",
                needs_review=needs_review,
            )
            session.add(row)
            parent = session.get(ChatSessionRow, session_id)
            if parent is not None:
                parent.updated_at = datetime.now(UTC)
            # flush 才有自增 id，commit 之后对象就过期了
            session.flush()
            new_id = row.id
            session.commit()
            _trim(session, session_id)
            return int(new_id) if new_id is not None else None
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("写入会话记忆失败：session=%s role=%s", session_id, role, exc_info=True)
        return None


def _trim(session: Any, session_id: str) -> None:
    """只保留最近 MAX_TURNS_KEPT 条。"""

    keep = session.execute(
        select(ChatTurnRow.id)
        .where(ChatTurnRow.session_id == session_id)
        .order_by(ChatTurnRow.id.desc())
        .limit(MAX_TURNS_KEPT)
    ).scalars().all()
    if len(keep) < MAX_TURNS_KEPT:
        return
    session.execute(
        delete(ChatTurnRow).where(
            ChatTurnRow.session_id == session_id,
            ChatTurnRow.id < min(keep),
        )
    )
    session.commit()


def turns_by_ids(ids: list[int]) -> list[Turn]:
    """按 id 批量取，保持传入顺序。召回用：先拿 id，再回表取正文。

    取不到的 id 直接跳过（对应的行可能已经被 `_trim` 或删会话清掉了）。
    这正是"索引脏了不会返回幽灵记忆"的实现方式——索引只是候选，
    PG 里没有的行一律不认。
    """

    if not ids:
        return []
    with _session() as session:
        rows = session.execute(
            select(ChatTurnRow).where(ChatTurnRow.id.in_(ids))
        ).scalars()
        by_id = {int(row.id): _to_turn(row) for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def recall_turns(
    query: str, *, owner: str = "", top_k: int = 6, kb_id: str = ""
) -> list[Turn]:
    """跨会话回忆：语义召回历史回答。

    返回的是**助手轮**（索引里只有助手轮），按相关度从高到低。调用方拿到的
    是一段段"以前答过什么"，用途有两个：判断这个问题是不是早就答过，
    以及给生成模型当补充材料。

    索引未配置、召回失败、召回后 PG 里都取不到——三种情况一律返回空列表，
    让上层退回"只查知识库"。回忆是加分项，不能成为问答的必经路径。
    """

    if not (query or "").strip():
        return []

    from familiar.knowledge import memory  # 延迟导入：memory 依赖 schema/embeddings，起不来时别拖累 chat

    pairs = memory.recall_ids(query, owner=owner, top_k=top_k, kb_id=kb_id)
    if not pairs:
        return []
    # turns_by_ids 按传入 id 的顺序返回，也就是召回的相关度顺序
    return turns_by_ids([turn_id for turn_id, _ in pairs])


def session_owner(session_id: str) -> str | None:
    """返回会话的 owner；会话不存在返回 None。

    图化之后这个校验不能省：`session_id` 就是图的 thread_id，拿到它就能推进
    这个会话的中断（`/answer/resume`）。返回 None 和返回空串要区分开——
    空串是"没记 owner 的历史数据"，不存在是"真没有这个会话"。
    """

    if not session_id:
        return None
    with _session() as session:
        row = session.get(ChatSessionRow, session_id)
        return None if row is None else (row.owner or "")


def delete_session(session_id: str) -> None:
    """删掉一个会话及其所有轮次。

    现在没有界面入口（"新对话"只是换个 id，旧记录留着），它主要用于验证脚本清理，
    以及以后真要做会话列表时的删除操作。外键 CASCADE 会带走 turns。
    """

    if not session_id:
        return
    with _session() as session:
        session.execute(delete(ChatSessionRow).where(ChatSessionRow.id == session_id))
        session.commit()
    # 记忆索引是派生数据，跟着会话一起清。延迟导入避免 chat <-> memory 互相拖累。
    from familiar.knowledge import memory

    memory.forget(session_id=session_id)


def _to_turn(row: ChatTurnRow) -> Turn:
    try:
        citations = json.loads(row.citations or "[]")
    except json.JSONDecodeError:
        citations = []
    return Turn(
        role=row.role,
        content=row.content,
        id=int(row.id or 0),
        search_query=row.search_query or "",
        citations=citations if isinstance(citations, list) else [],
        score=row.score,
        coverage=row.coverage or "",
        needs_review=bool(row.needs_review),
        created_at=_iso(row.created_at),
    )
