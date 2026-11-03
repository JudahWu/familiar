"""Gitee 相关能力的**兼容入口**。

真正的实现在 `app/tools/scm.py`（支持 Gitee / GitHub / GitLab）。
这个模块保留原来的函数名，是因为调用点很多（runtime、repo_mapping、reviewer、
工具层、若干脚本）；一次性全部改名收益不大、风险不小。

约定：
- 新代码请直接 `from familiar.tools import scm`；
- 老代码继续用这里的名字，行为等价，只是多认了 GitHub / GitLab 地址。
"""

from __future__ import annotations

from familiar.tools.scm import (
    GITEE,
    RepoRef,
    get_pull_request as _get_pull_request,
    git_remote_url,
    list_pull_request_comments as _list_pull_request_comments,
    list_pull_request_commits as _list_pull_request_commits,
    list_pull_request_files as _list_pull_request_files,
    mask_tokens,
    normalize_repo_url,
    parse_repo_url,
    post_pr_comment as _post_pr_comment,
    ref_for,
    token_for,
)
from familiar.tools.scm import create_pull_request as _create_pull_request

# 历史名字：`GiteeRepo` 现在就是通用的 RepoRef（多了 provider / host 字段）。
GiteeRepo = RepoRef

# `parse_gitee_repo_url` 现在会按域名判断平台，不再只认 gitee.com。
parse_gitee_repo_url = parse_repo_url
normalize_gitee_repo_url = normalize_repo_url


def get_gitee_token() -> str:
    """兼容旧接口：读取 Gitee 令牌。"""

    return token_for(GITEE)


def mask_token(text: str) -> str:
    """兼容旧接口：脱敏文本里的令牌（现在三家平台的都会处理）。"""

    return mask_tokens(text)


def _ref(owner: str, repo: str, provider: str = GITEE) -> RepoRef:
    return ref_for(owner, repo, provider)


def create_pull_request(
    *,
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    provider: str = GITEE,
) -> dict:
    """创建 PR/MR（按 provider 分发，默认 Gitee 保持旧行为）。"""

    return _create_pull_request(
        ref=_ref(owner, repo, provider),
        head=head,
        base=base,
        title=title,
        body=body,
    )


def get_pull_request(*, owner: str, repo: str, number: int, provider: str = GITEE) -> dict:
    return _get_pull_request(ref=_ref(owner, repo, provider), number=number)


def list_pull_request_commits(*, owner: str, repo: str, number: int, provider: str = GITEE) -> list:
    return _list_pull_request_commits(ref=_ref(owner, repo, provider), number=number)


def list_pull_request_files(*, owner: str, repo: str, number: int, provider: str = GITEE) -> list:
    return _list_pull_request_files(ref=_ref(owner, repo, provider), number=number)


def list_pull_request_comments(*, owner: str, repo: str, number: int, provider: str = GITEE) -> list:
    return _list_pull_request_comments(ref=_ref(owner, repo, provider), number=number)


def post_pr_comment(
    *, owner: str, repo: str, number: int, body: str, provider: str = GITEE
) -> dict:
    return _post_pr_comment(ref=_ref(owner, repo, provider), number=number, body=body)


def authenticated_clone_url(url: "str | RepoRef") -> str:
    """兼容旧接口：返回**不带凭据**的标准 clone URL。

    历史上这个函数会把 token 拼进 URL，现在改成只做规范化：token 一旦进了命令
    文本就会出现在日志、进程列表和 .git/config 里。真正的认证由
    LocalShellBackend 的 GIT_ASKPASS 完成（按域名从环境变量取令牌）。
    """

    target = url.clone_url if isinstance(url, RepoRef) else url
    return git_remote_url(parse_repo_url(target))
