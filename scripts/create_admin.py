from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from familiar.core.auth import hash_password  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 dashboard 登录用户的密码哈希，输出可直接粘贴到 .env 的 DASHBOARD_USERS。"
    )
    parser.add_argument("email", help="用户邮箱，例如 admin@example.com")
    parser.add_argument(
        "--password",
        help="密码；不传则交互式输入（不回显）。",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        import getpass

        password = getpass.getpass("密码：")
        confirm = getpass.getpass("再输一次：")
        if password != confirm:
            raise SystemExit("两次输入的密码不一致")
    if len(password) < 8:
        raise SystemExit("密码长度至少 8 位")

    encoded = hash_password(password)
    print()
    print("把下面这一行加入 .env（多个用户用逗号分隔）：")
    print()
    print(f"DASHBOARD_USERS={args.email.strip().lower()}:{encoded}")
    print()
    print("如需把该用户设为管理员，再配置：")
    print(f"DASHBOARD_ADMINS={args.email.strip().lower()}")


if __name__ == "__main__":
    main()
