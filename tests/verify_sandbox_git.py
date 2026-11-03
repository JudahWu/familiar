"""验证沙箱里 git 能不能用、认证环境变量有没有按命令注入。

做三件事：
1. 起一个真实沙箱（用的是 .env 里的 OPENSANDBOX_IMAGE），确认里面有 git；
2. 对比 `git` 命令与普通命令的环境，确认 askpass 只注入给 git；
3. 在 /projects 下真跑一次 clone（公开仓库），验证网络与挂载都通，然后清理。

注意：会在工作区 /projects 下建临时目录 `_git_probe`，结束后删除。
运行：python scripts/verify_sandbox_git.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from familiar.backends.opensandbox_backend import OpenSandboxBackend  # noqa: E402
from familiar.core.settings import OPENSANDBOX_IMAGE  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    print(f"镜像：{OPENSANDBOX_IMAGE}")
    backend = OpenSandboxBackend()
    started = time.time()
    try:
        print("1) 起沙箱并探测 git")
        res = backend.execute("git --version")
        elapsed = time.time() - started
        check("git 可执行", "git version" in res.output, res.output[:120])
        print(f"     （首个命令包含沙箱创建，耗时 {elapsed:.1f}s）")
        print(f"     {res.output.strip()}")

        print("2) 认证变量只注入给 git 命令")
        git_env = backend.execute('git --version >/dev/null && echo "GIT_ASKPASS=$GIT_ASKPASS"')
        check("git 命令带 GIT_ASKPASS", "/usr/local/bin/git-askpass.sh" in git_env.output, git_env.output[:160])
        # 只打印是否设置，不打印令牌本身
        token_flag = backend.execute(
            'if [ -n "$GITEE_ASKPASS_TOKEN" ] || [ -n "$GITHUB_ASKPASS_TOKEN" ] || [ -n "$GITLAB_ASKPASS_TOKEN" ]; '
            'then echo token=set; else echo token=empty; fi'
        )
        check("非 git 命令不带令牌", "token=empty" in token_flag.output, token_flag.output[:120])

        print("3) 在 /projects 下真实 clone（公开仓库）")
        clone = backend.execute(
            "rm -rf _git_probe && git clone --depth 1 -q "
            "https://github.com/octocat/Hello-World.git _git_probe && ls _git_probe",
            timeout=120,
        )
        check("clone 成功", "README" in clone.output, clone.output[:200])

        print("4) 清理")
        cleanup = backend.execute("rm -rf _git_probe && echo cleaned")
        check("清理完成", "cleaned" in cleanup.output)
    finally:
        health = backend.health()
        print(f"5) health: image={health.get('image')} git={health.get('git')} state={health.get('state')}")
        backend.shutdown()

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
