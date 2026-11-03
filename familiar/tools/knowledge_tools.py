"""知识库检索工具（给 Agent 用）。

存在的意义：知识库不只是独立页面，Agent 在写代码、答问题的时候也应该能查它。
没有这个工具，知识库和对话是两套东西，用户得自己复制粘贴。

工具只返回检索到的原文片段，不做生成——**生成交给调用它的模型**。如果这里
自己也调一次模型写答案，Agent 就会拿到两段互相矛盾的文字，而且平白多花一次调用。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from familiar.core.events import record_event
from familiar.knowledge import service, store
from familiar.tools.runtime_context import get_runtime_thread_id

logger = logging.getLogger("familiar.run.knowledge")

# 跨库检索时最多扫几个库。每个库都要做一次 embedding + 两次 Milvus 查询，
# 库多了会拖慢工具返回，而 Agent 大多数时候只关心最相关的那几段。
_MAX_BASES = 5


def _format_hit(index: int, hit: dict[str, Any], base_name: str) -> str:
    title = hit.get("title") or hit.get("doc_title") or "未命名文档"
    page = hit.get("page") or 0
    kind = "插图描述" if hit.get("category") == "image" else "正文"
    return (
        f"[{index}]《{title}》第 {page} 页 · {kind} · 知识库「{base_name}」\n"
        f"{hit.get('text', '').strip()}"
    )


def _search_all_bases(query: str, top_k: int) -> list[tuple[dict[str, Any], str]]:
    """不指定库时，逐个库检索再按分数合并。

    分数能直接比：`service.search` 在配了 embedding 时用的是 RRF 名次分，
    同一套算法算出来的值可以横向排序；只走 BM25 时两边都是 BM25 分数，也同量纲。
    """

    bases = store.list_bases()[:_MAX_BASES]
    merged: list[tuple[dict[str, Any], str]] = []
    for base in bases:
        try:
            hits = service.search(base.id, query, top_k=top_k)
        except Exception:  # noqa: BLE001 - 某个库坏了不该让整次检索失败
            logger.warning("检索知识库失败，跳过：%s", base.id, exc_info=True)
            continue
        merged.extend((hit, base.name) for hit in hits)

    merged.sort(key=lambda item: item[0].get("score") or 0.0, reverse=True)
    return merged[:top_k]


@tool("search_knowledge", parse_docstring=True)
def search_knowledge(query: str, kb_id: str = "", top_k: int = 5) -> str:
    """检索本地知识库（用户上传的 PDF 资料），返回相关原文片段。

    适合用来查用户上传的文档、手册、教材里的内容，例如框架用法、配置项含义、
    书中某个概念的原文表述。不要用它查代码仓库里的实现——那应该直接读文件。

    Args:
        query: 检索关键词或问题。用具体名词比整句提问更容易命中。
        kb_id: 指定知识库 ID。留空则在前几个知识库里依次检索并合并结果。
        top_k: 返回几条，默认 5，最大 20。

    Returns:
        带出处（文档名、页码、所属知识库）的原文片段；没有命中时返回明确说明。
    """

    normalized = " ".join((query or "").split())
    if not normalized:
        return "检索失败：query 不能为空"

    thread_id = get_runtime_thread_id()
    if thread_id:
        record_event(
            thread_id,
            f"search_knowledge:{normalized[:60]}",
            "检索知识库",
            kind="fetch",
            status="in_progress",
            detail=json.dumps({"query": normalized, "kb_id": kb_id}, ensure_ascii=False),
        )

    try:
        limit = min(20, max(1, int(top_k or 5)))
        if kb_id:
            base = store.get_base(kb_id)
            pairs = [(hit, base.name) for hit in service.search(kb_id, normalized, top_k=limit)]
        else:
            pairs = _search_all_bases(normalized, limit)
    except FileNotFoundError:
        return f"检索失败：知识库 {kb_id} 不存在"
    except Exception as exc:  # noqa: BLE001 - 工具失败要返回文本让模型继续处理
        logger.warning("知识库检索异常：query=%s", normalized, exc_info=True)
        if thread_id:
            record_event(
                thread_id,
                f"search_knowledge:{normalized[:60]}",
                "检索知识库",
                kind="fetch",
                status="error",
                detail=json.dumps({"error": str(exc)[:200]}, ensure_ascii=False),
            )
        return f"检索失败：{exc}"

    if not pairs:
        output = "知识库里没有检索到相关内容。可以换个说法，或告诉用户需要先上传对应文档。"
    else:
        output = "\n\n".join(
            _format_hit(index, hit, base_name) for index, (hit, base_name) in enumerate(pairs, start=1)
        )

    if thread_id:
        record_event(
            thread_id,
            f"search_knowledge:{normalized[:60]}",
            "检索知识库",
            kind="fetch",
            status="completed",
            detail=json.dumps(
                {"query": normalized, "hits": len(pairs), "preview": output[:400]},
                ensure_ascii=False,
            ),
        )
    return output
