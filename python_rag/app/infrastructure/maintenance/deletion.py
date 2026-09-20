# -*- coding: utf-8 -*-
"""删除清理服务：KB/文档软删除后的物理清理执行器

物理清理范围（ADR-0009）：源文件、解析产物（内容块与表格证据）、
Chunk 行、Chroma 集合、FTS 命名空间。documents/document_versions/
index_versions 行保留为墓碑，历史会话与引用快照可继续追溯。
删除由删除仓储任务化后由 Worker 调用，本服务不进队列。
"""
import dataclasses
import os

from app.domain.index_maintenance import collection_name, fts_namespace_name
from app.domain.ports import (
    ChunkRepository,
    ContentRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IndexVersionRepository,
    KeywordIndexGateway,
    VectorIndexGateway,
)


@dataclasses.dataclass(frozen=True)
class DeletionOutcome:
    """单个文档的物理清理结果

    failures 为文件系统层不可删除项（路径已缺失不算失败）；
    派生索引清理异常由调用方按任务引擎的错误分类处理
    """

    document_id: str
    removed_files: int
    removed_blocks: int
    cleaned_indexes: int
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class DeletionService:
    """KB/文档物理清理（幂等：已清理项重复执行为无操作）

    :param document_repo: 文档仓储（文档枚举）
    :param version_repo: 文档版本仓储（版本枚举）
    :param content_repo: 内容仓储（解析产物删除）
    :param chunk_repo: 切片仓储（切片与定位关系删除）
    :param index_repo: 索引版本仓储（索引版本枚举）
    :param vector_index: 向量索引网关（集合删除）
    :param keyword_index: 关键词索引网关（命名空间清空）
    """

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        content_repo: ContentRepository,
        chunk_repo: ChunkRepository,
        index_repo: IndexVersionRepository,
        vector_index: VectorIndexGateway,
        keyword_index: KeywordIndexGateway,
    ) -> None:
        self._documents = document_repo
        self._versions = version_repo
        self._content = content_repo
        self._chunks = chunk_repo
        self._indexes = index_repo
        self._vectors = vector_index
        self._keywords = keyword_index

    def cleanup_knowledge_base(self, kb_id: str) -> list[DeletionOutcome]:
        """清理知识库下全部文档的物理资产，并软删除仍在活动态的文档

        物理清理覆盖含已软删除在内的所有文档；仍在活动态的文档随
        KB 删除一并转入删除态（KB 行保留为墓碑）
        """
        documents = self._documents.list_by_kb(kb_id, include_deleted=True)
        outcomes = [self.cleanup_document(document.id) for document in documents]
        for document, outcome in zip(documents, outcomes):
            if document.deleted_at is None:
                self._documents.soft_delete(document.id)
        return outcomes

    def cleanup_document(self, document_id: str) -> DeletionOutcome:
        """清理单个文档的物理资产：逐版本删源文件/解析产物/切片/派生索引"""
        removed_files = 0
        removed_blocks = 0
        cleaned_indexes = 0
        failures: list[str] = []

        for version in self._versions.list_by_document(document_id):
            removed = self._remove_source_file(version.source_path)
            if removed is None:
                failures.append(version.source_path)
            elif removed:
                removed_files += 1

            for index in self._indexes.list_by_document_version(version.id):
                collection = index.vector_collection or collection_name(index.id)
                self._vectors.delete_collection(collection)
                namespace = index.fts_namespace or fts_namespace_name(index.id)
                self._keywords.rebuild_namespace(namespace, [])
                self._chunks.delete_index_chunks(index.id)
                cleaned_indexes += 1

            removed_blocks += self._content.delete_document_content(version.id)

        return DeletionOutcome(
            document_id=document_id,
            removed_files=removed_files,
            removed_blocks=removed_blocks,
            cleaned_indexes=cleaned_indexes,
            failures=tuple(failures),
        )

    @staticmethod
    def _remove_source_file(path: str) -> bool | None:
        """尽力删除源文件

        :return: True 已删除；False 文件本就不存在（幂等）；None 删除失败
        """
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except OSError:
            return None
