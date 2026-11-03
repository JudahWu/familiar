"""验证知识库链路：PG 元信息 + MinIO 原件 + Milvus 检索，三段都验。

用的是 Multimodal_RAG 里那本 Flink 第二章的 PDF（有文字层，适合验证抽取质量）。
不起 HTTP 服务，直接调 `app.knowledge.service`，所以能单独验证存储层是否正确。

运行：python scripts/verify_knowledge_retrieval.py [pdf路径]
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from familiar.core.settings import S3_BUCKET  # noqa: E402
from familiar.knowledge import objects, service, store  # noqa: E402

DEFAULT_PDF = Path("/Users/zhua/Downloads/Multimodal_RAG/dots_ocr/第二章 Apache Flink快速入门.pdf")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def pg_count(doc_id: str) -> int:
    with store._session() as session:
        return session.execute(
            text("select count(*) from knowledge_documents where id = :id"), {"id": doc_id}
        ).scalar_one()


def object_exists(key: str) -> bool:
    """直接问 S3：对象还在不在。用 stat_object 而不是列举，单个 key 更快。"""

    try:
        objects.client().stat_object(S3_BUCKET, key)
        return True
    except Exception:  # noqa: BLE001 - 不存在就会抛 S3Error
        return False


def main() -> int:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf.exists():
        print(f"找不到测试 PDF：{pdf}")
        return 2

    content = pdf.read_bytes()
    name = f"验证库-{uuid.uuid4().hex[:6]}"
    print(f"1) 建库：{name}")
    base = service.create_base(name, owner="verify@local")
    kb_id = base["id"]
    check("库已创建", bool(base["id"]) and base["chunks"] == 0)

    print(f"2) 导入：{pdf.name}（{len(content) / 1024 / 1024:.1f} MB）")
    started = time.time()
    doc = service.import_pdf(kb_id, filename=pdf.name, content=content)
    elapsed = time.time() - started
    print(
        f"     状态={doc['status']} 页={doc['pages']} 段落={doc['paragraphs']} "
        f"纯文字页={doc['text_pages']} 待识别页={doc['vision_pages']} 用时={elapsed:.1f}s"
    )
    check("导入成功", doc["status"] == "indexed", str(doc)[:300])
    check("切出了段落", doc["paragraphs"] > 10, f"只有 {doc['paragraphs']} 段")

    print("3) 三处存储各就各位")
    check("PostgreSQL 有文档行", pg_count(doc["id"]) == 1)
    stored = store.get_document(kb_id, doc["id"])
    check("元信息里记了对象 key", bool(stored.object_key), stored.object_key)
    check("记录了原始大小", stored.size_bytes == len(content), f"{stored.size_bytes} vs {len(content)}")
    # 从 MinIO 真读回来，比对字节，确认不是只写了个 key
    back = objects.get_document(stored.object_key)
    check("MinIO 里的原件可读且字节一致", back == content, f"{len(back)} vs {len(content)}")
    # 从列表接口读一遍，确认段落数是 PG 里那列给的（不再去问 Milvus）。
    # 按 kb_id 找而不是取 [0]：库里可能有别的库，取第一个会测错对象。
    listed = next((item for item in service.list_bases() if item["id"] == kb_id), None)
    check("列表页段落数来自 PG", bool(listed) and listed["chunks"] == doc["paragraphs"], str(listed and listed["chunks"]))

    print(f"3.5) 插图：抽出 {doc.get('images', 0)} 张")
    image_objects = [
        obj.object_name
        for obj in objects.client().list_objects(S3_BUCKET, prefix=f"{kb_id}/", recursive=True)
        if "/images/" in obj.object_name
    ]
    check("插图进了 MinIO", len(image_objects) == doc.get("images", 0), f"{len(image_objects)} vs {doc.get('images')}")
    if image_objects:
        first = objects.get_document(image_objects[0])
        check("插图内容非空", len(first) > 100, f"{len(first)}B")

    print("4) 重复导入应被挡住")
    dup = service.import_pdf(kb_id, filename=pdf.name, content=content)
    check("识别为重复", dup.get("duplicate") is True, str(dup)[:200])
    check("没有新增第二篇文档", len(service.get_base_detail(kb_id)["documents"]) == 1)

    print("5) 检索（query 里的词必须在语料里出现过，否则测的是噪音）")
    cases = [
        ("有界流和无界流的区别", ["有界流", "无界流"]),
        ("DataStream API", ["DataStream"]),
        ("WordCount 案例", ["WordCount"]),
    ]
    tops: list[float] = []
    for query, keywords in cases:
        hits = service.search(kb_id, query, top_k=3)
        head = hits[0] if hits else {}
        if hits:
            tops.append(hits[0]["score"])
        print(f"     {query!r} → {len(hits)} 条；首条 p{head.get('page')} score={head.get('score')}")
        check(f"命中：{query}", bool(hits))
        # BM25 的分数没有绝对意义，只能看"前 3 条里有没有真的包含关键词的段落"
        check(
            f"前 3 条含关键词：{query}",
            any(any(k in hit["text"] for k in keywords) for hit in hits),
            str([hit["text"][:30] for hit in hits])[:200],
        )

    # 语料里完全没有的词应该明显低分。这条是给"以后换成向量检索"留的回归基线：
    # 如果哪天乱答的分数和真正命中的分数一样高，说明打分坏了。
    miss = service.search(kb_id, "checkpoint 与 savepoint", top_k=3)
    miss_score = miss[0]["score"] if miss else 0.0
    print(f"     无对应内容的问题 → 首条 score={miss_score}（真正命中的首条约 {min(tops) if tops else 0}）")
    check("无关问题分数明显更低", miss_score < (min(tops) if tops else 0) * 0.5, f"{miss_score} vs {tops}")

    print("5.5) 插图条目：能被检索到，且带 category / image_path")
    all_hits = service.search(kb_id, "IntelliJ 插件", top_k=10)
    image_hits = [h for h in all_hits if h.get("category") == "image"]
    print(f"     命中 {len(all_hits)} 条，其中图片条目 {len(image_hits)} 条")
    check("能命中插图条目", bool(image_hits), str([h["text"][:40] for h in all_hits])[:200])
    if image_hits:
        check("图片条目带 MinIO key", bool(image_hits[0].get("image_path")), str(image_hits[0])[:160])
        check("图片条目有描述文本", len(image_hits[0].get("text") or "") > 10)

    print("6) 删除：三处都要清干净")
    key = stored.object_key
    service.delete_base(kb_id)
    check("PG 里库没了", not [item for item in service.list_bases() if item["id"] == kb_id])
    check("PG 里文档被级联删除", pg_count(doc["id"]) == 0)
    check("MinIO 里的原件被删", not object_exists(key), key)

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
