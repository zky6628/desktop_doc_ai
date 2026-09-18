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


class TaskStatus(StrEnum):
    """任务生命周期状态：queued 起步，succeeded/failed/cancelled 为终态"""

    QUEUED = "queued"
    WAITING_USER = "waiting_user"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    RETRY_WAITING = "retry_waiting"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(StrEnum):
    """任务处理阶段：描述当前或最近执行到的管线环节，与生命周期分离"""

    VALIDATING = "validating"
    STORING_FILE = "storing_file"
    ROUTING_PARSER = "routing_parser"
    PARSING_LOCAL = "parsing_local"
    SUBMITTING_CLOUD = "submitting_cloud"
    POLLING_CLOUD = "polling_cloud"
    DOWNLOADING_CLOUD_RESULT = "downloading_cloud_result"
    NORMALIZING = "normalizing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    WRITING_VECTOR_INDEX = "writing_vector_index"
    WRITING_KEYWORD_INDEX = "writing_keyword_index"
    VALIDATING_INDEX = "validating_index"
    ACTIVATING_VERSION = "activating_version"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Task:
    """任务"""

    id: str
    task_type: str
    knowledge_base_id: str | None
    document_id: str | None
    document_version_id: str | None
    index_version_id: str | None
    state: TaskStatus
    stage: TaskStage | None
    progress: float
    priority: int
    idempotency_key: str | None
    retry_count: int
    max_retries: int
    attempt_count: int
    stage_attempt: int
    total_attempt_count: int
    next_retry_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    cancel_requested_at: str | None
    checkpoint_json: str | None
    parent_task_id: str | None
    retry_origin: str | None
    error_code: str | None
    error_message: str | None
    input_json: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class TaskEvent:
    """任务审计事件（按写入顺序读取）"""

    id: str
    task_id: str
    event_type: str
    state: TaskStatus
    stage: TaskStage | None
    attempt_count: int
    worker: str | None
    created_at: str
    duration_ms: int | None
    checkpoint_json: str | None
    error_code: str | None
    detail_json: str | None


@dataclass(frozen=True)
class ExternalTask:
    """外部任务：云端解析供应方侧的提交与轮询事实

    稳定唯一键为 (provider, provider_batch_ref, source_ref)；
    provider_task_id 只是可空观测值，不作为恢复前提
    """

    id: str
    task_id: str
    provider: str
    provider_batch_ref: str
    source_ref: str
    provider_task_id: str | None
    upload_url_expires_at: str | None
    remote_cancel_state: str | None
    provider_status_summary: str | None
    state: str | None
    poll_count: int
    last_polled_at: str | None
    request_summary_json: str | None
    result_uri: str | None
    result_sha256: str | None
    expires_at: str | None
