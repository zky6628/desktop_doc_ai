# -*- coding: utf-8 -*-
"""检索融合测试：RRF 分值、稳定排序与固定候选数"""
import json

import pytest

from app.domain import retrieval
from app.domain.retrieval import (
    RouteCandidate,
    fuse_route_rankings,
    rank_route_candidates,
    retrieval_config_json,
)


def _candidate(
    chunk_id: str,
    *,
    dv: str = "dv-1",
    ordinal: int = 0,
    score: float = 1.0,
    raw: float = 0.0,
) -> RouteCandidate:
    """构造单路候选（默认同文档版本，便于按序号观察稳定排序）"""
    return RouteCandidate(
        chunk_id=chunk_id,
        document_version_id=dv,
        ordinal=ordinal,
        raw_score=raw,
        score=score,
    )


def test_frozen_candidate_counts_and_rrf_constant():
    """双路召回与融合候选数为冻结合同 20/20/20，RRF 常数为 60"""
    assert retrieval.VECTOR_TOP_K == 20
    assert retrieval.KEYWORD_TOP_K == 20
    assert retrieval.FUSED_TOP_K == 20
    assert retrieval.RRF_K == 60


def test_retrieval_config_json_is_canonical_and_deterministic():
    """检索配置内容为规范 JSON：重复产出逐字节一致且字段完整"""
    payload = json.loads(retrieval_config_json())
    assert payload == {
        "retrieval_config_version": retrieval.RETRIEVAL_CONFIG_VERSION,
        "vector_top_k": 20,
        "keyword_top_k": 20,
        "fused_top_k": 20,
        "rrf_k": 60,
        "query_text_type": "query",
    }
    assert retrieval_config_json() == retrieval_config_json()


def test_rank_route_orders_by_score_then_dv_then_ordinal():
    """单路排名：分数降序，同分按文档版本 ID 与切片序号稳定排序"""
    candidates = [
        _candidate("c-low", dv="dv-1", ordinal=1, score=0.5),
        _candidate("c-high-dv2", dv="dv-2", ordinal=9, score=1.0),
        _candidate("c-high-dv1", dv="dv-1", ordinal=0, score=1.0),
    ]

    ranked = rank_route_candidates(candidates, top_k=20)

    assert [candidate.chunk_id for _, candidate in ranked] == [
        "c-high-dv1",
        "c-high-dv2",
        "c-low",
    ]
    assert [rank for rank, _ in ranked] == [1, 2, 3]


def test_rank_route_truncates_to_top_k():
    """单路排名后按 top_k 截断（同分按稳定键取前若干）"""
    candidates = [_candidate(f"c-{n}", ordinal=n, score=1.0) for n in range(5)]

    ranked = rank_route_candidates(candidates, top_k=3)

    assert [candidate.chunk_id for _, candidate in ranked] == ["c-0", "c-1", "c-2"]


def test_fuse_merges_dual_route_hits_with_rrf_sum():
    """双路命中按切片去重，RRF 分值为两路之和且原始分数保留"""
    vector_ranked = [
        (1, _candidate("c1", score=0.9, raw=0.1)),
        (2, _candidate("c2", score=0.8, raw=0.2)),
    ]
    keyword_ranked = [(1, _candidate("c2", score=7.0, raw=-7.0))]

    fused = fuse_route_rankings(
        vector_ranked, keyword_ranked, rrf_k=60, fused_top_k=20
    )

    by_id = {candidate.chunk_id: candidate for candidate in fused}
    # c2 双路命中：1/(60+2) + 1/(60+1)，融合名次高于单路 c1
    assert by_id["c2"].rrf_rank == 1
    assert by_id["c2"].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["c2"].vector_rank == 2
    assert by_id["c2"].vector_score == pytest.approx(0.8)
    assert by_id["c2"].keyword_rank == 1
    assert by_id["c2"].keyword_raw_score == pytest.approx(-7.0)
    # c1 仅向量路命中，关键词路字段为空
    assert by_id["c1"].rrf_score == pytest.approx(1 / 61)
    assert by_id["c1"].vector_rank == 1
    assert by_id["c1"].keyword_rank is None
    assert by_id["c1"].keyword_raw_score is None


def test_fuse_breaks_score_ties_by_best_rank_then_dv_then_ordinal():
    """融合同分按最佳单路 rank、文档版本 ID、切片序号稳定排序"""
    vector_ranked = [(1, _candidate("c-a", dv="dv-2", ordinal=0))]
    keyword_ranked = [
        (1, _candidate("c-b", dv="dv-1", ordinal=5)),
        (2, _candidate("c-c", dv="dv-1", ordinal=3)),
    ]

    fused = fuse_route_rankings(
        vector_ranked, keyword_ranked, rrf_k=60, fused_top_k=20
    )

    # c-a 与 c-b 同分（各 1/61）同最佳 rank（1），按文档版本 ID 排序；
    # c-c 分数更低（1/62）殿后
    assert [candidate.chunk_id for candidate in fused] == ["c-b", "c-a", "c-c"]
    assert [candidate.rrf_rank for candidate in fused] == [1, 2, 3]


def test_fuse_truncates_to_fused_top_k():
    """融合产出按 fused_top_k 截断且名次从 1 连续"""
    vector_ranked = [
        (rank, _candidate(f"v-{rank}", ordinal=rank)) for rank in range(1, 6)
    ]

    fused = fuse_route_rankings(vector_ranked, [], rrf_k=60, fused_top_k=3)

    assert [candidate.chunk_id for candidate in fused] == ["v-1", "v-2", "v-3"]
    assert [candidate.rrf_rank for candidate in fused] == [1, 2, 3]
