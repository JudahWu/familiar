"""验证阶段 ④：答案生成、引用编号、自评与覆盖度判断、Agent 工具。

不需要真实 PDF 导入（那要三十秒和几十次 API 调用）——直接挑库里已有文档的库来问。
库里没有可用文档时，会提示先导入，不会假装跑通。

跑法：python scripts/verify_knowledge_answer.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from familiar.knowledge import answer as answer_module  # noqa: E402
from familiar.knowledge import chat, service, store  # noqa: E402
from familiar.tools import search_knowledge  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def citation_numbers(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"\[(\d{1,2})\]", text)]


def main() -> int:
    bases = [base for base in store.list_bases() if any(doc.status == "indexed" for doc in base.documents)]
    if not bases:
        print("库里没有已入库的文档，先上传一份 PDF 再跑这个脚本。")
        return 2

    # 优先挑一个有插图的库：图片检索那节需要拿库内的图当查询图
    kb = next(
        (base for base in bases if any(doc.images > 0 for doc in base.documents)),
        bases[0],
    )
    print(f"用库：{kb.name}（{kb.id}），{len(kb.documents)} 篇文档\n")

    print("1) 问一个库里有的问题")
    hit_result = answer_module.answer(kb.id, "Flink 的版本演进")
    answer_text = hit_result["answer"]
    print(f"     模型={hit_result['model']} 分数={hit_result['score']} 覆盖度={hit_result['coverage']}")
    print(f"     答案：{answer_text[:90]}…")
    check("答案非空", bool(answer_text.strip()))
    check("引用了命中段落", bool(hit_result["hits"]))
    check("答案里标了出处编号", bool(citation_numbers(answer_text)))
    # 编号越界说明模型编了引用，这是必须发现的问题：前端会把它标红
    over = [n for n in citation_numbers(answer_text) if n > len(hit_result["hits"])]
    check("编号都在命中范围内", not over, f"越界编号 {over}")
    check(
        "分数在 0-1 之间",
        hit_result["score"] is None or 0.0 <= hit_result["score"] <= 1.0,
        str(hit_result["score"]),
    )

    print("\n2) 问一个库里没有的问题（检查覆盖度判断）")
    miss_result = answer_module.answer(kb.id, "Kafka 的 ISR 副本同步机制是怎么实现的")
    print(f"     分数={miss_result['score']} 覆盖度={miss_result['coverage']} 待确认={miss_result['needs_review']}")
    print(f"     理由：{miss_result['reason']}")
    # 关键点：答案诚实（分数可以很高）但覆盖度必须是 none/partial，否则用户会以为库里真有答案
    check(
        "覆盖度判定为未覆盖或不完整",
        miss_result["coverage"] in {"none", "partial"},
        str(miss_result["coverage"]),
    )
    check("触发了待确认提示", miss_result["needs_review"], miss_result["review_reason"])

    print("\n3) 覆盖度完整时不打扰用户")
    check(
        "命中问题时不需要人工确认",
        hit_result["needs_review"] is False or hit_result["coverage"] in {"none", "partial"},
        f"coverage={hit_result['coverage']} review={hit_result['needs_review']}",
    )

    print("\n4) 换联网搜索：成功就用，失败要说清原因")
    web_result = answer_module.answer(kb.id, "Kafka 的 ISR 副本同步机制是怎么实现的", use_web=True)
    ok_path = web_result["web_used"] and not web_result["web_error"]
    err_path = (not web_result["web_used"]) and bool(web_result["web_error"])
    print(f"     web_used={web_result['web_used']} web_error={web_result['web_error'][:60] or '(无)'}")
    check("联网要么生效要么给出原因（不能静默退化）", ok_path or err_path)

    print("\n5) Agent 工具")
    tool_out = search_knowledge.invoke({"query": "有界流", "kb_id": kb.id, "top_k": 2})
    print(f"     {tool_out[:120]}…")
    check("工具名正确", getattr(search_knowledge, "name", "") == "search_knowledge")
    check("工具返回带出处", "第" in tool_out and "页" in tool_out)
    check("工具返回带库名", kb.name in tool_out)

    print("\n6) 空查询与不存在的库")
    try:
        answer_module.answer(kb.id, "   ")
        check("空问题应报错", False)
    except ValueError:
        check("空问题被拒绝", True)
    try:
        search_knowledge.invoke({"query": "x", "kb_id": "kb-not-exist", "top_k": 1})
        # 工具不抛错，返回文本即可——Agent 需要可读的失败信息而不是异常
        check("不存在的库返回可读文本", True)
    except Exception as exc:  # noqa: BLE001
        check("不存在的库返回可读文本", False, str(exc)[:80])

    print("\n7) 会话记忆：追问能被改写，历史能落库也能恢复")
    first = answer_module.answer(kb.id, "Flink 的窗口是什么")
    sid = first["session_id"]
    check("首问生成了 session_id", bool(sid), sid)
    check("首问没有历史", first["history_turns"] == 0, str(first["history_turns"]))

    follow = answer_module.answer(kb.id, "它有哪些类型？", session_id=sid)
    print(f"     追问：'它有哪些类型？' → 检索用 {follow['search_query']!r}")
    check("追问读到了历史", follow["history_turns"] > 0, str(follow["history_turns"]))
    # 这一条是整个记忆功能的核心：不改写的话"它"会被原样拿去检索，必然搜不准
    check("追问被改写成独立问题", follow["search_query"] != "它有哪些类型？", follow["search_query"])
    check("改写后仍能命中", bool(follow["hits"]))

    stored = chat.list_turns(sid)
    check("问答成对落库", len(stored) == 4, str([t.role for t in stored]))
    check(
        "助手轮带引用快照（刷新后引用可点）",
        bool(stored[-1].citations),
        str(len(stored[-1].citations)),
    )

    stateless = answer_module.answer(kb.id, "有界流是什么")
    check("不传 session_id 时退化成无记忆", stateless["history_turns"] == 0)

    # 清理验证产生的会话，别让每次跑脚本都往库里堆垃圾
    for turn_session in {sid, stateless.get("session_id")}:
        if turn_session:
            chat.delete_session(turn_session)
    check("验证会话已清理", not chat.list_turns(sid))

    print("\n8) 语义切分：同一页要被切成多块，而不是并成一大块")
    from familiar.knowledge.extract import CHUNK_SIZE, PageText, chunk_pages

    # 必须超过 CHUNK_SIZE 才会触发语义分段，所以两段各重复几遍把长度撑上去。
    # 前半讲窗口，后半讲部署，中间是天然的语义边界。
    window_text = (
        "窗口是流处理的核心抽象，把无界的流切成有限的片段来计算。"
        "滚动窗口按固定长度切分数据流，窗口之间不重叠。"
        "滑动窗口在滚动的基础上允许重叠，重叠比例决定计算的平滑程度。"
        "会话窗口按活动间隔划分数据，间隔超过阈值就认为一段会话结束。"
        "窗口的触发依赖水位线推进，水位线滞后会导致结果延迟输出。"
    ) * 3
    deploy_text = (
        "部署模式包括会话模式、独立模式和应用模式三种。"
        "会话模式适合资源充足的小规模集群，多个作业共享一个集群。"
        "独立模式把 JobManager 和 TaskManager 分开部署，一个作业一个集群。"
        "应用模式把用户代码放到集群内部执行，减少客户端依赖。"
        "生产环境通常还会配合 Kubernetes 做弹性伸缩和故障迁移。"
    ) * 3
    page = PageText(page=1, text=f"{window_text}\n\n{deploy_text}")
    print(f"     测试页 {len(page.text)} 字（上限 {CHUNK_SIZE}）")
    check("测试页超过切分上限（否则语义分支不会触发）", len(page.text) > CHUNK_SIZE)

    semantic_chunks = chunk_pages([page], semantic=True)
    plain_chunks = chunk_pages([page], semantic=False)
    print(f"     语义 {len(semantic_chunks)} 块 / 固定 {len(plain_chunks)} 块")
    check("语义切分真的切开了", len(semantic_chunks) > len(plain_chunks), str(len(semantic_chunks)))
    check("每块都不超过上限", all(len(c.text) <= CHUNK_SIZE for c in semantic_chunks))
    check("块编号连续", [c.chunk_index for c in semantic_chunks] == list(range(len(semantic_chunks))))
    # 关掉语义必须还能跑（降级路径不能因为语义代码有问题就挂）
    check("关掉语义仍可切分", bool(plain_chunks))

    print("\n9) 图片检索：拿库里自己的插图去搜，应该命中图片条目")
    image_docs = [
        doc
        for doc in kb.documents
        if doc.status == "indexed" and doc.images > 0
    ]
    if not image_docs:
        print("     跳过：这个库里没有插图")
    else:
        from familiar.knowledge import objects

        doc = image_docs[0]
        keys = [
            item.object_name
            for item in objects.client().list_objects("familiar-knowledge", recursive=True)
            if item.object_name.startswith(f"{kb.id}/{doc.id}/images/")
        ]
        if not keys:
            print("     跳过：MinIO 里没找到插图对象")
        else:
            raw = objects.get_document(sorted(keys)[0])
            hits = service.search(kb.id, image_bytes=raw, top_k=3)
            print(f"     查询图 {len(raw) // 1024}KB → 命中 {len(hits)} 条")
            check("图片检索有结果", bool(hits))
            check(
                "命中里包含图片条目",
                any(hit["category"] == "image" for hit in hits),
                str([hit["category"] for hit in hits]),
            )
            check("图片检索不带 BM25 分数异常", all(hit["score"] > 0 for hit in hits))

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
