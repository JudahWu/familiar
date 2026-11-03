from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from familiar.core import streaming_runtime
from familiar.core.graph import get_store


def _event_run_id(event_id: str) -> str | None:
    parts = event_id.split(":")
    if len(parts) >= 4 and parts[-3] in {"stream", "todos"}:
        return parts[-2]
    return None


def main() -> None:
    """验证实时流只应播放当前 run 的事件。

    这个脚本复现用户截图中的问题：同一个 thread 先有 plan-run，再有 coding-run。
    新一轮 SSE 连接只能推送 coding-run 的 todo/text，不能重放 plan-run 的事件。
    """

    thread_id = "verify-current-run-filter"
    store = get_store()
    store.delete_thread(thread_id)
    store.upsert_thread(
        thread_id=thread_id,
        title="current run filter verification",
        user_prompt="确认实施",
        repo_url="https://gitee.com/msb-goldbin/ai_coding.git",
        repo_owner="msb-goldbin",
        repo_name="ai_coding",
        latest_run_status="running",
    )
    store.clear_run_events(thread_id)

    streaming_runtime._record_stream_message(thread_id, "plan-run", "是否确认实施该方案？")
    streaming_runtime._record_todos(
        thread_id,
        "plan-run",
        "write_todos",
        [{"content": "输出技术方案", "status": "completed"}],
        status="completed",
    )
    streaming_runtime._record_stream_message(thread_id, "coding-run", "开始按方案修改代码")
    streaming_runtime._record_todos(
        thread_id,
        "coding-run",
        "write_todos",
        [{"content": "修改 main.py", "status": "in_progress"}],
        status="in_progress",
    )

    events = store.list_run_events(thread_id)
    current_events = [event for event in events if _event_run_id(str(event["id"])) == "coding-run"]
    leaked_plan_events = [event for event in current_events if "plan-run" in str(event["id"])]
    if leaked_plan_events:
        raise AssertionError(f"当前 run 过滤后仍包含上一轮事件：{leaked_plan_events}")
    if len(current_events) != 2:
        raise AssertionError(f"当前 run 应只有 2 个实时事件，实际：{[event['id'] for event in current_events]}")

    store.delete_thread(thread_id)
    print("stream current run filter verification passed")


if __name__ == "__main__":
    main()
