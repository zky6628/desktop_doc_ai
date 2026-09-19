# -*- coding: utf-8 -*-
"""领域层错误类型

Repository 实现把存储层约束冲突（唯一索引、校验失败等）翻译为这些
领域错误，上层据此生成用户可见的错误码，而不是直接暴露 SQL 异常。
"""


class RepositoryError(RuntimeError):
    """仓储操作失败的基类"""


class EntityNotFoundError(RepositoryError):
    """按 ID 操作的目标实体不存在"""


class DuplicateActiveNameError(RepositoryError):
    """活动状态下名称唯一约束冲突"""


class DuplicateActiveContentError(RepositoryError):
    """活动状态下内容哈希唯一约束冲突"""


class ActivationError(RepositoryError):
    """版本激活事务校验失败（状态不允许激活或双向一致性无法满足）"""


class TaskQueueFullError(RepositoryError):
    """任务队列容量已达上限（pending 合计或非终态合计超限），任务未被创建"""


class TaskStateConflictError(RepositoryError):
    """任务状态迁移不被状态机允许（含终态再迁移与自迁移）"""


class TaskLeaseLostError(RepositoryError):
    """任务租约无效：未持有租约、持有者不匹配或租约已过期"""


class ConfirmationConflictError(RepositoryError):
    """云端解析确认与任务状态冲突：仅等待确认的任务可被确认"""


class QueryStateConflictError(RepositoryError):
    """查询状态迁移不被状态机允许（终态再迁移或非法迁移）"""


class FileTooLargeError(RepositoryError):
    """上传文件超过单文件大小上限，暂存已中止且无残留"""


class UnsupportedFormatError(RepositoryError):
    """文件格式不被接受：扩展名不在白名单、扩展名/MIME/真实格式
    不一致，或容器损坏、加密"""


class EmptyFileError(RepositoryError):
    """上传文件内容为空"""


class PathUnsafeError(RepositoryError):
    """落盘路径校验失败：解析后逃出受控根目录"""


class InsufficientDiskSpaceError(RepositoryError):
    """磁盘剩余空间不足，无法暂存上传文件"""


class ParsingError(RepositoryError):
    """本地解析失败的基类：输入本身不可用，均属不可自动重试类错误

    error_code 是任务失败时写入的稳定错误码；错误消息只描述失败原因，
    不得携带用户路径或文档正文
    """

    error_code: str = "PARSING_FAILED"


class EmptyContentError(ParsingError):
    """解析产物无有效内容：文件为空或全部内容均为空白"""

    error_code = "PARSING_EMPTY_CONTENT"


class DocumentCorruptedError(ParsingError):
    """文档容器损坏或不是声称的格式，无法读出内容"""

    error_code = "PARSING_CORRUPTED"


class EncryptedDocumentError(ParsingError):
    """文档已加密，本地解析明确拒绝解密尝试"""

    error_code = "PARSING_ENCRYPTED"


class TextEncodingError(ParsingError):
    """文本文件编码无法识别：既不是合法 UTF-8 也不是合法 GBK"""

    error_code = "PARSING_TEXT_ENCODING"


class CloudParsingError(RepositoryError):
    """云端解析适配失败的基类：错误码供任务失败/自动重试分类使用

    错误消息只描述失败类别与供应商脱敏摘要，不得携带预签名 URL、
    用户路径或文档内容
    """

    error_code = "CLOUD_PARSING_FAILED"


class CloudTransportError(CloudParsingError):
    """瞬态传输/服务错误：网络中断、超时重置、429/408/5xx、供应商暂时不可用"""

    error_code = "CLOUD_TRANSIENT"


class CloudAuthError(CloudParsingError):
    """认证/授权失败：Token 错误或过期、401/403，不可自动重试"""

    error_code = "CLOUD_AUTH"


class CloudQuotaError(CloudParsingError):
    """供应商配额不足（每日解析额度等），不可自动重试"""

    error_code = "CLOUD_QUOTA"


class CloudInputRejectedError(CloudParsingError):
    """供应商拒绝输入：格式不支持、文件损坏、大小/页数超限等，不可自动重试"""

    error_code = "CLOUD_INPUT_REJECTED"


