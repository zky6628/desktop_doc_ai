# -*- coding: utf-8 -*-
"""查询编排测试：事件序列、降级、拒答、取消、失败与幂等重放"""
import json
import time
from types import SimpleNamespace

import chromadb
import pytest

from app.domain.errors import (
    GenerationRateLimitedError,
    RerankTransientError,
)
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.query import QueryOrchestrator
from app.infrastructure.retrieval import ContextResolver, RetrievalService
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteCitationRepository,
    SQLiteConfigRepository,
    SQLiteConversationRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteQueryEventStore,
    SQLiteQueryRunRepository,
)
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter

from .schema_helpers import fresh_db, insert_kb
from .test_retrieval_service import FixedQueryEmbedder, add_active_document


class IdentityRerankGateway:
    """按输入顺序原样返回的重排替身（分数递减保证稳定）"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple] = []

    def rerank(self, question, documents):
        self.calls.append((question, list(documents)))
        if self.error is not None:
            raise self.error
        return [
            SimpleNamespace(index=index, score=1.0 - index * 0.1)
            for index in range(len(documents))
        ]


class ScriptedGenerationGateway:
    """按脚本产出增量文本的生成替身（可选错误与暂停钩子）"""

    def __init__(self, deltas, error: Exception | None = None,
                 on_pause=None) -> None:
        self.deltas = list(deltas)
        self.error = error
        self.on_pause = on_pause
        self.calls: list[list[dict]] = []
        self.last_usage = (12, 6)

    def stream_answer(self, messages):
        self.calls.append(list(messages))
        for position, delta in enumerate(self.deltas):
            yield delta
            if position == 0 and self.on_pause is not None:
                self.on_pause()
        if self.error is not None:
            raise self.error


def _terminal(run):
    return run.state.value in ("completed", "failed", "cancelled")


def _wait_terminal(run_repo, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = run_repo.get(run_id)
        if _terminal(run):
            return run
        time.sleep(0.02)
    raise AssertionError("查询未在超时内到达终态")


@pytest.fixture()
def query_env(tmp_path):
    """临时库 + 真实检索/解析器/事件存储 + 替身网关的编排器"""
    db_path, _ = fresh_db(tmp_path, name="query_orch.db")
    conn = connect(db_path)
    vector_index = ChromaVectorIndexAdapter(
        chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    )
    keyword_index = SQLiteFtsKeywordIndex(conn)
    embedder = FixedQueryEmbedder([1.0, 0.0, 0.0, 0.0])
    retrieval_service = RetrievalService(
        index_repo=SQLiteIndexVersionRepository(conn),
        chunk_repo=SQLiteChunkRepository(conn),
        query_embedder=embedder,
        vector_index=vector_index,
        keyword_index=keyword_index,
        tokenizer=JiebaTokenizer(),
    )
    resolver = ContextResolver(
        index_repo=SQLiteIndexVersionRepository(conn),
        chunk_repo=SQLiteChunkRepository(conn),
        document_repo=SQLiteDocumentRepository(conn),
        version_repo=SQLiteDocumentVersionRepository(conn),
    )
    run_repo = SQLiteQueryRunRepository(conn)
    event_store = SQLiteQueryEventStore(conn)

    class Env:
        pass

    state = Env()
    state.conn = conn
    state.kb_id = insert_kb(conn, "问答知识库")
    state.embedder = embedder
    state.vector_index = vector_index
    state.keyword_index = keyword_index
    state.run_repo = run_repo
    state.event_store = event_store
    state.conversation_repo = SQLiteConversationRepository(conn)
    state.citation_repo = SQLiteCitationRepository(conn)
    state.rerank = IdentityRerankGateway()
    state.generation = ScriptedGenerationGateway([])
    state.orchestrator = QueryOrchestrator(
        run_repo=run_repo,
        event_store=event_store,
        conversation_repo=SQLiteConversationRepository(conn),
        config_repo=SQLiteConfigRepository(conn),
        citation_repo=SQLiteCitationRepository(conn),
        retrieval_service=retrieval_service,
        resolver=resolver,
        rerank_gateway=state.rerank,
        generation_gateway=state.generation,
        token_flush_batch=4,
        clock=lambda: 0.0,  # 固定时钟：仅按批大小触发落库
    )
    yield state
    # 连接关闭前等待在途查询线程结束（共享连接，避免跨线程关闭竞争）
    deadline = time.time() + 5
    while time.time() < deadline:
        pending = conn.execute(
            "SELECT COUNT(*) FROM query_runs"
            " WHERE state IN ('queued', 'running', 'cancel_requested')"
        ).fetchone()[0]
        if pending == 0:
            break
        time.sleep(0.02)
    conn.close()


def test_full_pipeline_emits_contract_event_sequence(query_env):
    """事件序列符合 SSE 合同：meta→stage×4→token 批次→citation→done"""
    env = query_env
    _, _, chunk_ids = add_active_document(
        env,
        display_name="手册.pdf",
        sha="a" * 64,
        items=[
            ("员工累计工作满一年可享受五天带薪年假。", [1.0, 0.0, 0.0, 0.0], "员工 累计 工作 满 一年 可 享受 五天 带薪 年假 。"),
            ("香蕉属于热带水果。", [0.0, 1.0, 0.0, 0.0], "香蕉 属于 热带 水果 。"),
        ],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["根据[S1]的规定，", "年假为五天", "。[S1]"]

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="年假")
    final = _wait_terminal(env.run_repo, run.id)

    assert final.state.value == "completed", (
        f"终态异常: {final.state} {final.error_code} {final.error_message}"
    )
    assert final.rerank_degraded is False
    assert final.server_ttft_ms is not None
    # 助手消息与引用快照落库
    assert final.assistant_message_id is not None
    assert "年假为五天" in env.conversation_repo.get_message_content(
        final.assistant_message_id
    )
    citations = env.citation_repo.list_by_message(final.assistant_message_id)
    assert [record.citation_order for record in citations] == [1]
    # [S1] 块经父切片展开后锚定父切片（子命中内容 ⊆ 父上下文）；
    # 父切片不是召回候选，各路分数为空（分数跟随候选而非展开项）
    assert citations[0].chunk_id != chunk_ids[0]
    assert citations[0].content_snapshot == "手册.pdf"
    assert citations[0].query_run_id == run.id
    assert citations[0].rerank_score is None

    # 事件序列与类型
    events = env.event_store.read_after(run.id)
    types = [event.event_type for event in events]
    assert types[0] == "meta"
    stage_events = [event for event in events if event.event_type == "stage"]
    stage_names = [json.loads(event.payload_json)["name"] for event in stage_events]
    assert stage_names == [
        "retrieval_started",
        "retrieval_completed",
        "rerank_completed",
        "generation_started",
    ]
    assert "tokens" in types
    assert types[-1] == "done"
    seqs = [event.event_seq for event in events]
    assert seqs == sorted(seqs) and seqs[0] == 1

    # token 批次：拼接恢复全文，序号范围连续
    token_events = [event for event in events if event.event_type == "tokens"]
    assert token_events
    assert "".join(event.token_text for event in token_events) == (
        "根据[S1]的规定，年假为五天。[S1]"
    )
    assert token_events[0].token_seq_start == 1
    assert token_events[-1].token_seq_end == 3


def test_degradation_on_rerank_transient_uses_rrf_order(query_env):
    """重排瞬态重试耗尽：降级 RRF 前 5 继续生成并记录"""
    env = query_env
    add_active_document(
        env,
        display_name="降级.pdf",
        sha="b" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.rerank.error = RerankTransientError("供应方限流")
    env.generation.deltas = ["回答[S1]"]

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="苹果")
    final = _wait_terminal(env.run_repo, run.id)

    assert final.state.value == "completed"
    assert final.rerank_degraded is True
    assert len(env.generation.calls) == 1
    events = env.event_store.read_after(run.id)
    stage_payloads = [
        event.payload_json for event in events if event.event_type == "stage"
    ]
    assert any('"rerank_degraded"' in payload for payload in stage_payloads)


def test_refusal_without_retrieval_candidates_skips_generation(query_env):
    """无可检索索引：拒答话术作答，不调用生成"""
    env = query_env

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="任意问题")
    final = _wait_terminal(env.run_repo, run.id)

    assert final.state.value == "completed"
    assert final.refused is True
    assert len(env.generation.calls) == 0
    answer = env.conversation_repo.get_message_content(
        final.assistant_message_id
    )
    assert answer == "当前知识库中没有足够依据回答该问题。"
    events = env.event_store.read_after(run.id)
    assert events[-1].event_type == "done"
    assert '"refused":true' in events[-1].payload_json


def test_cancel_at_token_checkpoint_keeps_partial_answer(query_env):
    """取消在检查点生效：保留已生成正文，终态 cancelled"""
    env = query_env
    add_active_document(
        env,
        display_name="取消.pdf",
        sha="c" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    def _request_cancel():
        env.run_repo.request_cancel(env.pending_run_id)

    env.pending_run_id = None
    env.generation.deltas = ["第一段", "第二段", "第三段"]
    env.generation.on_pause = _request_cancel

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="苹果")
    env.pending_run_id = run.id
    final = _wait_terminal(env.run_repo, run.id)

    assert final.state.value == "cancelled"
    answer = env.conversation_repo.get_message_content(
        final.assistant_message_id
    )
    assert answer == "第一段"  # 取消点之前已生成的正文保留
    events = env.event_store.read_after(run.id)
    assert events[-1].event_type == "cancelled"
    assert not env.citation_repo.list_by_message(final.assistant_message_id)


def test_generation_error_fails_query_with_contract_code(query_env):
    """生成限流：查询失败终态携带合同错误码与 error 事件"""
    env = query_env
    add_active_document(
        env,
        display_name="失败.pdf",
        sha="d" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = []
    env.generation.error = GenerationRateLimitedError("供应方限流")

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="苹果")
    final = _wait_terminal(env.run_repo, run.id)

    assert final.state.value == "failed"
    assert final.error_code == "MODEL_RATE_LIMITED"
    events = env.event_store.read_after(run.id)
    assert events[-1].event_type == "error"
    assert '"MODEL_RATE_LIMITED"' in events[-1].payload_json


def test_idempotency_replay_returns_same_run_without_restart(query_env):
    """幂等键重放返回同一查询且不重复启动生成"""
    env = query_env
    add_active_document(
        env,
        display_name="幂等.pdf",
        sha="e" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["回答"]

    first = env.orchestrator.start_query(
        kb_id=env.kb_id, question="苹果", idempotency_key="idem-1"
    )
    replay = env.orchestrator.start_query(
        kb_id=env.kb_id, question="苹果", idempotency_key="idem-1"
    )

    assert replay.id == first.id
    _wait_terminal(env.run_repo, first.id)
    assert len(env.generation.calls) == 1


def test_candidates_segments_and_usage_recorded(query_env):
    """候选快照（各阶段分数与 in_context）与分段指标/用量落库"""
    env = query_env
    add_active_document(
        env,
        display_name="手册.pdf",
        sha="a" * 64,
        items=[
            # 预分词文本须与 jieba 对该句的真实产出一致（短语合同）
            ("员工累计工作满一年可享受五天带薪年假。", [1.0, 0.0, 0.0, 0.0], "员工 累计 工作 满 一年 可 享受 五天 带薪 年 假 。"),
            ("香蕉属于热带水果。", [0.0, 1.0, 0.0, 0.0], "香蕉 属于 热带 水果 。"),
        ],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["根据[S1]回答"]
    env.generation.last_usage = (30, 20)

    run = env.orchestrator.start_query(kb_id=env.kb_id, question="带薪年假")
    final = _wait_terminal(env.run_repo, run.id)
    assert final.state.value == "completed"

    rows = env.conn.execute(
        "SELECT chunk_id, source, vector_rank, keyword_rank, rrf_rank,"
        " rerank_rank, rerank_score, in_context FROM retrieval_candidates"
        " WHERE query_run_id = ? ORDER BY rrf_rank",
        (run.id,),
    ).fetchall()
    assert len(rows) == 2
    first = rows[0]
    # 首候选双路命中且进入上下文，重排排名 1
    assert first[1] == "dual"
    assert first[2] == 1 and first[3] == 1
    assert first[5] == 1 and first[6] is not None
    assert first[7] == 1
    # 次候选仅向量路命中；同父邻接扩展进入上下文；重排排名 2
    second = rows[1]
    assert second[1] == "vector"
    assert second[2] == 2 and second[3] is None
    assert second[5] == 2 and second[6] is not None
    assert second[7] == 1

    segments = env.conn.execute(
        "SELECT retrieval_ms, rerank_ms, prompt_build_ms, model_ttft_ms,"
        " input_tokens, output_tokens FROM query_runs WHERE id = ?",
        (run.id,),
    ).fetchone()
    assert all(value is not None and value >= 0 for value in segments[:4])
    assert segments[4] == 30 and segments[5] == 20


def test_purge_expired_tokens_removes_only_expired(query_env):
    """懒清理只删除已过期 token 批次，未过期与终态事件保留"""
    env = query_env
    run = env.run_repo.create(kb_id=env.kb_id, question="问题", config_ids={})
    env.event_store.append(
        run.id, event_type="tokens", token_text="旧批次", expires_at="2000-01-01T00:00:00+00:00"
    )
    env.event_store.append(
        run.id, event_type="tokens", token_text="新批次",
        expires_at="2999-01-01T00:00:00+00:00",
    )

    purged = env.event_store.purge_expired_tokens()

    assert purged == 1
    remaining = env.event_store.read_after(run.id)
    assert [event.token_text for event in remaining] == ["新批次"]


def test_fail_interrupted_recovers_stale_runs(query_env):
    """启动恢复：残留非终态查询统一置为失败"""
    env = query_env
    stale = env.run_repo.create(
        kb_id=env.kb_id, question="问题", config_ids={}
    )
    env.run_repo.mark_running(stale.id)

    recovered = env.orchestrator.fail_interrupted()

    assert recovered >= 1
    run = env.run_repo.get(stale.id)
    assert run.state.value == "failed"
    assert run.error_code == "INTERNAL_ERROR"
