"""验证删除文档：Milvus 向量、MinIO 对象、PG 元信息三处都要清干净。

重点验两件容易漏的事：
1. **插图会不会变成孤儿**。插图在 MinIO 里的 key 是
   `<kb_id>/<doc_id>/images/xxx.png`，跟原件不是同一个对象。只删原件的话
   插图会留下来占空间。
2. **删掉正在导入的文档会怎样**。后台线程还在跑的话，会把删掉的向量和图片
   又写回去，用户看到的是"删了又冒出来"。

运行：python scripts/verify_knowledge_delete.py [pdf路径]
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from familiar.core.settings import S3_BUCKET  # noqa: E402
from familiar.knowledge import objects, service, store, tasks  # noqa: E402

DEFAULT_PDF = Path("/Users/zhua/Downloads/Multimodal_RAG/dots_ocr/第二章 Apache Flink快速入门.pdf")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def pg_rows(doc_id: str) -> int:
    with store._session() as session:
        return session.execute(
            text("select count(*) from knowledge_documents where id = :id"), {"id": doc_id}
        ).scalar_one()


def minio_objects(prefix: str) -> list[str]:
    return [o.object_name for o in objects.client().list_objects(S3_BUCKET, prefix=prefix, recursive=True)]


def milvus_rows(doc_id: str) -> int:
    rows = objects.client()  # 仅为保持 import 顺序整洁
    del rows
    from familiar.knowledge import schema

    hits = schema.client().query(
        collection_name=schema.COLLECTION,
        filter=f'doc_id == "{doc_id}"',
        output_fields=["doc_id"],
        limit=100,
    )
    return len(hits)


def main() -> int:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf.exists():
        print(f"找不到测试 PDF：{pdf}")
        return 2

    content = pdf.read_bytes()

    print("1) 建库 + 导入")
    kb_id = service.create_base(f"删除验证-{uuid.uuid4().hex[:5]}")["id"]
    doc = service.import_pdf(kb_id, filename=pdf.name, content=content)
    doc_id = doc["id"]
    print(f"   kb={kb_id} doc={doc_id} 段落={doc['paragraphs']} 插图={doc['images']}")

    prefix = f"{kb_id}/{doc_id}/"
    check("导入成功", doc["status"] == "indexed", str(doc)[:200])
    check("PG 有记录", pg_rows(doc_id) == 1)
    check("MinIO 有原件和插图", len(minio_objects(prefix)) >= 1 + doc["images"], str(minio_objects(prefix))[:200])
    check("Milvus 有向量", milvus_rows(doc_id) > 0)

    print("2) 非空库不允许删除")
    try:
        service.delete_base(kb_id)
        check("有文档时删库被拒绝", False, "居然删成功了")
    except ValueError as exc:
        check("有文档时删库被拒绝", True)
        print(f"   提示语：{exc}")
    except FileNotFoundError:
        check("有文档时删库被拒绝", False, "抛的是 FileNotFoundError，应该是 ValueError")
    check("库还在", any(b["id"] == kb_id for b in service.list_bases()))

    print("3) 删除文档")
    service.delete_document(kb_id, doc_id)

    check("PG 记录已删", pg_rows(doc_id) == 0)
    check("Milvus 向量已删", milvus_rows(doc_id) == 0, f"还剩 {milvus_rows(doc_id)} 行")
    left = minio_objects(prefix)
    check("MinIO 对象已删（含插图）", not left, f"还剩 {left}")

    print("4) 空库现在可以删了")
    service.delete_base(kb_id)
    check("库已删除", not any(b["id"] == kb_id for b in service.list_bases()))
    check("MinIO 前缀已清", not minio_objects(f"{kb_id}/"))

    print("5) 删除正在导入的文档：后台任务必须停下来")
    kb2 = service.create_base(f"取消验证-{uuid.uuid4().hex[:5]}")["id"]
    doc2 = store.add_document(kb2, filename=pdf.name, content=content)
    tasks.spawn_import(kb2, doc2.id, pdf.name, content)
    time.sleep(2)  # 让它跑起来，别在还没开始时就删
    service.delete_document(kb2, doc2.id)
    print("   已删除，等后台线程收尾…")
    for _ in range(40):
        if not tasks.is_cancelled(doc2.id):
            break
        time.sleep(0.5)
    time.sleep(3)  # 再给一点时间，让线程真正结束

    progress = tasks.get_progress(doc2.id)
    print(f"   进度状态：{progress.get('status') if progress else '（已清理）'}")
    check("后台任务已取消", bool(progress) and progress.get("status") == "cancelled", str(progress))
    check("取消后 PG 无残留", pg_rows(doc2.id) == 0)
    check("取消后 MinIO 无残留", not minio_objects(f"{kb2}/{doc2.id}/"))
    check("取消后 Milvus 无残留", milvus_rows(doc2.id) == 0, f"还剩 {milvus_rows(doc2.id)} 行")

    print("6) 清理")
    service.delete_base(kb2)
    check("库已清空", not minio_objects(f"{kb_id}/") and not minio_objects(f"{kb2}/"))

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
