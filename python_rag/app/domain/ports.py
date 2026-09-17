# -*- coding: utf-8 -*-
"""Repository Port：持久化操作的领域接口

Port 由领域层定义、基础设施层实现（SQLite Adapter）。
方法签名以领域实体为边界，实现不得泄漏 SQL 细节或供应商类型。
未在此定义的读写能力（如内容表的批量摄取）随对应里程碑补充。
"""
from abc import ABC, abstractmethod

from .entities import Document, DocumentVersion, IndexVersion, KnowledgeBase


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
