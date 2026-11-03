from __future__ import annotations

"""Langfuse 观测接入层（应用侧）。

只做一件事：让「一次 agent 运行 = 一条 Langfuse trace」。
业务逻辑一行不改，key 没配就整体退化成空操作。

接入点选在 `streaming_runtime.run_agent_with_event_stream`，因为它是所有
agent 运行（chat / planning / coding / review）唯一的出口。把 LangChain 的
CallbackHandler 塞进 `stream_events(config=...)`，LangGraph 内部每个节点、
每次 LLM 调用、每个工具调用都会自动挂成这棵 trace 上的子 observation——
token 用量、耗时、工具入参都由官方回调带出来，不用手写 span。

为什么不用 `@observe` 装饰器手动开 span：DeepAgents 的图是多层异步的
（agent → 中间件 → 子 agent → 工具），手写 span 很难对齐父子关系；
CallbackHandler 直接吃 LangChain 的 run tree，父子结构天然是对的。

环境变量（都在 .env 里，改完重启后端即生效）：
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`：缺任一则整体关闭。
- `LANGFUSE_BASE_URL`：自托管实例地址，默认 http://localhost:3001。
- `LANGFUSE_ENABLED`：显式开关，设 false 可临时关掉而不动 key。
- `LANGFUSE_ENVIRONMENT`：Langfuse 里的环境标签，默认 local。
- `LANGFUSE_SAMPLE_RATE`：采样率，默认 1.0（全采）。
- `LANGFUSE_MASK_SECRETS`：导出前把形如 API key / token 的字符串打码，默认开。
  注意 agent 会把仓库代码发进 prompt，trace 里本来就有业务内容；这个开关只挡
  凭证这类"顺手泄漏"的情况，不是通用的敏感信息脱敏。
"""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from familiar.core import settings

logger = logging.getLogger("familiar.core.observability")

# 进程级单例状态。_client_ready 后不再重复初始化；_client_failed 之后
# 不再反复重试以免每次运行都打一遍异常栈（key 配错属于配置问题，重启才是解法）。
_client_lock = threading.Lock()
_client_ready = False
_client_failed = False

# Langfuse / OTEL 的 trace_id 必须是 32 位小写十六进制。
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

# 只匹配"形状明确"的凭证，避免误伤正常代码。注意不匹配 pk-lf-*：那只是项目
# 公钥，本身不是秘密，留着还方便在 trace 里对号。
_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{16,})"
)


def _public_key() -> str:
    return (settings.LANGFUSE_PUBLIC_KEY or "").strip()


def _secret_key() -> str:
    return (settings.LANGFUSE_SECRET_KEY or "").strip()


def langfuse_enabled() -> bool:
    """是否具备把 trace 送出去的全部条件。

    三层判断：显式开关 → key 是否齐全 → SDK 是否真的装上了。
    任何一层不满足就静默关闭，绝不让观测拖垮业务运行。
    """

    if not settings.LANGFUSE_ENABLED:
        return False
    if not _public_key() or not _secret_key():
        return False
    try:
        import langfuse  # noqa: F401
    except Exception:  # pragma: no cover - 依赖缺失时才走到
        return False
    return True


def _mask_otel_spans(*, params: Any) -> Any:
    """导出前把 span 属性里的凭证打码。

    为什么不用 Langfuse 构造参数里的 `mask`：那个只作用于 SDK 自己的
    start_observation / update 接口，对 LangChain CallbackHandler 产生的 span
    完全无效。CallbackHandler 走的是纯 OTEL 通道，必须在导出阶段用
    `mask_otel_spans` 打补丁。

    这里只改字符串类型的属性值，且只在真的匹配到凭证形状时才打补丁，
    避免给每条 span 都产生无意义的 patch。
    """

    try:
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

        patches: dict[str, Any] = {}
        for identifier, span in params.spans.items():
            updates: dict[str, str] = {}
            for key, value in span.attributes.items():
                if isinstance(value, str) and len(value) >= 16 and _SECRET_PATTERN.search(value):
                    updates[key] = _SECRET_PATTERN.sub("***masked***", value)
            if updates:
                patches[identifier] = OtelSpanPatch(set_attributes=updates)
        return MaskOtelSpansResult(span_patches=patches) if patches else None
    except Exception:  # pragma: no cover - 打码失败宁可不打，也不能丢 trace
        logger.debug("Langfuse 凭证打码失败，本次按原样导出", exc_info=True)
        return None


def _ensure_client() -> bool:
    """初始化进程级 Langfuse 客户端（幂等）。

    这里显式传 key 和 base_url，而不是完全依赖环境变量：.env 一旦改名或
    被 shell 里的空值覆盖，SDK 会静默变成「不发数据」，排查成本很高。
    显式传参至少在初始化日志里能看出到底连的是哪个地址。

    注意 langfuse v4 的 CallbackHandler 内部走 `get_client()` 单例，
    所以这里建好的实例会被后续 handler 直接复用。
    """

    global _client_ready, _client_failed

    if _client_ready:
        return True
    if _client_failed:
        return False
    if not langfuse_enabled():
        return False

    with _client_lock:
        if _client_ready:
            return True
        if _client_failed:
            return False
        try:
            from langfuse import Langfuse

            kwargs: dict[str, Any] = {
                "public_key": _public_key(),
                "secret_key": _secret_key(),
                "base_url": settings.LANGFUSE_BASE_URL,
                "sample_rate": settings.LANGFUSE_SAMPLE_RATE,
            }
            if settings.LANGFUSE_ENVIRONMENT:
                kwargs["environment"] = settings.LANGFUSE_ENVIRONMENT
            if settings.LANGFUSE_MASK_SECRETS:
                kwargs["mask_otel_spans"] = _mask_otel_spans
            Langfuse(**kwargs)
            _client_ready = True
            logger.info(
                "Langfuse 观测已接入：base_url=%s environment=%s sample_rate=%s",
                settings.LANGFUSE_BASE_URL,
                settings.LANGFUSE_ENVIRONMENT or "-",
                settings.LANGFUSE_SAMPLE_RATE,
            )
        except Exception:
            _client_failed = True
            logger.warning("Langfuse 客户端初始化失败，本轮及后续运行不做 tracing", exc_info=True)

    return _client_ready


