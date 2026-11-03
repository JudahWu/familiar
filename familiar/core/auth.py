"""JWT 会话鉴权与用户身份解析。

设计约定（与前端现有契约保持一致）：
- 登录后把 JWT 写入 httpOnly Cookie（`osw_session`），浏览器请求自动携带，
  EventSource/SSE 也能复用同一套会话，不需要前端管理 token；
- `GET /dashboard/api/auth/dev-login` 供本地开发一键登录（需显式开启）；
- `POST /dashboard/api/auth/login` 用邮箱 + 密码登录，用户来自环境变量配置；
- 管理员由 `DASHBOARD_ADMINS` 指定；未配置时所有已登录用户都视为管理员
  （单用户工具的默认值，并在启动日志中提示）。

线程归属（thread owner）：
- 创建 thread 时记录当前用户邮箱；
- 非管理员只能访问自己的 thread；`owner` 为 NULL 的历史数据仅管理员可见。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from familiar.env_utils import get_env

logger = logging.getLogger("familiar.auth")

ALGORITHM = "HS256"
SESSION_COOKIE_NAME = "osw_session"
TOKEN_TTL_HOURS = 12
PBKDF2_ITERATIONS = 200_000


@dataclass(frozen=True)
class CurrentUser:
    """当前请求的登录用户。"""

    email: str
    is_admin: bool


# ---------------------------------------------------------------
# 开关与密钥
# ---------------------------------------------------------------

def auth_enabled() -> bool:
    """是否强制登录。默认开启；本地开发可通过 AUTH_ENABLED=false 临时关闭。"""

    return get_env("AUTH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def dev_login_enabled() -> bool:
    """是否开放一键 dev-login（仅限本地开发，生产必须关闭）。

    除显式的 0/false/no 外，任何非空值都视为开启，
    兼容历史上 `DASHBOARD_DEV_LOGIN=admin` 这类写法。
    """

    value = get_env("DASHBOARD_DEV_LOGIN", "").strip().lower()
    return bool(value) and value not in {"0", "false", "no"}


_JWT_SECRET: str | None = None


def _jwt_secret() -> str:
    """读取 JWT 签名密钥。

    未配置 DASHBOARD_JWT_SECRET 时生成进程级随机密钥：本地开发可以直接跑，
    但重启后所有会话失效。生产环境必须在 .env 中显式配置固定值。
    """

    global _JWT_SECRET
    if _JWT_SECRET is None:
        configured = get_env("DASHBOARD_JWT_SECRET").strip()
        if configured:
            _JWT_SECRET = configured
        else:
            _JWT_SECRET = secrets.token_urlsafe(48)
            logger.warning(
                "DASHBOARD_JWT_SECRET 未配置，已生成进程级随机密钥；服务重启后所有会话失效。"
                "生产环境请在 .env 中显式配置。"
            )
    return _JWT_SECRET


# ---------------------------------------------------------------
# 密码哈希（PBKDF2-SHA256，标准库实现，避免额外依赖 bcrypt）
# ---------------------------------------------------------------

def hash_password(password: str, *, salt: str | None = None) -> str:
    """生成 `pbkdf2_sha256$iterations$salt$digest` 格式的密码哈希。"""

    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    """校验密码是否匹配哈希值。任何格式异常都按校验失败处理。"""

    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------
# 用户注册表（环境变量驱动）
# ---------------------------------------------------------------

def _admin_emails() -> set[str]:
    # DASHBOARD_ADMINS 优先，兼容旧配置名 CONFIGURED_ADMINS。
    raw = get_env("DASHBOARD_ADMINS", "").strip() or get_env("CONFIGURED_ADMINS", "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


_ADMIN_WARNING_EMITTED = False


def is_admin_email(email: str) -> bool:
    """判断邮箱是否为管理员。

    DASHBOARD_ADMINS 未配置时，所有已登录用户都视为管理员（单用户默认），
    并在首次判断时输出一次提示。
    """

    global _ADMIN_WARNING_EMITTED
    admins = _admin_emails()
    if not admins:
        if not _ADMIN_WARNING_EMITTED:
            _ADMIN_WARNING_EMITTED = True
            logger.warning(
                "DASHBOARD_ADMINS 未配置：当前所有已登录用户均按管理员处理。"
                "多用户部署请在 .env 中显式指定管理员邮箱。"
            )
        return True
    return email.strip().lower() in admins


def _configured_users() -> dict[str, str]:
    """解析 DASHBOARD_USERS（email:hash,email:hash）为邮箱->哈希字典。"""

    users: dict[str, str] = {}
    for item in get_env("DASHBOARD_USERS", "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        email, encoded = item.split(":", 1)
        email = email.strip().lower()
        if email and encoded.strip():
            users[email] = encoded.strip()

    # dev 一键登录账号：同时允许通过 /auth/login 用密码登录，方便调试。
    if dev_login_enabled():
        dev_email = get_env("DASHBOARD_DEV_EMAIL", "").strip().lower()
        dev_password = get_env("DASHBOARD_DEV_PASSWORD", "").strip()
        if dev_email and dev_password and dev_email not in users:
            users[dev_email] = hash_password(dev_password)
    return users


def authenticate(email: str, password: str) -> CurrentUser | None:
    """校验邮箱 + 密码，成功返回当前用户，失败返回 None。"""

    email = (email or "").strip().lower()
    if not email or not password:
        return None
    for user_email, encoded in _configured_users().items():
        if hmac.compare_digest(user_email, email) and verify_password(password, encoded):
            return CurrentUser(email=email, is_admin=is_admin_email(email))
    return None


# ---------------------------------------------------------------
# JWT 签发与校验
# ---------------------------------------------------------------

def create_session_token(user: CurrentUser) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user.email,
        "admin": user.is_admin,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_TTL_HOURS),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=ALGORITHM)


def decode_session_token(token: str) -> CurrentUser | None:
    """解析会话 JWT；过期或非法时返回 None（由调用方决定 401）。"""

    try:
        payload: dict[str, Any] = jwt.decode(token, _jwt_secret(), algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    email = str(payload.get("sub") or "").strip()
    if not email:
        return None
    return CurrentUser(email=email, is_admin=bool(payload.get("admin")))


# ---------------------------------------------------------------
# Cookie 读写
# ---------------------------------------------------------------

def _cookie_secure() -> bool:
    return get_env("SESSION_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}


def set_session_cookie(response: JSONResponse, user: CurrentUser) -> None:
    """把会话 JWT 写入 httpOnly Cookie。"""

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(user),
        max_age=TOKEN_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


# ---------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------

async def get_current_user(request: Request) -> CurrentUser:
    """从会话 Cookie 解析当前用户。

    AUTH_ENABLED=false 时返回默认开发管理员，保证本地无配置也能跑；
    开启鉴权时缺少会话或会话无效一律 401。
    """

    if not auth_enabled():
        fallback = get_env("DASHBOARD_DEV_EMAIL", "dev@local").strip() or "dev@local"
        return CurrentUser(email=fallback, is_admin=True)

    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = decode_session_token(token) if token else None
    if user is None:
        return _unauthorized()
    return user


def _unauthorized() -> CurrentUser:
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或会话已过期，请重新登录",
    )


def can_access_thread(user: CurrentUser, thread: dict[str, Any] | None) -> bool:
    """判断用户是否可以访问指定 thread。

    管理员可以访问全部；非管理员只能访问 owner 为自己的 thread；
    owner 为 NULL 的历史 thread 仅管理员可见。
    """

    if thread is None:
        return False
    if user.is_admin:
        return True
    owner = thread.get("owner")
    return owner is not None and str(owner) == user.email
