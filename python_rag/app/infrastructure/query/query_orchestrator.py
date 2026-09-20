# -*- coding: utf-8 -*-
"""查询编排：检索 → 重排（可降级）→ 组装 → 流式生成 → 引用快照

查询为实时路径（不进任务队列）：创建查询运行后由后台线程执行管线，
事件即产即写事件存储（SSE 轮询消费，断线重连按序号重放）；取消在
token 检查点生效（保留已生成正文与引用）；重排瞬态失败按冻结策略
降级为 RRF 前 5 并记录（认证/配额类直接失败）；检索为空直接拒答
不调用模型；幂等重放不重复启动，并发重入由状态机迁移原子性兜底。
"""
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.citation import (
    build_citation_record,
    extract_citation_drafts,
    validate_citations,
)
from app.domain.context import (
    CONTEXT_TOKEN_BUDGET,
    NO_EVIDENCE_REFUSAL,
    OUTPUT_RESERVE_TOKENS,
    assemble_context,
    build_prompt_messages,
    context_config_json,
    token_estimate,
)
from app.domain.entities import QueryRun, QueryRunState
from app.domain.errors import (
    QueryStateConflictError,
    RepositoryError,
    RerankTransientError,
)
from app.domain.generation import RERANK_INLINE_RETRIES, generation_config_json
from app.domain.ports import (
    CitationRepository,
    ConversationRepository,
    GenerationGateway,
    PipelineConfigRepository,
    QueryEventStore,
    QueryRunRepository,
    RerankGateway,
)
from app.domain.rerank import RERANK_TOP_N, rerank_config_json
from app.domain.retrieval import (
    CandidateRecord,
    retrieval_config_json,
)
from app.infrastructure.retrieval import ContextResolver, RetrievalService

# 未知异常的兜底错误码（终态落库与 SSE error 事件）
_INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class QueryStart:
    """查询启动结果：运行快照与会话归属

    conversation_id 在新建查询路径上由本次同步创建并返回；幂等重放
    路径不重复建会话，会话归属以最终聚合为准（此处为 None）
    """

    run: QueryRun
    conversation_id: str | None


