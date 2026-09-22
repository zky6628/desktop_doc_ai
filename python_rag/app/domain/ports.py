# -*- coding: utf-8 -*-
"""Repository Port：持久化操作的领域接口

Port 由领域层定义、基础设施层实现（SQLite Adapter）。
方法签名以领域实体为边界，实现不得泄漏 SQL 细节或供应商类型。
未在此定义的读写能力（如内容表的批量摄取）随对应里程碑补充。
"""
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence

from .chunking import Chunk, StoredBlock, StoredChunk
from .citation import CitationRecord
from .entities import (
    ConversationMessage,
    ConversationSummary,
    Document,
    DocumentVersion,
    EvaluationRun,
    EvaluationRunState,
    ExternalTask,
    IndexVersion,
    KnowledgeBase,
    QueryEvent,
    QueryRun,
    Task,
    TaskEvent,
    TaskStage,
    TaskStatus,
)
from .ingest import ImportOutcome
from .keyword import KeywordDocument
from .parsing import ParsedDocument
from .rerank import RerankHit
from .retrieval import CandidateRecord, ChunkAnchor, IndexHit


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
    def list_active(
        self,
        *,
        limit: int = 50,
        after_created_at: str | None = None,
        after_id: str | None = None,
    ) -> list[KnowledgeBase]:
        """列出活动知识库（created_at + id 倒序的 keyset 分页）

        after_created_at/after_id 为上一页末行的排序键，二者必须同时
        提供才生效；首页两者传 None。limit 上限由调用方约束
        """

    @abstractmethod
    def rename(
        self, kb_id: str, name: str, description: str | None
    ) -> KnowledgeBase:
        """重命名知识库并可更新描述；目标不存在或已删除抛
        EntityNotFoundError；活动名称与其他知识库冲突抛
        DuplicateActiveNameError"""

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
    def list_by_kb(
        self,
        kb_id: str,
        include_deleted: bool = False,
        *,
        limit: int = 50,
        after_created_at: str | None = None,
        after_id: str | None = None,
    ) -> list[Document]:
        """列出知识库内文档（created_at + id 倒序的 keyset 分页；默认
        排除已软删除）；排序键语义与知识库列表一致"""

    @abstractmethod
    def soft_delete(self, doc_id: str) -> None:
        """软删除：写入 deleted_at/delete_requested_at 并置状态为 deleted；
        目标不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def set_active_version(self, doc_id: str, version_id: str) -> None:
        """回填活动版本指针并刷新 updated_at；非删除态文档同时置为
        ready（首个活动版本产生即文档就绪；多版本时最新激活胜出）。
        目标不存在抛 EntityNotFoundError"""


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
        chunking_config_id: str | None = None,
        embedding_profile_id: str | None = None,
        keyword_config_id: str | None = None,
    ) -> IndexVersion:
        """创建索引版本（staging）；版本不存在抛 EntityNotFoundError"""

    @abstractmethod
    def set_vector_collection(self, index_id: str, collection_name: str) -> None:
        """登记向量集合名（staging 期写入；集合按索引版本隔离，
        命名由向量适配层约定）。索引不存在抛 EntityNotFoundError"""

    @abstractmethod
    def set_fts_namespace(self, index_id: str, namespace: str) -> None:
        """登记 FTS 命名空间（staging 期写入；命名空间按索引版本
        隔离，命名由关键词适配层约定）。索引不存在抛 EntityNotFoundError"""

    @abstractmethod
    def record_validation(
        self, index_id: str, *, chunk_count: int, integrity_hash: str
    ) -> None:
        """记录完整性验证结果（切片数与检索单元完整性哈希），供
        重建与健康检查比对。索引不存在抛 EntityNotFoundError"""

    @abstractmethod
    def get(self, index_id: str) -> IndexVersion | None:
        """按 ID 读取；不存在时返回 None"""

    @abstractmethod
    def list_all(self) -> list[IndexVersion]:
        """列出全部索引版本（按创建时间升序）

        健康检查与补偿清理的全量扫描入口；孤儿派生资产的归属判定
        也以此为事实源
        """

    @abstractmethod
    def list_by_document_version(self, document_version_id: str) -> list[IndexVersion]:
        """列出文档版本的全部索引版本（按索引号升序）"""

    @abstractmethod
    def list_active_by_knowledge_base(self, kb_id: str) -> list[IndexVersion]:
        """按指针链解析知识库当前可检索的活动索引版本

        链路：documents.active_document_version_id →
        document_versions.active_index_version_id → 状态为 active 的
        index_versions；软删文档与指针未回填的版本不产出。知识库无
        文档或无可检索索引返回空列表（存在性与删除状态由调用方校验）"""

    @abstractmethod
    def delete(self, index_id: str) -> int:
        """删除残留索引版本行（补偿清理用）

        仅允许删除从未激活过的行（staging/validating/failed）；活动
        与退役行承载激活历史与验证基准，不受本方法影响。返回删除
        行数（0 = 状态不允许或不存在）。调用方须先清理该版本的切片
        与派生索引资产，行删除是清扫的最后一步"""

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

    @abstractmethod
    def list_document_blocks(self, document_version_id: str) -> list[StoredBlock]:
        """按序号升序读取该版本的已落库解析块（含表格证据）

        切片等下游阶段以此读取解析事实，与内存解析模型解耦。
        版本不存在抛 EntityNotFoundError"""

    @abstractmethod
    def delete_document_content(self, document_version_id: str) -> int:
        """删除该版本的全部解析产物（内容块与表格证据，单事务）

        物理清理链使用：引用该块的切片关联须先行删除。版本不存在
        抛 EntityNotFoundError；返回删除的块数"""


class PipelineConfigRepository(ABC):
    """流水线配置仓储：不可变配置版本历史的写入与复用"""

    @abstractmethod
    def ensure_config(self, config_type: str, config_json: str) -> str:
        """确保指定类型的配置行存在并返回其 ID（幂等）

        内容哈希命中在役配置行时直接复用；未命中时在事务内以类型内
        递增版本号创建新配置行。配置记录不可原地修改，参数变化通过
        新版本行表达。"""


class SystemSettingsRepository(ABC):
    """系统设置仓储：运行时可变键值设置的读写"""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """读取设置值 JSON 文本；键不存在返回 None"""

    @abstractmethod
    def put(self, key: str, value_json: str) -> None:
        """写入设置值（覆盖语义，键不存在则创建）"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除设置（恢复键的缺省语义）"""


