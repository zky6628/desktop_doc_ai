# -*- coding: utf-8 -*-
"""上下文事实解析器：活动索引切片与文档事实到组装输入

组合既有仓储读取（索引版本、切片、文档、文档版本），无新 SQL：
按指针链取活动索引，每个索引整体读取一次切片（v1 规模），文档
事实按文档版本缓存。指针链断裂（版本或文档缺失）属数据漂移，跳过
不伪造事实。
"""
from app.domain.context import ContextSource
from app.domain.ports import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IndexVersionRepository,
)


class ContextResolver:
    """上下文事实解析器（只读）

    :param index_repo: 索引版本仓储（活动索引与文档版本归属）
    :param chunk_repo: 切片仓储（切片事实与父子结构）
    :param document_repo: 文档仓储（文件名快照事实）
    :param version_repo: 文档版本仓储（版本号快照事实）
    """

    def __init__(
        self,
        *,
        index_repo: IndexVersionRepository,
        chunk_repo: ChunkRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
    ) -> None:
        self._index_repo = index_repo
        self._chunk_repo = chunk_repo
        self._document_repo = document_repo
        self._version_repo = version_repo

    def resolve(self, kb_id: str) -> list[ContextSource]:
        """解析知识库全部可检索切片及其文档事实

        :param kb_id: 知识库主键（存在性由调用方校验）
        :return: 组装输入切片序列（按索引创建序、切片序号序）
        """
        facts: dict[str, tuple[str, str, int]] = {}
        sources: list[ContextSource] = []
        for index in self._index_repo.list_active_by_knowledge_base(kb_id):
            document_version_id = index.document_version_id
            if document_version_id not in facts:
                version = self._version_repo.get(document_version_id)
                document = (
                    self._document_repo.get(version.document_id)
                    if version is not None
                    else None
                )
                if version is None or document is None:
                    continue
                facts[document_version_id] = (
                    document.id,
                    document.display_name,
                    version.version_no,
                )
            document_id, file_name, version_no = facts[document_version_id]
            for stored in self._chunk_repo.list_index_chunks(index.id):
                chunk = stored.chunk
                sources.append(
                    ContextSource(
                        chunk_id=stored.id,
                        document_version_id=document_version_id,
                        document_id=document_id,
                        file_name=file_name,
                        version_no=version_no,
                        ordinal=chunk.ordinal,
                        parent_ordinal=chunk.parent_ordinal,
                        content=chunk.content,
                        section_path=chunk.section_path,
                        page_start=chunk.page_start,
                        block_ids=chunk.block_ids,
                    )
                )
        return sources
