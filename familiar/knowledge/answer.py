"""知识库问答的对外门面：跑图、恢复中断、整理返回结构。

真正的流程在 `graph.py` 里，这里只做三件事：

1. **起一次图**：`start()` 把问题塞进状态，跑到结束或中断为止。
2. **恢复中断**：`resume()` 接用户对低分答案的裁决（approve / rejected），
   从 `human_approval` 之前继续往下跑。
3. **落记忆**：图正常结束（没有挂起的中断）之后，把这一问一答写进 PG，
   再把助手轮索引进 `memory.py`。中断期间不写——那一版答案还没定稿，
   用户可能拒绝并换联网重答，先记账会把两版答案都写进历史。

状态归 LangGraph 的 checkpointer 管，`thread_id` 是 `kb-<session_id>`。
所以刷新页面不丢中断，但也意味着**同一个 session 同时只能有一条在途问答**：
两个标签页同时提问会互相推进流程，前端应当按会话串行提交。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from familiar.core.events import record_event
from familiar.core.model import knowledge_model_name
from familiar.core.observability import langfuse_trace, record_scores, trace_id_for_run
from familiar.core.telemetry import record_kb_answer
from familiar.knowledge import chat, graph as graph_mod, memory

logger = logging.getLogger("familiar.knowledge.answer")

# 中断时前端要显示的两种理由，文案不同：
# - insufficient：库里没料，引导换库或换联网
# - low_score：有料但答得可疑，引导重答
_REASON_INSUFFICIENT = "insufficient"
_REASON_LOW_SCORE = "low_score"


def threshold() -> float:
    return graph_mod.threshold()


def _thread(session_id: str) -> str:
    """图和 PG 会话共用 session_id，但线程名加前缀，避免和 Agent 主线程撞车。"""

    return f"kb-{session_id}"


def _config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": _thread(session_id)}}


def start(
    kb_id: str,
    question: str,
    *,
    session_id: str | None = None,
    owner: str = "",
    image_b64: str = "",
    top_k: int | None = None,
) -> dict[str, Any]:
    """问一个问题。跑到图结束或停在人工审批为止。"""

    text = (question or "").strip()
    image = (image_b64 or "").strip()
    if not text and not image:
        raise ValueError("问题和图片至少要有一个")

    session_id = _ensure_session(session_id, kb_id=kb_id, owner=owner, question=text or "[图片]")

    initial: dict[str, Any] = {
        "kb_id": kb_id,
        "owner": owner,
        "session_id": session_id,
        "question": text,
        "image_b64": _strip_data_url(image) if image else "",
        "human_answer": "",
        "persisted": False,
    }
    if top_k:
        initial["top_k"] = top_k

    graph = graph_mod.get_graph()
    run = uuid.uuid4().hex
    started = time.perf_counter()
    thread = _thread(session_id)

    # 一层 trace 包住整次问答：改写、历史回忆、生成、评分、联网全在里面。
    # 这是这条链路唯一的观测缺口——它不经过 `run_agent_with_event_stream`，
    # 不套这一层的话 Langfuse 里一条都看不到，而它一次要打 10+ 次模型调用。
    with langfuse_trace(
        thread_id=thread,
        run_id=run,
        task_kind="knowledge",
        metadata={"kb_id": kb_id, "entry": "start", "session_id": session_id},
    ) as cfg:
        config = {**_config(session_id), **cfg}
        record_event(thread, f"kb-{run}-answer", "知识库问答", kind="think", status="in_progress")

        # 这个会话还挂着一条没裁决的答案。RAG 项目在这时的做法是把用户的新输入
        # 直接当成裁决（workflow.py:253-257）：输一句话就被判成 rejected 去联网，
        # 很容易误伤。这里改成先按"通过"把上一轮收尾并落库，再回答新问题——
        # 用户既然接着问了，就是不想再纠结上一版。
        pending = graph.get_state(config)
        if pending.next:
            logger.info("会话有未裁决的答案，先按通过收尾：session=%s", session_id)
            graph.update_state(config, {"human_answer": "approve"})
            for _ in graph.stream(None, config, stream_mode="values"):
                pass
            _payload(session_id, graph, config)

        try:
            # 同步驱动：问答不是流式接口，整段拿到才谈得上引用编号和评分
            for _ in graph.stream(initial, config, stream_mode="values"):
                pass
        except Exception as exc:  # noqa: BLE001 - 图里任何节点炸了都要变成可读的失败
            logger.exception("问答图执行失败：kb=%s", kb_id)
            _record_failure(session_id, run, started, exc)
            raise RuntimeError(f"问答执行失败：{exc}") from exc

        return _record_result(session_id, graph, config, run, started)


def resume(session_id: str, decision: str) -> dict[str, Any]:
    """接人工裁决继续跑。

    `decision` 只接受 approve / rejected。**没有默认值**：原项目里
    `human_answer` 默认是 `rejected`（my_state.py:21），所以恢复时不写就等于
    一定去联网。这里要求显式传，避免"点了个继续就自动联网"。
    """

    choice = (decision or "").strip().lower()
    if choice not in {"approve", "rejected"}:
        raise ValueError("decision 只能是 approve 或 rejected")

    graph = graph_mod.get_graph()
    run = uuid.uuid4().hex
    started = time.perf_counter()
    thread = _thread(session_id)

    with langfuse_trace(
        thread_id=thread,
        run_id=run,
        task_kind="knowledge",
        metadata={"entry": "resume", "decision": choice, "session_id": session_id},
    ) as cfg:
        config = {**_config(session_id), **cfg}
        state = graph.get_state(config)
        if not state.next:
            raise ValueError("这个会话当前没有待确认的答案")

        record_event(
            thread,
            f"kb-{run}-resume",
            "确认后继续",
            kind="think",
            status="in_progress",
            detail="approve" if choice == "approve" else "rejected → 联网重答",
        )
        graph.update_state(config, {"human_answer": choice})
        try:
            for _ in graph.stream(None, config, stream_mode="values"):
                pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复问答失败：session=%s", session_id)
            _record_failure(session_id, run, started, exc)
            raise

        return _record_result(session_id, graph, config, run, started)


def _record_result(
    session_id: str, graph: Any, config: dict[str, Any], run: str, started: float
) -> dict[str, Any]:
    """出结果时统一打点：业务事件（给运行时间线）+ 指标（给图）。"""

    payload = _payload(session_id, graph, config)
    interrupted = bool(payload.get("interrupted"))
    record_kb_answer(
        status="ok",
        interrupted=interrupted,
        coverage=str(payload.get("coverage") or "unknown"),
        duration_s=time.perf_counter() - started,
    )
    record_event(
        _thread(session_id),
        f"kb-{run}-answer",
        "知识库问答",
        kind="think",
        status="in_progress" if interrupted else "completed",
        detail=(
            f"等待确认（{payload.get('review_reason') or '低分'}）"
            if interrupted
            else f"命中 {len(payload.get('hits') or [])} 段，覆盖 {payload.get('coverage')}"
        ),
    )

    # 评分结论挂到 trace 上。走 score 而不是 span：span 在线程池里经常丢，
    # score 是独立写入的，而且在 Langfuse 里能按 name 聚合看趋势。
    record_scores(
        trace_id=trace_id_for_run(run),
        numeric={
            "faithfulness": payload.get("score"),
            "relevancy": payload.get("relevancy"),
            "support": payload.get("support"),
        },
        categorical={"coverage": payload.get("coverage")},
        comment=str(payload.get("reason") or ""),
    )
    return payload


def _record_failure(session_id: str, run: str, started: float, exc: Exception) -> None:
    record_kb_answer(
        status="failed",
        interrupted=False,
        coverage="unknown",
        duration_s=time.perf_counter() - started,
    )
    record_event(
        _thread(session_id),
        f"kb-{run}-answer",
        "知识库问答",
        kind="think",
        status="failed",
        detail=str(exc)[:200],
    )


def snapshot(session_id: str) -> dict[str, Any] | None:
    """只读当前状态，不推进图。前端刷新后用它恢复"待确认"的界面。"""

    if not session_id:
        return None
    graph = graph_mod.get_graph()
    config = _config(session_id)
    try:
        state = graph.get_state(config)
    except Exception:  # noqa: BLE001
        logger.warning("读取问答状态失败：session=%s", session_id, exc_info=True)
        return None
    if not state.values:
        return None
    return _payload(session_id, graph, config, state=state)


def _payload(
    session_id: str,
    graph: Any,
    config: dict[str, Any],
    *,
    state: Any = None,
) -> dict[str, Any]:
    """把图状态整理成前端要的结构，顺带在"跑完了"的时候落记忆。"""

    state = state or graph.get_state(config)
    values: dict[str, Any] = dict(state.values or {})
    interrupted = bool(state.next)
    answer_text = str(values.get("answer") or "")

    if not interrupted and values.get("answer") and not values.get("persisted"):
        _persist(session_id, values, graph, config)

    coverage = str(values.get("coverage") or "unknown")
    review_reason = ""
    if interrupted:
        review_reason = _REASON_INSUFFICIENT if coverage == "none" else _REASON_LOW_SCORE

    # 联网重答之后图就结束了，不会重新评分——那一版的分数属于**旧答案**，
    # 原样返回会让"好答案配上 0 分"同时出现在界面上。这里明确置空。
    rescored = bool(values.get("web_used"))
    return {
        "answer": answer_text,
        "hits": values.get("hits") or [],
        "score": None if rescored else values.get("score"),
        "coverage": "unknown" if rescored else coverage,
        "relevancy": None if rescored else values.get("relevancy"),
        "support": None if rescored else values.get("support"),
        "reason": (
            "联网重答未做质检（原流程在联网之后不回到评估节点）"
            if rescored
            else (values.get("reason") or "")
        ),
        "needs_review": interrupted,
        "review_reason": review_reason,
        "interrupted": interrupted,
        "human_answer": values.get("human_answer") or "",
        "model": knowledge_model_name(),
        "web_used": rescored,
        "web_error": values.get("web_error") or "",
        "threshold": graph_mod.threshold(),
        "search_query": values.get("search_query") or "",
        "history_hits": len(values.get("history_hits") or []),
        "session_id": session_id,
    }


def _persist(
    session_id: str,
    values: dict[str, Any],
    graph: Any,
    config: dict[str, Any],
) -> None:
    """落记忆：先 PG（事实来源），再索引（派生）。

    写失败只记日志。用户已经看到答案了，这里出问题不该变成 500。
    """

    question = str(values.get("question") or "").strip() or "[图片]"
    answer_text = str(values.get("answer") or "")
    try:
        chat.append_turn(
            session_id,
            role="user",
            content=question,
            search_query=str(values.get("search_query") or ""),
        )
        turn_id = chat.append_turn(
            session_id,
            role="assistant",
            content=answer_text,
            citations=values.get("hits") or [],
            score=values.get("score"),
            coverage=str(values.get("coverage") or ""),
            needs_review=bool(values.get("human_answer") == "rejected"),
        )
    except Exception:  # noqa: BLE001
        logger.warning("写入会话记忆失败：session=%s", session_id, exc_info=True)
        return

    if turn_id:
        memory.index_turn(
            turn_id=turn_id,
            session_id=session_id,
            kb_id=str(values.get("kb_id") or ""),
            owner=str(values.get("owner") or ""),
            role="assistant",
            text=answer_text,
        )

    # 标记已落库，避免同一 thread 再次读状态时重复写
    try:
        graph.update_state(config, {"persisted": True})
    except Exception:  # noqa: BLE001
        logger.debug("标记 persisted 失败", exc_info=True)


def _ensure_session(session_id: str | None, *, kb_id: str, owner: str, question: str) -> str:
    """拿会话 id。数据库挂了就退化成一次性问答，而不是让用户看不到答案。"""

    try:
        return chat.ensure_session(session_id, kb_id=kb_id, owner=owner, question=question)
    except Exception:  # noqa: BLE001
        logger.warning("会话初始化失败，本次按无记忆模式回答", exc_info=True)
        return session_id or f"adhoc-{kb_id}"


def _strip_data_url(value: str) -> str:
    return value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
