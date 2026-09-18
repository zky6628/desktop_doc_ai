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
    ExternalTask,
    IndexVersion,
    KnowledgeBase,
    Task,
    TaskEvent,
    TaskStage,
    TaskStatus,
)
from .ingest import ImportOutcome
from .parsing import ParsedDocument


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

    @abstractmethod
    def mark_parsed(
        self,
        version_id: str,
        *,
        parsed_content_sha256: str,
        parser_provider: str,
        parser_version: str,
    ) -> DocumentVersion:
        """回写解析结果：状态置为 parsed，记录解析内容哈希与解析器身份。

        解析内容哈希为统一解析模型即时复算的结构哈希；版本不存在抛
        EntityNotFoundError"""


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


class ContentRepository(ABC):
    """内容仓储：解析产物事实（内容块与表格证据）的持久化"""

    @abstractmethod
    def replace_document_content(
        self, document_version_id: str, parsed: ParsedDocument
    ) -> int:
        """把统一解析模型整体落库为该版本的解析事实（幂等重建）

        单事务内先删除该版本的表格证据与内容块，再按块序写入：
        同一版本重放不产生重复块。返回写入的块数。版本不存在抛
        EntityNotFoundError"""


class ImportRepository(ABC):
    """导入仓储：单事务建立文档、文档版本与导入任务

    任务队列容量检查与任务创建必须同事务：队列满时整体回滚，
    不得留下文档或版本残留。
    """

    @abstractmethod
    def create_import(
        self,
        kb_id: str,
        *,
        display_name: str,
        source_path: str,
        source_sha256: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        duplicate_policy: str = "skip",
        task_type: str = "import",
        parser_mode: str | None = None,
        parser_route_json: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImportOutcome:
        """在知识库内完成一次单文件导入

        重复内容按策略处理：skip 命中活动重复抛 DuplicateActiveContentError；
        new_version 复用活动文档并递增版本号。幂等键已存在时直接返回
        既有任务（不重复建立文档与版本）。知识库不存在抛
        EntityNotFoundError；队列容量不足抛 TaskQueueFullError 且
        无任何残留。"""


class ExternalTaskRepository(ABC):
    """外部任务仓储：云端批次关联与轮询事实的持久化

    以 (provider, provider_batch_ref, source_ref) 为稳定唯一键；
    预签名上传地址只驻留内存，库里只保存过期时间。
    """

    @abstractmethod
    def register(
        self,
        *,
        task_id: str,
        provider: str,
        provider_batch_ref: str,
        source_ref: str,
        upload_url_expires_at: str | None = None,
        request_summary_json: str | None = None,
    ) -> ExternalTask:
        """登记批次内一个源文件的云端关联（幂等）：同键已存在时返回
        既有记录，保证重放不重复登记"""

    @abstractmethod
    def get_by_refs(
        self, provider: str, provider_batch_ref: str, source_ref: str
    ) -> ExternalTask | None:
        """按稳定唯一键读取；不存在返回 None"""

    @abstractmethod
    def list_by_task(self, task_id: str) -> list[ExternalTask]:
        """列出任务关联的全部外部任务（按登记顺序）"""

    @abstractmethod
    def record_poll(
        self,
        external_task_id: str,
        *,
        state: str,
        status_summary: str | None = None,
        provider_task_id: str | None = None,
    ) -> ExternalTask:
        """记录一次轮询事实：轮询计数自增并刷新最近轮询时间、供应方
        状态与脱敏摘要；记录不存在抛 EntityNotFoundError"""

    @abstractmethod
    def set_result(
        self,
        external_task_id: str,
        *,
        result_sha256: str,
        expires_at: str | None = None,
        state: str | None = None,
    ) -> ExternalTask:
        """记录结果事实：内容哈希与过期时间；结果地址按安全合同不落库，
        下载由持有该地址的内存流程完成。记录不存在抛 EntityNotFoundError"""


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

    @abstractmethod
    def schedule_retry(
        self,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> Task:
        """安排当前阶段的业务自动重试：按重试序号计算退避（含抖动）并
        写入到期时间，释放租约后转入 retry_waiting；当前阶段重试预算
        耗尽或累计重试执行达到硬上限时转入 failed 终态。
        仅 running/waiting_external 可安排；租约丢失抛 TaskLeaseLostError"""

    @abstractmethod
    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> Task:
        """把任务置为失败终态（不可自动恢复的错误路径）：回填
        finished_at 与脱敏错误信息并释放租约；租约丢失抛
        TaskLeaseLostError，状态不允许失败抛 TaskStateConflictError"""

    @abstractmethod
    def request_cancel(self, task_id: str) -> Task:
        """请求取消任务（用户侧，不要求租约）：排队/等待确认的任务
        立即取消并收尾；执行中的任务先转入 cancel_requested 等待
        Worker 在安全检查点完成取消；重复请求幂等返回；
        终态任务抛 TaskStateConflictError"""

    @abstractmethod
    def cancel_at_checkpoint(self, task_id: str, worker_id: str) -> Task:
        """Worker 在安全检查点完成取消：cancel_requested 转入 cancelled，
        回填 finished_at 并释放租约；状态不是等待取消抛
        TaskStateConflictError，租约丢失抛 TaskLeaseLostError"""

    @abstractmethod
    def promote_due_retries(self) -> int:
        """把退避到期的 retry_waiting 任务批量转回 queued（返回提升数量）；
        当前阶段的重试预算保留，未到期任务保持不变"""

    @abstractmethod
    def retry_failed(
        self, task_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """手动重试：仅失败任务可重试，创建携带派生来源（父任务与
        manual 标记）的新任务并继承类型/引用/优先级/输入与重试预算，
        原任务保持终态；容量满抛 TaskQueueFullError；
        非失败状态抛 TaskStateConflictError"""

    @abstractmethod
    def requeue_stale_running(self) -> int:
        """把租约已失效（超过接管宽限或缺失）的执行中与等待外部结果
        任务重排回排队：仅清理租约与状态，处理阶段、进度、checkpoint
        与执行计数全部保留，重新领取后从断点继续；返回重排数量"""

    @abstractmethod
    def finish_stale_cancel_requests(self) -> int:
        """把等待取消但租约已失效的任务直接转入取消终态（原 Worker
        已无法到达检查点）：回填完成时间并释放租约；返回收尾数量"""

    @abstractmethod
    def recover_interrupted_tasks(self) -> dict[str, int]:
        """恢复被中断的任务（进程启动时与周期巡检调用，可重复执行）：
        提升到期重试、重排失效执行、收尾失效取消，返回各类处理数量"""
