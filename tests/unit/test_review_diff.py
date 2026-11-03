"""代码审查 diff 解析与位置校验的单元测试。"""

from __future__ import annotations

from familiar.review.diff import parse_unified_diff, validate_finding_location

SAMPLE_DIFF = """diff --git a/app/main.py b/app/main.py
index 1111111..2222222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1,4 +1,6 @@
 from fastapi import FastAPI
 
 app = FastAPI()
+SECRET = "demo"
+
 @app.get("/health")
 def health():
"""


def test_parse_unified_diff() -> None:
    summary = parse_unified_diff(SAMPLE_DIFF, base="master", head="feature/demo")
    assert summary.changed_file_paths == ("app/main.py",)
    assert summary.changed_lines_for("app/main.py") == {4, 5}


def test_validate_finding_location_on_changed_line() -> None:
    summary = parse_unified_diff(SAMPLE_DIFF)
    ok, message = validate_finding_location(summary, file="app/main.py", line=4)
    assert ok, message


def test_validate_finding_location_on_unchanged_line() -> None:
    summary = parse_unified_diff(SAMPLE_DIFF)
    ok, _ = validate_finding_location(summary, file="app/main.py", line=20)
    assert not ok


def test_validate_finding_location_on_other_file() -> None:
    summary = parse_unified_diff(SAMPLE_DIFF)
    ok, _ = validate_finding_location(summary, file="app/other.py", line=3)
    assert not ok
