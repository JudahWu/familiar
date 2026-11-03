from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from familiar.core import settings
from familiar.core.telemetry import context_suffix


class ContextFormatter(logging.Formatter):
    """控制台/文件用的文本格式，行尾自动附加当前日志上下文。

    结构化字段本身进了 OTel（ClickHouse 里是列），这里只是让人肉看控制台时
    能一眼知道这条属于哪次请求、哪轮运行。
    """

    def format(self, record: logging.LogRecord) -> str:
        return f"{super().format(record)}{context_suffix()}"


def _configure_console_encoding() -> None:
    """尽量把控制台输出切换为 UTF-8。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 控制台编码修正只是展示优化，不能因为它失败而影响服务启动。
            continue


def _log_level() -> int:
    """解析日志级别，非法配置回退到 INFO。"""

    return getattr(logging, settings.LOG_LEVEL, logging.INFO)


def _close_and_remove_handlers(logger: logging.Logger) -> None:
    """移除并关闭旧 handler，避免 reload/test 重复写日志。

    Windows 下文件句柄被占用会影响日志轮转和测试清理，因此不能只 remove，
    还要显式 close。
    """

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            continue


def _make_rotating_file_handler(path: Path) -> RotatingFileHandler:
    """兜底文件 handler：按大小轮转。

    以前用的是 TimedRotatingFileHandler（按天切），问题是一旦某个循环疯狂打日志，
    当天的文件没有上限，能写到几个 G 才等到第二天。这里改成按大小切：
    单文件到 LOG_FILE_MAX_MB 就换下一个，最多留 LOG_FILE_BACKUP_COUNT 个。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        filename=str(path),
        maxBytes=settings.LOG_FILE_MAX_BYTES,
        backupCount=settings.LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    return handler


def configure_logging() -> None:
    """配置后端日志系统。

    现在的分工：
    1. **控制台**：人看的文本格式，开发时直接读。
    2. **文件**：默认关闭（LOG_FILE_ENABLED=false）。开着也只做兜底，
       按大小轮转，不会无限长。
    3. **OTel**：出口在 app/core/telemetry.py，结构化日志进 ClickHouse，
       由 Grafana 查；指标进 Prometheus。

    以前那种“全量文件 + 按 logger 前缀复制一份 agent-runs.log”的做法去掉了：
    同一份内容写两遍，真实收益只有 `grep` 少打几个条件，而代价是磁盘翻倍。
    按字段筛由 OTel/ClickHouse 那边做。
    """

    _configure_console_encoding()
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(_log_level())
    _close_and_remove_handlers(root_logger)

    formatter = ContextFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] [pid=%(process)d thread=%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(_log_level())
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if settings.LOG_FILE_ENABLED:
        file_handler = _make_rotating_file_handler(settings.backend_log_path())
        file_handler.setLevel(_log_level())
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # 噪音 logger 压到 WARNING：uvicorn 的 access log 由我们自己的
    # HTTP 中间件替代（带 request_id 和耗时），httpx 这类第三方 INFO 没有分析价值。
    for name in settings.LOG_NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # 让每条日志自动带上 request_id / thread_id / run_id
    # （字段注入发生在 OTel handler 上，见 telemetry.ContextFieldsFilter）
    logging.getLogger("familiar").info(
        "日志系统已启动：level=%s file_enabled=%s otel=%s",
        settings.LOG_LEVEL,
        settings.LOG_FILE_ENABLED,
        settings.OTEL_ENDPOINT if settings.OTEL_ENABLED else "off",
    )


def read_recent_log(path: Path, max_lines: int = 200) -> list[str]:
    """读取日志文件末尾若干行，供 API 页面或前端展示。"""

    if max_lines < 1:
        max_lines = 1
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]
