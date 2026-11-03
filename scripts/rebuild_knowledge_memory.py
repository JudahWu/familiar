"""重建跨会话记忆的语义索引。

索引是可丢弃的派生数据（`app/knowledge/memory.py` 的设计前提），所以重建就是
"从 PG 重新灌一遍"。什么时候需要跑：

- 换了 embedding 模型（向量空间变了，旧索引全部作废）；
- 索引 collection 被误删、或者结构改了；
- 怀疑索引和 PG 对不上（召回出来的 turn_id 在 PG 里查不到）。

用法：

    python scripts/rebuild_knowledge_memory.py            # 全量重建
    python scripts/rebuild_knowledge_memory.py --dry-run  # 只统计会灌多少条

注意：重建期间召回会短暂返回空（collection 被 drop 后重建），问答不受影响，
只是这段时间没有"跨会话回忆"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from familiar.knowledge import memory  # noqa: E402
from familiar.knowledge.chat import ChatSessionRow, ChatTurnRow  # noqa: E402
from familiar.knowledge.store import _session  # noqa: E402


def load_rows() -> list[dict]:
    """取所有助手轮，连带会话上的 kb_id / owner。

    只取助手轮：和 `memory.index_turn` 的口径一致，用户那句"那它呢"索引进去
    只是噪音。
    """

    with _session() as session:
        rows = session.execute(
            select(
                ChatTurnRow.id,
                ChatTurnRow.session_id,
                ChatTurnRow.role,
                ChatTurnRow.content,
                ChatSessionRow.kb_id,
                ChatSessionRow.owner,
            )
            .join(ChatSessionRow, ChatSessionRow.id == ChatTurnRow.session_id)
            .where(ChatTurnRow.role == "assistant")
            .order_by(ChatTurnRow.id)
        ).all()
    return [
        {
            "turn_id": row.id,
            "session_id": row.session_id,
            "role": row.role,
            "text": row.content,
            "kb_id": row.kb_id,
            "owner": row.owner or "",
        }
        for row in rows
        if (row.content or "").strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="重建知识库问答的跨会话记忆索引")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args()

    if not memory.available():
        print("未配置 embedding（EMBEDDING_PROVIDER / EMBEDDING_DIM），索引无法重建")
        return 1

    rows = load_rows()
    print(f"待索引的助手轮：{len(rows)} 条")
    if args.dry_run:
        return 0

    written = memory.rebuild(rows)
    print(f"已写入：{written} 条 → collection {memory.COLLECTION}")
    return 0 if written == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