class EvaluationRunRepository(ABC):
    """评测运行仓储：运行事实与组粒度结果快照"""

    @abstractmethod
    def create(
        self,
        *,
        knowledge_base_id: str,
        task_id: str,
        target_version_ids: Sequence[str],
        questions: Sequence[str],
        param_groups: Sequence[dict],
    ) -> EvaluationRun:
        """创建运行记录（running 态）"""

    @abstractmethod
    def get(self, run_id: str) -> EvaluationRun | None:
        """按主键读取运行"""

    @abstractmethod
    def get_by_task(self, task_id: str) -> EvaluationRun | None:
        """按编排任务读取运行（Worker 分派入口）"""

    @abstractmethod
    def list_by_knowledge_base(
        self, knowledge_base_id: str, *, limit: int = 20
    ) -> list[EvaluationRun]:
        """按知识库列出运行（创建时间倒序截断）"""

    @abstractmethod
    def get_running(self) -> EvaluationRun | None:
        """读取执行中的运行（评测独占，至多一个）"""

    @abstractmethod
    def mark_progress(
        self, run_id: str, group_index: int, question_index: int
    ) -> None:
        """推进进度指针（组内问题边界调用）"""

    @abstractmethod
    def save_group_result(
        self, run_id: str, group_index: int, result: dict
    ) -> None:
        """写入单组结果快照（增量替换该组位置）"""

    @abstractmethod
    def mark_terminal(
        self,
        run_id: str,
        state: EvaluationRunState,
        *,
        error_code: str | None = None,
    ) -> None:
        """收尾为终态（completed/failed/cancelled）"""


class EmbeddingCacheRepository(ABC):
    """嵌入缓存仓储：模型 + 文本侧别 + 内容哈希维度的向量复用"""

    @abstractmethod
    def get_many(
        self, model_name: str, text_type: str, content_hashes: Sequence[str]
    ) -> dict[str, list[float]]:
        """批量读取缓存向量，返回命中项的 hash → 向量映射"""

    @abstractmethod
    def put_many(
        self,
        model_name: str,
        text_type: str,
        dimensions: int,
        entries: Sequence[tuple[str, list[float]]],
    ) -> None:
        """批量写入缓存（幂等，同键已存在时保留既有值）"""


