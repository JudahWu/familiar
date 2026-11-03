"""知识库/文档的元信息（PostgreSQL）。

为什么从 JSON 文件搬到数据库：文件式元信息在单进程单机下够用，但它做不到
"改一条状态而不重写整个文件"——第三章的逐页进度会每页都写一次，文件方案
每次都重写整份 JSON，并发下必丢更新。数据库里一条 UPDATE 就够。

分工（重要）：
- PostgreSQL 是**唯一事实来源**：库里有什么、到哪一步了；
- MinIO 只存原始字节（`objects.py`），Milvus 只存能搜到的段落。
  后两者都是派生物，删的时候先删它们、最后删这里的行。

表结构见 `Base` 下的两个 ORM 类。对外仍然返回 dataclass
（`BaseMeta` / `DocumentMeta`），这样 service 与接口层的代码不用跟着改。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from familiar.core.settings import DATABASE_URL
from familiar.knowledge import objects

logger = logging.getLogger("familiar.knowledge.store")

DocStatus = Literal["pending", "parsing", "indexed", "failed"]

_LOCK = threading.RLock()
_ENGINE: Any = None
_SESSION_FACTORY: Any = None


class Base(DeclarativeBase):
    pass


class KnowledgeBaseRow(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 预留多用户：现在填登录邮箱，单用户场景下就是一串固定值。
    # 用空串而不是 NULL，因为 UNIQUE(owner, name) 遇到 NULL 不生效
    # （PostgreSQL 里 NULL != NULL），那样重名校验会失效。
    owner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    documents: Mapped[list["KnowledgeDocumentRow"]] = relationship(
        back_populates="base",
        cascade="all, delete-orphan",
        order_by="KnowledgeDocumentRow.created_at",
    )

    __table_args__ = (UniqueConstraint("owner", "name", name="uq_knowledge_bases_owner_name"),)


class KnowledgeDocumentRow(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kb_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)

    # 对象存储位置。存 key 不存 URL：endpoint 会随环境变（本机/内网/线上），
    # 把 URL 存进库等于把环境写死在数据里。
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 同一个 PDF 传两次能认出来，不用再解析一遍；也用来校验对象没坏。
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vision_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 成功入库的段落数。冗余存一份，列表页不用每次都去问 Milvus。
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    base: Mapped[KnowledgeBaseRow] = relationship(back_populates="documents")

    __table_args__ = (
        # 用 CHECK 不用 PG 的 ENUM：③ 很可能多出 pending_vision 之类的新状态，
        # 改代码里一行字符串比 ALTER TYPE 省事。
        CheckConstraint(
            "status IN ('pending', 'parsing', 'indexed', 'failed')",
            name="ck_knowledge_documents_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_knowledge_documents_progress"),
        Index("ix_knowledge_documents_kb_id", "kb_id"),
        Index("ix_knowledge_documents_kb_status", "kb_id", "status"),
    )


@dataclass
class DocumentMeta:
    id: str
    title: str
    filename: str
    kb_id: str = ""
    object_key: str = ""
    size_bytes: int = 0
    checksum: str = ""
    pages: int = 0
    status: DocStatus = "pending"
    progress: int = 0
    paragraphs: int = 0
    images: int = 0
    text_pages: int = 0
    vision_pages: int = 0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class BaseMeta:
    id: str
    name: str
    owner: str = ""
    created_at: str = ""
    documents: list[DocumentMeta] = field(default_factory=list)


# dataclass 字段名 → 数据库列名。对外沿用旧名字（paragraphs/images/checksum），
# 库里用更准确的名字（chunk_count/image_count/checksum_sha256）。
_FIELD_TO_COLUMN = {
    "paragraphs": "chunk_count",
    "images": "image_count",
    "checksum": "checksum_sha256",
}


def _normalize_db_url(url: str) -> str:
    """SQLAlchemy 的 `postgresql://` 会去找 psycopg2，而项目依赖的是 psycopg 3。

    与 `app/store/postgres_store.py` 里的同名逻辑一致；没有抽成公共函数是因为
    它只有 5 行，而跨模块共用会把 knowledge 和业务 store 绑在一起。
    """

    url = (url or "").strip()
    if not url:
        raise RuntimeError("知识库需要 PostgreSQL：请先在 .env 里配置 DATABASE_URL")
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _session_factory() -> Any:
    """惰性建引擎 + 建表。

    表是启动时 create_all 建的，没有迁移工具——这个项目的业务表也是这么做的
    （见 postgres_store），保持一致；表结构一旦要改字段类型，得手动 ALTER。
    """

    global _ENGINE, _SESSION_FACTORY
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY
    with _LOCK:
        if _SESSION_FACTORY is None:
            _ENGINE = create_engine(_normalize_db_url(DATABASE_URL), pool_pre_ping=True, future=True)
            Base.metadata.create_all(_ENGINE)
            _SESSION_FACTORY = sessionmaker(bind=_ENGINE, future=True)
    return _SESSION_FACTORY


def _session() -> Session:
    return _session_factory()()


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _slugify(name: str) -> str:
    """中文名转不成 slug，所以用前缀 + 短随机串，保证 id 唯一且不含特殊字符。

    id 保持短 slug 不换 UUID，因为 Milvus 里的 kb_id / doc_id 就是这些值，
    换 UUID 等于把已有的向量全部作废；而且 Attu 里看到 flink-c18f108d
    比看到一串 uuid 更容易认。
    """

    ascii_part = "".join(c if c.isalnum() and c.isascii() else "-" for c in name.lower())
    ascii_part = "-".join(part for part in ascii_part.split("-") if part)[:16]
    suffix = uuid.uuid4().hex[:8]
    return f"{ascii_part}-{suffix}" if ascii_part else f"kb-{suffix}"


def _doc_from_row(row: KnowledgeDocumentRow) -> DocumentMeta:
    return DocumentMeta(
        id=row.id,
        title=row.title,
        filename=row.filename,
        kb_id=row.kb_id,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        checksum=row.checksum_sha256 or "",
        pages=row.pages,
        status=row.status,  # type: ignore[arg-type]
        progress=row.progress,
        paragraphs=row.chunk_count,
        images=row.image_count,
        text_pages=row.text_pages,
        vision_pages=row.vision_pages,
        error=row.error,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _base_from_row(row: KnowledgeBaseRow) -> BaseMeta:
    return BaseMeta(
        id=row.id,
        name=row.name,
        owner=row.owner,
        created_at=_iso(row.created_at),
        documents=[_doc_from_row(doc) for doc in row.documents],
    )


def create_base(name: str, *, owner: str = "") -> BaseMeta:
    """新建知识库。同一 owner 下重名直接报错，避免界面上两个同名库难以分辨。"""

    clean = name.strip()
    if not clean:
        raise ValueError("知识库名称不能为空")
    with _session() as session:
        exists = (
            session.query(KnowledgeBaseRow)
            .filter(KnowledgeBaseRow.owner == owner, KnowledgeBaseRow.name == clean)
            .first()
        )
        if exists:
            raise ValueError(f"知识库「{clean}」已存在")
        row = KnowledgeBaseRow(id=_slugify(clean), name=clean, owner=owner)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _base_from_row(row)


def list_bases(*, owner: str | None = None) -> list[BaseMeta]:
    """列出知识库。owner 为 None 时不过滤（管理/调试用）。"""

    with _session() as session:
        query = session.query(KnowledgeBaseRow).order_by(KnowledgeBaseRow.created_at)
        if owner is not None:
            query = query.filter(KnowledgeBaseRow.owner == owner)
        return [_base_from_row(row) for row in query.all()]


def get_base(kb_id: str) -> BaseMeta:
    with _session() as session:
        row = session.get(KnowledgeBaseRow, kb_id)
        if row is None:
            raise FileNotFoundError(f"知识库不存在：{kb_id}")
        return _base_from_row(row)


def delete_base(kb_id: str) -> None:
    """只删 PostgreSQL 里的行（文档靠外键 CASCADE 一起走）。

    Milvus 与 MinIO 由 service 层在此之前清理 —— 顺序是刻意的：
    派生物先删、事实来源最后删，中途失败时还能从库里看到残留。
    """

    with _session() as session:
        row = session.get(KnowledgeBaseRow, kb_id)
        if row is None:
            raise FileNotFoundError(f"知识库不存在：{kb_id}")
        session.delete(row)
        session.commit()


def add_document(
    kb_id: str,
    *,
    filename: str,
    title: str | None = None,
    content: bytes | None = None,
    content_type: str = "application/pdf",
    checksum: str | None = None,
) -> DocumentMeta:
    """登记一篇待处理的文档，并把原件写进对象存储。

    doc_id 先定下来再算 object_key，所以 key 在写对象之前就是确定的，
    不会出现"写进去了但库里的 key 对不上"。
    """

    get_base(kb_id)  # 库不存在时早点报错，别等写完对象才发现
    doc_id = f"doc-{uuid.uuid4().hex[:10]}"
    key = objects.document_key(kb_id, doc_id)
    size = 0
    if content is not None:
        objects.put_document(key, content, content_type=content_type)
        size = len(content)

    with _session() as session:
        row = KnowledgeDocumentRow(
            id=doc_id,
            kb_id=kb_id,
            title=(title or filename.rsplit(".", 1)[0])[:500],
            filename=filename[:500],
            object_key=key,
            content_type=content_type,
            size_bytes=size,
            checksum_sha256=checksum or (_sha256(content) if content else None),
            status="pending",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _doc_from_row(row)


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def update_document(kb_id: str, doc_id: str, **fields: Any) -> DocumentMeta:
    with _session() as session:
        row = session.get(KnowledgeDocumentRow, doc_id)
        if row is None or row.kb_id != kb_id:
            raise FileNotFoundError(f"文档不存在：{doc_id}")
        for key, value in fields.items():
            column = _FIELD_TO_COLUMN.get(key, key)
            if not hasattr(row, column):
                raise AttributeError(f"未知字段：{key}")
            setattr(row, column, value)
        row.updated_at = datetime.now(UTC)
        if fields.get("status") == "indexed":
            row.indexed_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)
        return _doc_from_row(row)


def get_document(kb_id: str, doc_id: str) -> DocumentMeta:
    with _session() as session:
        row = session.get(KnowledgeDocumentRow, doc_id)
        if row is None or row.kb_id != kb_id:
            raise FileNotFoundError(f"文档不存在：{doc_id}")
        return _doc_from_row(row)


def find_document(doc_id: str) -> DocumentMeta:
    """按 doc_id 找文档（不限定知识库）。

    图片预览接口用：URL 里只有 doc_id，没有 kb_id。
    """

    with _session() as session:
        row = session.get(KnowledgeDocumentRow, doc_id)
        if row is None:
            raise FileNotFoundError(f"文档不存在：{doc_id}")
        return _doc_from_row(row)


def find_by_checksum(kb_id: str, checksum: str) -> DocumentMeta | None:
    """同一个库里有相同内容的文档就返回它，用来挡住重复导入。"""

    if not checksum:
        return None
    with _session() as session:
        row = (
            session.query(KnowledgeDocumentRow)
            .filter(
                KnowledgeDocumentRow.kb_id == kb_id,
                KnowledgeDocumentRow.checksum_sha256 == checksum,
                KnowledgeDocumentRow.status == "indexed",
            )
            .first()
        )
        return _doc_from_row(row) if row else None


def delete_document(kb_id: str, doc_id: str) -> None:
    with _session() as session:
        row = session.get(KnowledgeDocumentRow, doc_id)
        if row is None or row.kb_id != kb_id:
            raise FileNotFoundError(f"文档不存在：{doc_id}")
        session.delete(row)
        session.commit()


def chunk_total(kb_id: str) -> int:
    """库里所有文档的段落总和。列表页用它，不去问 Milvus。"""

    from sqlalchemy import func

    with _session() as session:
        total = (
            session.query(func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0))
            .filter(KnowledgeDocumentRow.kb_id == kb_id)
            .scalar()
        )
        return int(total or 0)
