"""Gitee 相关 LangChain 工具。

本模块把 `gitee_api.py` 中的低层 API 调用包装成模型可调用的工具。
这里负责处理运行期上下文、只读任务拦截、任务事件记录和业务 Store 状态更新。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from familiar.core.events import record_event
from familiar.core.graph import get_store
from familiar.tools import scm
from familiar.tools.gitee_api import (
    create_pull_request,
    get_pull_request,
    list_pull_request_comments,
    list_pull_request_commits,
    list_pull_request_files,
    post_pr_comment,
)
from familiar.tools.runtime_context import get_runtime_thread_id, runtime_is_read_only_task

logger = logging.getLogger("familiar.run.scm")


def current_provider() -> str:
    """判断当前会话绑定的仓库属于哪个平台。

    一次会话只绑一个仓库，所以直接看 thread 的 repo_url 就够了，不需要模型
    在工具参数里显式传平台（模型很容易漏传或传错）。
    拿不到时退回 Gitee，保持旧行为。
    """

    thread_id = get_runtime_thread_id()
    if not thread_id:
        return scm.GITEE
    try:
        thread = get_store().get_thread(thread_id) or {}
        repo_url = str(thread.get("repo_url") or "").strip()
        if repo_url:
            return scm.parse_repo_url(repo_url).provider
    except Exception:
        # 解析失败不应该让工具直接崩，退回默认平台由其报更具体的错误
        logger.debug("无法从 thread 推断仓库平台，回退 Gitee", exc_info=True)
    return scm.GITEE


@tool
def open_gitee_pull_request(
    owner: str,
    repo: str,
    head: str,
    base: str = "master",
    title: str = "Familiar generated changes",
    body: str = "由 Familiar 自动生成。",
) -> dict[str, Any]:
    """为已经推送的分支创建或复用 Pull Request / Merge Request。

    平台（Gitee / GitHub / GitLab）由当前会话绑定的仓库地址自动决定，
    不需要在参数里指定。

    Args:
        owner: 仓库 owner（GitLab 多级命名空间时用 group/sub 形式）。
        repo: 仓库名称。
        head: 已推送的源分支。
        base: 目标分支，默认 master。
        title: PR 标题。
        body: PR 描述。

    Returns:
        成功时返回 `ok=True`、PR URL 和原始 API 数据；只读任务或 API 失败时返回错误信息。
    """

    if runtime_is_read_only_task():
        # 分析、规划、问答等只读任务不能执行外部写操作。
        # 这层拦截可以避免模型在用户尚未确认实施前创建 PR。
        return {
            "ok": False,
            "error": "当前任务是只读任务，不能创建 Pull Request。请先向用户输出方案或分析结论，等待确认实施。",
        }

    thread_id = get_runtime_thread_id()
    provider = current_provider()
    # 记录关键参数但不记录 token；认证由 scm 和 LocalShellBackend 内部处理。
    logger.info(
        "准备创建 PR：provider=%s owner=%s repo=%s head=%s base=%s title=%s",
        provider,
        owner,
        repo,
        head,
        base,
        title,
    )
    if thread_id:
        # 事件表用于前端展示工具执行进度，也便于排查 Agent 在哪个阶段失败。
        record_event(thread_id, "gitee:pr", "创建或复用 Pull Request", kind="fetch", status="in_progress")

    pr = create_pull_request(
        owner=owner, repo=repo, head=head, base=base, title=title, body=body, provider=provider
    )
    pr_url = pr.get("html_url") or pr.get("url") or ""

    if thread_id:
        # Store 中保存 PR 状态，后续任务列表、详情页和最终结果都可以直接读取。
        get_store().update_thread_status(thread_id, "pr_created", pr_url=pr_url, branch_name=head)
        record_event(
            thread_id,
            "gitee:pr",
            "创建或复用 Pull Request",
            kind="fetch",
            status="completed",
            detail=pr_url,
        )

    if pr.get("reused"):
        # 重复创建同一 head/base PR 时，底层 API 会返回 reused=True 的兼容结构。
        logger.info("PR 已存在，复用已有 PR：thread_id=%s pr_url=%s", thread_id, pr_url)
    else:
        logger.info("PR 创建完成：thread_id=%s pr_url=%s", thread_id, pr_url)
    return {"ok": True, "pr_url": pr_url, "raw": pr}


@tool
def publish_gitee_pr_comment(owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
    """向指定 Pull Request / Merge Request 发布普通评论。

    Args:
        owner: 仓库 owner。
        repo: 仓库名称。
        number: PR 编号。
        body: 评论正文。

    Returns:
        平台 API 返回的评论 JSON。
    """

    # 评论内容可能较长，不写入日志；日志只保留仓库和 PR 编号用于问题定位。
    logger.info("准备发布 PR 评论：owner=%s repo=%s number=%s", owner, repo, number)
    return post_pr_comment(owner=owner, repo=repo, number=number, body=body, provider=current_provider())


@tool
def get_gitee_pull_request_context(owner: str, repo: str, number: int) -> dict[str, Any]:
    """读取 Pull Request 审查上下文。

    Reviewer 子 Agent 使用该工具获取 PR 标题、描述、变更文件、提交列表和已有评论。
    该工具只读，不会修改远端仓库。
    """

    provider = current_provider()
    logger.info("读取 PR 上下文：provider=%s owner=%s repo=%s number=%s", provider, owner, repo, number)
    pr = get_pull_request(owner=owner, repo=repo, number=number, provider=provider)
    commits = list_pull_request_commits(owner=owner, repo=repo, number=number, provider=provider)
    files = list_pull_request_files(owner=owner, repo=repo, number=number, provider=provider)
    comments = list_pull_request_comments(owner=owner, repo=repo, number=number, provider=provider)
    return {
        "pull_request": pr,
        "commits": commits,
        "files": files,
        "comments": comments,
        "summary": {
            "title": pr.get("title") if isinstance(pr, dict) else None,
            "state": pr.get("state") if isinstance(pr, dict) else None,
            "files_count": len(files),
            "commits_count": len(commits),
            "comments_count": len(comments),
        },
    }
