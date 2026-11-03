"""FastAPI 应用入口模块。

本模块是 Familiar 后端的服务入口，负责：
- 加载环境变量和日志配置
- 创建 FastAPI 应用实例
- 配置跨域资源共享（CORS）
- 注册所有 API 路由
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from familiar.core.logging_config import configure_logging
from familiar.core.telemetry import (
    configure_telemetry,
    log_context,
    record_http_request,
    shutdown_telemetry,
    timeit_ms,
)
from familiar.env_utils import load_environment

logger = logging.getLogger("familiar.app")


def _route_template(request: Request) -> str:
    """返回路由模板（例如 /threads/{thread_id}）。

    指标 label 里绝不能放具体 id，否则 Prometheus 的时间序列会按请求数暴涨。
    路由没匹配上（404）时统一记成 unmatched。
    """

    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"

# ---------------------------------------------------------------
# 启动前置初始化
# ---------------------------------------------------------------
# 注意：FastAPI/Uvicorn 是本本项目本中唯一的服务化入口。
# 这里显式调用 load_environment() 加载 .env 文件中的配置，
# 并调用 configure_logging() 初始化日志系统。
# 这样做的好处是不依赖 langgraph dev 等开发服务器自带的环境加载机制。
#
# configure_telemetry() 把日志/指标接到 OTel collector：
# 没配 OTEL_ENABLED=false 或 collector 没起时会安静退化成只打 stdout。
# ---------------------------------------------------------------
load_environment()
configure_logging()
configure_telemetry()

# 延迟导入 API 路由模块
# 使用 # noqa: E402 忽略 flake8 关于导入顺序的警告，
# 因为此处必须等环境变量和日志配置初始化完成后才能导入路由模块。
from familiar.api import dashboard_router, knowledge_router, router  # noqa: E402

# ---------------------------------------------------------------
# 创建 FastAPI 应用实例
# ---------------------------------------------------------------
app = FastAPI(
    title="Familiar Backend",  # 应用名称
    version="0.1.0",                     # 应用版本号
)

# ---------------------------------------------------------------
# 配置 CORS 中间件
# ---------------------------------------------------------------
# 允许来自以下前端的跨域请求：
#   - 127.0.0.1:3000 / localhost:3000  （常见 React/Next.js 开发端口）
#   - 127.0.0.1:5173 / localhost:5173  （常见 Vite 开发端口）
# 生产环境中应替换为实际的前端域名。
# ---------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,   # 允许携带凭证（如 Cookie）
    allow_methods=["*"],       # 允许所有 HTTP 方法
    allow_headers=["*"],       # 允许所有请求头
)


# ---------------------------------------------------------------
# 请求上下文中间件
# ---------------------------------------------------------------
# 每个请求生成一个 request_id 并绑到日志上下文里，之后这条请求链路上
# （包括后台线程里的 runtime 日志，只要是从这里派生的上下文）打的每条
# 日志都会自动带 request_id，排查时能按一次请求把所有行捞出来。
#
# 注意：SSE 是流式响应，这里量到的是“开始返回”的耗时，不是整条流跑完的
# 时长。整条流的耗时靠 agent 运行那条指标（familiar_agent_run_duration）。
# ---------------------------------------------------------------
@app.middleware("http")
async def _observe_request(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    with log_context(request_id=request_id):
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "HTTP 请求异常：%s %s",
                request.method,
                request.url.path,
            )
            record_http_request(
                method=request.method,
                route=_route_template(request),
                status_code=500,
                duration_s=time.perf_counter() - started,
            )
            raise

        duration_s = time.perf_counter() - started
        route = _route_template(request)
        record_http_request(
            method=request.method,
            route=route,
            status_code=response.status_code,
            duration_s=duration_s,
        )
        response.headers["x-request-id"] = request_id

        # /health 每次探活都打一条太吵，跳过
        if request.url.path not in {"/health", "/favicon.ico"}:
            log = logger.warning if response.status_code >= 500 else logger.info
            log(
                "HTTP %s %s -> %s (%dms)",
                request.method,
                route,
                response.status_code,
                timeit_ms(started),
                extra={
                    "http_method": request.method,
                    "http_route": route,
                    "status_code": response.status_code,
                    "duration_ms": timeit_ms(started),
                },
            )
        return response

# ---------------------------------------------------------------
# 注册路由
# ---------------------------------------------------------------
# router          - 主要 API 路由（核心业务逻辑）
# dashboard_router - 仪表盘相关 API 路由（管理/监控用途）
# ---------------------------------------------------------------
app.include_router(router)
app.include_router(dashboard_router)
app.include_router(knowledge_router)


# ---------------------------------------------------------------
# 启动时收敛遗留运行状态
# ---------------------------------------------------------------
# 本地工具是单进程模型。进程退出后，未完成的 run 不可能再有人推进到终态；
# 如果不在这里收敛，前端会一直显示“运行中”，而且没有任何错误可看。
# ---------------------------------------------------------------
@app.on_event("startup")
def _reconcile_orphaned_runs() -> None:
    """把上个进程遗留的 running 状态标记为 failed。"""

    try:
        # 延迟导入：Store 会读取 DATA_DIR 等配置，必须等 load_environment 之后再初始化。
        from familiar.core.graph import get_store

        count = get_store().fail_orphaned_runs()
        if count:
            logger.warning("启动时发现 %s 个遗留的 running 运行，已标记为 failed", count)
    except Exception:
        # 收敛失败不应阻止服务启动，但要留下日志，避免问题被静默吞掉。
        logger.exception("启动时收敛遗留运行状态失败")


@app.on_event("startup")
def _reclaim_orphaned_sandboxes() -> None:
    """回收上个进程遗留的 OpenSandbox 容器。

    沙箱的空闲回收只认本进程内存里的注册表，进程重启后旧容器就成了孤儿
    （实测会一直活到 12 小时硬生命周期结束）。启动时对账一次：新进程
    此刻不可能拥有任何沙箱，server 上还在跑的都是上个进程的遗留。
    """

    from familiar.core.settings import SANDBOX_TYPE

    if SANDBOX_TYPE != "opensandbox":
        return
    try:
        # 延迟导入：opensandbox SDK 较重，且只有 opensandbox 模式才需要。
        from familiar.backends.opensandbox_backend import reclaim_orphan_sandboxes

        killed = reclaim_orphan_sandboxes()
        if killed:
            logger.warning("启动时回收了 %s 个孤儿沙箱：%s", len(killed), ", ".join(killed))
    except Exception:
        # 对账失败（如 OpenSandbox server 还没就绪）不应阻止服务启动。
        logger.exception("启动时回收孤儿沙箱失败")


@app.on_event("startup")
def _warm_up_langfuse() -> None:
    """启动时就把 Langfuse 客户端建好。

    agent 运行本身是懒接入（每次 run 现取 handler），所以这里预建如果失败
    也不该拦住服务：只是把配置问题提前暴露在启动日志里。
    """

    try:
        from familiar.core.observability import langfuse_enabled, warm_up

        if not langfuse_enabled():
            logger.info("Langfuse 观测未启用（未配置 key 或 LANGFUSE_ENABLED=false）")
            return
        warm_up()
    except Exception:
        logger.exception("Langfuse 初始化失败，agent 运行将不带 trace")


@app.on_event("shutdown")
def _flush_langfuse() -> None:
    """退出前把缓冲区里的观测数据推干净。

    两套管道分别 flush：Langfuse（LLM trace）和 OTel（日志/指标）。
    任何一个失败都只记日志，不影响进程退出。
    """

    try:
        from familiar.core.observability import flush

        flush()
    except Exception:
        logger.exception("退出前 flush Langfuse 失败")

    try:
        shutdown_telemetry()
    except Exception:
        logger.exception("退出前关闭 OTel 管道失败")
