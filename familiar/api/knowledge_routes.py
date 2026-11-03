"""知识库 HTTP 接口。

前缀是 `/dashboard/api/knowledge`，和仪表盘其它接口同一套鉴权（会话 Cookie）。

接口分三类：库与文档的管理、纯检索（`/search`，只返回命中段落，用于调试和 Agent
工具）、以及带生成的问答（`/answer`，检索 + 生成 + 自评）。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from familiar.core.auth import CurrentUser, get_current_user
from familiar.knowledge import answer as answer_service
from familiar.knowledge import chat, objects, service, store, tasks

logger = logging.getLogger("familiar.dashboard.knowledge")

knowledge_router = APIRouter(prefix="/dashboard/api/knowledge")


class CreateBaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class SearchRequest(BaseModel):
    kb_id: str
    # 图片查询时 query 可以留空
    query: str = ""
    top_k: int = Field(default=5, ge=1, le=20)
    # data URL 或裸 base64。用 JSON 传而不是 multipart，是因为它要跟 query 一起走同一个请求。
    image_base64: str | None = None


class AnswerRequest(BaseModel):
    kb_id: str
    # 图片查询时 query 可以留空
    query: str = ""
    image_base64: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    # 会话 id。传了就有记忆（读历史、改写追问、写回历史）；不传就是一次性问答。
    session_id: str | None = None


class ResumeRequest(BaseModel):
    """人工裁决。只接受 approve / rejected，**没有默认值**。

    RAG 项目里 `human_answer` 默认是 `rejected`（my_state.py:21），恢复时不写
    就等于一定去联网。这里要求显式传，避免"点了个继续就自动联网"。
    """

    session_id: str
    decision: str = Field(pattern="^(approve|rejected)$")


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


# 图片上限。截图一般 200KB 以内，8MB 已经是很宽松的上限；再大 base64 会撑爆请求体。
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _decode_image(data: str | None) -> bytes | None:
    """把 data URL 或裸 base64 解成字节。

    前端用 FileReader 读出来是 `data:image/png;base64,...`，也允许只传逗号后面那段，
    省得调用方自己切。
    """

    text = (data or "").strip()
    if not text:
        return None
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    # base64 膨胀约 4/3，先按长度挡一次，避免为了报错去解码一个几十兆的串
    if len(text) > _MAX_IMAGE_BYTES * 4 // 3 + 64:
        raise HTTPException(status_code=413, detail="图片太大，请控制在 8MB 以内")
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="图片数据不是合法的 base64") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    return raw


def _owner_filter(user: CurrentUser) -> str | None:
    """跟会话列表保持同一套多用户语义：管理员看全部，普通用户只看自己的。

    库里 owner 用空串而非 NULL 存未登录场景，所以过滤时不用担心 NULL 匹配问题。
    """

    return None if user.is_admin else user.email


@knowledge_router.get("/bases")
def list_bases(user: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    return service.list_bases(owner=_owner_filter(user))


@knowledge_router.post("/bases")
def create_base(
    body: CreateBaseRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        # 创建时一律记创建人，不跟着 _owner_filter 走——管理员建的库 owner
        # 也该是他自己，否则以后取消管理员权限就找不到自己的库了。
        return service.create_base(body.name, owner=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@knowledge_router.get("/bases/{kb_id}")
def get_base(kb_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return service.get_base_detail(kb_id)
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc


@knowledge_router.delete("/bases/{kb_id}")
def delete_base(kb_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    try:
        service.delete_base(kb_id)
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        # 库里还有文档时不允许删库
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@knowledge_router.post("/bases/{kb_id}/documents")
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """上传 PDF，**立刻返回**，解析在后台跑。

    一本书要跑几十秒（8 张插图实测 31 秒：每张要一次视觉描述 + 一次向量）。
    放在请求里同步做会被浏览器、nginx 甚至 natapp 隧道掐断，所以改成后台任务。

    返回的 status 是 "parsing"，用
    `GET /dashboard/api/knowledge/documents/{doc_id}/progress` 查进度。
    """

    content = await file.read()
    try:
        return service.start_import(kb_id, filename=file.filename or "upload.pdf", content=content)
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 登记阶段失败要给出明确原因
        logger.exception("上传失败：kb=%s file=%s", kb_id, file.filename)
        raise HTTPException(status_code=500, detail=f"上传失败：{exc}") from exc


@knowledge_router.get("/documents/{doc_id}/progress")
def import_progress(
    doc_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """查询一篇文档的导入进度。

    进度存在进程内存里（见 app/knowledge/tasks.py）。进程重启会丢，但重启时
    后台任务也一起断了，前端拿到 404 就重新上传。
    """

    progress = tasks.get_progress(doc_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="没有这个导入任务")
    return progress


@knowledge_router.get("/documents/{doc_id}/images/{name}")
def get_image(
    doc_id: str,
    name: str,
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """取一张插图。

    走后端代理而不是 MinIO 的预签名 URL：预签名里的 endpoint 是
    `localhost:9002`，用 natapp 域名访问时浏览器会指向用户自己机器的
    localhost，必然拿不到图。
    """

    safe_name = Path(name).name  # 防止 ../ 之类的路径穿越
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="图片名不合法")
    try:
        doc = store.find_document(doc_id)
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc

    key = f"{doc.kb_id}/{doc_id}/images/{safe_name}"
    try:
        data = objects.get_document(key)
    except objects.ObjectStorageError as exc:
        raise HTTPException(status_code=404, detail=f"取图失败：{exc}") from exc

    media_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(safe_name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return Response(content=data, media_type=media_type)


@knowledge_router.delete("/bases/{kb_id}/documents/{doc_id}")
def delete_document(
    kb_id: str,
    doc_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        service.delete_document(kb_id, doc_id)
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc
    return {"ok": True}


@knowledge_router.post("/search")
def search(body: SearchRequest, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    image = _decode_image(body.image_base64)
    if not (body.query or "").strip() and not image:
        raise HTTPException(status_code=400, detail="问题和图片至少要有一个")
    hits = service.search(body.kb_id, body.query, top_k=body.top_k, image_bytes=image)
    return {"query": body.query, "kb_id": body.kb_id, "image": bool(image), "hits": hits}


@knowledge_router.post("/answer")
def answer(body: AnswerRequest, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """基于检索结果生成答案，并给出自评分数。

    这是一个慢接口：改写 + 历史回忆 + 检索 + 生成 + 三指标评分，通常 8~25 秒。
    没有做流式，因为答案要先完整拿到才能算引用序号和自评分。

    返回里的 `interrupted=True` 表示**图停在了人工审批之前**：答案是"待确认"状态，
    前端要把两个按钮摆出来，用户选完调 `/answer/resume` 继续。这种时候这一问一答
    还没有落进历史，刷新会回到同一个待确认状态（状态在 checkpointer 里）。
    """

    if body.image_base64:
        # 先在入口校验（长度、base64 合法性），再交给图——图的状态要能放进
        # checkpointer，所以只能传字符串，不能传 bytes。
        _decode_image(body.image_base64)

    try:
        return answer_service.start(
            body.kb_id,
            body.query,
            top_k=body.top_k,
            session_id=body.session_id,
            owner=user.email,
            image_b64=body.image_base64 or "",
        )
    except FileNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 模型/网络错误要给出可读原因
        logger.exception("知识库问答失败：kb=%s", body.kb_id)
        raise HTTPException(status_code=502, detail=f"生成回答失败：{exc}") from exc


@knowledge_router.post("/answer/resume")
def resume_answer(
    body: ResumeRequest, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    """对低分答案做裁决，继续跑图。

    `approve` → 收尾；`rejected` → 走联网搜索重答（图里 `human_approval` 的另一条边）。
    """

    session_id = body.session_id
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    try:
        _assert_session_owner(session_id, user)
        return answer_service.resume(session_id, body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("继续问答失败：session=%s", session_id)
        raise HTTPException(status_code=502, detail=f"继续问答失败：{exc}") from exc


@knowledge_router.get("/answer/state")
def answer_state(
    session_id: str, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    """只读当前问答状态，不推进图。

    前端刷新后用它判断"这个会话是不是有一条待确认的答案"，是的话直接把按钮
    恢复出来，而不是让用户重新问一遍。
    """

    _assert_session_owner(session_id, user)
    payload = answer_service.snapshot(session_id)
    return payload or {"session_id": session_id, "answer": "", "interrupted": False}


def _assert_session_owner(session_id: str, user: CurrentUser) -> None:
    """会话归属校验。

    这里是**必须**的：图化之后 session_id 就是 thread_id，谁拿到它谁就能推进
    这个会话的中断。原来无状态问答时这个校验可以省（`chat.py` 的注释解释过），
    现在省不得。
    """

    owner = chat.session_owner(session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner and owner != user.email and not user.is_admin:
        raise HTTPException(status_code=403, detail="无权访问这个会话")


@knowledge_router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """恢复一个会话的最近若干轮。

    刷新页面后前端靠它把对话接上——包括答案里的引用，`citations` 存了命中段落快照，
    所以恢复出来的 [1][2] 仍然点得开。
    """

    turns = chat.list_turns(session_id)
    return {"session_id": session_id, "turns": [turn.to_payload() for turn in turns]}
