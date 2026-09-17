# -*- coding: utf-8 -*-
"""领域实体：业务表的不可变值对象

实体字段与业务表列一一对应；时间字段为 UTC ISO-8601 文本，
状态为 StrEnum（序列化即字符串）。实体不可变，状态变更通过
Repository 方法写库后重新读取。
"""
from dataclasses import dataclass
from enum import StrEnum


class KnowledgeBaseStatus(StrEnum):
    """知识库生命周期状态"""

    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


class DocumentStatus(StrEnum):
    """文档生命周期状态"""

    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    UPDATING = "updating"
    DELETING = "deleting"
    FAILED = "failed"
    DELETED = "deleted"


class IndexVersionStatus(StrEnum):
    """索引版本生命周期状态：staging -> validating -> active -> retired/failed"""

    STAGING = "staging"
    VALIDATING = "validating"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeBase:
    """知识库"""

    id: str
    name: str
    description: str | None
    status: KnowledgeBaseStatus
    deleted_at: str | None
    delete_requested_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Document:
    """文档"""

    id: str
    knowledge_base_id: str
    display_name: str
    source_sha256: str
    status: DocumentStatus
    active_document_version_id: str | None
    deleted_at: str | None
    delete_requested_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentVersion:
    """文档版本"""

    id: str
    document_id: str
    version_no: int
    source_path: str
    source_sha256: str
    mime_type: str | None
    size_bytes: int | None
    parser_mode: str | None
    parser_provider: str | None
    parser_version: str | None
    parsed_content_sha256: str | None
    status: str
    active_index_version_id: str | None
    created_at: str
    activated_at: str | None


@dataclass(frozen=True)
class IndexVersion:
    """索引版本"""

    id: str
    document_version_id: str
    index_no: int
    status: IndexVersionStatus
    parser_config_id: str | None
    chunking_config_id: str | None
    embedding_profile_id: str | None
    vector_collection: str | None
    fts_namespace: str | None
    chunk_count: int | None
    integrity_hash: str | None
    created_at: str
    activated_at: str | None
    retired_at: str | None
