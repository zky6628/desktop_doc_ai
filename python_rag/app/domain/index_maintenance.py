# -*- coding: utf-8 -*-
"""索引维护：派生索引命名合同与健康检查

向量集合与 FTS 命名空间按索引版本隔离，归属信息由名字唯一承载
（命名合同可由索引版本 ID 确定性复算，无需 metadata）。健康检查
对照 SQLite 切片事实复核派生索引的数量与 ID 一致性，产出结构化
结论供设置页与证据包消费；只读无副作用，不进入任务队列。
"""
from dataclasses import dataclass

from app.domain.chunking import integrity_hash
from app.domain.entities import IndexVersion, IndexVersionStatus
from app.domain.ports import (
    ChunkRepository,
    IndexVersionRepository,
    KeywordIndexGateway,
    VectorIndexGateway,
)

# 向量集合命名前缀：集合名 = 前缀 + 索引版本 ID
COLLECTION_PREFIX = "wb-idx-"

# FTS 命名空间命名前缀：命名空间 = 前缀 + 索引版本 ID
FTS_NAMESPACE_PREFIX = "fts-"

# 健康问题码：机读结论，供设置页与证据包按类别呈现
ISSUE_VECTOR_COLLECTION_MISSING = "VECTOR_COLLECTION_MISSING"
ISSUE_VECTOR_ID_MISMATCH = "VECTOR_ID_MISMATCH"
ISSUE_KEYWORD_COUNT_MISMATCH = "KEYWORD_COUNT_MISMATCH"
ISSUE_CHUNK_COUNT_MISMATCH = "CHUNK_COUNT_MISMATCH"
ISSUE_INTEGRITY_HASH_MISMATCH = "INTEGRITY_HASH_MISMATCH"
ISSUE_CHUNK_FACTS_MISSING = "CHUNK_FACTS_MISSING"


def collection_name(index_version_id: str) -> str:
    """按命名合同解析索引版本的向量集合名"""
    return f"{COLLECTION_PREFIX}{index_version_id}"


def fts_namespace_name(index_version_id: str) -> str:
    """按命名合同解析索引版本的 FTS 命名空间"""
    return f"{FTS_NAMESPACE_PREFIX}{index_version_id}"


@dataclass(frozen=True)
class IndexHealthFinding:
    """单个活动索引版本的健康结论

    issues 为机读问题码集合；healthy 为真当且仅当 issues 为空。
    向量差集仅在 ID 不一致时填充，供修复定位缺失/异物记录
    """

    index_version_id: str
    document_version_id: str
    healthy: bool
    issues: tuple[str, ...]
    expected_child_count: int
    actual_vector_count: int
    actual_keyword_count: int
    missing_vector_ids: tuple[str, ...]
    unexpected_vector_ids: tuple[str, ...]


@dataclass(frozen=True)
class IndexHealthReport:
    """一次健康检查的整体结论

    孤儿与命名不符的派生资产属于清洁度问题而非检索健康问题，
    单独列出；不健康仅由活动索引的比对失败决定
    """

    healthy: bool
    findings: tuple[IndexHealthFinding, ...]
    orphan_collections: tuple[str, ...]
    orphan_namespaces: tuple[str, ...]
    unexpected_collections: tuple[str, ...]
    unexpected_namespaces: tuple[str, ...]


