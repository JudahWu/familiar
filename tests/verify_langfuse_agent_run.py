"""真实链路校验：跑一次真正的 DeepAgent，确认 Langfuse 拿到完整 trace。

和 verify_langfuse_trace.py 的区别：
- 那个用 FakeListChatModel 只验「接线对不对」，不花钱、秒回；
- 这个走 `app.server.get_agent` 真实装配 + DeepSeek 真实调用，验的是
  「deepagents 的图里能不能把 callback 一路传下去、模型名和 token 用量有没有上去」。
  会消耗一次很小的 DeepSeek 额度，并且往 checkpoints.sqlite 里写一个临时 thread。

跑法：
    /Users/zhua/.workbuddy/binaries/python/envs/default/bin/python scripts/verify_langfuse_agent_run.py
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

from familiar.core.observability import (
    flush,
    langfuse_enabled,
    langfuse_trace,
    trace_id_for_run,
)

CH_CONTAINER = os.environ.get("LANGFUSE_CLICKHOUSE_CONTAINER", "familiar-lf-clickhouse-1")
CH_DATABASE = os.environ.get("LANGFUSE_CLICKHOUSE_DATABASE", "default")

# 只问一句不需要工具、不需要读文件的话，把额度和时间压到最低。
PROMPT = "用一句话回答：1+1 等于几？不要调用任何工具。"


def _query_events(trace_id: str) -> list[dict[str, Any]]:
    sql = (
        "SELECT name, type, trace_name, session_id, environment, is_app_root, "
        "provided_model_name, usage_details, metadata_names, metadata_values "
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


def main() -> int:
    if not langfuse_enabled():
        print("FAIL: Langfuse 未启用，检查 .env 里的 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
        return 1

    from familiar.server import get_agent, release_backend_for_thread

    thread_id = f"verify-agent-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    trace_id = trace_id_for_run(run_id)
    print(f"thread_id = {thread_id}")
    print(f"run_id    = {run_id} -> trace_id {trace_id}")

    agent = get_agent(
        {"configurable": {"thread_id": thread_id, "task_kind": "chat", "__is_for_execution__": True}}
    )

    try:
        with langfuse_trace(thread_id=thread_id, run_id=run_id, task_kind="chat") as cfg:
            print(f"注入 config: keys={sorted(cfg)} callbacks={[type(c).__name__ for c in cfg.get('callbacks', [])]}")
            stream = agent.stream_events(
                {"messages": [{"role": "user", "content": PROMPT}]},
                version="v3",
                config={"configurable": {"thread_id": thread_id}, **cfg},
            )
            for _event in stream:
                pass
            output = stream.output
        messages = output.get("messages", []) if isinstance(output, dict) else []
        if messages:
            print(f"回答: {messages[-1].content}")
    finally:
        release_backend_for_thread(thread_id)

    flush()

    rows: list[dict[str, Any]] = []
    for _ in range(20):
        rows = _query_events(trace_id)
        if rows:
            break
        time.sleep(1)

    if not rows:
        print("FAIL: 真实运行结束，但 ClickHouse 里查不到这条 trace")
        return 1

    gens = [r for r in rows if r.get("type") == "GENERATION"]
    root = next((r for r in rows if r.get("is_app_root")), rows[0])
    print("OK: trace 已落地")
    print(f"  trace_name = {root.get('trace_name')}")
    print(f"  session_id = {root.get('session_id')}")
    print(f"  span 数    = {len(rows)}，其中 GENERATION {len(gens)} 条")
    for g in gens:
        print(f"    - {g.get('name')} model={g.get('provided_model_name')} usage={g.get('usage_details')}")

    if not gens:
        print("FAIL: trace 里有结构但没有 GENERATION，说明模型调用没被记录")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
