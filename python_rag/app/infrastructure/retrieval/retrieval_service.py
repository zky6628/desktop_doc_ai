# -*- coding: utf-8 -*-
"""双路检索编排：指针链解析、查询嵌入、双路召回与 RRF 融合

查询为实时请求路径（不进任务队列）：按知识库指针链定位各文档版本
的活动索引，问题文本经查询侧向量化与预分词后分别召回向量与关键词
两路候选，融合为固定数量候选。知识库的存在性与删除状态由调用方
校验；切片事实缺失或归属不符的命中丢弃并计数（索引漂移由健康检查
发现，检索不伪造事实）。
"""
from app.domain import retrieval
from app.domain.entities import IndexVersion
from app.domain.ports import (
    ChunkRepository,
    IndexVersionRepository,
    KeywordIndexGateway,
    QueryEmbeddingGateway,
    TextTokenizer,
    VectorIndexGateway,
)
from app.domain.retrieval import (
    ChunkAnchor,
    IndexHit,
    RetrievalOutcome,
    RouteCandidate,
    fuse_route_rankings,
    rank_route_candidates,
)

# 命中及其来源索引版本（归属校验与候选事实补全的依据）
HitSource = tuple[IndexVersion, IndexHit]


class RetrievalService:
    """知识库级双路检索服务

    :param vector_top_k / keyword_top_k / fused_top_k / rrf_k: 检索
        参数（默认取领域冻结常量；配置行登记随查询链路接线）
    """

    def __init__(
        self,
        *,
        index_repo: IndexVersionRepository,
        chunk_repo: ChunkRepository,
        query_embedder: QueryEmbeddingGateway,
        vector_index: VectorIndexGateway,
        keyword_index: KeywordIndexGateway,
        tokenizer: TextTokenizer,
        vector_top_k: int = retrieval.VECTOR_TOP_K,
        keyword_top_k: int = retrieval.KEYWORD_TOP_K,
        fused_top_k: int = retrieval.FUSED_TOP_K,
        rrf_k: int = retrieval.RRF_K,
    ) -> None:
        self._index_repo = index_repo
        self._chunk_repo = chunk_repo
        self._query_embedder = query_embedder
        self._vector_index = vector_index
        self._keyword_index = keyword_index
        self._tokenizer = tokenizer
        self._vector_top_k = vector_top_k
        self._keyword_top_k = keyword_top_k
        self._fused_top_k = fused_top_k
        self._rrf_k = rrf_k

    def retrieve(self, kb_id: str, question: str) -> RetrievalOutcome:
        """按问题检索知识库，返回融合候选

        问题为空白时拒绝（ValueError）；知识库无可检索索引时返回空
        产出且不发起查询嵌入
        """
        if not question.strip():
            raise ValueError("检索问题不能为空")
        indexes = self._index_repo.list_active_by_knowledge_base(kb_id)
        if not indexes:
            return RetrievalOutcome(candidates=(), dropped_hit_count=0)

        query_vector = self._query_embedder.embed_query(question)
        tokens = self._tokenizer.tokenize([question])[0]

        vector_hits = self._collect_vector_hits(indexes, query_vector)
        keyword_hits = self._collect_keyword_hits(indexes, tokens)

        anchors = {
            anchor.chunk_id: anchor
            for anchor in self._chunk_repo.get_chunk_anchors(
                sorted({hit.chunk_id for _, hit in vector_hits + keyword_hits})
            )
        }
        vector_candidates, dropped = self._enrich(vector_hits, anchors)
        keyword_candidates, keyword_dropped = self._enrich(keyword_hits, anchors)

        vector_ranked = rank_route_candidates(vector_candidates, self._vector_top_k)
        keyword_ranked = rank_route_candidates(
            keyword_candidates, self._keyword_top_k
        )
        fused = fuse_route_rankings(
            vector_ranked,
            keyword_ranked,
            rrf_k=self._rrf_k,
            fused_top_k=self._fused_top_k,
        )
        return RetrievalOutcome(
            candidates=tuple(fused),
            dropped_hit_count=dropped + keyword_dropped,
        )

    def _collect_vector_hits(
        self, indexes: list[IndexVersion], query_vector: list[float]
    ) -> list[HitSource]:
        """逐活动集合查询向量路命中"""
        hits: list[HitSource] = []
        for index in indexes:
            if index.vector_collection is None:
                # 活动索引的资产名在激活前必已登记；缺失属数据漂移，
                # 跳过该索引不伪造命中
                continue
            found = self._vector_index.query_vectors(
                index.vector_collection, query_vector, self._vector_top_k
            )
            hits.extend((index, hit) for hit in found)
        return hits

    def _collect_keyword_hits(
        self, indexes: list[IndexVersion], tokens: list[str]
    ) -> list[HitSource]:
        """逐活动命名空间查询关键词路命中（空词元不发起查询）"""
        if not tokens:
            return []
        hits: list[HitSource] = []
        for index in indexes:
            if index.fts_namespace is None:
                # 同向量路：活动索引缺失资产名属数据漂移，跳过
                continue
            found = self._keyword_index.query_keywords(
                index.fts_namespace, tokens, self._keyword_top_k
            )
            hits.extend((index, hit) for hit in found)
        return hits

    @staticmethod
    def _enrich(
        hits: list[HitSource],
        anchors: dict[str, ChunkAnchor],
    ) -> tuple[list[RouteCandidate], int]:
        """命中回查切片锚点并校验归属，构造单路候选

        命中必须能回查到切片事实且归属产生它的索引版本；不满足视为
        索引漂移，丢弃并计数
        """
        candidates: list[RouteCandidate] = []
        dropped = 0
        for index, hit in hits:
            anchor = anchors.get(hit.chunk_id)
            if anchor is None or anchor.index_version_id != index.id:
                dropped += 1
                continue
            candidates.append(
                RouteCandidate(
                    chunk_id=hit.chunk_id,
                    document_version_id=index.document_version_id,
                    ordinal=anchor.ordinal,
                    raw_score=hit.raw_score,
                    score=hit.score,
                )
            )
        return candidates, dropped
