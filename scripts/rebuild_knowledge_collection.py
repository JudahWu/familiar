"""重建 Milvus 向量集合。

为什么必须重建：Milvus 不允许给已有集合补一个可用的向量字段。实测 2.6.25：
事后 add 一个 `nullable=True` 的 FLOAT_VECTOR 字段能建索引，但一检索就报
`field dense is not loaded, please reload the collection` —— 加字段后集合的
加载状态是残的。所以下面这些情况都必须重建集合：

- 换 embedding 模型（维度变了）
- 第一次加 dense 字段
- 加 image_path / category 这类新字段

重建会清空所有向量。脚本会**连带删掉受影响的文档记录和 MinIO 里的原件**，
理由是：只删向量、留元信息，会出现"PG 里显示已入库、但向量没了"的幽灵文档，
而且重新上传同一份 PDF 会被 checksum 去重逻辑挡住（它只认 status='indexed'）。
三条路径一起清干净，重新导入就是全新的一条。

用法：
    python scripts/rebuild_knowledge_collection.py            # 重建并清理
    python scripts/rebuild_knowledge_collection.py --dry-run  # 只看会删什么
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from familiar.knowledge import schema, service, store  # noqa: E402


def _existing_documents() -> list[tuple[str, str]]:
    """列出集合里现存的 (kb_id, doc_id)。"""

    if not schema.collection_exists():
        return []
    try:
        rows = schema.client().query(
            collection_name=schema.COLLECTION,
            filter="id >= 0",
            output_fields=["kb_id", "doc_id"],
            limit=16384,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"读取现有数据失败：{exc}")
        return []
    seen: list[tuple[str, str]] = []
    for row in rows:
        pair = (row.get("kb_id", ""), row.get("doc_id", ""))
        if pair[0] and pair[1] and pair not in seen:
            seen.append(pair)
    return seen


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    print(f"目标集合：{schema.COLLECTION}")
    print(f"稠密向量维度：{schema.embedding_dim()}（None 表示不建 dense 字段）")

    pairs = _existing_documents()
    if not pairs:
        print("集合里没有数据，只需重建集合本身。")
    else:
        bases = sorted({kb for kb, _ in pairs})
        print(f"集合里现有 {len(pairs)} 篇文档，涉及 {len(bases)} 个知识库：")
        for kb in bases:
            docs = [doc for owner, doc in pairs if owner == kb]
            print(f"  {kb}  {len(docs)} 篇")
        if dry_run:
            print("\n--dry-run：到此为止，不会改动任何东西。")
            return 0

    if pairs:
        print("\n删除文档记录（PG 行 + MinIO 原件）...")
        for kb_id, doc_id in pairs:
            try:
                service.delete_document(kb_id, doc_id)
                print(f"  已删 {kb_id}/{doc_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  删不掉 {kb_id}/{doc_id}：{exc}")

    print("\n重建集合...")
    if schema.collection_exists():
        schema.client().drop_collection(schema.COLLECTION)
        print("  旧集合已删除")
    schema.ensure_collection()
    print(f"  新集合已创建：{schema.COLLECTION}")

    fields = [f["name"] for f in schema._describe_fields().values()]
    print(f"  字段：{fields}")

    print("\n完成。重新上传 PDF 即可恢复。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
