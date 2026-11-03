from __future__ import annotations

"""OpenTelemetry 接入层：结构化日志 + 运行指标。

只负责“给进程一个到 OTel 的出口”，日志怎么产生还是走标准库 logging。
应用只认 OTLP 一种协议，发到本地 otel-collector，再由它分流：

    logs    → ClickHouse 的 logs 库  → Grafana 用 SQL 查
    metrics → Prometheus 暴露格式     → Prometheus 抓、Grafana 出面板

为什么不直接写 ClickHouse / 暴露 /metrics：
- 直连存储会把“日志后端换掉”变成一次改代码的活儿；
- collector 那一层能统一做批量、重试、队列持久化，应用侧不用重复实现。

上下文怎么串：
`log_context(...)` 往 contextvars 里塞 request_id / thread_id / run_id / task_kind，
之后同一调用链上每条日志都会自动带这些字段，不用每个调用点手拼。
指标也读同一份上下文，方便在 Prometheus 里按 task_kind 切分。

设计约束：
- `OTEL_ENABLED=false` 或 collector 没起来时，所有函数退化成空操作，不影响业务。
- 导出失败只在后台线程丢批次，绝不阻塞请求。
"""

import contextvars
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from familiar.core import settings

logger = logging.getLogger("familiar.telemetry")

# 当前调用链的日志/指标上下文。可嵌套（上下文管理器会合并而不是覆盖）。
_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "familiar_log_context", default={}
)

# 初始化状态
_configured = False
_logger_provider: Any = None
_meter_provider: Any = None
_instruments: dict[str, Any] = {}


# ── 上下文 ────────────────────────────────────────────────────────


