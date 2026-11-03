"""把 tests/verify_*.py 冒烟脚本包成 pytest 用例。

这些是历史遗留的「人工 print + assert」脚本，依赖真实服务或 LLM（Milvus、
Langfuse、Gitee、沙箱、dots_ocr 等），不适合放进 CI 的离线单元测试。这里用
subprocess 逐个跑，退出码 0 即通过。

默认被 `-m "not smoke"` 跳过，需要时手动跑：`pytest -m smoke`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS = sorted(_TESTS_DIR.glob("verify_*.py"))


@pytest.mark.smoke
@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_smoke_script(script: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=_TESTS_DIR.parent,
        capture_output=True,
        text=True,
        timeout=900,
    )
    tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
    assert proc.returncode == 0, f"{script.name} 退出码 {proc.returncode}：\n{tail}"
