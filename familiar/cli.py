"""命令行入口。

`familiar` 这个可执行命令来自 pyproject.toml 的 `[project.scripts]`：
`familiar = "familiar.cli:main"`。它只做一件事——起 FastAPI 服务，
等价于 `uvicorn familiar.app:app`，但不用记模块路径。

生产部署仍然建议直接用 uvicorn/gunicorn 挂 `familiar.app:app`，
这里的入口主要是给本地 `pip install -e .` 之后的开发者用。
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "familiar.app:app",
        host=os.environ.get("FAMILIAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("FAMILIAR_PORT", "2024")),
    )


if __name__ == "__main__":
    main()