def trace_id_for_run(run_id: str) -> str | None:
    """把业务 run_id 映射成 Langfuse trace_id。

    run_id 是 uuid4（带横线），去掉横线正好是 32 位十六进制，可以直接当
    trace_id 用。好处是日志里的 run_id 和 Langfuse UI 里的 trace 能对上号，
    排查"这一轮到底跑了什么"时不用再靠时间去翻。
    """

    compact = (run_id or "").replace("-", "").lower()
    return compact if _HEX32.match(compact) else None


def record_scores(
    *,
    trace_id: str | None,
    numeric: dict[str, float | None] | None = None,
    categorical: dict[str, str | None] | None = None,
    comment: str = "",
) -> None:
    """把一次运行的评测结论挂到 trace 上，走 Langfuse Score 而不是 span。

    为什么不用 span：评分是"这一轮的结论"，不是"一次调用"。做成 span 只能在
    单条 trace 里看，做过 score 之后可以在 Langfuse 里按 name 聚合，直接看
    faithfulness 的趋势和分布——这才是评测数据该有的用法。

    这个接口也是被逼出来的：知识库问答的评分阶段跑在线程池里，span 导出不稳定
    （实测 evaluate_node 及其子调用经常丢），而 score 是独立写入的，不受影响。

    未启用 Langfuse 时什么都不做，调用方不用分叉。
    """

    client = _ensure_client()
    if not client or not trace_id:
        return
    # `_ensure_client()` 只回 bool，真正的实例在 SDK 的单例里（回调处理器也复用它）
    try:
        from langfuse import get_client

        sdk = get_client()
    except Exception:  # noqa: BLE001
        logger.debug("拿不到 Langfuse 客户端，跳过 score 写入", exc_info=True)
        return

    for name, value in (numeric or {}).items():
        if value is None:
            continue
        try:
            sdk.create_score(trace_id=trace_id, name=name, value=float(value), comment=comment or None)
        except Exception:  # noqa: BLE001 - 打点失败不影响答案
            logger.debug("写入 score 失败：%s", name, exc_info=True)

    for name, value in (categorical or {}).items():
        if not value:
            continue
        try:
            sdk.create_score(trace_id=trace_id, name=name, value=str(value), comment=comment or None)
        except Exception:  # noqa: BLE001
            logger.debug("写入 score 失败：%s", name, exc_info=True)


def _trace_name(task_kind: str | None, thread_id: str) -> str:
    kind = (task_kind or "agent").strip() or "agent"
    return f"{kind} · {thread_id[:8]}"


def warm_up() -> bool:
    """启动时预建客户端。

    好处是配置问题（地址写错、key 不对）在启动日志里就能看出来，
    而不是等到用户发第一条消息时才在业务日志里冒一行 warning。
    """

    return _ensure_client()


@contextmanager
def langfuse_trace(
    *,
    thread_id: str,
    run_id: str,
    task_kind: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """给一次 agent 运行套一层 Langfuse trace。

    用法是把 yield 出来的片段直接并进 LangGraph 的 config：

        with langfuse_trace(thread_id=..., run_id=..., task_kind=...) as cfg:
            stream = agent.stream_events(..., config={"configurable": {...}, **cfg})

    未启用 / 初始化失败时 yield 空 dict，调用方代码完全不用分叉。
    """

    if not _ensure_client():
        yield {}
        return

    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        trace_id = trace_id_for_run(run_id)
        handler = CallbackHandler(trace_context={"trace_id": trace_id} if trace_id else None)
    except Exception:
        logger.warning("Langfuse CallbackHandler 创建失败，本轮不做 tracing", exc_info=True)
        yield {}
        return

    trace_metadata: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if task_kind:
        trace_metadata["task_kind"] = task_kind
    if metadata:
        trace_metadata.update({k: v for k, v in metadata.items() if v is not None})

    tags = [t for t in (task_kind, settings.LANGFUSE_ENVIRONMENT) if t]

    # propagate_attributes 负责 session_id / trace 名称 / tags / metadata 这些
    # trace 级别的维度，让 Langfuse 里能按会话、按任务类型筛选和聚合。
    with propagate_attributes(
        session_id=thread_id,
        trace_name=_trace_name(task_kind, thread_id),
        tags=tags or None,
        metadata=trace_metadata,
    ):
        yield {"callbacks": [handler]}


def flush() -> None:
    """把缓冲中的 span 立刻推给 Langfuse。

    后端是长驻进程，正常靠 SDK 的后台批量导出即可；这个钩子主要给
    校验脚本和进程退出路径用，避免最后几条 trace 停在缓冲区里。
    """

    if not _client_ready:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:  # pragma: no cover - 退出路径上不再抛错
        logger.debug("Langfuse flush 失败", exc_info=True)
