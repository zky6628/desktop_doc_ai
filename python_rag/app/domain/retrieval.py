# -*- coding: utf-8 -*-
"""检索融合：单路命中、RRF 融合与检索配置

固定链路前段：向量与关键词两路各自召回并按统一分数排序取固定数量，
再以 RRF 融合去重产出固定数量候选。排序全部确定性：分数之外以文档
版本 ID 与切片序号稳定排序，相同输入必然得到相同候选次序。分数语义
统一为越高越相关，供应方原始分数随候选保留供追踪与评测。
"""
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.embedding import EMBEDDING_QUERY_TEXT_TYPE
from app.domain.parsing import canonical_json

# 检索配置版本：候选数/融合参数语义变化时必须递增
RETRIEVAL_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
RETRIEVAL_CONFIG_TYPE = "retrieval"

# 双路召回与融合的固定候选数（性能与评测合同，普通模式不接受客户端修改）
VECTOR_TOP_K = 20
KEYWORD_TOP_K = 20
FUSED_TOP_K = 20

# RRF 融合常数：rank 从 1 起，rrf = sum(1 / (RRF_K + rank))
RRF_K = 60


def retrieval_config_json() -> str:
    """产出当前检索参数集的规范 JSON（配置行的内容与哈希来源）

    查询侧文本侧别一并入档：查询与文档必须使用兼容模型、维度与侧别
    参数（模型与维度沿用嵌入配置，两侧口径不混用）。

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "retrieval_config_version": RETRIEVAL_CONFIG_VERSION,
            "vector_top_k": VECTOR_TOP_K,
            "keyword_top_k": KEYWORD_TOP_K,
            "fused_top_k": FUSED_TOP_K,
            "rrf_k": RRF_K,
            "query_text_type": EMBEDDING_QUERY_TEXT_TYPE,
        }
    )


@dataclass(frozen=True)
class IndexHit:
    """单路索引命中：切片主键与其分数

    score 为统一语义的标准化分数（越高越相关）；raw_score 为供应方
    原始返回值（向量距离 / 关键词 bm25 值），随命中保留供追踪
    """

    chunk_id: str
    raw_score: float
    score: float


@dataclass(frozen=True)
class ChunkAnchor:
    """切片锚点事实：命中回查切片表得到的归属索引版本与序号"""

    chunk_id: str
    index_version_id: str
    ordinal: int


@dataclass(frozen=True)
class RouteCandidate:
    """单路候选：已补全文档版本与切片序号事实的索引命中"""

    chunk_id: str
    document_version_id: str
    ordinal: int
    raw_score: float
    score: float


@dataclass(frozen=True)
class RetrievalCandidate:
    """融合候选：检索链路的固定产出，携带各路排名/分数与融合名次

    单路字段为空表示该候选未在该路命中
    """

    chunk_id: str
    document_version_id: str
    ordinal: int
    rrf_rank: int
    rrf_score: float
    vector_rank: int | None
    vector_raw_score: float | None
    vector_score: float | None
    keyword_rank: int | None
    keyword_raw_score: float | None
    keyword_score: float | None


@dataclass(frozen=True)
class CandidateRecord:
    """待持久化的候选快照行（各阶段排名/分数与是否进入上下文）"""

    query_run_id: str
    chunk_id: str | None
    source: str
    vector_rank: int | None
    vector_score: float | None
    keyword_rank: int | None
    keyword_score: float | None
    rrf_rank: int
    rrf_score: float
    rerank_rank: int | None
    rerank_score: float | None
    in_context: bool


@dataclass(frozen=True)
class RetrievalOutcome:
    """一次检索的产出：融合候选、被丢弃的命中计数与各路召回计数

    dropped_hit_count 记录切片事实缺失或归属不符而无法进入融合的
    命中数（索引漂移由健康检查定位修复，检索不伪造事实）；
    vector_hit_count / keyword_hit_count 为融合前各路的原始命中数
    （含后续被丢弃的部分），供调试检索观察各阶段规模
    """

    candidates: tuple[RetrievalCandidate, ...]
    dropped_hit_count: int
    vector_hit_count: int = 0
    keyword_hit_count: int = 0


def rank_route_candidates(
    candidates: Sequence[RouteCandidate], top_k: int
) -> list[tuple[int, RouteCandidate]]:
    """单路排名：分数降序，同分按文档版本 ID、切片序号稳定排序

    :param candidates: 单路候选（未排名）
    :param top_k: 排名后保留的候选数上限
    :return: (rank 从 1 起, 候选) 序列
    """
    ordered = sorted(
        candidates,
        key=lambda item: (-item.score, item.document_version_id, item.ordinal),
    )
    return [
        (rank, candidate)
        for rank, candidate in enumerate(ordered[:top_k], start=1)
    ]


def fuse_route_rankings(
    vector_ranked: Sequence[tuple[int, RouteCandidate]],
    keyword_ranked: Sequence[tuple[int, RouteCandidate]],
    *,
    rrf_k: int,
    fused_top_k: int,
) -> list[RetrievalCandidate]:
    """RRF 融合两路排名：按切片主键去重后取固定数量候选

    rrf_score = sum(1 / (rrf_k + 单路 rank))，rank 从 1 起；相同融合
    分数按最佳单路 rank、文档版本 ID、切片序号稳定排序

    :param vector_ranked: 向量路 (rank, 候选) 序列
    :param keyword_ranked: 关键词路 (rank, 候选) 序列
    :param rrf_k: RRF 常数
    :param fused_top_k: 融合产出候选数上限
    :return: 融合名次从 1 起的候选序列
    """
    vector_hits = {candidate.chunk_id: candidate for _, candidate in vector_ranked}
    keyword_hits = {candidate.chunk_id: candidate for _, candidate in keyword_ranked}
    vector_ranks = {candidate.chunk_id: rank for rank, candidate in vector_ranked}
    keyword_ranks = {candidate.chunk_id: rank for rank, candidate in keyword_ranked}

    facts: dict[str, RouteCandidate] = dict(vector_hits)
    for chunk_id, candidate in keyword_hits.items():
        facts.setdefault(chunk_id, candidate)

    scored: list[tuple[float, int, str]] = []
    for chunk_id in facts:
        ranks = [
            rank
            for rank in (vector_ranks.get(chunk_id), keyword_ranks.get(chunk_id))
            if rank is not None
        ]
        rrf_score = sum(1.0 / (rrf_k + rank) for rank in ranks)
        scored.append((rrf_score, min(ranks), chunk_id))
    scored.sort(
        key=lambda item: (
            -item[0],
            item[1],
            facts[item[2]].document_version_id,
            facts[item[2]].ordinal,
        )
    )

    fused: list[RetrievalCandidate] = []
    for rrf_rank, (rrf_score, _, chunk_id) in enumerate(
        scored[:fused_top_k], start=1
    ):
        vector = vector_hits.get(chunk_id)
        keyword = keyword_hits.get(chunk_id)
        base = facts[chunk_id]
        fused.append(
            RetrievalCandidate(
                chunk_id=chunk_id,
                document_version_id=base.document_version_id,
                ordinal=base.ordinal,
                rrf_rank=rrf_rank,
                rrf_score=rrf_score,
                vector_rank=vector_ranks.get(chunk_id),
                vector_raw_score=vector.raw_score if vector is not None else None,
                vector_score=vector.score if vector is not None else None,
                keyword_rank=keyword_ranks.get(chunk_id),
                keyword_raw_score=keyword.raw_score if keyword is not None else None,
                keyword_score=keyword.score if keyword is not None else None,
            )
        )
    return fused
