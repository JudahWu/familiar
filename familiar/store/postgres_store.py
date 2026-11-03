from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from familiar.core.settings import DATABASE_URL

# 业务 Store 的表结构刻意和 LocalSqliteStore 保持一致：
# threads / runs / run_events / thread_messages / thread_plans /
# review_findings / settings。这样 PERSISTENCE_TYPE 在 sqlite 与 postgres
# 之间切换时，调用方（graph.get_store / events / repo_mapping）完全无感。
#
# 实现采用 SQLAlchemy ORM（声明式模型 + Session），不拼接 SQL。


def utc_now() -> str:
    """统一使用 UTC 时间存储，避免本地时区变化影响排序和排查。"""

    return datetime.now(UTC).isoformat()


def _normalize_db_url(url: str) -> str:
    """把 postgresql:// 归一成 postgresql+psycopg://，强制使用 psycopg 3 驱动。

    SQLAlchemy 默认 postgresql:// 会去找 psycopg2，而本项目依赖是 psycopg（v3）。
    这里在创建引擎前统一修正，用户填 postgresql:// 或 postgresql+psycopg:// 都能用。
    """

    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class Base(DeclarativeBase):
    pass


class Threads(Base):
    __tablename__ = "threads"

    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    user_prompt: Mapped[str | None] = mapped_column(Text)
    repo_url: Mapped[str | None] = mapped_column(Text)
    repo_owner: Mapped[str | None] = mapped_column(Text)
    repo_name: Mapped[str | None] = mapped_column(Text)
    branch_name: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    latest_run_status: Mapped[str] = mapped_column(String, nullable=False)
    context_used: Mapped[int | None] = mapped_column()
    context_total: Mapped[int | None] = mapped_column()
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Runs(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class RunEvents(Base):
    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ThreadMessages(Base):
    __tablename__ = "thread_messages"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 列名用 metadata，但属性名避开 Base.metadata 保留字，显式映射到列。
    msg_metadata: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ThreadPlans(Base):
    __tablename__ = "thread_plans"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    plan_text: Mapped[str] = mapped_column(Text, nullable=False)
    plan_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    approved_at: Mapped[str | None] = mapped_column(Text)


class ReviewFindings(Base):
    __tablename__ = "review_findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    file: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int | None] = mapped_column()
    severity: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class PostgresStore:
    """业务数据 Store 的 PostgreSQL 实现（SQLAlchemy ORM）。

    公开 API 与 LocalSqliteStore 一一对应（方法名、参数、返回值都一致），
    调用方无需感知底层是 SQLite 还是 PostgreSQL。完整 messages 和 LangGraph
    thread state 不在这里，由 checkpoint 数据库保存。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        # db_path 仅为兼容 LocalSqliteStore 签名；Postgres 实现统一走 DATABASE_URL。
        self._engine: Engine = create_engine(
            _normalize_db_url(DATABASE_URL),
            pool_pre_ping=True,
            future=True,
            connect_args={"connect_timeout": 10},
        )
        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine, future=True)

    def close(self) -> None:
        """关闭连接池。"""

        self._engine.dispose()

    @staticmethod
    def _to_dict(obj: Base) -> dict[str, Any]:
        d = {c.name: getattr(obj, c.key) for c in obj.__table__.columns}
        if "metadata" in d and isinstance(d["metadata"], str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (ValueError, TypeError):
                d["metadata"] = None
        return d

    # ── threads ─────────────────────────────────────────────

    def upsert_thread(
        self,
        *,
        thread_id: str,
        title: str,
        repo_url: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        user_prompt: str | None = None,
        owner: str | None = None,
        latest_run_status: str = "pending",
    ) -> None:
        now = utc_now()
        with self._Session() as session:
            existing = session.get(Threads, thread_id)
            created_at = existing.created_at if existing else now
            # 可空字段沿用 COALESCE 语义：新值为 None 时保留旧值。
            obj = Threads(
                thread_id=thread_id,
                title=title,
                user_prompt=user_prompt if user_prompt is not None else (existing.user_prompt if existing else None),
                repo_url=repo_url if repo_url is not None else (existing.repo_url if existing else None),
                repo_owner=repo_owner if repo_owner is not None else (existing.repo_owner if existing else None),
                repo_name=repo_name if repo_name is not None else (existing.repo_name if existing else None),
                branch_name=branch_name if branch_name is not None else (existing.branch_name if existing else None),
                pr_url=pr_url if pr_url is not None else (existing.pr_url if existing else None),
                owner=owner if owner is not None else (existing.owner if existing else None),
                latest_run_status=latest_run_status,
                created_at=created_at,
                updated_at=now,
            )
            session.merge(obj)
            session.commit()

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._Session() as session:
            obj = session.get(Threads, thread_id)
            return self._to_dict(obj) if obj else None

    def list_threads(self, limit: int = 50, *, owner: str | None = None) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = select(Threads)
            if owner is not None:
                stmt = stmt.where(Threads.owner == owner)
            stmt = stmt.order_by(Threads.updated_at.desc()).limit(limit)
            return [self._to_dict(o) for o in session.scalars(stmt).all()]

    def update_thread_status(
        self,
        thread_id: str,
        status: str,
        *,
        pr_url: str | None = None,
        branch_name: str | None = None,
    ) -> None:
        with self._Session() as session:
            obj = session.get(Threads, thread_id)
            if obj is None:
                return
            obj.latest_run_status = status
            if pr_url is not None:
                obj.pr_url = pr_url
            if branch_name is not None:
                obj.branch_name = branch_name
            obj.updated_at = utc_now()
            session.commit()

    def update_thread_usage(self, thread_id: str, *, used: int, total: int) -> None:
        with self._Session() as session:
            obj = session.get(Threads, thread_id)
            if obj is None:
                return
            obj.context_used = int(used)
            obj.context_total = int(total)
            obj.updated_at = utc_now()
            session.commit()

    # ── runs / run_events ──────────────────────────────────

    def record_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        status: str,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        now = utc_now()
        with self._Session() as session:
            existing = session.get(Runs, run_id)
            obj = Runs(
                run_id=run_id,
                thread_id=thread_id,
                status=status,
                started_at=existing.started_at if existing else now,
                finished_at=now if finished else (existing.finished_at if existing else None),
                error=error,
            )
            session.merge(obj)
            session.commit()

    def add_run_event(
        self,
        *,
        event_id: str,
        thread_id: str,
        kind: str,
        title: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        now = utc_now()
        with self._Session() as session:
            obj = RunEvents(
                id=event_id,
                thread_id=thread_id,
                kind=kind,
                title=title,
                status=status,
                detail=detail,
                created_at=now,
                updated_at=now,
            )
            session.merge(obj)
            session.commit()

    def list_run_events(self, thread_id: str) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = (
                select(RunEvents)
                .where(RunEvents.thread_id == thread_id)
                .order_by(RunEvents.created_at.asc())
            )
            return [self._to_dict(o) for o in session.scalars(stmt).all()]

    def clear_run_events(self, thread_id: str) -> None:
        with self._Session() as session:
            session.query(RunEvents).filter(RunEvents.thread_id == thread_id).delete()
            session.commit()

    def finish_open_run_events(self, thread_id: str, *, status: str = "completed") -> None:
        with self._Session() as session:
            session.query(RunEvents).filter(
                RunEvents.thread_id == thread_id,
                RunEvents.status.in_(["pending", "in_progress"]),
            ).update({RunEvents.status: status, RunEvents.updated_at: utc_now()})
            session.commit()

    def fail_orphaned_runs(self, reason: str = "服务重启，上次运行未正常结束") -> int:
        """把上个进程遗留的 running 状态收敛为 failed，返回收敛数量。"""

        now = utc_now()
        with self._Session() as session:
            orphan_runs = session.query(Runs).filter(Runs.status == "running").all()
            if not orphan_runs:
                return 0
            for run in orphan_runs:
                run.status = "failed"
                run.error = run.error or reason
                run.finished_at = run.finished_at or now
                session.query(Threads).filter(Threads.thread_id == run.thread_id).update(
                    {Threads.latest_run_status: "failed", Threads.updated_at: now}
                )
                session.query(RunEvents).filter(
                    RunEvents.thread_id == run.thread_id,
                    RunEvents.status.in_(["pending", "in_progress"]),
                ).update({RunEvents.status: "error", RunEvents.updated_at: now})
            session.commit()
            return len(orphan_runs)

    def get_latest_run(self, thread_id: str) -> dict[str, Any] | None:
        with self._Session() as session:
            stmt = (
                select(Runs)
                .where(Runs.thread_id == thread_id)
                .order_by(Runs.started_at.desc())
                .limit(1)
            )
            obj = session.scalars(stmt).first()
            return self._to_dict(obj) if obj else None

    # ── thread_messages ────────────────────────────────────

    def add_thread_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        author: str,
        content: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_content = content.strip()
        if author == "user" and normalized_content:
            with self._Session() as session:
                latest = (
                    session.query(ThreadMessages)
                    .filter(ThreadMessages.thread_id == thread_id, ThreadMessages.author == "user")
                    .order_by(ThreadMessages.created_at.desc())
                    .first()
                )
                if latest is not None and str(latest.content).strip() == normalized_content:
                    return
        with self._Session() as session:
            obj = ThreadMessages(
                message_id=message_id,
                thread_id=thread_id,
                run_id=run_id,
                author=author,
                content=normalized_content,
                metadata=json.dumps(metadata or {}, ensure_ascii=False),
                created_at=utc_now(),
            )
            session.merge(obj)
            session.commit()

    def list_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = (
                select(ThreadMessages)
                .where(ThreadMessages.thread_id == thread_id)
                .order_by(ThreadMessages.created_at.asc())
            )
            return [self._to_dict(o) for o in session.scalars(stmt).all()]

    # ── thread_plans ───────────────────────────────────────

    def add_thread_plan(
        self,
        *,
        plan_id: str,
        thread_id: str,
        prompt: str,
        plan_text: str,
        plan_path: str,
        run_id: str | None = None,
        status: str = "pending",
    ) -> None:
        now = utc_now()
        with self._Session() as session:
            obj = ThreadPlans(
                plan_id=plan_id,
                thread_id=thread_id,
                run_id=run_id,
                status=status,
                prompt=prompt.strip(),
                plan_text=plan_text.strip(),
                plan_path=plan_path,
                created_at=now,
                approved_at=None,
            )
            session.merge(obj)
            session.commit()

    def get_latest_thread_plan(
        self,
        thread_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        with self._Session() as session:
            stmt = select(ThreadPlans).where(ThreadPlans.thread_id == thread_id)
            if status is not None:
                stmt = stmt.where(ThreadPlans.status == status)
            stmt = stmt.order_by(ThreadPlans.created_at.desc()).limit(1)
            obj = session.scalars(stmt).first()
            return self._to_dict(obj) if obj else None

    def list_thread_plans(self, thread_id: str) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = (
                select(ThreadPlans)
                .where(ThreadPlans.thread_id == thread_id)
                .order_by(ThreadPlans.created_at.asc())
            )
            return [self._to_dict(o) for o in session.scalars(stmt).all()]

    def approve_thread_plan(self, plan_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._Session() as session:
            obj = session.get(ThreadPlans, plan_id)
            if obj is None:
                return None
            obj.status = "approved"
            obj.approved_at = now
            session.commit()
            return self._to_dict(obj)

    # ── review_findings ────────────────────────────────────

    def add_finding(
        self,
        *,
        finding_id: str,
        thread_id: str,
        file: str,
        line: int | None,
        severity: str,
        title: str,
        description: str,
        status: str = "open",
    ) -> None:
        now = utc_now()
        with self._Session() as session:
            obj = ReviewFindings(
                id=finding_id,
                thread_id=thread_id,
                file=file,
                line=line,
                severity=severity,
                title=title,
                description=description,
                status=status,
                created_at=now,
                updated_at=now,
            )
            session.merge(obj)
            session.commit()

    def list_findings(self, thread_id: str) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = (
                select(ReviewFindings)
                .where(ReviewFindings.thread_id == thread_id)
                .order_by(ReviewFindings.created_at.asc())
            )
            return [self._to_dict(o) for o in session.scalars(stmt).all()]

    # ── settings ────────────────────────────────────────────

    def set_setting(self, key: str, value: Any) -> None:
        with self._Session() as session:
            obj = Settings(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=utc_now())
            session.merge(obj)
            session.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._Session() as session:
            obj = session.get(Settings, key)
        if obj is None:
            return default
        try:
            return json.loads(obj.value)
        except (ValueError, TypeError):
            return default

    # ── 删除 ───────────────────────────────────────────────

    def delete_thread(self, thread_id: str) -> bool:
        with self._Session() as session:
            if session.get(Threads, thread_id) is None:
                return False
            for model in (ReviewFindings, ThreadPlans, ThreadMessages, RunEvents, Runs):
                session.query(model).filter(model.thread_id == thread_id).delete()
            session.delete(session.get(Threads, thread_id))
            session.commit()
        return True
