from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from familiar.env_utils import get_env

# 项目根目录固定为当前项目目录。Windows 开发环境通常是
# E:\my_project\Familiar，Linux/Docker 部署时通常是 /opt/familiar/app。
# 后续所有本项目自己的数据文件、日志文件都默认放在这个目录下面，
# 避免 LangGraph dev 或第三方工具把文件散落到隐藏目录中。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# SQLite 数据目录：默认使用项目内 data/。
# 用户仍然可以通过 .env 覆盖，但本项目推荐显式落在项目目录中，
# 方便直接打开 SQLite 文件观察 checkpoint 和业务 Store 的区别。
DATA_DIR = Path(get_env("FAMILIAR_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
CHECKPOINT_DB_PATH = Path(
    get_env("CHECKPOINT_DB_PATH", str(DATA_DIR / "checkpoints.sqlite"))
).resolve()
STORE_DB_PATH = Path(get_env("STORE_DB_PATH", str(DATA_DIR / "store.sqlite"))).resolve()
LANGGRAPH_STORE_DB_PATH = Path(
    get_env("LANGGRAPH_STORE_DB_PATH", str(DATA_DIR / "langgraph_store.sqlite"))
).resolve()

# ── 外部服务连接（PostgreSQL / Redis / OpenSandbox）──
# 三者由项目根目录的 docker-compose.yml 统管：
#   - PostgreSQL：root/root，库 familiar，端口 5432
#   - Redis：密码 root，端口 6379
#   - OpenSandbox server：端口 8080（Docker runtime，命令在隔离容器里跑）
# PERSISTENCE_TYPE 控制业务/会话数据落在 SQLite 还是 PostgreSQL；
# 默认 sqlite（零改动、可直接跑），设为 postgres 才走 PG。
PERSISTENCE_TYPE = get_env("PERSISTENCE_TYPE", "sqlite").strip().lower()
DATABASE_URL = get_env("DATABASE_URL", "postgresql://root:root@localhost:5432/familiar")
REDIS_URL = get_env("REDIS_URL", "redis://:root@localhost:6379")
OPENSANDBOX_URL = get_env("OPENSANDBOX_URL", "http://localhost:8080")
# 运行沙箱 backend 开关：local_shell（默认，直接在本机跑）或 opensandbox（隔离容器）。
# 设为 opensandbox 才会创建 OpenSandbox 容器；其余情况一律回退到 LocalShellBackend。
SANDBOX_TYPE = get_env("SANDBOX_TYPE", "local_shell").strip().lower()
# OpenSandbox 容器镜像：默认 python:3.12-slim（自带 python3，BaseSandbox 的
# ls/read/edit/grep/glob 默认实现都依赖容器内 python3 跑脚本）。
OPENSANDBOX_IMAGE = get_env("OPENSANDBOX_IMAGE", "python:3.12-slim")
# 沙箱空闲多久后回收（秒）。默认 1800s = 30 分钟，对齐 GitHub Codespaces 默认值。
# 每次 execute/upload_files/download_files 调用都会重置计时；到点由 server.py 的
# 后台 sweep 线程 kill 掉沙箱并清掉缓存条目。
OPENSANDBOX_IDLE_TIMEOUT = int(get_env("OPENSANDBOX_IDLE_TIMEOUT", "1800"))
# 沙箱硬生命周期（秒），只当兜底。默认 43200s = 12 小时，对齐 Codespaces 的
# "最长生存期 12 小时"。空闲回收才是主机制，这个只是防止沙箱永远不死。
# 注意：OpenSandbox server 强制下限 60 秒，低于此值创建直接 422。
OPENSANDBOX_MAX_LIFETIME = max(60, int(get_env("OPENSANDBOX_MAX_LIFETIME", "43200")))

# ── Langfuse 可观测性（LLM 调用分析）──
# 自托管 Langfuse 由 docker-compose.yml 统管，UI/API 在 3001 端口。
# key 在 Langfuse 项目设置里创建，填到 .env 后重启后端即可生效；
# 缺任一 key 时观测层整体退化成空操作，不影响 agent 运行。
LANGFUSE_ENABLED = get_env("LANGFUSE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LANGFUSE_PUBLIC_KEY = get_env("LANGFUSE_PUBLIC_KEY", "").strip()
LANGFUSE_SECRET_KEY = get_env("LANGFUSE_SECRET_KEY", "").strip()
LANGFUSE_BASE_URL = get_env("LANGFUSE_BASE_URL", "http://localhost:3001").strip()
# Langfuse 里的环境标签，方便把本地调试和线上流量的 trace 分开看。
LANGFUSE_ENVIRONMENT = get_env("LANGFUSE_ENVIRONMENT", "local").strip()
# 采样率：本地调试保持 1.0 全采；线上如果量大可以调到 0.1 之类省存储。
LANGFUSE_SAMPLE_RATE = float(get_env("LANGFUSE_SAMPLE_RATE", "1.0"))
# 导出前把形如 API key / token 的字符串打码（默认开）。只挡凭证形状的串，
# 不做通用脱敏——仓库代码本身仍然会进 trace，介意的话应换独立实例/关 tracing。
LANGFUSE_MASK_SECRETS = get_env("LANGFUSE_MASK_SECRETS", "true").strip().lower() in {"1", "true", "yes", "on"}

# 日志目录：所有后端日志和 Agent 运行日志都写入项目内 logs/。
# 这样既能在控制台实时看，也能在课后通过日志文件复盘 Agent 做了什么。
LOG_DIR = Path(get_env("FAMILIAR_LOG_DIR", str(PROJECT_ROOT / "logs"))).resolve()
LOG_LEVEL = get_env("FAMILIAR_LOG_LEVEL", "INFO").upper()
LOG_ROTATION_WHEN = get_env("FAMILIAR_LOG_WHEN", "midnight")
LOG_ROTATION_INTERVAL = int(get_env("FAMILIAR_LOG_INTERVAL", "1"))
LOG_RETENTION_DAYS = int(get_env("FAMILIAR_LOG_RETENTION_DAYS", "14"))

# ── 日志落盘开关 ──
# 默认关：日志走 stdout + OTel（→ ClickHouse / Grafana 可查），不再落本地文件。
# 需要兜底时打开，切换成按大小轮转（不是按天），保证单文件不会无限长。
LOG_FILE_ENABLED = get_env("LOG_FILE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
LOG_FILE_MAX_BYTES = int(get_env("LOG_FILE_MAX_MB", "50")) * 1024 * 1024
LOG_FILE_BACKUP_COUNT = int(get_env("LOG_FILE_BACKUP_COUNT", "5"))
# 噪音 logger，统一压到 WARNING（uvicorn 的 access log 由我们自己的中间件替代）
LOG_NOISY_LOGGERS = (
    "uvicorn.access",
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "charset_normalizer",
)

# ── OpenTelemetry（logs / metrics 的出口）──
# 应用只发 OTLP，不直连 ClickHouse / Prometheus；由 otel-collector 分流。
# collector 在宿主机 4318（见 docker-compose.yml 的 otel-collector）。
OTEL_ENABLED = get_env("OTEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
OTEL_ENDPOINT = get_env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
OTEL_SERVICE_NAME = get_env("OTEL_SERVICE_NAME", "familiar")
OTEL_ENVIRONMENT = get_env("OTEL_ENVIRONMENT", "local")


def log_date_text(target_date: date | None = None) -> str:
    """返回历史日志文件使用的日期文本。

    TimedRotatingFileHandler 当前写入固定文件名，例如 backend.log；
    历史文件默认追加 YYYY-MM-DD 后缀，例如 backend.log.2026-07-10。
    这个函数保留给日志 API 读取历史日期时使用。
    """

    return (target_date or date.today()).isoformat()


def backend_log_path(target_date: date | None = None) -> Path:
    """返回后端日志路径。

    不传日期时返回当前正在写入的固定文件名；传日期时返回标准轮转后的
    历史文件名。
    """

    if target_date is None:
        return LOG_DIR / "backend.log"
    return LOG_DIR / f"backend.log.{log_date_text(target_date)}"


def agent_log_path(target_date: date | None = None) -> Path:
    """返回 Agent 运行日志路径。"""

    if target_date is None:
        return LOG_DIR / "agent-runs.log"
    return LOG_DIR / f"agent-runs.log.{log_date_text(target_date)}"


BACKEND_LOG_PATH = backend_log_path()
AGENT_LOG_PATH = agent_log_path()

def _default_workspace_root() -> str:
    """返回跨平台默认 Agent 工作区。

    生产部署应优先通过 AI_WORKSPACE_ROOT 显式配置，例如：
    - Linux/Docker: /opt/familiar/workspace
    - Windows: E:\ai_workspace

    默认值只用于本地开发兜底，避免代码里写死某一个操作系统路径。
    """

    if os.name == "nt":
        return r"E:\ai_workspace"
    return str(PROJECT_ROOT.parent / "ai_workspace")


# Agent 操作真实代码仓库时使用的工作区。它和项目源码目录分离，
# 避免 Agent 在执行用户任务时误改 Familiar 项目本身。
WORKSPACE_ROOT = Path(get_env("AI_WORKSPACE_ROOT", _default_workspace_root())).expanduser().resolve()
PROJECTS_DIR = WORKSPACE_ROOT / "projects"

# 知识库的三处存储（各管一段，别混）：
#   PostgreSQL → 元信息（库名、文档、状态、进度），是唯一事实来源
#   MinIO      → PDF 原件与 ③ 阶段切出来的插图
#   Milvus     → 能搜到的段落与索引
# 原来还有一个 WORKSPACE_ROOT/knowledge 目录放元信息和原件，已经废弃：
# 文件式元信息在"每页都要写一次进度"的场景下会反复重写整份 JSON，并发必丢更新。

# Milvus 向量库（docker-compose 的 milvus 服务）。Attu 管理界面在 8000。
MILVUS_URI = get_env("MILVUS_URI", "http://localhost:19530").strip() or "http://localhost:19530"

# 知识库的原始文件存放（docker-compose 的 minio 服务，控制台在 9003）。
# 存 PDF 原件和 ③ 阶段切出来的插图；元信息在 PostgreSQL，不在这里。
# endpoint 允许带 scheme（http:// / https://），objects 模块会自己拆出 secure 标志。
S3_ENDPOINT = get_env("S3_ENDPOINT", "http://127.0.0.1:9002").strip() or "http://127.0.0.1:9002"
S3_ACCESS_KEY = get_env("S3_ACCESS_KEY", "familiar").strip()
S3_SECRET_KEY = get_env("S3_SECRET_KEY", "familiar-dev-secret").strip()
S3_BUCKET = get_env("S3_BUCKET", "familiar-knowledge").strip() or "familiar-knowledge"

# 向量模型。**文本与图片必须用同一个模型**——它们落在同一个 dense 字段里，
# 不同模型产生的向量之间 IP 距离没有意义，图文互搜会出乱子。
#
# provider=dashscope 走 DashScope 原生接口：多模态 embedding 在 OpenAI 兼容模式
# 下不可用（实测 /embeddings 返回 404），只有原生 SDK 能调。
# provider=openai 则走标准 HTTP /embeddings，适合本地 LM Studio / vLLM。
EMBEDDING_PROVIDER = get_env("EMBEDDING_PROVIDER", "dashscope").strip() or "dashscope"
EMBEDDING_MODEL = get_env("EMBEDDING_MODEL", "multimodal-embedding-v1").strip()
# provider=openai 时才用下面两个
EMBEDDING_BASE_URL = get_env("EMBEDDING_BASE_URL", "").strip()
EMBEDDING_API_KEY = get_env("EMBEDDING_API_KEY", "").strip()

# 多模态视觉模型：给书里的插图写描述。走 OpenAI 兼容端点。
ALIBABA_API_KEY = get_env("ALIBABA_API_KEY", "").strip()
ALIBABA_BASE_URL = get_env("ALIBABA_BASE_URL", "").strip()
VISION_MODEL = get_env("VISION_MODEL", "qwen3-vl-plus").strip()

# 版面识别服务（可选）。OpenAI 兼容端点，模型名默认 dots_ocr。
# 配了就能按版面裁出矢量插图、顺带识别没有文字层的扫描页；
# 没配就降级为只抽 PDF 内嵌的位图（操作截图类插图仍然拿得到）。
OCR_BASE_URL = get_env("OCR_BASE_URL", "").strip()
OCR_MODEL = get_env("OCR_MODEL", "dots_ocr").strip()

# DeepAgents skills 目录。本项目统一放在 WORKSPACE_ROOT/skills，
# 这样可以通过 DeepAgents 原生 backend route 暴露为 `/skills/`。
SKILLS_DIR = WORKSPACE_ROOT / "skills"
