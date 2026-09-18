# -*- coding: utf-8 -*-
"""Repository Port：持久化操作的领域接口

Port 由领域层定义、基础设施层实现（SQLite Adapter）。
方法签名以领域实体为边界，实现不得泄漏 SQL 细节或供应商类型。
未在此定义的读写能力（如内容表的批量摄取）随对应里程碑补充。
"""
from abc import ABC, abstractmethod

from .entities import (
    Document,
    DocumentVersion,
    IndexVersion,
    KnowledgeBase,
    Task,
    TaskEvent,
    TaskStage,
    TaskStatus,
)


class KnowledgeBaseRepository(ABC):
    """知识库仓储"""

    @abstractmethod
    def create(self, name: str, description: str | None = None) -> KnowledgeBase:
        """创建知识库；活动名称冲突时抛 DuplicateActiveNameError"""

    @abstractmethod
    def get(self, kb_id: str) -> KnowledgeBase | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def find_active_by_name(self, name: str) -> KnowledgeBase | None:
        """按名称查找活动记录；不存在时返回 None"""

    @abstractmethod
    def list_active(self) -> list[KnowledgeBase]:
        """列出全部活动知识库（按创建时间倒序）"""

    @abstractmethod
    def soft_delete(self, kb_id: str) -> None:
        """软删除：写入 deleted_at/delete_requested_at 并置状态为 deleted；
        目标不存在时抛 EntityNotFoundError"""


class DocumentRepository(ABC):
    """文档仓储"""

    @abstractmethod
    def create(self, kb_id: str, display_name: str, source_sha256: str) -> Document:
        """在知识库内创建文档；活动内容重复时抛 DuplicateActiveContentError；
        知识库不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def get(self, doc_id: str) -> Document | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def list_by_kb(self, kb_id: str, include_deleted: bool = False) -> list[Document]:
        """列出知识库内文档（按创建时间倒序；默认排除已软删除）"""

    @abstractmethod
    def soft_delete(self, doc_id: str) -> None:
        """软删除：写入 deleted_at/delete_requested_at 并置状态为 deleted；
        目标不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def set_active_version(self, doc_id: str, version_id: str) -> None:
        """回填活动版本指针并刷新 updated_at；目标不存在时抛 EntityNotFoundError"""


class DocumentVersionRepository(ABC):
    """文档版本仓储"""

    @abstractmethod
    def create(
        self,
        document_id: str,
        source_path: str,
        source_sha256: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        parser_mode: str | None = None,
        parser_provider: str | None = None,
        parser_version: str | None = None,
        parsed_content_sha256: str | None = None,
    ) -> DocumentVersion:
        """创建新版本：version_no 在文档内自动递增；
        文档不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def get(self, version_id: str) -> DocumentVersion | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def list_by_document(self, document_id: str) -> list[DocumentVersion]:
        """列出文档全部版本（按版本号升序）"""


class IndexVersionRepository(ABC):
    """索引版本仓储"""

    @abstractmethod
    def create(
        self,
        document_version_id: str,
        vector_collection: str | None = None,
        fts_namespace: str | None = None,
    ) -> IndexVersion:
        """创建索引版本（staging）；版本不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def get(self, index_id: str) -> IndexVersion | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def list_by_document_version(self, document_version_id: str) -> list[IndexVersion]:
        """列出文档版本的全部索引版本（按索引号升序）"""

    @abstractmethod
    def activate(self, index_id: str) -> IndexVersion:
        """激活事务：同文档版本的既有 active 索引退役、目标置为 active、
        并把 document_versions.active_index_version_id 指向目标，
        全过程单事务且校验双向一致。失败场景：
        目标不存在抛 EntityNotFoundError；状态为 retired/failed 抛
        ActivationError；事务内任一步失败整体回滚。"""


class TaskRepository(ABC):
    """任务仓储"""

    @abstractmethod
    def create(
        self,
        task_type: str,
        *,
        knowledge_base_id: str | None = None,
        document_id: str | None = None,
        document_version_id: str | None = None,
        index_version_id: str | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        input_json: str | None = None,
        max_retries: int = 3,
        parent_task_id: str | None = None,
        retry_origin: str | None = None,
    ) -> Task:
        """创建任务（初始 queued）：同一事务内完成幂等键命中检查、
        队列容量检查、插入与创建事件，保证容量与写入原子。
        pending 合计或非终态合计达到上限时抛 TaskQueueFullError；
        幂等键已存在时直接返回既有任务，不重复创建。"""

    @abstractmethod
    def get(self, task_id: str) -> Task | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def transition(
        self,
        task_id: str,
        target_state: TaskStatus,
        stage: TaskStage | None = None,
    ) -> Task:
        """状态迁移：仅允许状态机定义的迁移；非法迁移写审计事件后抛
        TaskStateConflictError。进入 running 首次回填 started_at，
        进入 cancel_requested 时回填 cancel_requested_at，
        进入终态时回填 finished_at。stage 提供时随迁移同批更新，
        未提供时保持原值。目标不存在抛 EntityNotFoundError。"""

    @abstractmethod
    def list_events(self, task_id: str) -> list[TaskEvent]:
        """按写入顺序读取任务的全部审计事件"""

    @abstractmethod
    def claim_next(
        self, worker_id: str, task_type: str | None = None
    ) -> Task | None:
        """领取下一个可执行任务：单事务内按有效租约计数（running 与
        cancel_requested 中租约未过期者）少于执行上限时，选取 priority
        降序、created_at 升序的首个 queued 任务，写入租约与执行计数并
        置为 running；无排队任务或无空闲执行许可时返回 None"""

    @abstractmethod
    def heartbeat(self, task_id: str, worker_id: str) -> Task:
        """Worker 心跳续约：持有者匹配且租约未过期时延长到期时间并
        刷新心跳时间；租约丢失抛 TaskLeaseLostError，
        任务不存在抛 EntityNotFoundError"""

    @abstractmethod
    def update_stage(
        self,
        task_id: str,
        worker_id: str,
        *,
        stage: TaskStage,
        progress: float | None = None,
        checkpoint_json: str | None = None,
    ) -> Task:
        """租约守卫的阶段推进写入：进入与当前不同的阶段时 stage_attempt
        置 1（新阶段开始执行），同阶段保持不变；progress 与 checkpoint
        提供时写入。租约丢失抛 TaskLeaseLostError"""

    @abstractmethod
    def complete_stage(
        self, task_id: str, worker_id: str, *, stage: TaskStage
    ) -> Task:
        """租约守卫的阶段完成：当前阶段与目标一致时 stage_attempt 归零，
        阶段推进由后续写入表达；阶段不匹配抛 TaskStateConflictError，
        租约丢失抛 TaskLeaseLostError"""
