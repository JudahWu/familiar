"""知识库导入的后台任务与进度。

导入一本书现在要跑几十秒：抽文本、切块、给每张插图写描述、算向量。
8 张插图实测 31 秒，插图多或者书更大时更久。放在 HTTP 请求里同步做会超时
（浏览器、nginx、natapp 隧道都可能在 30 秒到 60 秒之间掐断），所以改成
「上传立刻返回 + 后台跑 + 前端轮询进度」。

**为什么不用 run_events + SSE**：那套是给 Agent 运行记时间线用的，键是
thread_id。知识库导入的归属是文档，塞进去会污染会话时间线，而导入本身只有
几十秒，1 秒一次的轮询足够跟得上。用进程内的进度表更简单，也不怕 Redis 或
数据库的瞬时异常。

进程重启会让进度记录丢失——但重启时后台线程也跟着死了，任务本来就断了，
前端轮询拿到"任务不存在"，重新上传即可。
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("familiar.knowledge.tasks")

# doc_id -> 进度
_PROGRESS: dict[str, "ImportProgress"] = {}
# doc_id -> 取消信号。删除文档时置位，后台线程在关键节点检查。
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()


class ImportCancelled(RuntimeError):
    """导入被取消（通常是用户删掉了这篇文档）。

    与普通失败区分开：取消不算错误，不该把文档标成 failed。
    """


@dataclass
class ImportProgress:
    doc_id: str
    kb_id: str = ""
    status: str = "pending"  # pending | running | done | failed
    stage: str = ""  # text | image
    done: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "kb_id": self.kb_id,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            **self.extra,
        }


def set_progress(doc_id: str, **fields: Any) -> None:
    with _LOCK:
        current = _PROGRESS.get(doc_id)
        if current is None:
            current = ImportProgress(doc_id=doc_id)
            _PROGRESS[doc_id] = current
        for key, value in fields.items():
            if hasattr(current, key):
                setattr(current, key, value)


def get_progress(doc_id: str) -> dict[str, Any] | None:
    with _LOCK:
        current = _PROGRESS.get(doc_id)
        return current.as_dict() if current else None


def clear_progress(doc_id: str) -> None:
    with _LOCK:
        _PROGRESS.pop(doc_id, None)


def cancel(doc_id: str) -> None:
    """请求取消这篇文档的导入任务。

    必须在文档被删除时调用。否则后台线程会继续跑，把已经删掉的向量和图片
    又写回去——用户看到的是"删了又冒出来"。
    """

    with _LOCK:
        event = _CANCEL_EVENTS.get(doc_id)
        if event is not None:
            event.set()
        current = _PROGRESS.get(doc_id)
        if current is not None and current.status in {"pending", "running"}:
            current.status = "cancelled"
            current.message = "已取消"


def is_cancelled(doc_id: str) -> bool:
    with _LOCK:
        event = _CANCEL_EVENTS.get(doc_id)
        return event.is_set() if event is not None else False


def spawn_import(kb_id: str, doc_id: str, filename: str, content: bytes) -> None:
    """起一个后台线程跑导入。

    刻意用裸线程而不是线程池：导入是低频操作（用户手动上传），但每本书要跑
    几十秒，用默认大小的池可能被长任务占满。裸线程 + daemon 保证进程退出时
    不被卡住。
    """

    from familiar.knowledge import service

    set_progress(doc_id, kb_id=kb_id, status="running", message="正在解析")

    cancel_event = threading.Event()
    with _LOCK:
        _CANCEL_EVENTS[doc_id] = cancel_event

    def _run() -> None:
        def _hook(stage: str, done: int, total: int) -> None:
            label = "正在切分正文" if stage == "text" else "正在处理插图"
            set_progress(
                doc_id,
                stage=stage,
                done=done,
                total=total,
                message=f"{label} {done}/{total}" if total else label,
            )

        try:
            service.run_import(
                kb_id, doc_id, filename, content, progress=_hook, cancel_event=cancel_event
            )
            set_progress(doc_id, status="done", message="已完成", done=1, total=1)
        except ImportCancelled:
            logger.info("导入已取消：kb=%s doc=%s", kb_id, doc_id)
            set_progress(doc_id, status="cancelled", message="已取消")
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须落到进度里
            logger.exception("导入失败：kb=%s doc=%s", kb_id, doc_id)
            set_progress(
                doc_id,
                status="failed",
                message="导入失败",
                error=str(exc)[:500],
                extra={"traceback": traceback.format_exc()[-1500:]},
            )
        finally:
            with _LOCK:
                _CANCEL_EVENTS.pop(doc_id, None)

    thread = threading.Thread(target=_run, name=f"knowledge-import-{doc_id}", daemon=True)
    thread.start()
