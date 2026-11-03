"""联网搜索工具。

用于在本地仓库/知识库的上下文不足时补充外部公开资料，例如框架文档、第三方库变更
说明、错误信息背景。

**后端是 DeepSeek 的搜索接口**（`/v1/tools/search`），不是智谱。换的原因很实际：
项目本来就要 `DEEPSEEK_API_KEY`，同一把钥匙顺手能搜网页，不必再维护另一套 key 和
SDK；少一个可选依赖就少一处「装了没用、没装报错」的隐患。实现细节见
`app/tools/deepseek_search.py`。

工具名保留 `web_search` 没有改：提示词、技能文档、错误事件映射都按这个名字写的，
改名只会制造一堆要同步的引用，而模型并不关心后端是谁。
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from familiar.core.events import record_event
from familiar.tools.deepseek_search import DeepSeekSearchError, format_for_context, search_web
from familiar.tools.runtime_context import get_runtime_thread_id

logger = logging.getLogger("familiar.run.web_search")

# 条数。5 条足够覆盖一个具体问题，再多主要是重复内容，而且正文会挤占上下文。
_RESULT_COUNT = 5


@tool("web_search", parse_docstring=True)
def web_search(query: str) -> str:
    """使用 DeepSeek 网页搜索接口查找公开资料。

    适用于需要外部资料支撑的任务，例如查询最新框架文档、第三方库用法、
    API 变更说明、行业资料或错误信息背景。不要搜索密钥、token、私有仓库内容。

    Args:
        query: 需要搜索的关键词或问题。

    Returns:
        搜索结果摘要文本（带标题和链接）；失败时返回以「搜索失败：」开头的可读原因。
    """

    thread_id = get_runtime_thread_id()
    # 压缩空白：模型有时会把 query 写成多行，带着换行发出去命中率会变差。
    normalized_query = " ".join((query or "").split())
    if not normalized_query:
        return "搜索失败：query 不能为空"

    if thread_id:
        # 写入工具调用事件，前端可以展示"正在联网搜索"的运行过程。
        record_event(
            thread_id,
            f"web_search:{normalized_query[:80]}",
            "联网搜索资料",
            kind="fetch",
            status="in_progress",
            detail=json.dumps({"query": normalized_query}, ensure_ascii=False),
        )

    try:
        hits = search_web(normalized_query, count=_RESULT_COUNT)
        output = format_for_context(hits) if hits else "没有搜索到任何内容。"
    except DeepSeekSearchError as exc:
        # 鉴权失败、余额不足、限流都走这里。返回文本而不是抛异常：调用方（Agent 或
        # 知识库问答）需要能继续降级，而不是整个请求 500。
        logger.warning("联网搜索失败：query=%s error=%s", normalized_query, exc)
        if thread_id:
            record_event(
                thread_id,
                f"web_search:{normalized_query[:80]}",
                "联网搜索资料",
                kind="fetch",
                status="error",
                detail=json.dumps({"query": normalized_query, "error": str(exc)}, ensure_ascii=False),
            )
        return f"搜索失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都不该让工具崩掉
        logger.exception("联网搜索出现未预期异常：query=%s", normalized_query)
        return f"搜索失败：{exc}"

    if thread_id:
        record_event(
            thread_id,
            f"web_search:{normalized_query[:80]}",
            "联网搜索资料",
            kind="fetch",
            status="completed",
            detail=json.dumps(
                {"query": normalized_query, "hits": len(hits), "preview": output[:1200]},
                ensure_ascii=False,
            ),
        )
    return output
