from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from familiar.core import streaming_runtime
from familiar.core.graph import get_store


def _event_detail_text(event: dict) -> str:
    detail = event.get("detail")
    if not detail:
        return ""
    parsed = json.loads(detail)
    return str(parsed.get("text") or "")


def main() -> None:
    """验证同一 thread 的不同 run 会写入不同事件 id。

    前端的消息 id 会从事件 id 中解析 run_id。如果事件 id 不带 run_id，
    上一轮 plan 的正文和本轮 coding 的正文就会被拼成同一条 AIMessage。
    """

    thread_id = "verify-stream-run-grouping"
    store = get_store()
    store.delete_thread(thread_id)
    store.upsert_thread(
        thread_id=thread_id,
        title="stream grouping verification",
        user_prompt="验证流式消息分组",
        repo_url="https://gitee.com/msb-goldbin/ai_coding.git",
        repo_owner="msb-goldbin",
        repo_name="ai_coding",
        latest_run_status="running",
    )
    store.clear_run_events(thread_id)

    streaming_runtime._record_stream_message(thread_id, "plan-run", "是否确认实施该方案？")
    streaming_runtime._record_stream_message(thread_id, "coding-run", "开始修改代码")
    streaming_runtime._record_todos(
        thread_id,
        "plan-run",
        "write_todos",
        [{"content": "输出技术方案", "status": "completed"}],
        status="completed",
    )
    streaming_runtime._record_todos(
        thread_id,
        "coding-run",
        "write_todos",
        [{"content": "修改代码", "status": "in_progress"}],
        status="in_progress",
    )

    events = store.list_run_events(thread_id)
    ids = {event["id"] for event in events}
    expected = {
        f"{thread_id}:stream:plan-run:message",
        f"{thread_id}:stream:coding-run:message",
        f"{thread_id}:todos:plan-run:write_todos",
        f"{thread_id}:todos:coding-run:write_todos",
    }
    missing = expected - ids
    if missing:
        raise AssertionError(f"缺少按 run_id 分组的事件：{sorted(missing)}")

    texts_by_id = {event["id"]: _event_detail_text(event) for event in events}
    if texts_by_id[f"{thread_id}:stream:plan-run:message"] != "是否确认实施该方案？":
        raise AssertionError("plan run 正文事件内容不正确")
    if texts_by_id[f"{thread_id}:stream:coding-run:message"] != "开始修改代码":
        raise AssertionError("coding run 正文事件内容不正确")

    store.delete_thread(thread_id)
    print("stream run grouping verification passed")


if __name__ == "__main__":
    main()
