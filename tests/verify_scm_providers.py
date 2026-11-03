"""验证三家代码托管平台的接入层（不需要真实令牌，不发真实请求）。

做法：把 `scm.httpx.Client` 换成一个假的记录型 client，只检查
「请求打到了哪个地址、带了什么 body、返回值是否被归一化」。

覆盖：
1. Gitee / GitHub / GitLab 的地址解析与平台判定；
2. PR/MR 创建（含 GitLab 多级命名空间的项目 ID 编码）；
3. 读详情 / 提交 / 文件 / 评论的路径映射；
4. 令牌缺失时的报错、令牌脱敏；
5. `gitee_api` 兼容层仍然可用。

运行：python scripts/verify_scm_providers.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 造几个假令牌，避免依赖真实 .env
os.environ["GITHUB_TOKEN"] = "ghp_fake_github_token_123456"
os.environ["GITLAB_TOKEN"] = "glpat-fake_gitlab_token_123456"
os.environ["GITEE_TOKEN"] = "fake_gitee_token_123456"

from familiar.tools import gitee_api, scm  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """记录所有请求，并按预设返回固定 JSON。"""

    calls: list[dict] = []
    responses: list[object] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, params=None, json=None, data=None, headers=None):
        FakeClient.calls.append(
            {"method": method, "url": url, "params": params, "json": json, "data": data, "headers": headers or {}}
        )
        payload = FakeClient.responses.pop(0) if FakeClient.responses else {}
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def with_fake(responses: list):
    FakeClient.calls = []
    FakeClient.responses = list(responses)
    scm.httpx.Client = FakeClient  # type: ignore[assignment]
    return FakeClient.calls


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


print("1) 地址解析")
table = {
    "https://gitee.com/msb-goldbin/ai_coding.git": ("gitee", "msb-goldbin/ai_coding"),
    "https://github.com/openai/openai-python": ("github", "openai/openai-python"),
    "git@github.com:foo/bar.git": ("github", "foo/bar"),
    "https://gitlab.com/group/sub/repo.git": ("gitlab", "group/sub/repo"),
    "github.com/foo/bar": ("github", "foo/bar"),
    "owner/repo": ("gitee", "owner/repo"),
}
for raw, (provider, path) in table.items():
    ref = scm.parse_repo_url(raw)
    check(f"{raw} -> {provider}", ref.provider == provider and ref.path == path, f"got {ref.provider} {ref.path}")

try:
    scm.parse_repo_url("https://bitbucket.org/a/b")
    check("不支持的域名应报错", False)
except ValueError:
    check("不支持的域名应报错", True)

print("2) GitHub 创建 PR")
calls = with_fake([{"number": 7, "html_url": "https://github.com/foo/bar/pull/7"}])
gh = scm.parse_repo_url("https://github.com/foo/bar")
pr = scm.create_pull_request(ref=gh, head="feat/x", base="main", title="t", body="b")
check("POST /repos/foo/bar/pulls", calls[0]["method"] == "POST" and calls[0]["url"].endswith("/repos/foo/bar/pulls"), calls[0]["url"])
check("Bearer 认证头", calls[0]["headers"].get("Authorization", "").startswith("Bearer ghp_"))
check("body 用 json 传", calls[0]["json"] == {"head": "feat/x", "base": "main", "title": "t", "body": "b"})
check("返回 html_url", pr.get("html_url", "").endswith("/pull/7"))

print("3) GitHub 重复 PR 复用（422 → 按 head/base 找回）")
calls = with_fake([
    FakeResponse({"message": "A pull request already exists"}, 422),
    [{"number": 9, "html_url": "https://github.com/foo/bar/pull/9"}],
])
pr = scm.create_pull_request(ref=gh, head="feat/x", base="main", title="t", body="b")
check("422 后回查已有 PR", len(calls) == 2 and calls[1]["params"]["head"] == "foo:feat/x", str(calls[-1].get("params")))
check("标记 reused", pr.get("reused") is True and pr.get("number") == 9)

print("4) GitLab 创建 MR（多级命名空间）")
calls = with_fake([{"iid": 3, "web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/3"}])
gl = scm.parse_repo_url("https://gitlab.com/group/sub/repo")
mr = scm.create_pull_request(ref=gl, head="feat/x", base="main", title="t", body="b")
check("项目 ID 做了 URL 编码", "/projects/group%2Fsub%2Frepo/merge_requests" in calls[0]["url"], calls[0]["url"])
check("PRIVATE-TOKEN 认证", calls[0]["headers"].get("PRIVATE-TOKEN", "").startswith("glpat-"))
check("GitLab 字段名是 source/target_branch", calls[0]["json"]["source_branch"] == "feat/x")
check("归一化成 number/html_url", mr.get("number") == 3 and mr.get("html_url", "").endswith("/merge_requests/3"))

print("5) 读取接口路径映射")
paths = {
    "gitee 文件": ("https://gitee.com/o/r", "/pulls/1/files"),
    "github 文件": ("https://github.com/o/r", "/pulls/1/files"),
}
for label, (url, expect_suffix) in paths.items():
    ref = scm.parse_repo_url(url)
    calls = with_fake([[]])
    scm.list_pull_request_files(ref=ref, number=1)
    check(label, calls[0]["url"].endswith(expect_suffix), calls[0]["url"])

calls = with_fake([[]])
scm.list_pull_request_comments(ref=gh, number=1)
check("GitHub 评论走 issues 接口", "/issues/1/comments" in calls[0]["url"], calls[0]["url"])

calls = with_fake([[]])
scm.list_pull_request_comments(ref=gl, number=1)
check("GitLab 评论走 notes 接口", "/merge_requests/1/notes" in calls[0]["url"], calls[0]["url"])

calls = with_fake([{"changes": [{"new_path": "a.py", "diff": "@@", "new_file": True}]}])
files = scm.list_pull_request_files(ref=gl, number=1)
check("GitLab changes 归一化成 files", files[0]["filename"] == "a.py" and files[0]["status"] == "added", str(files[:1]))

print("6) 发评论")
calls = with_fake([{"id": 1}])
scm.post_pr_comment(ref=gh, number=2, body="hello")
check("GitHub 评论 POST issues/2/comments", calls[0]["method"] == "POST" and calls[0]["url"].endswith("/issues/2/comments"))
check("body 字段正确", calls[0]["json"] == {"body": "hello"})

print("7) 令牌缺失与脱敏")
os.environ.pop("GITLAB_TOKEN", None)
os.environ.pop("SCM_GITLAB_TOKEN", None)
try:
    scm.token_for(scm.GITLAB)
    check("缺令牌应报错", False)
except RuntimeError as exc:
    check("缺令牌应报错且提示变量名", "GITLAB_TOKEN" in str(exc))
masked = scm.mask_tokens("token=ghp_fake_github_token_123456 结束")
check("脱敏替换令牌", "ghp_fake" not in masked and "***" in masked, masked)

print("8) 兼容层 gitee_api")
check("parse_gitee_repo_url 仍可用（且认 github）", gitee_api.parse_gitee_repo_url("https://github.com/a/b").provider == "github")
check("GiteeRepo 别名指向 RepoRef", gitee_api.GiteeRepo is scm.RepoRef)

print()
if failures:
    print(f"{len(failures)} 项失败：")
    for item in failures:
        print("  -", item)
    sys.exit(1)
print("全部检查通过。")
