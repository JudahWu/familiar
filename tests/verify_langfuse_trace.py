"""校验 Langfuse 应用侧接入是否真的把 trace 送出去了。

思路：不碰 DeepSeek 额度，用 FakeListChatModel 起一个最小 LangGraph 图，
走和线上完全相同的 `observability.langfuse_trace` 分支（config 注入 callback
+ propagate_attributes），然后直接查 Langfuse 的 ClickHouse，看这条 trace 有没有落地。

为什么查 ClickHouse 而不是调 REST API：Langfuse v4 是 events_only 模式，
`/api/public/traces`、`/api/public/observations`、`/api/public/sessions`
这些读接口在 v4 部署上直接返回 404（官方说明见 /docs/v4），事件统一写进
ClickHouse 的 events_full / events_core 表。所以校验只能落到数据层。

跑法：
    /Users/zhua/.workbuddy/binaries/python/envs/default/bin/python scripts/verify_langfuse_trace.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from familiar.core import settings
from familiar.core.observability import (
    flush,
    langfuse_enabled,
    langfuse_trace,
    trace_id_for_run,
)

# compose 里的 ClickHouse 容器名（V4 事件存储）。
CH_CONTAINER = os.environ.get("LANGFUSE_CLICKHOUSE_CONTAINER", "familiar-lf-clickhouse-1")
CH_DATABASE = os.environ.get("LANGFUSE_CLICKHOUSE_DATABASE", "default")


class _State(TypedDict, total=False):
    messages: list[BaseMessage]


def _build_graph() -> Any:
    """最小图：一个节点调一次 chat model。"""

    model = FakeListChatModel(responses=["pong from fake model"])

    def node(state: _State) -> dict[str, Any]:
        reply = model.invoke(state["messages"])
        return {"messages": [*state["messages"], reply]}

    graph = StateGraph(_State)
    graph.add_node("chat", node)
    graph.add_edge(START, "chat")
    graph.add_edge("chat", END)
    return graph.compile()


def _query_events(trace_id: str) -> list[dict[str, Any]]:
    """直接读 ClickHouse，拿这条 trace 下的所有 span。"""

    sql = (
        "SELECT name, type, trace_name, session_id, tags, environment, is_app_root, "
        "metadata_names, metadata_values "
        f"FROM {CH_DATABASE}.events_full WHERE trace_id = '{trace_id}' "
        "ORDER BY start_time FORMAT JSONEachRow"
    )
    proc = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "-q", sql],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "clickhouse-client 执行失败")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _fmt_meta(row: dict[str, Any]) -> dict[str, str]:
    return dict(zip(row.get("metadata_names") or [], row.get("metadata_values") or []))


def main() -> int:
    if not langfuse_enabled():
        print("FAIL: Langfuse 未启用，检查 .env 里的 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_ENABLED")
        return 1

    print(f"base_url   : {settings.LANGFUSE_BASE_URL}")
    print(f"environment: {settings.LANGFUSE_ENVIRONMENT or '-'}")

    thread_id = f"verify-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    trace_id = trace_id_for_run(run_id)
    print(f"thread_id  : {thread_id}")
    print(f"run_id     : {run_id} -> trace_id {trace_id}")

    graph = _build_graph()
    with langfuse_trace(thread_id=thread_id, run_id=run_id, task_kind="verify") as cfg:
        handlers = [type(c).__name__ for c in cfg.get("callbacks", [])]
        print(f"注入 config : keys={sorted(cfg)} callbacks={handlers}")
        result = graph.invoke(
            {"messages": [{"role": "user", "content": "ping"}]},
            config={"configurable": {"thread_id": thread_id}, **cfg},
        )
    print(f"图执行结果 : {result['messages'][-1].content}")

    flush()

    try:
        # 摄取链路是异步的（SDK 批量导出 → worker 入队 → ClickHouse 批量写入），
        # 所以这里轮询等一会儿，不要一次查不到就判失败。
        rows: list[dict[str, Any]] = []
        for _ in range(15):
            rows = _query_events(trace_id)
            if rows:
                break
            time.sleep(1)
    except Exception as exc:
        print(f"WARN: 查 ClickHouse 失败（{exc}）")
        print(f"请手动打开 {settings.LANGFUSE_BASE_URL} 搜索 trace_id={trace_id}")
        return 2

    if not rows:
        print("FAIL: 图上跑完了，但 ClickHouse 里查不到这条 trace")
        return 1

    root = next((r for r in rows if r.get("is_app_root")), rows[0])
    print("OK: trace 已落地")
    print(f"  trace_name  = {root.get('trace_name')}")
    print(f"  session_id  = {root.get('session_id')}")
    print(f"  tags        = {root.get('tags')}")
    print(f"  environment = {root.get('environment')}")
    print(f"  spans       = {[(r.get('type'), r.get('name')) for r in rows]}")
    meta = _fmt_meta(root)
    print(f"  自定义 metadata = { {k: meta.get(k) for k in ('thread_id', 'run_id', 'task_kind')} }")

    ok = (
        root.get("trace_name", "").startswith("verify ·")
        and root.get("session_id") == thread_id
        and meta.get("run_id") == run_id
    )
    if not ok:
        print("FAIL: trace 落地了，但 trace_name / session_id / run_id 属性不完整")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
