"""知识库原始文件的对象存储（MinIO / S3 兼容接口）。

为什么不再往工作区目录里写 PDF：
- 工作区是 Agent 的读写区，`rm -rf`、git clean、换工作区都可能把它连带清掉；
- 对象存储的生命周期独立，将来换成云上的 OSS/S3 只改配置、不动代码；
- 配了校验和，能认出"同一个 PDF 传了两次"，省一次解析。

存的是 **key**（`<kb_id>/<doc_id>/source.pdf`）而不是完整 URL ——
endpoint 会随环境变（本机 localhost、内网 IP、线上域名），把 URL 写进数据库
等于把环境写死在数据里。取文件时用 endpoint + bucket + key 现拼。

一个刻意的取舍：**不在这里做重试**。MinIO 挂了就是挂了，让导入直接失败并
把原因写进文档的 error 字段，比静默重试三次再失败好排查。
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any

from familiar.core.settings import S3_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT, S3_SECRET_KEY

logger = logging.getLogger("familiar.knowledge.objects")

_LOCK = threading.RLock()  # 必须可重入：ensure_bucket() 持锁期间会调 client()
_CLIENT: Any = None
_BUCKET_READY = False


class ObjectStorageError(RuntimeError):
    """对象存储不可用。消息里带上了 endpoint，方便判断是配置错还是服务没起。"""


def _endpoint_and_secure() -> tuple[str, bool]:
    """minio SDK 要的是 `host:port` 加一个 secure 布尔，不能直接吃 URL。"""

    raw = S3_ENDPOINT
    if raw.startswith("https://"):
        return raw[len("https://") :].rstrip("/"), True
    if raw.startswith("http://"):
        return raw[len("http://") :].rstrip("/"), False
    return raw.rstrip("/"), False


def client() -> Any:
    """惰性创建并复用客户端。

    Minio 实例内部是 urllib3 连接池，线程安全，所以整个进程共用一个。
    """

    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _LOCK:
        if _CLIENT is None:
            import urllib3
            from minio import Minio

            endpoint, secure = _endpoint_and_secure()
            # 必须自己配超时：minio SDK 默认 timeout=None，服务端一旦不响应就
            # 永久挂着——表现是上传请求迟迟不返回、日志里却没有任何错误。
            # 这个坑真实踩过，排查了很久才定位到"没有 read timeout"。
            http = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=5.0, read=120.0),
                maxsize=8,
                retries=urllib3.Retry(
                    total=3,
                    backoff_factor=0.3,
                    status_forcelist=[500, 502, 503, 504],
                ),
            )
            _CLIENT = Minio(
                endpoint,
                access_key=S3_ACCESS_KEY,
                secret_key=S3_SECRET_KEY,
                secure=secure,
                http_client=http,
            )
            logger.info("对象存储已连接：%s（bucket=%s）", endpoint, S3_BUCKET)
    return _CLIENT


def ensure_bucket() -> None:
    """首次使用时建桶。桶已存在不算错，所以只用 exists 判断，不靠异常。"""

    global _BUCKET_READY
    if _BUCKET_READY:
        return
    with _LOCK:
        if _BUCKET_READY:
            return
        try:
            c = client()
            if not c.bucket_exists(S3_BUCKET):
                c.make_bucket(S3_BUCKET)
                logger.info("已创建对象存储桶：%s", S3_BUCKET)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStorageError(
                f"无法连接对象存储（{S3_ENDPOINT}）：{type(exc).__name__}: {exc}"
            ) from exc
        _BUCKET_READY = True


def document_key(kb_id: str, doc_id: str, filename: str = "source.pdf") -> str:
    """对象在桶里的路径。带 kb_id 一层，删库时按前缀清干净。"""

    return f"{kb_id}/{doc_id}/{filename}"


def put_document(key: str, data: bytes, content_type: str = "application/pdf") -> None:
    ensure_bucket()
    try:
        client().put_object(
            S3_BUCKET,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
    except Exception as exc:  # noqa: BLE001
        raise ObjectStorageError(
            f"写入对象存储失败（{S3_ENDPOINT}/{S3_BUCKET}/{key}）：{type(exc).__name__}: {exc}"
        ) from exc


def get_document(key: str) -> bytes:
    ensure_bucket()
    try:
        response = client().get_object(S3_BUCKET, key)
    except Exception as exc:  # noqa: BLE001
        raise ObjectStorageError(f"读取对象失败（{key}）：{type(exc).__name__}: {exc}") from exc
    try:
        return response.read()
    finally:
        # 不 close + release_conn 会让连接池里的连接一直挂着，重复几次就耗尽
        response.close()
        response.release_conn()


def delete_document(key: str) -> None:
    ensure_bucket()
    client().remove_object(S3_BUCKET, key)


def delete_prefix(prefix: str) -> int:
    """删掉某个前缀下的所有对象，返回删除数量。

    删库/删文档用。**尽力而为**：单个对象删失败只记日志不抛错，
    因为此时 PostgreSQL 里的行还没删，抛出会把整个删除流程卡在半路，
    留下"库里还有、对象没了"以外的更糟状态（元信息删不掉、用户重试也没用）。
    残留的孤儿对象不影响使用，以后写个扫描脚本清就行。
    """

    ensure_bucket()
    deleted = 0
    try:
        for obj in client().list_objects(S3_BUCKET, prefix=prefix, recursive=True):
            try:
                client().remove_object(S3_BUCKET, obj.object_name)
                deleted += 1
            except Exception:  # noqa: BLE001
                logger.warning("删除对象失败，留下孤儿：%s", obj.object_name, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("列举前缀失败，跳过对象清理：%s", prefix, exc_info=True)
    return deleted


def status() -> dict[str, Any]:
    """给健康检查/排查用的轻量探测。不建桶，只测端点可达。"""

    endpoint, secure = _endpoint_and_secure()
    info: dict[str, Any] = {
        "endpoint": f"{'https' if secure else 'http'}://{endpoint}",
        "bucket": S3_BUCKET,
        "ready": _BUCKET_READY,
    }
    try:
        info["bucket_exists"] = client().bucket_exists(S3_BUCKET)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
