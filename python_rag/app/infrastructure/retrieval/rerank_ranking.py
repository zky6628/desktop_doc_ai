# -*- coding: utf-8 -*-
"""重排排名：候选重排与瞬态失败的冻结降级策略

查询链路与本地调试检索共用同一套重排语义：瞬态失败进程内重试一次，
重试耗尽降级为 RRF 前 N（检索候选本身即 RRF 序，降级保持与生产一致
的候选次序并显式标注）；认证/配额/协议类失败不降级，原样抛出由调用
方按错误族处理。
"""
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.errors import RerankTransientError
from app.domain.generation import RERANK_INLINE_RETRIES
from app.domain.ports import RerankGateway
from app.domain.rerank import RERANK_TOP_N
from app.domain.retrieval import RetrievalCandidate


@dataclass(frozen=True)
class RerankRanking:
    """重排结果：是否降级、入选候选序列（重排名次序）与重排分数表

    降级时 ranked_ids 为 RRF 前 N 的候选主键，rerank_scores 为空；
    rerank_rank 由调用方按 ranked_ids 位置推导
    """

    degraded: bool
    ranked_ids: list[str]
    rerank_scores: dict[str, float]


def rank_with_rerank(
    rerank_gateway: RerankGateway,
    question: str,
    candidates: Sequence[RetrievalCandidate],
    texts: Sequence[str],
    *,
    rerank_top_n: int | None = None,
) -> RerankRanking:
    """重排候选：瞬态重试一次后按冻结策略降级为 RRF 前 N

    :param rerank_gateway: 重排网关
    :param question: 查询问题（重排输入）
    :param candidates: 融合候选（RRF 序，重排输入的次序依据）
    :param texts: 与候选一一对应的重排文本（可短于候选——缺失事实的
        候选不参与重排，命中下标越界自然跳过）
    :param rerank_top_n: 入选数量覆盖（仅供本地调试检索；None 用冻结常量）
    :raises RerankTransientError: 内联重试耗尽前不应抛出（已转降级）；
        本函数不再向外传播瞬态错误
    """
    top_n = RERANK_TOP_N if rerank_top_n is None else rerank_top_n
    if not texts:
        # 无可重排文本属数据漂移（候选存在而切片事实缺失），
        # 与瞬态降级同形：回退 RRF 序并标注降级
        return RerankRanking(
            True, [candidate.chunk_id for candidate in candidates[:top_n]], {}
        )
    try:
        hits = _rerank_with_retry(rerank_gateway, question, list(texts))
    except RerankTransientError:
        # 冻结降级策略：使用 RRF 前 N（检索候选本身即 RRF 序）
        ranked_ids = [candidate.chunk_id for candidate in candidates[:top_n]]
        return RerankRanking(True, ranked_ids, {})
    rerank_scores: dict[str, float] = {}
    for hit in hits:
        if hit.index < len(candidates):
            rerank_scores[candidates[hit.index].chunk_id] = hit.score
    top_ids = [candidates[hit.index].chunk_id for hit in hits[:top_n]]
    return RerankRanking(False, top_ids, rerank_scores)


def _rerank_with_retry(
    rerank_gateway: RerankGateway, question: str, texts: list[str]
):
    """进程内即重试（仅瞬态类）；重试耗尽抛原错误交降级路径"""
    last_error: RerankTransientError | None = None
    for _ in range(RERANK_INLINE_RETRIES + 1):
        try:
            return rerank_gateway.rerank(question, texts)
        except RerankTransientError as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]