class IndexHealthService:
    """派生索引健康检查（只读）

    :param index_repo: 索引版本仓储（活动索引与资产归属的事实源）
    :param chunk_repo: 切片仓储（子切片事实与完整性基准的比对输入）
    :param vector_index: 向量索引网关（数量与 ID 集合读取）
    :param keyword_index: 关键词索引网关（命名空间文档数读取）
    """

    def __init__(
        self,
        *,
        index_repo: IndexVersionRepository,
        chunk_repo: ChunkRepository,
        vector_index: VectorIndexGateway,
        keyword_index: KeywordIndexGateway,
    ) -> None:
        self._index_repo = index_repo
        self._chunk_repo = chunk_repo
        self._vector_index = vector_index
        self._keyword_index = keyword_index

    def check_all(self) -> IndexHealthReport:
        """检查全部活动索引并扫描孤儿派生资产

        逐活动索引比对：切片行数与登记基准、完整性哈希重算、向量
        数量与 ID 集合、关键词命名空间文档数。孤儿扫描覆盖全部
        索引版本行（含在途 staging）登记或按命名合同派生的资产名，
        命名不符合合同的集合与命名空间只报告不动
        """
        indexes = self._index_repo.list_all()
        referenced_collections: set[str] = set()
        referenced_namespaces: set[str] = set()
        for index in indexes:
            referenced_collections.add(
                index.vector_collection or collection_name(index.id)
            )
            referenced_namespaces.add(
                index.fts_namespace or fts_namespace_name(index.id)
            )

        findings = tuple(
            self._check_index(index)
            for index in indexes
            if index.status is IndexVersionStatus.ACTIVE
        )

        collections = self._vector_index.list_collections()
        namespaces = self._keyword_index.list_namespaces()
        orphan_collections = tuple(
            sorted(
                name
                for name in collections
                if name.startswith(COLLECTION_PREFIX)
                and name not in referenced_collections
            )
        )
        orphan_namespaces = tuple(
            sorted(
                name
                for name in namespaces
                if name.startswith(FTS_NAMESPACE_PREFIX)
                and name not in referenced_namespaces
            )
        )
        unexpected_collections = tuple(
            sorted(
                name
                for name in collections
                if not name.startswith(COLLECTION_PREFIX)
            )
        )
        unexpected_namespaces = tuple(
            sorted(
                name
                for name in namespaces
                if not name.startswith(FTS_NAMESPACE_PREFIX)
            )
        )
        return IndexHealthReport(
            healthy=all(finding.healthy for finding in findings),
            findings=findings,
            orphan_collections=orphan_collections,
            orphan_namespaces=orphan_namespaces,
            unexpected_collections=unexpected_collections,
            unexpected_namespaces=unexpected_namespaces,
        )

    def check_knowledge_base(self, kb_id: str) -> tuple[IndexHealthFinding, ...]:
        """检查指定知识库的活动索引（KB 级健康检查任务使用）

        只比对活动索引与切片事实，不做全库孤儿扫描（孤儿清洁度属于
        全量清扫职责）；知识库无可检索索引时返回空元组
        """
        indexes = self._index_repo.list_active_by_knowledge_base(kb_id)
        return tuple(self._check_index(index) for index in indexes)

    def _check_index(self, index: IndexVersion) -> IndexHealthFinding:
        """比对单个活动索引与切片事实"""
        issues: list[str] = []
        stored_chunks = self._chunk_repo.list_index_chunks(index.id)
        children = [
            stored for stored in stored_chunks if stored.chunk.parent_ordinal is not None
        ]
        if index.chunk_count is not None and index.chunk_count != len(children):
            issues.append(ISSUE_CHUNK_COUNT_MISMATCH)
        if index.integrity_hash is not None and children:
            recomputed = integrity_hash([stored.chunk for stored in children])
            if recomputed != index.integrity_hash:
                issues.append(ISSUE_INTEGRITY_HASH_MISMATCH)

        expected_ids = [stored.id for stored in children]
        collection = index.vector_collection or collection_name(index.id)
        actual_count = self._vector_index.count_vectors(collection)
        actual_ids = set(self._vector_index.list_vector_ids(collection))
        missing = [chunk_id for chunk_id in expected_ids if chunk_id not in actual_ids]
        unexpected = sorted(actual_ids.difference(expected_ids))
        if not children:
            # 活动索引没有任何检索单元：切片事实缺失（无法检索）
            issues.append(ISSUE_CHUNK_FACTS_MISSING)
        elif actual_count == 0 and not actual_ids:
            # 集合整体缺失或为空（适配层对二者同样返回零值）
            issues.append(ISSUE_VECTOR_COLLECTION_MISSING)
        elif actual_count != len(expected_ids) or missing or unexpected:
            issues.append(ISSUE_VECTOR_ID_MISMATCH)

        namespace = index.fts_namespace or fts_namespace_name(index.id)
        keyword_count = self._keyword_index.count_documents(namespace)
        if keyword_count != len(children):
            issues.append(ISSUE_KEYWORD_COUNT_MISMATCH)

        return IndexHealthFinding(
            index_version_id=index.id,
            document_version_id=index.document_version_id,
            healthy=not issues,
            issues=tuple(issues),
            expected_child_count=len(children),
            actual_vector_count=actual_count,
            actual_keyword_count=keyword_count,
            missing_vector_ids=tuple(missing),
            unexpected_vector_ids=tuple(unexpected),
        )
