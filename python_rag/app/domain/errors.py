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
