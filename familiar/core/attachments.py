"""用户上传附件的落地与安全校验。

前端把文件以 base64 随消息提交，这里负责：
- 数量 / 大小 / 文件名安全校验；
- 拒绝明显是密钥的文件（.env、私钥、证书等）；
- 落盘到工作区 `tmp/uploads/<thread_id>/`，返回 Agent 可读的虚拟路径。

虚拟路径与 `LocalShellBackend` 的映射一致：`/tmp` 指向工作区下的 tmp 目录，
所以 Agent 用文件工具直接读 `/tmp/uploads/<thread_id>/<name>` 就能拿到内容。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("familiar.attachments")

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_COUNT = 5
MAX_TOTAL_BYTES = 50 * 1024 * 1024

FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    ".npmrc",
    ".pypirc",
    ".netrc",
}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

_SAFE_NAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class AttachmentRejected(ValueError):
    """附件被拒绝（数量、大小或安全策略）。"""


@dataclass(frozen=True)
class SavedAttachment:
    name: str
    path: str
    virtual_path: str
    mime_type: str
    size: int
    is_image: bool


def _safe_filename(name: str) -> str:
    basename = Path(name.replace("\\", "/")).name.strip()
    # \w 在 UNICODE 模式下保留中日韩文字，避免中文名被洗成空串。
    basename = _SAFE_NAME_RE.sub("_", basename).lstrip(".")
    return basename[:80] or "file"


def attachments_to_upload_parts(saved: list[SavedAttachment]) -> tuple[list[str], list[str]]:
    """把保存结果转成 (虚拟路径列表, 图片 data URL 列表)。

    图片走多模态 image_url；其他文件只给路径，让 Agent 用文件工具读。
    """

    paths = [item.virtual_path for item in saved]
    image_urls: list[str] = []
    for item in saved:
        if not item.is_image:
            continue
        raw = Path(item.path).read_bytes()
        image_urls.append(f"data:{item.mime_type};base64,{base64.b64encode(raw).decode('ascii')}")
    return paths, image_urls


def _is_forbidden(name: str) -> bool:
    lowered = name.lower()
    if lowered in FORBIDDEN_NAMES:
        return True
    return any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)


def _decode_base64(payload: Any) -> bytes:
    if not isinstance(payload, str) or not payload.strip():
        raise AttachmentRejected("附件内容为空")
    try:
        # 容忍 data URL 前缀，前端两种格式都可能出现。
        if payload.startswith("data:"):
            payload = payload.split(",", 1)[-1]
        return base64.b64decode(payload.strip(), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentRejected("附件内容不是合法的 base64 数据") from exc


def save_attachments(
    *,
    thread_id: str,
    workspace_root: Path,
    attachments: list[dict[str, Any]] | None,
) -> list[SavedAttachment]:
    """把提交过来的附件落盘，返回保存结果列表。

    任何一条不合法都会整体拒绝（fail-fast），避免半成功状态让前端误判。
    """

    if not attachments:
        return []
    if len(attachments) > MAX_FILE_COUNT:
        raise AttachmentRejected(f"附件最多 {MAX_FILE_COUNT} 个，当前 {len(attachments)} 个")

    upload_dir = Path(workspace_root) / "tmp" / "uploads" / thread_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved: list[SavedAttachment] = []
    total = 0
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise AttachmentRejected(f"第 {index + 1} 个附件格式不正确")
        name = str(item.get("name") or "").strip()
        if not name:
            raise AttachmentRejected(f"第 {index + 1} 个附件缺少文件名")
        if _is_forbidden(name):
            raise AttachmentRejected(f"出于安全考虑，拒绝接收「{name}」这类可能包含密钥的文件")
        mime_type = str(item.get("mimeType") or "application/octet-stream")
        declared_size = item.get("size")
        if isinstance(declared_size, int) and declared_size > MAX_FILE_BYTES:
            raise AttachmentRejected(f"「{name}」超过单文件 20MB 上限")

        content = _decode_base64(item.get("base64"))
        if len(content) > MAX_FILE_BYTES:
            raise AttachmentRejected(f"「{name}」超过单文件 20MB 上限")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentRejected("附件总大小超过 50MB 上限")

        safe_name = f"{uuid.uuid4().hex[:8]}_{_safe_filename(name)}"
        target = upload_dir / safe_name
        target.write_bytes(content)
        saved.append(
            SavedAttachment(
                name=name,
                path=str(target),
                virtual_path=f"/tmp/uploads/{thread_id}/{safe_name}",
                mime_type=mime_type,
                size=len(content),
                is_image=mime_type.startswith("image/"),
            )
        )
        logger.info("附件已落地：thread_id=%s name=%s size=%s", thread_id, name, len(content))

    return saved
