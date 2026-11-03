"""LangGraph 持久化层：checkpointer 与长期记忆 store。

项目里其实有三套持久化，职责不同：

1. `checkpoints.sqlite` —— LangGraph checkpointer，保存 Agent 的 thread state、
   messages、工具调用状态。负责"重启后接着聊"。
2. `langgraph_store.sqlite` —— LangGraph Store，服务 DeepAgents 的
   `/memories/...` 长期记忆文件。
3. `store.sqlite` —— 业务 Store（见 `app/store/`），存线程列表、run 事件、
   review findings 等页面要查的数据。

三套都可以通过 `PERSISTENCE_TYPE=postgres` 切到 PostgreSQL，这样重启服务
（或换机器）之后记忆和会话历史都不丢。

PG 版本的实现说明：
- `langgraph-checkpoint-postgres` / `langgraph-store-postgres` 底层用 psycopg2，
  不认识 SQLAlchemy 风格的 `postgresql+psycopg://` 后缀，这里统一转换。
- 官方 API 是 `with PostgresSaver.from_conn_string(url) as saver:` 形式，退出
  上下文会关掉连接池。而 FastAPI 进程要活很久，所以这里用一个全局 ExitStack
  把上下文按住，直到进程退出（atexit）才统一关闭。
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from familiar.core.settings import DATABASE_URL, PERSISTENCE_TYPE

logger = logging.getLogger("familiar.persistence")

# 持有 PG saver/store 的上下文，保证连接池在整个进程生命周期内可用。
_PG_STACK = ExitStack()
atexit.register(_PG_STACK.close)


def _pg_conn_string() -> str:
    """把业务用的 DATABASE_URL 转成 langgraph postgres 包能接受的连接串。

    `langgraph-checkpoint-postgres` 和 `langgraph-store-postgres` 都基于 psycopg2，
    无法解析 SQLAlchemy 风格的 `postgresql+psycopg://` 驱动后缀，这里剥掉。
    """

    url = (DATABASE_URL or "").strip()
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _safe_url(url: str) -> str:
    """日志里隐藏密码，只保留 host/db 部分。"""

    if "@" not in url:
        return url
    _, _, tail = url.rpartition("@")
    return f"***@{tail}"


def make_checkpointer(db_path: Path) -> Any:
    """创建 LangGraph checkpointer。

    `PERSISTENCE_TYPE=postgres` 时写 PostgreSQL，否则写本地 SQLite 文件。
    """

    if PERSISTENCE_TYPE == "postgres":
        return _make_pg_checkpointer()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def _make_pg_checkpointer() -> Any:
    from langgraph.checkpoint.postgres import PostgresSaver

    conn_str = _pg_conn_string()
    logger.info("使用 PostgreSQL checkpointer：%s", _safe_url(conn_str))
    saver = _PG_STACK.enter_context(PostgresSaver.from_conn_string(conn_str))
    saver.setup()
    return saver


def make_langgraph_store(db_path: Path) -> Any:
    """创建 DeepAgents StoreBackend 使用的 LangGraph Store。

    该数据库只负责 `/memories/...` 这类长期记忆文件的底层持久化。
    表结构、文件内容格式和读写协议都由 LangGraph Store 管理，项目代码只负责
    提供数据库路径和复用同一个 store 实例。
    """

    if PERSISTENCE_TYPE == "postgres":
        return _make_pg_langgraph_store()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    store.setup()
    return store


def _make_pg_langgraph_store() -> Any:
    from langgraph.store.postgres import PostgresStore

    conn_str = _pg_conn_string()
    logger.info("使用 PostgreSQL LangGraph Store：%s", _safe_url(conn_str))
    store = _PG_STACK.enter_context(PostgresStore.from_conn_string(conn_str))
    store.setup()
    return store
