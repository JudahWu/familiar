from familiar.core.settings import PERSISTENCE_TYPE
from .postgres_store import PostgresStore
from .sqlite_store import LocalSqliteStore

__all__ = ["LocalSqliteStore", "PostgresStore", "build_store"]


def build_store(db_path) -> "LocalSqliteStore | PostgresStore":
    """按 PERSISTENCE_TYPE 选择业务 Store 实现。

    - sqlite（默认）：LocalSqliteStore，零外部依赖、可直接跑；
    - postgres：PostgresStore，会话/记忆/业务数据落到 docker-compose 的 PostgreSQL。

    调用方（graph.get_store / events / repo_mapping）无需感知底层实现。
    """

    if PERSISTENCE_TYPE == "postgres":
        return PostgresStore(db_path)
    return LocalSqliteStore(db_path)
