"""OpenSandbox 后端实现。

这个模块把 DeepAgents 的沙箱协议落到 OpenSandbox 隔离容器里。

和 LocalShellBackend 的区别：
- LocalShellBackend 在本机用 subprocess 跑命令，路径直接落在本机文件系统；
- OpenSandboxBackend 把本机的工作区子目录（projects/skills/policies/reviews/
  runtimes/tmp/logs）通过 host 卷挂载进隔离容器，命令与文件操作都在容器里执行。

为什么需要这套设计：
1. OpenSandbox SDK 是纯异步的（`Sandbox.create` / `commands.run` / `files.*` 都
   是 asyncio 协程），而 DeepAgents 的 `BaseSandbox` 协议把 `execute()` 定义成
   同步方法（async 版本 `aexecute` 默认用 `asyncio.to_thread` 包了一层同步
   `execute`）。所以这里用一个独立后台线程 + 专属事件循环承载所有异步调用，
   同步方法通过 `run_coroutine_threadsafe` 把协程投到那个循环里执行。
2. 容器内路径和本机虚拟路径保持一致（都叫 `/projects`、`/skills` ...），这样
   可以直接复用 `BaseSandbox` 的默认 ls/read/write/edit/grep/glob 实现，
   不需要做虚拟路径翻译。
3. 本地 macOS + Docker 场景下，SDK 默认会直连容器的 execd 端口，但容器在 Docker
   bridge 网络里，本机直连不通。所以必须 `use_server_proxy=True`，让流量走
   OpenSandbox server（localhost:8080）代理转发。
4. 本地 opensandbox-server 的 /ping 健康检查在某些情况下会超时，这里用
   `skip_health_check=True` 绕过（endpoint 发布仍然会被等待），不影响实际
   命令执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    LsResult,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import Host, SandboxFilter, Volume
from opensandbox.sync.adapters.factory import AdapterFactorySync

from familiar.core.settings import (
    OPENSANDBOX_IMAGE,
    OPENSANDBOX_MAX_LIFETIME,
    OPENSANDBOX_URL,
    WORKSPACE_ROOT,
)
from familiar.env_utils import get_env

logger = logging.getLogger("familiar.run.opensandbox")

# 工作区子目录 → 容器内虚拟路径的挂载规划。
# 注意：键是主机上 WORKSPACE_ROOT 下的子目录名，值是容器内的虚拟路径，
# 必须和 LocalShellBackend 里的 VIRTUAL_* 常量保持一致，Agent 工具链才能
# 把 /projects、/skills 等路径正确落到容器里。
MOUNT_SUBDIRS: dict[str, str] = {
    "projects": "/projects",
    "skills": "/skills",
    "policies": "/policies",
    "reviews": "/reviews",
    "runtimes": "/runtimes",
    "tmp": "/tmp",
    "logs": "/logs",
}

# 给 SDK 的 ready_timeout 一个宽松上限：endpoint 发布 + 健康检查（已跳过）的总预算。
_READY_TIMEOUT = timedelta(seconds=90)
# 单条命令在容器内的最长运行时间（默认），可被 execute(timeout=...) 覆盖。
_DEFAULT_CMD_TIMEOUT = timedelta(seconds=600)
# 等待沙箱就绪的同步上限（线程里阻塞用）。
_SANDBOX_CREATE_TIMEOUT = 120.0

# 镜像里预置的 askpass 脚本路径（见 ops/sandbox/Dockerfile）。
# 脚本本身不含令牌，只从环境变量读。
_GIT_ASKPASS_PATH = "/usr/local/bin/git-askpass.sh"
# 只在命令真的在调 git 时才注入凭据，缩短令牌在容器里的可见窗口。
# 覆盖 `git ...`、`sudo git ...`、`sh -c "git push"` 这几种常见写法。
_GIT_CMD_RE = re.compile(r"(^|[\s;&|(])git(\s|$)")

# 平台 → (环境变量候选, askpass 用户名)
_GIT_TOKEN_ENVS: dict[str, tuple[tuple[str, ...], str]] = {
    "GITEE": (("GITEE_TOKEN", "SCM_GITEE_TOKEN"), "oauth2"),
    "GITHUB": (("GITHUB_TOKEN", "GH_TOKEN", "SCM_GITHUB_TOKEN"), "x-access-token"),
    "GITLAB": (("GITLAB_TOKEN", "SCM_GITLAB_TOKEN"), "oauth2"),
}


def _parse_opensandbox_url(url: str) -> tuple[str, str]:
    """把 OPENSANDBOX_URL 拆成 (protocol, domain)。

    domain 形如 `localhost:8080`，ConnectionConfig 用它拼 base_url。
    若 URL 带了 http(s):// 前缀就按前缀走，否则默认 http。
    """

    parsed = urlparse(url)
    protocol = parsed.scheme or "http"
    # urlparse 对 `localhost:8080` 这种没有 scheme 的字符串会把整个当 path，
    # 这种情况下 parsed.netloc 为空，直接用原始 host:port。
    domain = parsed.netloc or parsed.path
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = domain.split("://", 1)[1]
    return protocol, domain


def reclaim_orphan_sandboxes() -> list[str]:
    """回收 OpenSandbox server 上所有仍在运行的沙箱，返回被回收的 id。

    背景：空闲回收（sweep）依赖进程内存里的 _BACKENDS 注册表，进程一旦重启，
    旧进程创建的容器就成了没有任何人管的孤儿——它们不在新进程的注册表里，
    sweeper 永远碰不到，会一直活到硬生命周期结束（默认 12 小时）。

    本函数只在应用启动时调用一次：新进程此刻还不可能拥有任何沙箱，所以
    server 上所有 RUNNING 的沙箱必然是上个进程的遗留，直接全部回收。

    注意：本地部署假设同一时间只有一个后端进程共用一个 OpenSandbox server。
    如果同时跑两个后端实例，后启动的会把先启动实例正在用的沙箱也回收掉
    （先启动实例的自愈机制会重建沙箱，影响有限，但应尽量避免）。
    """

    protocol, domain = _parse_opensandbox_url(OPENSANDBOX_URL)
    conn = ConnectionConfig(protocol=protocol, domain=domain, use_server_proxy=True)
    service = AdapterFactorySync(conn).create_sandbox_service()

    # 先收集完整列表再逐个 kill：边删边翻页会让分页错位漏杀。
    infos: list[Any] = []
    page = 1
    while True:
        result = service.list_sandboxes(
            SandboxFilter(states=["RUNNING"], page_size=100, page=page)
        )
        batch = list(result.sandbox_infos)
        if not batch:
            break
        infos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    killed: list[str] = []
    for info in infos:
        try:
            service.kill_sandbox(info.id)
            killed.append(info.id)
            logger.info("回收孤儿沙箱：%s（创建于 %s）", info.id, info.created_at)
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响其余回收
            logger.warning("回收孤儿沙箱 %s 失败：%s", info.id, exc)
    return killed


class OpenSandboxBackend(BaseSandbox):
    """基于 OpenSandbox 隔离容器的 DeepAgents backend。

    线程模型：
    - 一个 daemon 线程持有专属 asyncio 事件循环；
    - 沙箱创建、命令执行、文件上传/下载的协程都投到这个循环里跑；
    - 同步方法（execute/upload_files/download_files）通过
      `run_coroutine_threadsafe(...).result()` 阻塞等待结果返回。

    这样即使调用方处在 asyncio 事件循环里（例如 FastAPI 路由或 LangGraph 节点），
    也不会因为“已有运行中的事件循环”而报错。
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str] | None = None,
        *,
        timeout: int | None = None,
        image: str | None = None,
    ) -> None:
        # 工作区根目录：默认用 settings 里的 WORKSPACE_ROOT，也可由调用方覆盖。
        self.root = Path(workspace or WORKSPACE_ROOT).expanduser().resolve()
        # timeout 参数保留兼容：不传则用 settings 的硬生命周期；传了则以传入值为准
        # （测试里需要造短生命周期沙箱）。
        self.default_timeout = timeout if timeout is not None else OPENSANDBOX_MAX_LIFETIME
        self.image = image or OPENSANDBOX_IMAGE

        # 确保主机侧工作区子目录存在，否则卷挂载会失败或挂载成空目录。
        for sub in MOUNT_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

        # 专属事件循环 + 后台线程。
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._sb: Sandbox | None = None
        self._create_error: BaseException | None = None
        self._closed = False
        # 容器内 git 版本（探测后填；None 表示没有 git 或还没探测）
        self._git_version: str | None = None
        # 懒创建：_ensure_sandbox() 不在 __init__ 里触发，改由第一次真正要用沙箱时
        # （execute/upload_files/download_files）经 _ensure_started() 拉起。
        # 这样纯聊天、查网页这类不碰文件系统的会话一个容器都不会建。
        self._started = False
        self._start_lock = threading.Lock()
        # 空闲计时：每次使用都刷新，server.py 的 sweep 线程据此回收。
        self._last_used = time.monotonic()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def idle_seconds(self) -> float:
        """距上次使用过了多少秒（从未使用过则按创建时间算）。"""

        return time.monotonic() - self._last_used

    def _touch(self) -> None:
        self._last_used = time.monotonic()

    def _ensure_started(self) -> None:
        """幂等地触发一次沙箱创建。自愈路径会先把它重置回 False。"""

        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._ready.clear()
            asyncio.run_coroutine_threadsafe(self._ensure_sandbox(), self._loop)

    # ── 事件循环线程 ────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _ensure_sandbox(self) -> None:
        try:
            self._sb = await self._create_sandbox()
        except Exception as exc:  # noqa: BLE001 - 需要把任意异常透传给等待方
            self._create_error = exc
            logger.exception("OpenSandbox 创建失败：%s", exc)
        finally:
            self._ready.set()

    def _wait_ready(self) -> Sandbox:
        """阻塞直到沙箱就绪，返回 Sandbox 实例或抛出创建异常。"""

        self._ensure_started()
        if not self._ready.wait(timeout=_SANDBOX_CREATE_TIMEOUT):
            raise RuntimeError("OpenSandbox 在限定时间内未就绪（endpoint 发布超时？）")
        if self._create_error is not None:
            raise self._create_error
        if self._sb is None:
            raise RuntimeError("OpenSandbox 未就绪且未记录错误")
        return self._sb

    def _invalidate(self) -> None:
        """把当前沙箱标记为不可用，下次 _wait_ready() 会重建。

        用于自愈：沙箱被硬生命周期杀掉了，但缓存里的 backend 还活着，
        不重置的话每次调用都会拿到那个死沙箱，报同一个错直到进程重启。
        """

        self._sb = None
        self._create_error = None
        self._started = False
        self._git_version = None
        self._ready.clear()
        logger.info("OpenSandbox backend 已标记失效，下次使用将重建沙箱")

    async def _create_sandbox(self) -> Sandbox:
        protocol, domain = _parse_opensandbox_url(OPENSANDBOX_URL)
        conn = ConnectionConfig(
            protocol=protocol,
            domain=domain,
            use_server_proxy=True,  # 关键：macOS 本机无法直接连容器 execd，走 server 代理
        )
        volumes = [
            Volume(
                name=sub,
                host=Host(path=str(self.root / sub)),
                mount_path=mount_path,
                read_only=False,
            )
            for sub, mount_path in MOUNT_SUBDIRS.items()
        ]
        sb = await Sandbox.create(
            image=self.image,
            entrypoint=["tail", "-f", "/dev/null"],
            timeout=timedelta(seconds=self.default_timeout),
            ready_timeout=_READY_TIMEOUT,
            resource={"cpu": "1000m", "memory": "1Gi"},
            volumes=volumes,
            skip_health_check=True,  # 本地 /ping 健康检查可能超时，绕过不影响执行
            connection_config=conn,
        )
        logger.info("OpenSandbox 已创建：%s", sb.id)
        # 探一下镜像里到底有没有 git。默认的 python:*-slim 是没有的，
        # 提前发现比让 Agent 在 “git: not found” 上反复试要好。
        self._git_version = await self._probe_git(sb)
        if self._git_version is None:
            logger.warning(
                "当前沙箱镜像 %s 里没有 git，coding 任务会失败。"
                "请用 scripts/build_sandbox_image.sh 构建并让 OPENSANDBOX_IMAGE 指向它。",
                self.image,
            )
        return sb

    async def _probe_git(self, sb: Sandbox) -> str | None:
        """探测容器内的 git 版本，没有则返回 None。"""

        try:
            result = await sb.commands.run(
                "git --version",
                opts=RunCommandOpts(
                    working_directory="/projects",
                    timeout=timedelta(seconds=15),
                ),
            )
        except Exception:  # noqa: BLE001 - 探测失败按“没有 git”处理
            logger.debug("探测 git 失败", exc_info=True)
            return None
        text = _join_messages(result.logs.stdout).strip()
        if text.startswith("git version"):
            logger.info("沙箱 git 可用：%s", text)
            return text
        return None

    # ── 同步执行原语（投到专属循环） ────────────────────────────

    def _run_coro(self, coro: Any, timeout: float | None = None) -> Any:
        # 循环已停（backend 已关闭）时直接失败，避免 future.result() 干等超时，
        # 同时把协程关掉，杜绝 “coroutine was never awaited” 警告。
        if self._loop.is_closed() or not self._loop.is_running():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("OpenSandbox backend 已关闭，事件循环不可用")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── BaseSandbox 必须实现的抽象方法 ──────────────────────────

    @property
    def id(self) -> str:
        try:
            sb = self._sb
        except Exception:  # noqa: BLE001
            sb = None
        if sb is not None:
            return f"opensandbox:{sb.id}"
        return f"opensandbox:{self.root}"

    def get_work_dir(self) -> str:
        return "/projects"

    def get_workspace_root(self) -> str:
        return "/"

    # ── /skills 宿主直读（避免每轮 skills 扫描白建容器）──────────
    #
    # DeepAgents 的 SkillsMiddleware 在 before_agent 阶段（每轮对话开始、
    # 模型还没看到消息之前）就会 ls 技能目录并 download SKILL.md。
    # BaseSandbox 的 ls/download_files 默认实现最终全走 execute()，
    # 于是"纯聊天也建容器"。而 /skills 本来就是只读 host 卷，内容与
    # 宿主 ai_workspace/skills 完全一致，直接从宿主读即可，不必进容器。

    _SKILLS_VIRTUAL_PREFIX = "/skills"

    def _skills_host_path(self, virtual: str) -> Path | None:
        """虚拟路径若位于 /skills 下，返回对应宿主路径；否则返回 None。"""

        prefix = self._SKILLS_VIRTUAL_PREFIX
        if virtual == prefix:
            return self.root / "skills"
        if virtual.startswith(prefix + "/"):
            rel = virtual[len(prefix) + 1 :]
            return self.root / "skills" / rel
        return None

    def _host_ls(self, virtual: str, host: Path) -> LsResult:
        """用宿主目录内容构造 LsResult，路径保持虚拟形式。"""

        if not host.exists():
            return LsResult(error=f"file_not_found: {virtual}")
        try:
            entries = sorted(host.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError as exc:
            return LsResult(error=str(exc))
        infos: list[dict[str, Any]] = []
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            infos.append(
                {
                    "path": f"{virtual.rstrip('/')}/{entry.name}",
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
        return LsResult(entries=infos)

    def ls(self, path: str) -> LsResult:
        host = self._skills_host_path(path)
        if host is not None:
            return self._host_ls(path, host)
        return super().ls(path)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self._touch()
        result = self._execute_once(command, timeout=timeout)
        if result is not None:
            return result

        # 自愈：沙箱可能已被硬生命周期杀掉了（连续工作超过 MAX_LIFETIME，
        # 或进程重启前的残留）。标记失效重建一次再试，避免这一轮直接失败。
        logger.info("检测到沙箱不可用，重建后重试：%s", command[:120])
        self._invalidate()
        retry = self._execute_once(command, timeout=timeout)
        if retry is not None:
            return retry
        # 重建也失败，把第二次的异常如实抛给调用方。
        raise RuntimeError("OpenSandbox 沙箱重建后仍然无法执行命令")

    def _git_envs_for(self, command: str) -> dict[str, str]:
        """给 git 命令注入认证环境变量（逐命令注入，命令结束即消失）。

        为什么这么做：
        - 沙箱里没有 token 也没有 askpass 时，私有仓库 clone/push 会卡在输入密码；
        - 把令牌写进镜像或容器文件系统，等于让 Agent 随时能读到；
        - `RunCommandOpts.envs` 只作用于这一条命令的进程，进程退出环境就没了。

        没有配置任何令牌时返回空 dict —— 此时不注入 GIT_ASKPASS，git 遇到私有
        仓库会明确失败（GIT_TERMINAL_PROMPT 在镜像/环境里也已关闭），不会挂住。
        """

        if not _GIT_CMD_RE.search(command or ""):
            return {}

        envs: dict[str, str] = {}
        for platform, (names, username) in _GIT_TOKEN_ENVS.items():
            token = ""
            for name in names:
                token = get_env(name).strip()
                if token:
                    break
            if not token:
                continue
            envs[f"{platform}_ASKPASS_USERNAME"] = username
            envs[f"{platform}_ASKPASS_TOKEN"] = token

        if not envs:
            return {}

        envs["GIT_ASKPASS"] = _GIT_ASKPASS_PATH
        envs["GIT_TERMINAL_PROMPT"] = "0"
        envs["GCM_INTERACTIVE"] = "Never"
        # 自建 GitLab 的域名不一定带 "gitlab" 字样，给 askpass 脚本兜底用
        envs["GITLAB_ASKPASS_HOST"] = (
            get_env("GITLAB_BASE_URL", "https://gitlab.com").strip().rstrip("/")
        )
        return envs

    def _execute_once(self, command: str, *, timeout: int | None = None) -> ExecuteResponse | None:
        """执行一次命令。

        返回 None 表示"沙箱没了，调用方该考虑重建"；返回 ExecuteResponse 表示
        这次执行已有确定结果（成功，或命令本身失败/环境类错误）。
        """

        try:
            sb = self._wait_ready()
        except Exception as exc:  # noqa: BLE001
            if _is_sandbox_gone(exc):
                return None
            logger.warning("OpenSandbox 就绪失败：%s", exc)
            return ExecuteResponse(output=f"[OpenSandbox 执行异常] {exc}", exit_code=1)

        cmd_timeout = timedelta(seconds=timeout) if timeout else _DEFAULT_CMD_TIMEOUT
        opts = RunCommandOpts(
            working_directory="/projects",
            timeout=cmd_timeout,
            # 只有 git 命令才带凭据，其它命令的环境保持干净
            envs=self._git_envs_for(command) or None,
        )

        async def _run() -> Any:
            return await sb.commands.run(command, opts=opts)

        try:
            exec_result = self._run_coro(_run())
        except Exception as exc:  # noqa: BLE001
            if _is_sandbox_gone(exc):
                return None
            logger.warning("OpenSandbox 命令执行异常：%s", exc)
            # 命令执行失败也返回一个“可消费”的响应，错误信息进 output。
            return ExecuteResponse(output=f"[OpenSandbox 执行异常] {exc}", exit_code=1)

        stdout_text = _join_messages(exec_result.logs.stdout)
        stderr_text = _join_messages(exec_result.logs.stderr)
        if stderr_text:
            if stdout_text:
                output = f"{stdout_text}\n[stderr]\n{stderr_text}"
            else:
                output = f"[stderr]\n{stderr_text}"
        else:
            output = stdout_text
        return ExecuteResponse(output=output, exit_code=exec_result.exit_code)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        self._touch()
        sb = self._wait_ready()
        responses: list[FileUploadResponse] = []
        for path, data in files:
            async def _up(p: str = path, d: bytes = data) -> None:
                await sb.files.write_file(p, d)

            try:
                self._run_coro(_up(), timeout=60.0)
                responses.append(FileUploadResponse(path=path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("OpenSandbox 上传失败 %s：%s", path, exc)
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self._touch()
        sb: Sandbox | None = None
        responses: list[FileDownloadResponse] = []
        for path in paths:
            # /skills 下的内容直接读宿主（见 ls 上的注释），不碰容器。
            host = self._skills_host_path(path)
            if host is not None:
                try:
                    responses.append(FileDownloadResponse(path=path, content=host.read_bytes()))
                except OSError as exc:
                    logger.warning("宿主 skills 读取失败 %s：%s", path, exc)
                    responses.append(FileDownloadResponse(path=path, content=None, error=str(exc)))
                continue

            if sb is None:
                sb = self._wait_ready()

            async def _down(p: str = path) -> bytes:
                return await sb.files.read_bytes(p)  # type: ignore[union-attr]

            try:
                content = self._run_coro(_down(), timeout=60.0)
                responses.append(FileDownloadResponse(path=path, content=content))
            except Exception as exc:  # noqa: BLE001
                logger.warning("OpenSandbox 下载失败 %s：%s", path, exc)
                responses.append(FileDownloadResponse(path=path, content=None, error=str(exc)))
        return responses

    # ── 健康检查与销毁 ──────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        # 注意：这里绝不调 _wait_ready()——那会触发懒创建，让一次健康检查
        # 白建一个容器。只如实报告当前状态：还没建就是 not_started。
        base = {
            "type": "opensandbox",
            "root": str(self.root),
            "image": self.image,
            "git": self._git_version,
            "sandbox_id": None,
            "idle_seconds": round(self.idle_seconds(), 1),
        }
        if self._closed:
            return {**base, "healthy": False, "state": "closed", "error": None}
        if not self._started:
            return {**base, "healthy": False, "state": "not_started", "error": None}
        if self._create_error is not None:
            return {
                **base,
                "healthy": False,
                "state": "create_failed",
                "error": str(self._create_error),
            }
        if self._sb is None or not self._ready.is_set():
            return {**base, "healthy": False, "state": "creating", "error": None}
        return {
            **base,
            "sandbox_id": self._sb.id,
            "healthy": True,
            "state": "ready",
            "error": None,
        }

    def shutdown(self) -> None:
        """销毁沙箱并停止后台事件循环线程。

        幂等：重复调用直接返回。必须先 kill 沙箱再停循环，否则投到已停止循环上
        的协程永远不会被 await（会触发 “coroutine was never awaited” 警告）。
        """

        if self._closed:
            return
        self._closed = True

        # 创建可能还在飞行中（懒创建刚触发就被回收），先等一下让它落地再杀，
        # 否则这个容器没人 kill，会一直活到硬生命周期结束。
        if self._started and self._sb is None and not self._ready.is_set():
            self._ready.wait(timeout=_SANDBOX_CREATE_TIMEOUT)

        sb = self._sb
        if sb is not None:
            try:
                self._run_coro(sb.kill(), timeout=30.0)
                logger.info("OpenSandbox 沙箱已销毁：%s", sb.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("OpenSandbox 销毁失败：%s", exc)
        else:
            logger.info("OpenSandbox backend 关闭时没有需要销毁的沙箱")
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("OpenSandbox backend 已关闭")

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _is_sandbox_gone(exc: BaseException) -> bool:
    """判断异常是不是"沙箱已经没了"。

    OpenSandbox 在沙箱被硬生命周期杀掉后，命令会报
    `SandboxApiException: ... [DOCKER::SANDBOX_NOT_FOUND] Sandbox <id> not found.`
    这里按消息内容匹配，不依赖具体异常类型（SDK 各版本抛的类型不完全稳定）。
    """

    text = str(exc)
    if "SANDBOX_NOT_FOUND" in text:
        return True
    lowered = text.lower()
    return "sandbox" in lowered and "not found" in lowered


def _join_messages(messages: list[Any]) -> str:
    """把 OutputMessage 列表拼成字符串，兼容行尾与连续消息的换行。

    复刻 opensandbox SDK 内部 `_format_output_messages` 的拼接逻辑：相邻消息之间
    只在两边都没有换行符时才补一个换行，避免重复空行。
    """

    parts: list[str] = []
    last_char = ""
    for msg in messages:
        text = msg.text if hasattr(msg, "text") else str(msg)
        if text and last_char and last_char not in "\r\n" and text[0] not in "\r\n":
            parts.append("\n")
        parts.append(text)
        if text:
            last_char = text[-1]
    return "".join(parts).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
