"""代码托管平台（SCM）统一接入层。

支持 Gitee / GitHub / GitLab 三家，负责三件事：

1. **解析仓库地址**：把 URL、`git@host:owner/repo`、`host/owner/repo`、
   以及历史遗留的裸 `owner/repo`（默认按 Gitee 处理）统一成 `RepoRef`。
2. **取令牌**：按平台读不同的环境变量，缺失时给出明确提示。
3. **调 API**：把三家的 PR/MR 接口差异收敛成同一组函数
   （创建 / 读详情 / 读提交 / 读文件 / 读评论 / 发评论）。

上层工具（`gitee_tools.py`）和 reviewer 只依赖这里的统一签名，
不需要知道当前仓库到底在哪家平台上。

为什么保留 `gitee_api.py`：历史调用点很多，那个模块现在是本模块的兼容别名层，
对外函数名不变，避免一次性改几十处。新代码直接用 `app.tools.scm`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from familiar.env_utils import get_env

GITEE = "gitee"
GITHUB = "github"
GITLAB = "gitlab"

# host -> provider。带 www. 的写法一并归到标准域名。
_HOST_TO_PROVIDER = {
    "gitee.com": GITEE,
    "www.gitee.com": GITEE,
    "github.com": GITHUB,
    "www.github.com": GITHUB,
    "gitlab.com": GITLAB,
    "www.gitlab.com": GITLAB,
}

# 各平台令牌的环境变量，按优先级排列。
_TOKEN_ENV = {
    GITEE: ("GITEE_TOKEN", "SCM_GITEE_TOKEN"),
    GITHUB: ("GITHUB_TOKEN", "GH_TOKEN", "SCM_GITHUB_TOKEN"),
    GITLAB: ("GITLAB_TOKEN", "SCM_GITLAB_TOKEN"),
}

_DISPLAY = {GITEE: "Gitee", GITHUB: "GitHub", GITLAB: "GitLab"}

# askpass 用的用户名：GitHub 用 x-access-token，GitLab/Gitee 用 oauth2 都能过。
_ASKPASS_USERNAME = {GITEE: "oauth2", GITHUB: "x-access-token", GITLAB: "oauth2"}


@dataclass(frozen=True)
class RepoRef:
    """标准化后的仓库引用。

    owner 在 GitLab 上可能是多级命名空间（`group/subgroup`），所以统一用
    `path` 属性拼出完整路径，避免各处自己手拼。
    """

    provider: str
    host: str
    owner: str
    repo: str
    clone_url: str
    web_url: str

    @property
    def path(self) -> str:
        """完整路径（GitLab 上是 `group/subgroup/repo`）。"""

        return f"{self.owner}/{self.repo}"

    @property
    def display(self) -> str:
        return _DISPLAY.get(self.provider, self.provider)


# ── 解析 ──────────────────────────────────────────────────────────


def _provider_for_host(host: str) -> str | None:
    """判断域名属于哪个平台；自建 GitLab 用 GITLAB_BASE_URL 的域名兜底。"""

    normalized = (host or "").lower()
    provider = _HOST_TO_PROVIDER.get(normalized)
    if provider:
        return provider
    gitlab_host = urlparse(get_env("GITLAB_BASE_URL", "https://gitlab.com")).hostname or ""
    if gitlab_host and normalized == gitlab_host.lower():
        return GITLAB
    github_host = urlparse(get_env("GITHUB_BASE_URL", "https://github.com")).hostname or ""
    if github_host and normalized == github_host.lower():
        return GITHUB
    return None


def parse_repo_url(value: str) -> RepoRef:
    """解析仓库地址。

    支持的写法：
    - `https://github.com/owner/repo(.git)`
    - `git@gitlab.com:group/sub/repo.git`
    - `github.com/owner/repo`
    - `owner/repo`（历史写法，按 Gitee 处理）

    Raises:
        ValueError: 域名不被支持，或路径无法解析出 owner/repo。
    """

    raw = (value or "").strip()
    if not raw:
        raise ValueError("仓库地址为空")

    # 历史写法：只有 owner/repo，没有域名。默认按 Gitee 保持兼容。
    if "/" not in raw:
        raise ValueError(f"无法解析仓库地址: {value}")
    head = raw.split("/", 1)[0].lower()
    if ":" not in raw and "." not in head and "@" not in raw:
        return _build_ref(GITEE, "gitee.com", raw)

    # SCP 风格的 SSH 地址：git@host:owner/repo.git
    if raw.startswith("git@") and ":" in raw:
        host_part, path_part = raw[4:].split(":", 1)
        return _ref_from_parts(host_part, path_part, value)

    # 没有 scheme 的 host/owner/repo
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "ssh", "git"}:
        raise ValueError(f"不支持的仓库地址协议: {value}")
    path = parsed.path.strip("/")
    if not path:
        # ssh://git@host:22/owner/repo 这种，path 在 parsed.path 里，正常不会为空
        raise ValueError(f"无法解析仓库地址: {value}")
    return _ref_from_parts(parsed.hostname or "", path, value)


def _ref_from_parts(host: str, path: str, original: str) -> RepoRef:
    provider = _provider_for_host(host)
    if provider is None:
        raise ValueError(
            f"不支持的仓库域名 {host}；当前支持 gitee.com / github.com / gitlab.com，"
            "自建 GitLab 请在 .env 里配置 GITLAB_BASE_URL。"
        )
    return _build_ref(provider, host.lower(), path, original)


def _build_ref(provider: str, host: str, path: str, original: str = "") -> RepoRef:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"无法解析仓库地址（缺少 owner/repo）: {original or path}")
    repo = re.sub(r"\.git$", "", parts[-1])
    owner = "/".join(parts[:-1])
    if provider in {GITEE, GITHUB} and "/" in owner:
        # 这两家不支持多级命名空间，出现 group/sub/repo 一定是地址写错了。
        raise ValueError(f"{_DISPLAY[provider]} 仓库路径只能是 owner/repo: {original or path}")

    base = _web_base(provider, host)
    web_url = f"{base}/{owner}/{repo}"
    return RepoRef(
        provider=provider,
        host=host,
        owner=owner,
        repo=repo,
        clone_url=f"{web_url}.git",
        web_url=web_url,
    )


def _web_base(provider: str, host: str) -> str:
    """返回网页根地址（自建 GitLab 时用 GITLAB_BASE_URL）。"""

    if provider == GITLAB:
        configured = get_env("GITLAB_BASE_URL", "").strip()
        if configured:
            return configured.rstrip("/")
    if provider == GITHUB:
        configured = get_env("GITHUB_BASE_URL", "").strip()
        if configured:
            return configured.rstrip("/")
    if provider == GITEE:
        # 兼容历史变量名 GITEE_WEB_BASE_URL
        configured = (
            get_env("GITEE_BASE_URL", "").strip() or get_env("GITEE_WEB_BASE_URL", "").strip()
        )
        if configured:
            return configured.rstrip("/")
    return f"https://{host}"


def normalize_repo_url(value: str) -> str:
    """把各种写法统一成标准 HTTPS clone URL（映射表的业务键）。"""

    return parse_repo_url(value).clone_url


def default_host(provider: str) -> str:
    """平台的默认域名（自建 GitLab 取 GITLAB_BASE_URL 的域名）。"""

    if provider == GITLAB:
        host = urlparse(get_env("GITLAB_BASE_URL", "https://gitlab.com")).hostname
        return (host or "gitlab.com").lower()
    if provider == GITHUB:
        host = urlparse(get_env("GITHUB_BASE_URL", "https://github.com")).hostname
        return (host or "github.com").lower()
    return "gitee.com"


def ref_for(owner: str, repo: str, provider: str = GITEE) -> RepoRef:
    """按 owner/repo + 平台拼出 RepoRef。

    工具层拿到的参数通常只有 owner/repo（模型不需要知道域名），平台由运行上下文决定，
    这里负责补上域名和 clone 地址。
    """

    if provider not in {GITEE, GITHUB, GITLAB}:
        raise ValueError(f"不支持的平台: {provider}")
    return _build_ref(provider, default_host(provider), f"{owner}/{repo}", f"{owner}/{repo}")


def remote_candidates(ref: RepoRef) -> set[str]:
    """生成用于比较本地 remote origin 的候选形式。"""

    return {
        ref.clone_url.lower(),
        ref.clone_url.removesuffix(".git").lower(),
        f"git@{ref.host}:{ref.path}.git".lower(),
        f"git@{ref.host}:{ref.path}".lower(),
    }


# ── 令牌 ──────────────────────────────────────────────────────────


def token_for(provider: str) -> str:
    """读取指定平台的令牌。

    Raises:
        RuntimeError: 该平台没配令牌。错误信息里只提变量名，不泄露任何值。
    """

    for name in _TOKEN_ENV.get(provider, ()):
        token = get_env(name).strip()
        if token:
            return token
    names = " / ".join(_TOKEN_ENV.get(provider, ()))
    raise RuntimeError(
        f"缺少 {_DISPLAY.get(provider, provider)} 访问令牌：请在 .env 配置 {names}"
    )


def configured_tokens() -> dict[str, str]:
    """返回当前已配置的令牌（provider -> token），用于脱敏。"""

    tokens: dict[str, str] = {}
    for provider, names in _TOKEN_ENV.items():
        for name in names:
            token = get_env(name).strip()
            if token:
                tokens[provider] = token
                break
    return tokens


def mask_tokens(text: str) -> str:
    """把文本里出现的任意平台令牌替换成 ***。"""

    masked = text
    for token in configured_tokens().values():
        masked = masked.replace(token, "***")
    return masked


def askpass_username(provider: str) -> str:
    return _ASKPASS_USERNAME.get(provider, "oauth2")


# ── HTTP ──────────────────────────────────────────────────────────


def _api_base(provider: str) -> str:
    if provider == GITEE:
        return get_env("GITEE_API_BASE_URL", "https://gitee.com/api/v5").rstrip("/")
    if provider == GITHUB:
        return get_env("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    base = get_env("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/")
    return f"{base}/api/v4"


def _headers(provider: str) -> dict[str, str]:
    token = token_for(provider)
    if provider == GITHUB:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    if provider == GITLAB:
        return {"PRIVATE-TOKEN": token}
    # Gitee 走 query/form 参数，这里只补 UA，避免风控把裸请求当爬虫
    return {"User-Agent": "familiar-agent"}


def _project_id(ref: RepoRef) -> str:
    """GitLab 用 URL 编码后的完整路径当项目 ID。"""

    return quote(ref.path, safe="")


def _request(
    ref: RepoRef,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    form_body: dict | None = None,
    expect: str = "json",
):
    """统一发起请求，错误信息统一脱敏后抛出。"""

    url = f"{_api_base(ref.provider)}{path}"
    query = dict(params or {})
    if ref.provider == GITEE:
        # Gitee v5 只认 access_token 参数/表单
        query["access_token"] = token_for(GITEE)
        if form_body is not None:
            form_body = {**form_body, "access_token": token_for(GITEE)}

    with httpx.Client(timeout=30) as client:
        response = client.request(
            method,
            url,
            params=query or None,
            json=json_body,
            data=form_body,
            headers=_headers(ref.provider),
        )
    if response.status_code >= 400:
        raise RuntimeError(
            mask_tokens(f"{ref.display} API {method} {path} 失败: {response.status_code} {response.text}")
        )
    if expect == "text":
        return response.text
    return response.json()


# ── Pull / Merge Request ──────────────────────────────────────────


def _pr_number(data: dict, provider: str) -> int | None:
    key = "number" if provider != GITLAB else "iid"
    value = data.get(key)
    return value if isinstance(value, int) else None


def create_pull_request(
    *,
    ref: RepoRef,
    head: str,
    base: str,
    title: str,
    body: str,
) -> dict:
    """创建 PR/MR，已存在相同源分支时复用。"""

    provider = ref.provider
    if provider == GITEE:
        data = _request(
            ref,
            "POST",
            f"/repos/{ref.owner}/{ref.repo}/pulls",
            form_body={"head": head, "base": base, "title": title, "body": body},
        )
        return {**data, "html_url": data.get("html_url") or data.get("url")}

    if provider == GITHUB:
        try:
            data = _request(
                ref,
                "POST",
                f"/repos/{ref.owner}/{ref.repo}/pulls",
                json_body={"head": head, "base": base, "title": title, "body": body},
            )
            return {**data, "html_url": data.get("html_url")}
        except RuntimeError as exc:
            existing = _find_existing_github_pr(ref, head=head, base=base, error=exc)
            if existing is None:
                raise
            return existing

    # GitLab
    try:
        data = _request(
            ref,
            "POST",
            f"/projects/{_project_id(ref)}/merge_requests",
            json_body={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        return _normalize_gitlab_mr(data)
    except RuntimeError as exc:
        existing = _find_existing_gitlab_mr(ref, head=head, error=exc)
        if existing is None:
            raise
        return existing


def _find_existing_github_pr(ref: RepoRef, *, head: str, base: str, error: RuntimeError) -> dict | None:
    """GitHub 在重复 PR 时返回 422，这里按 head/base 找回已有 PR。"""

    text = str(error)
    if "422" not in text and "A pull request already exists" not in text:
        return None
    try:
        items = _request(
            ref,
            "GET",
            f"/repos/{ref.owner}/{ref.repo}/pulls",
            params={"head": f"{ref.owner}:{head}", "base": base, "state": "open"},
        )
    except RuntimeError:
        return None
    if isinstance(items, list) and items:
        found = dict(items[0])
        found["reused"] = True
        found["message"] = "已复用相同 head/base 的现有 Pull Request"
        return found
    return None


def _find_existing_gitlab_mr(ref: RepoRef, *, head: str, error: RuntimeError) -> dict | None:
    text = str(error)
    if "409" not in text and "already exists" not in text.lower():
        return None
    try:
        items = _request(
            ref,
            "GET",
            f"/projects/{_project_id(ref)}/merge_requests",
            params={"source_branch": head, "state": "opened"},
        )
    except RuntimeError:
        return None
    if isinstance(items, list) and items:
        found = _normalize_gitlab_mr(items[0])
        found["reused"] = True
        found["message"] = "已复用相同源分支的现有 Merge Request"
        return found
    return None


def _normalize_gitlab_mr(data: dict) -> dict:
    """把 GitLab MR 映射成和 Gitee/GitHub 接近的结构，方便上层统一读取。"""

    return {
        **data,
        "number": data.get("iid"),
        "html_url": data.get("web_url"),
        "url": data.get("web_url"),
        "body": data.get("description"),
    }


def get_pull_request(*, ref: RepoRef, number: int) -> dict:
    if ref.provider == GITEE:
        data = _request(ref, "GET", f"/repos/{ref.owner}/{ref.repo}/pulls/{number}")
        return data if isinstance(data, dict) else {"items": data}
    if ref.provider == GITHUB:
        data = _request(ref, "GET", f"/repos/{ref.owner}/{ref.repo}/pulls/{number}")
        return data if isinstance(data, dict) else {"items": data}
    data = _request(ref, "GET", f"/projects/{_project_id(ref)}/merge_requests/{number}")
    return _normalize_gitlab_mr(data) if isinstance(data, dict) else {"items": data}


def list_pull_request_commits(*, ref: RepoRef, number: int) -> list:
    if ref.provider == GITEE:
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/commits"
    elif ref.provider == GITHUB:
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/commits"
    else:
        path = f"/projects/{_project_id(ref)}/merge_requests/{number}/commits"
    data = _request(ref, "GET", path)
    return data if isinstance(data, list) else [data]


def list_pull_request_files(*, ref: RepoRef, number: int) -> list:
    if ref.provider == GITEE:
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/files"
    elif ref.provider == GITHUB:
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/files"
    else:
        # GitLab 没有独立的 files 接口，变更列表在 MR 详情的 changes 里
        data = _request(ref, "GET", f"/projects/{_project_id(ref)}/merge_requests/{number}/changes")
        changes = data.get("changes", []) if isinstance(data, dict) else []
        return [
            {
                "filename": item.get("new_path") or item.get("old_path"),
                "status": "renamed" if item.get("renamed_file") else ("added" if item.get("new_file") else "modified"),
                "patch": item.get("diff"),
                "raw": item,
            }
            for item in changes
        ]
    data = _request(ref, "GET", path)
    return data if isinstance(data, list) else [data]


def list_pull_request_comments(*, ref: RepoRef, number: int) -> list:
    if ref.provider == GITEE:
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/comments"
    elif ref.provider == GITHUB:
        # GitHub 的 PR 普通评论挂在 issue 上
        path = f"/repos/{ref.owner}/{ref.repo}/issues/{number}/comments"
    else:
        path = f"/projects/{_project_id(ref)}/merge_requests/{number}/notes"
    data = _request(ref, "GET", path)
    return data if isinstance(data, list) else [data]


def post_pr_comment(*, ref: RepoRef, number: int, body: str) -> dict:
    if ref.provider == GITEE:
        return _request(
            ref,
            "POST",
            f"/repos/{ref.owner}/{ref.repo}/pulls/{number}/comments",
            form_body={"body": body},
        )
    if ref.provider == GITHUB:
        return _request(
            ref,
            "POST",
            f"/repos/{ref.owner}/{ref.repo}/issues/{number}/comments",
            json_body={"body": body},
        )
    return _request(
        ref,
        "POST",
        f"/projects/{_project_id(ref)}/merge_requests/{number}/notes",
        json_body={"body": body},
    )


def git_remote_url(ref: RepoRef) -> str:
    """返回不带凭据的 clone 地址（认证由 askpass 环境变量负责）。"""

    return ref.clone_url