def current_context() -> dict[str, Any]:
    """返回当前上下文快照（只读用途）。"""

    return dict(_LOG_CONTEXT.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """在当前调用链上绑定日志字段。

    支持嵌套：内层只覆盖同名字段，外层的其它字段继续有效。

        with log_context(thread_id=tid):
            with log_context(run_id=rid):
                logger.info("开始")   # 带 thread_id + run_id
    """

    current = _LOG_CONTEXT.get()
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    token = _LOG_CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _LOG_CONTEXT.reset(token)


class ContextFieldsFilter(logging.Filter):
    """把上下文里的字段附加到每条 LogRecord 上。

    **必须挂在 handler 上，不能挂在 logger 上**：logger 级别的 filter 只在
    “这条记录由该 logger 自己产生”时才会执行，子 logger（agent.runtime 之类）
    冒泡到 root 的 handler 时不经过 root 的 filter。挂在 handler 上才对所有
    经过它的记录生效。

    OTel 的 LoggingHandler 会把 record 上的额外属性原样带成日志属性，
    所以这里只做“赋值”就够，不用碰 handler 内部实现。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _LOG_CONTEXT.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def context_suffix() -> str:
    """控制台用的紧凑上下文后缀，例如 [request_id=ab12cd34 run_id=ef56]。"""

    ctx = _LOG_CONTEXT.get()
    if not ctx:
        return ""
    parts = []
    for key in ("request_id", "thread_id", "run_id", "task_kind"):
        if key in ctx and ctx[key]:
            parts.append(f"{key}={str(ctx[key])[:8]}")
    return f" [{ ' '.join(parts) }]" if parts else ""


# ── 初始化 ────────────────────────────────────────────────────────


def is_enabled() -> bool:
    return bool(settings.OTEL_ENABLED)


def configure_telemetry() -> bool:
    """初始化 OTel 的 logs / metrics 管道（幂等）。

    返回是否成功启用。任何异常都吞掉并记一条本地日志——观测不能拦住服务启动。
    """

    global _configured, _logger_provider, _meter_provider

    if _configured or not is_enabled():
        return _configured

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": settings.OTEL_SERVICE_NAME,
                "deployment.environment": settings.OTEL_ENVIRONMENT,
            }
        )

        # ── logs ──
        _logger_provider = LoggerProvider(resource=resource)
        _logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=f"{settings.OTEL_ENDPOINT}/v1/logs"),
                max_queue_size=4096,
                max_export_batch_size=512,
                schedule_delay_millis=2000,
            )
        )

        handler = LoggingHandler(
            level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
            logger_provider=_logger_provider,
        )
        # 先把上下文注进 record，再挡掉导出器自身的噪音日志
        handler.addFilter(ContextFieldsFilter())
        handler.addFilter(_ExportNoiseFilter())
        logging.getLogger().addHandler(handler)

        # ── metrics ──
        _meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{settings.OTEL_ENDPOINT}/v1/metrics"),
                    export_interval_millis=15000,
                )
            ],
        )
        meter = _meter_provider.get_meter("familiar")
        # 默认的直方图分桶是按“通用数值”给的（0/5/10/…/10000），对秒级的
        # agent 运行完全没有区分度。这里按实际量级显式给分桶：0.5s ~ 30min。
        run_buckets = [0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800]
        http_buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
        _instruments.update(
            {
                "agent_runs": meter.create_counter(
                    "familiar.agent.runs",
                    unit="1",
                    description="Agent 运行次数",
                ),
                "agent_run_seconds": meter.create_histogram(
                    "familiar.agent.run.duration",
                    unit="s",
                    description="单次 agent 运行耗时",
                    explicit_bucket_boundaries_advisory=run_buckets,
                ),
                "http_requests": meter.create_counter(
                    "familiar.http.requests",
                    unit="1",
                    description="HTTP 请求数",
                ),
                "http_request_seconds": meter.create_histogram(
                    "familiar.http.request.duration",
                    unit="s",
                    description="HTTP 请求耗时",
                    explicit_bucket_boundaries_advisory=http_buckets,
                ),
                # ── 知识库 ──
                # 一次问答要打 10+ 次模型调用，是所有路径里最贵的，之前完全没指标。
                "kb_answers": meter.create_counter(
                    "familiar.kb.answers",
                    unit="1",
                    description="知识库问答次数",
                ),
                "kb_answer_seconds": meter.create_histogram(
                    "familiar.kb.answer.duration",
                    unit="s",
                    description="单次知识库问答耗时（改写+检索+生成+评分）",
                    explicit_bucket_boundaries_advisory=[1, 2, 5, 10, 20, 30, 45, 60, 120],
                ),
                "kb_imports": meter.create_counter(
                    "familiar.kb.imports",
                    unit="1",
                    description="知识库文档导入次数",
                ),
                "kb_import_seconds": meter.create_histogram(
                    "familiar.kb.import.duration",
                    unit="s",
                    description="单篇文档导入耗时（解析+切分+向量化）",
                    explicit_bucket_boundaries_advisory=[5, 15, 30, 60, 120, 300, 600, 1800, 3600],
                ),
                "kb_import_failed_pages": meter.create_histogram(
                    "familiar.kb.import.failed_pages",
                    unit="1",
                    description="单篇文档里没取到正文的页数",
                    explicit_bucket_boundaries_advisory=[0, 1, 2, 5, 10, 30],
                ),
            }
        )

        _configured = True
        logger.info(
            "OpenTelemetry 已接入：endpoint=%s service=%s env=%s",
            settings.OTEL_ENDPOINT,
            settings.OTEL_SERVICE_NAME,
            settings.OTEL_ENVIRONMENT,
        )
        return True
    except Exception:
        # 依赖缺失 / collector 配置错，都不该影响业务
        logger.warning("OpenTelemetry 初始化失败，本次运行只留 stdout 日志", exc_info=True)
        return False


class _ExportNoiseFilter(logging.Filter):
    """挡掉导出器自身的日志，避免“导出日志产生的日志再被导出”的递归。"""

    _BLOCKED = ("opentelemetry", "httpx", "httpcore", "urllib3", "google.auth")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self._BLOCKED)


# ── 指标写入 ──────────────────────────────────────────────────────


def record_agent_run(*, task_kind: str, status: str, duration_s: float) -> None:
    """记录一次 agent 运行：次数 + 耗时。"""

    if not _configured:
        return
    attrs = {"task_kind": task_kind or "unknown", "status": status}
    try:
        _instruments["agent_runs"].add(1, attrs)
        _instruments["agent_run_seconds"].record(duration_s, {"task_kind": attrs["task_kind"]})
    except Exception:  # pragma: no cover - 指标失败不影响业务
        pass


def record_http_request(*, method: str, route: str, status_code: int, duration_s: float) -> None:
    """记录一次 HTTP 请求。route 用模板路径，避免把 id 带进 label 造成基数爆炸。"""

    if not _configured:
        return
    status_class = f"{status_code // 100}xx"
    try:
        _instruments["http_requests"].add(
            1, {"method": method, "status_class": status_class}
        )
        _instruments["http_request_seconds"].record(
            duration_s, {"method": method, "route": route}
        )
    except Exception:  # pragma: no cover
        pass


def record_kb_answer(
    *, status: str, interrupted: bool, coverage: str, duration_s: float
) -> None:
    """记录一次知识库问答。

    `interrupted` 单独成 label，因为"拦截率"是这条链路最需要盯的数：它一高，
    说明检索或解析有问题，用户看到的是"资料不足"而不是答案。
    `coverage` 只有四个取值（full/partial/none/unknown），基数可控。
    """

    if not _configured:
        return
    attrs = {
        "status": status or "unknown",
        "interrupted": "true" if interrupted else "false",
        "coverage": coverage or "unknown",
    }
    try:
        _instruments["kb_answers"].add(1, attrs)
        _instruments["kb_answer_seconds"].record(duration_s, {"interrupted": attrs["interrupted"]})
    except Exception:  # pragma: no cover
        pass


def record_kb_import(*, status: str, pages_failed: int, duration_s: float) -> None:
    """记录一次文档导入。

    `pages_failed` 是"没取到正文的页数"。解析失败目前只记在文档行上，用户不点进
    导入结果就看不到——打成指标才能在图上看出"这批书识别质量在下降"。
    """

    if not _configured:
        return
    try:
        _instruments["kb_imports"].add(1, {"status": status or "unknown"})
        _instruments["kb_import_seconds"].record(duration_s, {"status": status or "unknown"})
        _instruments["kb_import_failed_pages"].record(max(0, int(pages_failed)))
    except Exception:  # pragma: no cover
        pass


# ── 收尾 ──────────────────────────────────────────────────────────


def shutdown_telemetry() -> None:
    """把缓冲区里的日志/指标推干净再退出。"""

    global _configured
    if not _configured:
        return
    for provider in (_meter_provider, _logger_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover
            logger.debug("OTel provider shutdown 失败", exc_info=True)
    _configured = False


def timeit_ms(start: float) -> int:
    """小工具：把 time.perf_counter() 的起点换算成毫秒耗时。"""

    return int((time.perf_counter() - start) * 1000)
