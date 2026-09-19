# -*- coding: utf-8 -*-
"""查询 API 测试：创建/恢复/SSE/取消端点与信封契约

向量检索以确定性替身承担（向量检索行为已由检索/适配器测试覆盖，
本文件聚焦 HTTP 契约），避免 chroma 原生库跨用例线程问题。
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import ApiV1Dependencies, QueryDependencies, create_api_router
from app.domain.retrieval import IndexHit
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
    SQLiteKnowledgeBaseRepository,
    SQLiteQueryEventStore,
    SQLiteQueryRunRepository,
)
from tests.infrastructure.schema_helpers import fresh_db, insert_kb
from tests.infrastructure.test_retrieval_service import (
    FixedQueryEmbedder,
    add_active_document,
)


class DotProductVectorIndex:
    """确定性假向量索引：按点积降序返回命中（写入按集合存储）"""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, list[float]]] = {}

    def upsert_vectors(self, collection_name, ids, vectors) -> None:
        store = self._collections.setdefault(collection_name, {})
        for position, chunk_id in enumerate(ids):
            store[chunk_id] = list(vectors[position])

    def query_vectors(self, collection_name, query_vector, top_k):
        store = self._collections.get(collection_name, {})
        hits = [
            IndexHit(
                chunk_id=chunk_id,
                raw_score=1.0 - sum(a * b for a, b in zip(query_vector, vector)),
                score=sum(a * b for a, b in zip(query_vector, vector)),
            )
            for chunk_id, vector in store.items()
        ]
        hits.sort(key=lambda hit: -hit.score)
        return hits[:top_k]


class IdentityRerankGateway:
    """按输入顺序原样返回的重排替身"""

    def rerank(self, question, documents):
        return [
            SimpleNamespace(index=index, score=1.0 - index * 0.1)
            for index in range(len(documents))
        ]


class ScriptedGenerationGateway:
    """按脚本产出增量文本的生成替身"""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.calls = 0

    def stream_answer(self, messages):
        self.calls += 1
        yield from self.deltas


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + 独立 v1 应用（含查询路由）+ TestClient"""
    db_path, _ = fresh_db(tmp_path, "query_api.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "查询 API 测试库")
    kb_repo = SQLiteKnowledgeBaseRepository(conn)

    vector_index = DotProductVectorIndex()
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
    generation = ScriptedGenerationGateway()
    orchestrator = QueryOrchestrator(
        run_repo=SQLiteQueryRunRepository(conn),
        event_store=SQLiteQueryEventStore(conn),
        conversation_repo=SQLiteConversationRepository(conn),
        config_repo=SQLiteConfigRepository(conn),
        citation_repo=SQLiteCitationRepository(conn),
        retrieval_service=retrieval_service,
        resolver=ContextResolver(
            index_repo=SQLiteIndexVersionRepository(conn),
            chunk_repo=SQLiteChunkRepository(conn),
            document_repo=SQLiteDocumentRepository(conn),
            version_repo=SQLiteDocumentVersionRepository(conn),
        ),
        rerank_gateway=IdentityRerankGateway(),
        generation_gateway=generation,
        token_flush_batch=4,
        clock=lambda: 0.0,
    )
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=None,
                task_repo=None,
                query=QueryDependencies(
                    orchestrator=orchestrator,
                    run_repo=SQLiteQueryRunRepository(conn),
                    kb_repo=kb_repo,
                    conversation_repo=SQLiteConversationRepository(conn),
                    citation_repo=SQLiteCitationRepository(conn),
                ),
            )
        )
    )
    client = TestClient(application)

    class Env:
        pass

    state = Env()
    state.conn = conn
    state.kb_id = kb_id
    state.client = client
    state.embedder = embedder
    state.vector_index = vector_index
    state.keyword_index = keyword_index
    state.generation = generation
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


