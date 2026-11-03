"""代码托管接入层（scm）的离线单元测试。

不发真实请求：把 `scm.httpx.Client` 换成一个记录型假 client，只检查
「请求打到哪、带什么 body、返回值怎么归一化」。

GITHUB_TOKEN / GITLAB_TOKEN 用假值就能生效，因为项目 .env 里没有这两个 key
（`get_env` 的 `_load_non_empty_env(override=True)` 只覆盖 .env 里存在的 key）。
GITEE_TOKEN 在 .env 里有个占位值，所以这里不测它的具体 token，只测 URL 映射。
"""

from __future__ import annotations

from typing import Any

import pytest

from familiar.tools import gitee_api, scm


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class FakeClient:
    """记录所有请求，并按预设顺序返回固定 JSON。"""

    calls: list[dict[str, Any]] = []
    responses: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        data: Any = None,
        headers: dict | None = None,
    ) -> "FakeResponse":
        FakeClient.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "data": data,
                "headers": headers or {},
            }
        )
        payload = FakeClient.responses.pop(0) if FakeClient.responses else {}
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """每次测试前重置假 client，并注入假令牌。"""
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setattr(scm.httpx, "Client", FakeClient)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_github_token_123456")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fake_gitlab_token_123456")
    monkeypatch.setenv("GITEE_TOKEN", "fake_gitee_token_123456")


@pytest.mark.parametrize(
    ("raw", "provider", "path"),
    [
        ("https://gitee.com/msb-goldbin/ai_coding.git", "gitee", "msb-goldbin/ai_coding"),
        ("https://github.com/openai/openai-python", "github", "openai/openai-python"),
        ("git@github.com:foo/bar.git", "github", "foo/bar"),
        ("https://gitlab.com/group/sub/repo.git", "gitlab", "group/sub/repo"),
        ("github.com/foo/bar", "github", "foo/bar"),
        ("owner/repo", "gitee", "owner/repo"),
    ],
)
def test_parse_repo_url(raw: str, provider: str, path: str) -> None:
    ref = scm.parse_repo_url(raw)
    assert ref.provider == provider
    assert ref.path == path


def test_parse_repo_url_rejects_unknown_host() -> None:
    with pytest.raises(ValueError):
        scm.parse_repo_url("https://bitbucket.org/a/b")


def test_create_github_pr() -> None:
    FakeClient.responses = [{"number": 7, "html_url": "https://github.com/foo/bar/pull/7"}]
    ref = scm.parse_repo_url("https://github.com/foo/bar")
    pr = scm.create_pull_request(ref=ref, head="feat/x", base="main", title="t", body="b")

    call = FakeClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/repos/foo/bar/pulls")
    assert call["headers"].get("Authorization", "").startswith("Bearer ghp_")
    assert call["json"] == {"head": "feat/x", "base": "main", "title": "t", "body": "b"}
    assert pr.get("html_url", "").endswith("/pull/7")


def test_create_github_pr_reuses_existing_on_422() -> None:
    FakeClient.responses = [
        FakeResponse({"message": "A pull request already exists"}, 422),
        [{"number": 9, "html_url": "https://github.com/foo/bar/pull/9"}],
    ]
    ref = scm.parse_repo_url("https://github.com/foo/bar")
    pr = scm.create_pull_request(ref=ref, head="feat/x", base="main", title="t", body="b")

    assert len(FakeClient.calls) == 2
    assert FakeClient.calls[1]["params"]["head"] == "foo:feat/x"
    assert pr.get("reused") is True
    assert pr.get("number") == 9


def test_create_gitlab_mr_with_namespace_encoding() -> None:
    FakeClient.responses = [
        {"iid": 3, "web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/3"}
    ]
    ref = scm.parse_repo_url("https://gitlab.com/group/sub/repo")
    mr = scm.create_pull_request(ref=ref, head="feat/x", base="main", title="t", body="b")

    call = FakeClient.calls[0]
    assert "/projects/group%2Fsub%2Frepo/merge_requests" in call["url"]
    assert call["headers"].get("PRIVATE-TOKEN", "").startswith("glpat-")
    assert call["json"]["source_branch"] == "feat/x"
    assert mr.get("number") == 3
    assert mr.get("html_url", "").endswith("/merge_requests/3")


def test_list_files_path_mapping() -> None:
    FakeClient.responses = [[]]
    ref = scm.parse_repo_url("https://github.com/o/r")
    scm.list_pull_request_files(ref=ref, number=1)
    assert FakeClient.calls[0]["url"].endswith("/pulls/1/files")


def test_list_comments_path_mapping() -> None:
    FakeClient.responses = [[]]
    gh = scm.parse_repo_url("https://github.com/o/r")
    scm.list_pull_request_comments(ref=gh, number=1)
    assert "/issues/1/comments" in FakeClient.calls[0]["url"]

    FakeClient.calls = []
    FakeClient.responses = [[]]
    gl = scm.parse_repo_url("https://gitlab.com/o/r")
    scm.list_pull_request_comments(ref=gl, number=1)
    assert "/merge_requests/1/notes" in FakeClient.calls[0]["url"]


def test_list_files_gitlab_normalizes_changes() -> None:
    FakeClient.responses = [{"changes": [{"new_path": "a.py", "diff": "@@", "new_file": True}]}]
    gl = scm.parse_repo_url("https://gitlab.com/o/r")
    files = scm.list_pull_request_files(ref=gl, number=1)
    assert files[0]["filename"] == "a.py"
    assert files[0]["status"] == "added"


def test_post_comment() -> None:
    FakeClient.responses = [{"id": 1}]
    gh = scm.parse_repo_url("https://github.com/o/r")
    scm.post_pr_comment(ref=gh, number=2, body="hello")
    call = FakeClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/issues/2/comments")
    assert call["json"] == {"body": "hello"}


def test_token_for_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITLAB_TOKEN"):
        scm.token_for(scm.GITLAB)


def test_mask_tokens() -> None:
    # GITHUB_TOKEN 的假值会被脱敏；GITEE 的占位值也参与脱敏
    masked = scm.mask_tokens("token=ghp_fake_github_token_123456 结束")
    assert "ghp_fake_github_token_123456" not in masked
    assert "***" in masked


def test_gitee_api_compat_layer() -> None:
    assert gitee_api.parse_gitee_repo_url("https://github.com/a/b").provider == "github"
    assert gitee_api.GiteeRepo is scm.RepoRef