class EmbeddingGateway(ABC):
    """Embedding 网关：文本向量化的供应方边界

    输出顺序与输入顺序严格一致；瞬态失败以领域错误表达，由调用方
    （任务引擎）按退避策略重试，网关内部不重试
    """

    @abstractmethod
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本序列向量化

        :param texts: 文本序列（允许为空，空输入返回空列表且不发起请求）
        :return: 与输入同序的向量列表
        :raises EmbeddingTransientError: 网络或供应方瞬态故障
        :raises EmbeddingAuthError: 密钥无效
        :raises EmbeddingQuotaError: 配额不足
        :raises EmbeddingProtocolViolationError: 响应不符合协议或对齐校验失败
        """


class QueryEmbeddingGateway(ABC):
    """查询侧向量化网关：检索问题的查询侧嵌入边界

    与构建侧共用同一模型与维度；查询侧文本参数由本端口实现固定，
    两侧口径不混用（侧别由方法保证而非实例配置，消除误用场景）。
    错误族与构建侧一致，瞬态失败由调用方决定重试策略
    """

    @abstractmethod
    def embed_query(self, question: str) -> list[float]:
        """把单个检索问题向量化

        :param question: 问题文本（调用方保证非空白）
        :return: 查询向量（维度与构建侧一致）
        :raises EmbeddingTransientError: 网络或供应方瞬态故障
        :raises EmbeddingAuthError: 密钥无效
        :raises EmbeddingQuotaError: 配额不足
        :raises EmbeddingProtocolViolationError: 响应不符合协议
        """


class RerankGateway(ABC):
    """重排网关：候选相关性重排的供应方边界

    输入为问题与文档文本序列（下标语义由调用方维护，命中经下标
    映射回切片）；瞬态失败以领域错误表达，由查询链路决定重试与
    降级策略，网关内部不重试、不降级
    """

    @abstractmethod
    def rerank(self, question: str, documents: Sequence[str]) -> list[RerankHit]:
        """按问题相关性重排文档文本，返回相关度降序的前 top_n 命中

        :param question: 检索问题
        :param documents: 候选文档文本序列（与输入下标一一对应）
        :return: 按相关度降序的命中（index 为输入下标，score 越高越
            相关）；空输入返回空列表且不发起请求
        :raises RerankTransientError: 网络或供应方瞬态故障
        :raises RerankAuthError: 密钥无效
        :raises RerankQuotaError: 配额不足
        :raises RerankProtocolViolationError: 响应不符合协议或校验失败
        """


class GenerationGateway(ABC):
    """生成网关：流式回答的供应方边界

    输出为增量文本片段的迭代器（按生成顺序）；瞬态失败以领域错误
    表达，由查询链路决定重试与终态策略，网关内部不重试。实现须在
    流结束后经 last_usage 暴露供应方用量 (输入, 输出) token（供应方
    未提供时为 None）
    """

    @property
    @abstractmethod
    def last_usage(self) -> tuple[int, int] | None:
        """最近一次流式生成的供应方用量 (输入, 输出) token"""

    @abstractmethod
    def stream_answer(
        self, messages: Sequence[dict[str, str]]
    ) -> "Iterator[str]":
        """按消息序列流式生成回答

        :param messages: 供应方消息列表（system/user 角色）
        :return: 增量文本片段迭代器（拼接后即回答全文）
        :raises GenerationRateLimitedError: 生成限流
        :raises GenerationTransientError: 网络或供应方瞬态故障
        :raises GenerationAuthError: 密钥无效
        :raises GenerationQuotaError: 配额不足
        :raises GenerationProtocolViolationError: 响应不符合协议
        """


class QueryRunRepository(ABC):
    """查询运行仓储：查询状态机与评测事实的持久化

    状态机：queued → running → completed/failed/cancelled；取消请求
    将 running/queued 置为 cancel_requested，由执行方在检查点收尾
    """

    @abstractmethod
    def create(
        self,
        *,
        kb_id: str,
        question: str,
        config_ids: dict[str, str],
        idempotency_key: str | None = None,
    ) -> QueryRun:
        """创建查询运行（queued）

        幂等键命中时直接返回既有查询（不重复创建）；知识库不存在抛
        EntityNotFoundError"""

    @abstractmethod
    def get(self, run_id: str) -> QueryRun | None:
        """按 ID 读取；不存在返回 None"""

    @abstractmethod
    def mark_running(self, run_id: str) -> None:
        """queued → running（回填 started_at）；迁移非法抛
        QueryStateConflictError，不存在抛 EntityNotFoundError"""

    @abstractmethod
    def mark_first_token(self, run_id: str) -> None:
        """回填首 token 时间与服务端 TTFT（幂等：重复调用不覆盖）"""

    @abstractmethod
    def request_cancel(self, run_id: str) -> QueryRun:
        """请求取消：queued/running → cancel_requested（幂等：已处于
        取消中或终态时返回当前快照不迁移）；不存在抛 EntityNotFoundError"""

    @abstractmethod
    def is_cancel_requested(self, run_id: str) -> bool:
        """查询是否处于取消中（执行方检查点轮询）"""

    @abstractmethod
    def finalize(
        self,
        run_id: str,
        *,
        state: str,
        refused: bool = False,
        degraded: bool = False,
        assistant_message_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> QueryRun:
        """收尾终态（cancel_requested/running → completed/failed/cancelled）：
        回填完成时间、总耗时、拒答/降级标志、消息关联与错误信息；
        不存在抛 EntityNotFoundError"""

    @abstractmethod
    def attach_message_ids(
        self,
        run_id: str,
        *,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
    ) -> None:
        """回填查询与会话/消息的关联（评测记录独立于会话删除）；
        查询不存在抛 EntityNotFoundError"""

    @abstractmethod
    def record_segments(
        self,
        run_id: str,
        *,
        retrieval_ms: int,
        resolve_ms: int,
        rerank_ms: int,
        prompt_build_ms: int,
        model_ttft_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """记录分段耗时与生成用量（查询收尾前调用，幂等覆盖）；
        查询不存在抛 EntityNotFoundError"""

    @abstractmethod
    def record_candidates(
        self, run_id: str, records: Sequence[CandidateRecord]
    ) -> int:
        """单事务批量写入候选快照（UNIQUE(查询, 切片) 重放忽略）；
        返回实际写入行数。查询不存在抛 EntityNotFoundError"""

    @abstractmethod
    def record_client_metric(
        self,
        run_id: str,
        *,
        client_send_at: str,
        first_sse_token_received_at: str,
        first_token_rendered_at: str,
        client_ttft_ms: int,
        client_instance_id_hash: str | None,
        network_context_json: str | None,
    ) -> bool:
        """记录客户端遥测（主键即查询，重放忽略返回 False）；查询
        不存在抛 EntityNotFoundError。时间顺序校验由调用方负责"""

    @abstractmethod
    def fail_interrupted(self) -> int:
        """把重启残留的 queued/running/cancel_requested 查询统一置为
        failed（进程重启中断）；返回处理数量（启动恢复调用，幂等）"""


class QueryEventStore(ABC):
    """查询事件仓储：SSE 事件的持久化与断点重放

    event_seq 查询内单调递增（1 起）；token 批次合并写库并携带过期
    时间（终态后 30 分钟），过期清理归补偿清理链
    """

    @abstractmethod
    def append(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: dict | None = None,
        token_text: str | None = None,
        token_seq_start: int | None = None,
        token_seq_end: int | None = None,
        expires_at: str | None = None,
    ) -> int:
        """追加一条事件并返回其 event_seq（查询内单调递增）；
        查询不存在抛 EntityNotFoundError"""

    @abstractmethod
    def read_after(self, run_id: str, after_seq: int = 0) -> list[QueryEvent]:
        """按序号升序读取 event_seq > after_seq 的事件（断点重放）；
        token 批次已过期的事件不返回（聚合正文经查询读取获取）"""

    @abstractmethod
    def expire_tokens(self, run_id: str, expiry_horizon_seconds: int) -> None:
        """把该查询 token 批次的过期时间设为终态后保留窗（30 分钟）；
        非终态调用为空操作由执行方保证"""

    @abstractmethod
    def purge_expired_tokens(self) -> int:
        """删除全部已过期的 token 批次行（懒清理：新查询创建时顺带
        执行）；返回删除行数"""


class ConversationRepository(ABC):
    """会话仓储：查询链路的会话与消息事实"""

    @abstractmethod
    def ensure_conversation(self, kb_id: str, conversation_id: str | None) -> str:
        """确保会话存在并返回其主键：给定 ID 时校验存在（不存在抛
        EntityNotFoundError）；未给定时创建新会话"""

    @abstractmethod
    def add_message(self, conversation_id: str, role: str, content: str) -> str:
        """追加消息并返回主键（role 为 user/assistant）

        未命名会话（标题为空）的首条用户消息同时生成默认标题：空白
        折叠后截取消息前缀，仅首次生效，此后不随消息变化
        """

    @abstractmethod
    def rename(self, conversation_id: str, title: str) -> None:
        """重命名会话标题；不改变最近活跃排序键（updated_at）。

        会话不存在抛 EntityNotFoundError
        """

    @abstractmethod
    def get_message_content(self, message_id: str) -> str | None:
        """按主键读取消息正文；不存在返回 None（最终答案聚合用）"""

    @abstractmethod
    def list_by_knowledge_base(
        self,
        kb_id: str | None,
        *,
        limit: int = 50,
        after_updated_at: str | None = None,
        after_id: str | None = None,
    ) -> list[ConversationSummary]:
        """列出会话（updated_at + id 倒序的 keyset 分页，最近活跃优先），
        含最后一条消息摘要（无消息时摘要字段为 None）

        kb_id 为 None 时不按知识库过滤（跨库全量历史，含已删除知识库
        的会话）；给定值时仅返回该库会话。after_updated_at/after_id 为
        上一页末行的排序键，二者必须同时提供才生效；首页两者传 None。
        limit 上限由调用方约束
        """

    @abstractmethod
    def list_messages(self, conversation_id: str) -> list[ConversationMessage]:
        """按创建时间升序读取会话全部消息（同刻消息按主键序保持
        写入顺序）；会话不存在抛 EntityNotFoundError"""

    @abstractmethod
    def delete(self, conversation_id: str) -> None:
        """删除会话：消息与引用快照经级联清除，查询运行的会话/消息
        关联列由存储层置空（查询指标事实保留）；会话不存在抛
        EntityNotFoundError"""


class ChunkRepository(ABC):
    """切片仓储：索引版本内切片与其定位关系的持久化"""

    @abstractmethod
    def replace_index_chunks(self, index_version_id: str, chunks: Sequence[Chunk]) -> int:
        """把切片序列写入索引版本（按序号幂等 upsert）

        以 (索引版本, 序号) 为行级键：已存在的切片原地更新并保留
        首次写入的主键（下游向量索引因此获得稳定 ID），新切片插入
        新主键；定位关系按切片整体替换；序号超出本次输入范围的残留
        行一并清除。切片主键不存在抛 EntityNotFoundError（含子切片
        引用的父序号缺失）。返回写入切片数。"""

    @abstractmethod
    def list_index_chunks(self, index_version_id: str) -> list[StoredChunk]:
        """按序号升序读取该索引版本的全部切片（含定位关系）

        向量化与验证阶段以此读取切片事实；索引版本不存在抛
        EntityNotFoundError"""

    @abstractmethod
    def get_chunk_anchors(self, chunk_ids: Sequence[str]) -> list[ChunkAnchor]:
        """按主键批量读取切片锚点事实（归属索引版本与序号）

        检索命中回查业务事实用；不存在的主键不产生结果行"""

    @abstractmethod
    def count_index_chunks(self, index_version_id: str) -> int:
        """返回该索引版本的切片行数（健康检查与清理的目标判定用）；
        索引版本不存在返回 0"""

    @abstractmethod
    def delete_index_chunks(self, index_version_id: str) -> int:
        """删除该索引版本的全部切片与定位关系（补偿清理用，单事务）

        定位关系随切片一并删除；索引版本不存在时无操作。返回删除
        的切片数"""


class VectorIndexGateway(ABC):
    """向量索引网关：Chroma 等向量库的供应方边界（写入、验证与查询）

    集合按索引版本隔离，集合名由调用方按适配层命名约定传入；
    查询按统一语义返回越高越相关的分数并保留供应方原始值
    """

    @abstractmethod
    def upsert_vectors(
        self,
        collection_name: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """按记录 ID 幂等写入/更新向量（集合不存在时创建）"""

    @abstractmethod
    def query_vectors(
        self, collection_name: str, query_vector: Sequence[float], top_k: int
    ) -> list[IndexHit]:
        """按查询向量取最近邻命中

        :param collection_name: 集合名（集合不存在返回空列表且不产生
            创建副作用）
        :param query_vector: 查询向量（维度与构建侧一致）
        :param top_k: 返回命中数上限
        :return: 按相关度降序的命中（score 越高越相关，原始距离随
            命中保留）"""

    @abstractmethod
    def count_vectors(self, collection_name: str) -> int:
        """返回集合内向量数量；集合不存在返回 0"""

    @abstractmethod
    def list_vector_ids(self, collection_name: str) -> list[str]:
        """返回集合内全部记录 ID；集合不存在返回空列表"""

    @abstractmethod
    def delete_collection(self, collection_name: str) -> None:
        """删除整个集合（补偿清理用）；集合不存在时无操作"""

    @abstractmethod
    def list_collections(self) -> list[str]:
        """返回向量库内全部集合名

        健康检查与补偿清理据此扫描孤儿集合（归属由命名合同解析，
        无 metadata 可依赖）"""


class KeywordIndexGateway(ABC):
    """关键词索引网关：FTS5 命名空间的写入、验证与查询

    命名空间按索引版本隔离；写入内容与查询词元均为分词器产出的
    预分词序列（分词由 TextTokenizer 承担，网关不做分词）。查询按
    统一语义返回越高越相关的分数并保留原始 bm25 值
    """

    @abstractmethod
    def rebuild_namespace(
        self, namespace: str, documents: Sequence[KeywordDocument]
    ) -> int:
        """以重建方式写入命名空间（先清空该命名空间再插入，幂等）。

        返回写入文档数。命名空间内既有内容无论来自哪次尝试都会被
        本次输入完全取代"""

    @abstractmethod
    def query_keywords(
        self, namespace: str, tokens: Sequence[str], top_k: int
    ) -> list[IndexHit]:
        """按预分词词元序列取两级匹配命中

        匹配两级：先按词元序列做短语精确匹配（词面强信号）；短语零
        命中且词元数大于 1 时，降级为全词元 AND 匹配（不要求相邻与
        语序）。同一查询内两级至多产出一路结果，短语命中存在时 AND
        级不触达。

        :param namespace: 命名空间（不存在返回空列表）
        :param tokens: 分词器产出的词元序列
        :param top_k: 返回命中数上限
        :return: 按相关度降序的命中（score 越高越相关，原始 bm25 值
            随命中保留）"""

    @abstractmethod
    def count_documents(self, namespace: str) -> int:
        """返回命名空间内文档数量；命名空间不存在返回 0"""

    @abstractmethod
    def list_namespaces(self) -> list[str]:
        """返回虚拟表内已存在的全部命名空间

        健康检查与补偿清理据此扫描孤儿命名空间（命名空间即行集合，
        存在性由行承载）"""


class TextTokenizer(ABC):
    """文本分词器：中文关键词检索的预分词边界"""

    @abstractmethod
    def tokenize(self, texts: Sequence[str]) -> list[list[str]]:
        """把文本序列分词为词元序列

        :param texts: 文本序列
        :return: 与输入同序的词元列表（已做归一化：小写、去空白）
        """


class CitationRepository(ABC):
    """引用快照仓储：引用不可变事实的持久化与历史读取

    快照行在写入时定格文件名/版本/页码/章节/引文与四类分数；切片
    清理只置空切片指针（存储层 ON DELETE SET NULL），快照事实保留
    """

    @abstractmethod
    def insert_citations(self, records: Sequence[CitationRecord]) -> int:
        """单事务批量写入引用快照

        以 (助手消息, 引用序号) 为唯一键：重放冲突行忽略不覆盖
        （幂等）。返回实际写入行数。消息不存在时抛 EntityNotFoundError"""

    @abstractmethod
    def list_by_message(self, assistant_message_id: str) -> list[CitationRecord]:
        """按助手消息读取引用快照（引用序号升序）

        历史会话展示与评测导出用；消息不存在返回空列表"""


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

    @abstractmethod
    def create_replace(
        self,
        document_id: str,
        *,
        source_path: str,
        source_sha256: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        parser_mode: str | None = None,
        parser_route_json: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImportOutcome:
        """在指定文档上建立新版本并创建导入任务（替换编排）

        与导入同构的单事务：目标不存在或已删除抛 EntityNotFoundError；
        所在知识库已删除抛 KnowledgeBaseDeletedError；存在非终态任务
        抛 TaskStateConflictError；新内容与当前活动版本相同抛
        DuplicateActiveContentError；队列容量不足抛 TaskQueueFullError。
        成功时文档身份列（source_sha256）随新内容更新"""

    @abstractmethod
    def create_rebuild(
        self, document_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """为文档当前活动版本创建索引重建任务（组合事务）

        守卫与任务创建同一事务：目标不存在或已删除抛
        EntityNotFoundError；所属知识库已删除抛
        KnowledgeBaseDeletedError；没有活动版本抛
        VersionConflictError；存在非终态任务抛
        TaskStateConflictError；队列容量不足抛 TaskQueueFullError"""


class DeletionRepository(ABC):
    """删除仓储：软删除与删除任务的组合事务写入

    容量检查、软删除时间写入与删除任务创建必须同事务：队列满时
    整体回滚，资源保持未删除。
    """

    @abstractmethod
    def create_kb_deletion(
        self, kb_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """软删除知识库并创建物理清理任务（delete_kb）

        幂等键已存在时直接返回既有任务；知识库不存在抛
        EntityNotFoundError；已处于删除态抛 TaskStateConflictError；
        队列容量不足抛 TaskQueueFullError 且软删除一并回滚"""

    @abstractmethod
    def create_document_deletion(
        self, document_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """软删除文档并创建物理清理任务（delete_document）

        幂等键已存在时直接返回既有任务；文档不存在或已删除抛
        EntityNotFoundError；存在非终态任务抛 TaskStateConflictError；
        队列容量不足抛 TaskQueueFullError 且软删除一并回滚"""


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
    def append_event(
        self, task_id: str, event_type: str, *, detail_json: str | None = None
    ) -> TaskEvent:
        """追加一条审计事件（状态快照取任务当前值）

        清扫等无状态迁移的工作用它记录动作摘要，保证清理动作可
        审计。任务不存在抛 EntityNotFoundError"""

    @abstractmethod
    def count_non_terminal_by_document_version(self, document_version_id: str) -> int:
        """统计引用该文档版本且尚未进入终态的任务数

        补偿清理的守卫依据：存在在途任务时其文档版本的 staging
        索引可能被续跑复用，不得清理"""

    @abstractmethod
    def count_non_terminal_by_document(self, document_id: str) -> int:
        """统计引用该文档且尚未进入终态的任务数（替换/重建/删除的
        在途守卫）；目标不存在返回 0"""

    @abstractmethod
    def list_by_document(self, document_id: str, limit: int = 5) -> list[Task]:
        """按创建时间倒序列出引用该文档的最近任务（文档详情页的
        任务历史）；目标不存在返回空列表"""

    @abstractmethod
    def list_tasks(
        self,
        *,
        states: Sequence[TaskStatus] | None = None,
        task_type: str | None = None,
        knowledge_base_id: str | None = None,
        document_id: str | None = None,
        limit: int = 50,
        after_created_at: str | None = None,
        after_id: str | None = None,
    ) -> list[Task]:
        """按筛选条件列出任务（created_at + id 倒序的 keyset 分页）

        states 为空元组/None 时不按状态过滤；排序键语义与知识库
        列表一致（任务中心的队列视图入口）"""

    @abstractmethod
    def queue_position(self, task_id: str) -> int | None:
        """queued 任务的队列位次（按领取顺序：priority 降序、
        created_at/id 升序）；非排队状态或任务不存在返回 None"""

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