def _wait_terminal(client, query_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/v1/queries/{query_id}")
        body = response.json()
        if body["data"]["state"] in ("completed", "failed", "cancelled"):
            return body["data"]
        time.sleep(0.02)
    raise AssertionError(
        f"查询未在超时内到达终态: {body['data'].get('error')}"
    )


def test_create_query_returns_202_with_stream_url(runtime):
    """创建查询返回 202：query_id + 流地址 + queued 状态与统一信封"""
    env = runtime
    response = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": env.kb_id, "question": "问题"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["data"]["state"] == "queued"
    assert body["data"]["stream_url"].endswith("/events")
    # 等待后台线程结束（teardown 关闭连接前不得有在途执行）
    _wait_terminal(env.client, body["data"]["query_id"])


def test_create_query_rejects_missing_kb_and_blank_question(runtime):
    """知识库缺失 404；空白问题 400"""
    env = runtime
    missing = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": "no-such-kb", "question": "问题"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    blank = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": env.kb_id, "question": "   "},
    )
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "INVALID_PARAM"


def test_get_query_aggregates_answer_and_citations(runtime):
    """终态后聚合正文与引用快照（token 批次过期后亦可展示）"""
    env = runtime
    add_active_document(
        env,
        display_name="手册.pdf",
        sha="a" * 64,
        items=[("年假制度内容", [1.0, 0.0, 0.0, 0.0], "年假 制度 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["回答[S1]。"]

    created = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": env.kb_id, "question": "年假"},
    ).json()["data"]
    data = _wait_terminal(env.client, created["query_id"])
    assert data["state"] == "completed", f"终态异常: {data['error']}"
    assert data["answer"] == "回答[S1]。"
    assert data["server_ttft_ms"] is not None
    assert data["citations"]
    # 引用锚定经父切片展开的 [S1] 块（子命中内容 ⊆ 父上下文）
    assert data["citations"][0]["content"] == "手册.pdf"


def test_sse_stream_emits_contract_frames_and_closes_on_terminal(runtime):
    """SSE 帧格式：id 单调、event 类型齐全、终态后服务端关闭流

    查询已到终态后整流拉取（一次 GET 读取全部帧并自然结束，等价于
    客户端连接到已完结流的行为）
    """
    env = runtime
    add_active_document(
        env,
        display_name="流式.pdf",
        sha="b" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["根据[S1]回答"]

    created = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": env.kb_id, "question": "苹果"},
    ).json()["data"]
    _wait_terminal(env.client, created["query_id"])

    response = env.client.get(f"/api/v1/queries/{created['query_id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in response.text.split("\n\n") if frame.strip()]

    assert frames, "SSE 流为空"
    ids = [int(frame.split("\n")[0][4:]) for frame in frames]
    assert ids == sorted(ids) and ids[0] >= 1
    event_types = [frame.split("\nevent: ")[1].split("\n")[0] for frame in frames]
    assert event_types[0] == "meta"
    assert event_types[-1] == "done"
    assert "tokens" in event_types
    # token 帧携带批次序号范围与文本
    token_frame = next(
        frame
        for frame, event_type in zip(frames, event_types)
        if event_type == "tokens"
    )
    assert '"text"' in token_frame and '"from"' in token_frame


def test_last_event_id_replays_only_later_events(runtime):
    """携带 Last-Event-ID 重连：只重放其后的帧（不重复不遗漏）"""
    env = runtime
    add_active_document(
        env,
        display_name="重放.pdf",
        sha="c" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]
    env.generation.deltas = ["完整回答[S1]"]

    created = env.client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": env.kb_id, "question": "苹果"},
    ).json()["data"]
    _wait_terminal(env.client, created["query_id"])

    full = env.client.get(f"/api/v1/queries/{created['query_id']}/events")
    partial = env.client.get(
        f"/api/v1/queries/{created['query_id']}/events",
        headers={"Last-Event-ID": "3"},
    )

    def _ids(sse_text: str) -> list[int]:
        return [
            int(line[4:])
            for line in sse_text.split("\n")
            if line.startswith("id: ")
        ]

    all_ids = _ids(full.text)
    partial_ids = _ids(partial.text)
    assert partial_ids == [seq for seq in all_ids if seq > 3]


def test_cancel_endpoint_is_idempotent_and_404_for_missing(runtime):
    """取消端点：不存在 404；幂等（重复请求返回同状态）"""
    env = runtime
    missing = env.client.post("/api/v1/queries/no-such-id/cancel")
    assert missing.status_code == 404