class CloudProtocolViolationError(CloudParsingError):
    """供应商响应违反协议：未知状态码、缺字段、schema 不符，不可自动重试"""

    error_code = "CLOUD_PROTOCOL_VIOLATION"


class CloudTimeoutError(CloudParsingError):
    """云端解析超时：单文件超过轮询时限仍未完成"""

    error_code = "CLOUD_TIMEOUT"


class ArchiveRejectedError(CloudParsingError):
    """结果压缩包安全校验拒绝：路径穿越、链接条目、超限、炸弹、白名单外类型"""

    error_code = "CLOUD_ARCHIVE_REJECTED"


class EmbeddingError(RepositoryError):
    """Embedding 网关失败的基类：错误码供任务失败/自动重试分类使用

    错误消息只描述失败类别与脱敏摘要，不得携带密钥或文本内容
    """

    error_code = "EMBEDDING_FAILED"


class EmbeddingTransientError(EmbeddingError):
    """瞬态传输/服务错误：网络中断、超时、429/408/5xx，可自动重试"""

    error_code = "EMBEDDING_TRANSIENT"


class EmbeddingAuthError(EmbeddingError):
    """认证失败：API Key 无效或被拒绝，不可自动重试"""

    error_code = "EMBEDDING_AUTH"


class EmbeddingQuotaError(EmbeddingError):
    """配额不足：欠费或额度耗尽，不可自动重试"""

    error_code = "EMBEDDING_QUOTA"


class EmbeddingProtocolViolationError(EmbeddingError):
    """响应违反协议：未知业务错误码、数量/维度/对齐校验失败，不可自动重试"""

    error_code = "EMBEDDING_PROTOCOL_VIOLATION"


class RerankError(RepositoryError):
    """Rerank 网关失败的基类：错误码供查询链路失败/降级分类使用

    错误消息只描述失败类别与脱敏摘要，不得携带密钥或文本内容
    """

    error_code = "RERANK_FAILED"


class RerankTransientError(RerankError):
    """瞬态传输/服务错误：网络中断、超时、429/408/5xx，可自动重试"""

    error_code = "RERANK_TRANSIENT"


class RerankAuthError(RerankError):
    """认证失败：API Key 无效或被拒绝，不可自动重试"""

    error_code = "RERANK_AUTH"


class RerankQuotaError(RerankError):
    """配额不足：欠费或额度耗尽，不可自动重试"""

    error_code = "RERANK_QUOTA"


class RerankProtocolViolationError(RerankError):
    """响应违反协议：未知业务错误码、数量/下标/分数校验失败，不可自动重试"""

    error_code = "RERANK_PROTOCOL_VIOLATION"


class GenerationError(RepositoryError):
    """生成网关失败的基类：错误码供查询失败终态与 HTTP 错误映射使用

    错误消息只描述失败类别与脱敏摘要，不得携带密钥、问题或回答正文
    """

    error_code = "GENERATION_FAILED"


class GenerationTransientError(GenerationError):
    """瞬态传输/服务错误：网络中断、超时、5xx，可重试（HTTP NETWORK_ERROR）"""

    error_code = "NETWORK_ERROR"


class GenerationRateLimitedError(GenerationError):
    """生成限流：429/限流族业务码（HTTP MODEL_RATE_LIMITED，客户端稍后重试）"""

    error_code = "MODEL_RATE_LIMITED"


class GenerationAuthError(GenerationError):
    """认证失败：API Key 无效或被拒绝（服务端配置问题，内部错误）"""

    error_code = "GENERATION_AUTH"


class GenerationQuotaError(GenerationError):
    """配额不足：欠费或额度耗尽（服务端配置问题，内部错误）"""

    error_code = "GENERATION_QUOTA"


class GenerationProtocolViolationError(GenerationError):
    """响应违反协议：未知业务错误码、流式增量结构不符（内部错误）"""

    error_code = "GENERATION_PROTOCOL_VIOLATION"


class IndexValidationError(RepositoryError):
    """派生索引完整性校验失败：数量或 ID 集合与业务事实不一致

    属确定性校验失败，不自动重试；staging 产物留待补偿清理，
    重试通过人工触发（重新导入产生新索引版本）
    """

    error_code = "INDEX_VALIDATION_FAILED"