class QueryOrchestrator:
    """知识库问答编排器

    :param token_flush_interval / token_flush_batch: token 批次合并
        写库的触发条件（时间秒 / 数量，先到先触发）
    :param token_retention_seconds: token 批次终态后保留窗（30 分钟）
    :param clock: 单调时钟（测试注入）
    """

    def __init__(
        self,
        *,
        run_repo: QueryRunRepository,
        event_store: QueryEventStore,
        conversation_repo: ConversationRepository,
        config_repo: PipelineConfigRepository,
        citation_repo: CitationRepository,
        retrieval_service: RetrievalService,
        resolver: ContextResolver,
        rerank_gateway: RerankGateway,
        generation_gateway: GenerationGateway,
        token_flush_interval: float = 0.2,
        token_flush_batch: int = 32,
        token_retention_seconds: int = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run_repo = run_repo
        self._event_store = event_store
        self._conversation_repo = conversation_repo
        self._config_repo = config_repo
        self._citation_repo = citation_repo
        self._retrieval_service = retrieval_service
        self._resolver = resolver
        self._rerank_gateway = rerank_gateway
        self._generation_gateway = generation_gateway
        self._token_flush_interval = token_flush_interval
        self._token_flush_batch = token_flush_batch
        self._token_retention_seconds = token_retention_seconds
        self._clock = clock

    def start_query(
        self,
        *,
        kb_id: str,
        question: str,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> QueryStart:
        """创建查询运行并启动后台执行（幂等键重放返回既有查询）"""
        if not question.strip():
            raise ValueError("问题不能为空")
        config_ids = {
            "retrieval": self._config_repo.ensure_config(
                "retrieval", retrieval_config_json()
            ),
            "rerank": self._config_repo.ensure_config(
                "rerank", rerank_config_json()
            ),
            "context": self._config_repo.ensure_config(
                "context", context_config_json()
            ),
            "generation": self._config_repo.ensure_config(
                "generation", generation_config_json()
            ),
        }
        run = self._run_repo.create(
            kb_id=kb_id,
            question=question,
            config_ids=config_ids,
            idempotency_key=idempotency_key,
        )
        if run.state is not QueryRunState.QUEUED:
            # 幂等重放：既有查询已在执行或已终态，不重复启动
            return QueryStart(run=run, conversation_id=None)
        # 懒清理：新查询创建时顺带回收已过期的 token 批次（03 保留期）
        self._event_store.purge_expired_tokens()
        conversation = self._conversation_repo.ensure_conversation(
            kb_id, conversation_id
        )
        user_message_id = self._conversation_repo.add_message(
            conversation, "user", question
        )
        threading.Thread(
            target=self._execute,
            args=(run.id, kb_id, question, conversation, user_message_id),
            daemon=True,
            name=f"query-{run.id[:8]}",
        ).start()
        return QueryStart(run=run, conversation_id=conversation)

    def request_cancel(self, run_id: str) -> QueryRun:
        """请求取消查询（幂等）"""
        return self._run_repo.request_cancel(run_id)

    def get_run(self, run_id: str) -> QueryRun | None:
        return self._run_repo.get(run_id)

    def read_events(self, run_id: str, after_seq: int = 0):
        return self._event_store.read_after(run_id, after_seq)

    def fail_interrupted(self) -> int:
        """启动恢复：把进程重启残留的未完成查询置为失败"""
        return self._run_repo.fail_interrupted()

    def _execute(
        self,
        run_id: str,
        kb_id: str,
        question: str,
        conversation_id: str,
        user_message_id: str,
    ) -> None:
        """后台执行查询管线（异常一律落失败终态，不向线程外传播）"""
        try:
            try:
                self._run_repo.mark_running(run_id)
            except QueryStateConflictError:
                # 幂等重放与首请求并发竞态：另一线程已接管执行，退出
                return
            self._event_store.append(
                run_id,
                event_type="meta",
                payload={"query_id": run_id, "knowledge_base_id": kb_id},
            )
            self._emit_stage(run_id, "retrieval_started")
            retrieval_started = self._clock()
            result = self._retrieval_service.retrieve(kb_id, question)
            retrieval_ms = int((self._clock() - retrieval_started) * 1000)
            self._emit_stage(
                run_id, "retrieval_completed", {"candidates": len(result.candidates)}
            )

            if not result.candidates:
                self._finalize_refusal(
                    run_id, kb_id, conversation_id, user_message_id
                )
                return

            sources = self._resolver.resolve(kb_id)
            sources_by_id = {source.chunk_id: source for source in sources}
            candidate_texts = [
                sources_by_id[candidate.chunk_id].content
                for candidate in result.candidates
                if candidate.chunk_id in sources_by_id
            ]
            rerank_started = self._clock()
            degraded, ranked_ids, rerank_scores = self._rank_candidates(
                question, result.candidates, candidate_texts
            )
            rerank_ms = int((self._clock() - rerank_started) * 1000)
            if degraded:
                self._emit_stage(run_id, "rerank_degraded")
            self._emit_stage(run_id, "rerank_completed")

            prompt_started = self._clock()
            budget = (
                CONTEXT_TOKEN_BUDGET
                - OUTPUT_RESERVE_TOKENS
                - token_estimate(question)
            )
            assembled = assemble_context(sources, ranked_ids, token_budget=budget)
            prompt_build_ms = int((self._clock() - prompt_started) * 1000)
            if not assembled.blocks:
                self._finalize_refusal(
                    run_id, kb_id, conversation_id, user_message_id
                )
                return

            # 候选快照落库：各阶段排名/分数与是否进入上下文（评测主体）
            in_context_ids = {block.chunk_id for block in assembled.blocks}
            rerank_rank_by_chunk = {
                chunk_id: position
                for position, chunk_id in enumerate(ranked_ids, start=1)
            }
            candidate_records = [
                CandidateRecord(
                    query_run_id=run_id,
                    chunk_id=candidate.chunk_id,
                    source=(
                        "dual"
                        if candidate.vector_rank is not None
                        and candidate.keyword_rank is not None
                        else (
                            "vector"
                            if candidate.vector_rank is not None
                            else "keyword"
                        )
                    ),
                    vector_rank=candidate.vector_rank,
                    vector_score=candidate.vector_score,
                    keyword_rank=candidate.keyword_rank,
                    keyword_score=candidate.keyword_score,
                    rrf_rank=candidate.rrf_rank,
                    rrf_score=candidate.rrf_score,
                    rerank_rank=rerank_rank_by_chunk.get(candidate.chunk_id)
                    if not degraded
                    else None,
                    rerank_score=rerank_scores.get(candidate.chunk_id),
                    in_context=candidate.chunk_id in in_context_ids,
                )
                for candidate in result.candidates
            ]
            self._run_repo.record_candidates(run_id, candidate_records)

            self._emit_stage(run_id, "generation_started")
            answer_text, cancelled, model_ttft_ms = self._stream_generation(
                run_id, assembled, question
            )
            if cancelled:
                self._finalize_cancelled(
                    run_id, kb_id, conversation_id, user_message_id,
                    answer_text, assembled,
                )
                return

            message_id = self._persist_citations(
                run_id, kb_id, conversation_id, user_message_id,
                answer_text, assembled, result.candidates, rerank_scores,
            )
            usage = self._generation_gateway.last_usage
            self._run_repo.record_segments(
                run_id,
                retrieval_ms=retrieval_ms,
                rerank_ms=rerank_ms,
                prompt_build_ms=prompt_build_ms,
                model_ttft_ms=model_ttft_ms,
                input_tokens=usage[0] if usage else None,
                output_tokens=usage[1] if usage else None,
            )
            self._event_store.append(run_id, event_type="done", payload={})
            self._event_store.expire_tokens(run_id, self._token_retention_seconds)
            self._run_repo.finalize(
                run_id,
                state=QueryRunState.COMPLETED.value,
                degraded=degraded,
                assistant_message_id=message_id,
            )
        except RepositoryError as exc:
            self._fail(run_id, getattr(exc, "error_code", _INTERNAL_ERROR), str(exc))
        except Exception as exc:  # noqa: BLE001 - 线程边界兜底，必须落终态
            self._fail(run_id, _INTERNAL_ERROR, type(exc).__name__)

    def _rank_candidates(self, question, candidates, candidate_texts):
        """重排候选（瞬态重试一次后按冻结策略降级为 RRF 前 5）"""
        if not candidate_texts:
            return True, [], {}
        try:
            hits = self._rerank_with_retry(question, candidate_texts)
        except RerankTransientError:
            # 冻结降级策略：使用 RRF 前 5（检索候选本身即 RRF 序）
            ranked_ids = [
                candidate.chunk_id for candidate in candidates[:RERANK_TOP_N]
            ]
            return True, ranked_ids, {}
        rerank_scores: dict[str, float] = {}
        for hit in hits:
            if hit.index < len(candidates):
                rerank_scores[candidates[hit.index].chunk_id] = hit.score
        top_ids = [candidates[hit.index].chunk_id for hit in hits[:RERANK_TOP_N]]
        return False, top_ids, rerank_scores

    def _rerank_with_retry(self, question: str, texts: list[str]):
        """进程内即重试（仅瞬态类）；重试耗尽抛原错误交降级路径"""
        last_error: RerankTransientError | None = None
        for _ in range(RERANK_INLINE_RETRIES + 1):
            try:
                return self._rerank_gateway.rerank(question, texts)
            except RerankTransientError as exc:
                last_error = exc
        raise last_error  # type: ignore[misc]

    def _stream_generation(self, run_id, assembled, question):
        """流式生成：增量累积、批次落库、取消检查点

        :return: (全文, 是否取消, 模型首 token 耗时 ms)
        """
        messages = build_prompt_messages(assembled.blocks, question)
        parts: list[str] = []
        buffer: list[str] = []
        token_seq = 0
        batch_start = 0
        last_flush = self._clock()
        model_ttft_ms = 0
        for delta in self._generation_gateway.stream_answer(messages):
            if token_seq == 0:
                model_ttft_ms = int((self._clock() - last_flush) * 1000)
                last_flush = self._clock()
            if self._run_repo.is_cancel_requested(run_id):
                self._flush_tokens(run_id, buffer, batch_start, token_seq)
                return "".join(parts), True, model_ttft_ms
            token_seq += 1
            parts.append(delta)
            buffer.append(delta)
            if token_seq == 1:
                self._run_repo.mark_first_token(run_id)
            now = self._clock()
            if (
                len(buffer) >= self._token_flush_batch
                or now - last_flush >= self._token_flush_interval
            ):
                self._flush_tokens(run_id, buffer, batch_start, token_seq)
                buffer = []
                batch_start = token_seq
                last_flush = now
        self._flush_tokens(run_id, buffer, batch_start, token_seq)
        return "".join(parts), False, model_ttft_ms

    def _flush_tokens(self, run_id, buffer, batch_start, token_seq) -> None:
        if not buffer:
            return
        self._event_store.append(
            run_id,
            event_type="tokens",
            token_text="".join(buffer),
            token_seq_start=batch_start + 1,
            token_seq_end=token_seq,
        )

    def _persist_citations(
        self, run_id, kb_id, conversation_id, user_message_id,
        answer_text, assembled, candidates, rerank_scores,
    ) -> str:
        """提取并校验引用，落快照与 citation 事件；返回助手消息主键"""
        drafts = extract_citation_drafts(answer_text)
        validations = validate_citations(drafts, list(assembled.blocks))
        candidates_by_chunk = {
            candidate.chunk_id: candidate for candidate in candidates
        }
        message_id = self._conversation_repo.add_message(
            conversation_id, "assistant", answer_text
        )
        self._run_repo.attach_message_ids(
            run_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=message_id,
        )
        records = []
        for order, validation in enumerate(validations, start=1):
            chunk_id = validation.block.chunk_id if validation.block else None
            candidate = candidates_by_chunk.get(chunk_id) if chunk_id else None
            record = build_citation_record(
                validation,
                assistant_message_id=message_id,
                citation_order=order,
                knowledge_base_id=kb_id,
                query_run_id=run_id,
                vector_score=candidate.vector_score if candidate else None,
                keyword_score=candidate.keyword_score if candidate else None,
                fusion_score=candidate.rrf_score if candidate else None,
                rerank_score=rerank_scores.get(chunk_id),
            )
            records.append(record)
            self._event_store.append(
                run_id, event_type="citation", payload=_citation_payload(record)
            )
        if records:
            self._citation_repo.insert_citations(records)
        return message_id

    def _finalize_refusal(
        self, run_id, kb_id, conversation_id, user_message_id
    ) -> None:
        """无证据拒答：固定话术作答，不调用模型"""
        message_id = self._conversation_repo.add_message(
            conversation_id, "assistant", NO_EVIDENCE_REFUSAL
        )
        self._run_repo.attach_message_ids(
            run_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=message_id,
        )
        self._event_store.append(
            run_id, event_type="done", payload={"refused": True}
        )
        self._event_store.expire_tokens(run_id, self._token_retention_seconds)
        self._run_repo.finalize(
            run_id,
            state=QueryRunState.COMPLETED.value,
            refused=True,
            assistant_message_id=message_id,
        )

    def _finalize_cancelled(
        self, run_id, kb_id, conversation_id, user_message_id,
        answer_text, assembled,
    ) -> None:
        """取消收尾：保留已生成正文与引用"""
        message_id = self._conversation_repo.add_message(
            conversation_id, "assistant", answer_text
        )
        self._run_repo.attach_message_ids(
            run_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=message_id,
        )
        drafts = extract_citation_drafts(answer_text)
        validations = validate_citations(drafts, list(assembled.blocks))
        records = [
            build_citation_record(
                validation,
                assistant_message_id=message_id,
                citation_order=order,
                knowledge_base_id=kb_id,
                query_run_id=run_id,
            )
            for order, validation in enumerate(validations, start=1)
        ]
        if records:
            self._citation_repo.insert_citations(records)
        self._event_store.append(run_id, event_type="cancelled", payload={})
        self._event_store.expire_tokens(run_id, self._token_retention_seconds)
        self._run_repo.finalize(
            run_id,
            state=QueryRunState.CANCELLED.value,
            assistant_message_id=message_id,
        )

    def _fail(self, run_id: str, error_code: str, message: str) -> None:
        try:
            self._event_store.append(
                run_id, event_type="error", payload={"code": error_code}
            )
            self._event_store.expire_tokens(run_id, self._token_retention_seconds)
            self._run_repo.finalize(
                run_id,
                state=QueryRunState.FAILED.value,
                error_code=error_code,
                error_message=message,
            )
        except Exception:  # noqa: BLE001, S110 - 收尾自身失败只可静默（已无更高层）
            pass

    def _emit_stage(self, run_id: str, name: str, extra: dict | None = None) -> None:
        payload = {"name": name}
        if extra:
            payload.update(extra)
        self._event_store.append(run_id, event_type="stage", payload=payload)


def _citation_payload(record) -> dict:
    """citation 事件载荷（CitationDTO 子集，供 UI 渲染）"""
    return {
        "citation_order": record.citation_order,
        "chunk_id": record.chunk_id,
        "file_name": record.file_name_snapshot,
        "version_no": record.version_no_snapshot,
        "page_no": record.page_no,
        "section_path": record.section_path,
        "content": record.content_snapshot,
        "validation_state": record.validation_state,
        "vector_score": record.vector_score,
        "keyword_score": record.keyword_score,
        "fusion_score": record.fusion_score,
        "rerank_score": record.rerank_score,
    }
