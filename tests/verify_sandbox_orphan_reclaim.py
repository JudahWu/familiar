"""验证孤儿沙箱回收逻辑（不依赖真实 OpenSandbox server）。

背景：进程重启后，旧进程创建的沙箱不在新进程的 _BACKENDS 注册表里，
sweeper 永远碰不到。reclaim_orphan_sandboxes() 在应用启动时对账一次，
把 server 上所有 RUNNING 的沙箱全部回收。

本脚本 mock 掉 SDK 的 sandbox service，验证：
1. 多页列表会被完整收集（收集完再 kill，避免边删边翻页漏杀）；
2. 每个 RUNNING 沙箱都被调用一次 kill_sandbox；
3. 单个 kill 失败不影响其余回收；
4. 空列表时返回空且不抛异常。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import familiar.backends.opensandbox_backend as backend_mod
from familiar.backends.opensandbox_backend import reclaim_orphan_sandboxes


class FakeService:
    """模拟 AdapterFactorySync(...).create_sandbox_service() 的返回。"""

    def __init__(self, pages: list[list[Any]], fail_ids: set[str] | None = None):
        self._pages = pages
        self._fail_ids = fail_ids or set()
        self.killed: list[str] = []
        self.list_calls: list[int] = []

    def list_sandboxes(self, filter: Any):  # noqa: A002 - 与 SDK 签名对齐
        self.list_calls.append(filter.page or 0)
        page_index = filter.page - 1 if filter.page else 0
        return SimpleNamespace(
            sandbox_infos=self._pages[page_index] if page_index < len(self._pages) else []
        )

    def kill_sandbox(self, sandbox_id: str) -> None:
        if sandbox_id in self._fail_ids:
            raise RuntimeError("boom")
        self.killed.append(sandbox_id)


def _info(sandbox_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=sandbox_id,
        status=SimpleNamespace(state="RUNNING"),
        created_at=datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc),
    )


def main() -> int:
    failures: list[str] = []

    # 场景 1：两页数据，全部应被收集并逐个 kill。
    svc = FakeService(
        pages=[[_info(f"s-{i}") for i in range(100)], [_info("s-last")]]
    )
    backend_mod.__dict__["_fake_service_for_test"] = svc  # 仅供断言观察，不参与逻辑
    original_factory = backend_mod.AdapterFactorySync

    class FakeFactory:
        def __init__(self, conn: Any) -> None:
            pass

        def create_sandbox_service(self) -> FakeService:
            return svc

    backend_mod.AdapterFactorySync = FakeFactory
    try:
        killed = reclaim_orphan_sandboxes()
        if len(svc.killed) != 101:
            failures.append(f"场景1：应 kill 101 个，实际 {len(svc.killed)}")
        if killed != svc.killed:
            failures.append("场景1：返回值与实际 kill 列表不一致")
        if svc.list_calls != [1, 2]:
            failures.append(f"场景1：应翻页 2 次，实际 {svc.list_calls}")
    finally:
        backend_mod.AdapterFactorySync = original_factory
        backend_mod.__dict__.pop("_fake_service_for_test", None)

    # 场景 2：某个 kill 失败，其余照常回收，函数不抛异常。
    svc2 = FakeService(pages=[[_info("s-a"), _info("s-b"), _info("s-c")]], fail_ids={"s-b"})
    factory_holder: dict[str, Any] = {}

    class FakeFactory2:
        def __init__(self, conn: Any) -> None:
            pass

        def create_sandbox_service(self) -> FakeService:
            return svc2

    factory_holder["f"] = FakeFactory2
    backend_mod.AdapterFactorySync = factory_holder["f"]
    try:
        killed = reclaim_orphan_sandboxes()
        if killed != ["s-a", "s-c"]:
            failures.append(f"场景2：失败应跳过，实际 {killed}")
        if len(svc2.killed) != 2:
            failures.append(f"场景2：应 kill 2 个，实际 {len(svc2.killed)}")
    finally:
        backend_mod.AdapterFactorySync = original_factory

    # 场景 3：server 上没有 RUNNING 沙箱，返回空列表。
    svc3 = FakeService(pages=[[]])

    class FakeFactory3:
        def __init__(self, conn: Any) -> None:
            pass

        def create_sandbox_service(self) -> FakeService:
            return svc3

    backend_mod.AdapterFactorySync = FakeFactory3
    try:
        killed = reclaim_orphan_sandboxes()
        if killed != []:
            failures.append(f"场景3：空列表应返回 []，实际 {killed}")
    finally:
        backend_mod.AdapterFactorySync = original_factory

    if failures:
        print("FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("OK：孤儿沙箱回收逻辑（分页收集 / 逐个 kill / 失败隔离 / 空列表）全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
