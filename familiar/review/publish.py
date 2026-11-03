from __future__ import annotations

from familiar.review.findings import ReviewFinding, format_findings_comment
from familiar.tools.gitee_api import post_pr_comment


def publish_findings_to_gitee(
    *,
    owner: str,
    repo: str,
    number: int,
    findings: list[ReviewFinding],
) -> dict:
    body = format_findings_comment(findings)
    return post_pr_comment(owner=owner, repo=repo, number=number, body=body)
