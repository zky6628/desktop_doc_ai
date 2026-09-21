# -*- coding: utf-8 -*-
"""调试检索 API 测试：门控、覆盖参数、候选明细与零写入契约

向量检索与重排以确定性替身承担（检索行为已由检索服务测试覆盖，
本文件聚焦 HTTP 契约）：门控未开启整体 422；候选明细只含定位事实
与各阶段分数（不含正文）；瞬态重排失败镜像生产降级语义并显式标注；
调试观察零写入（查询运行、候选快照与事件行数不变）。
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import (
    ApiV1Dependencies,
    SearchDependencies,
    create_api_router,
)
from app.domain.errors import RerankAuthError, RerankTransientError
from app.domain.retrieval import IndexHit
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.retrieval import ContextResolver, RetrievalService
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
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
    """按输入顺序原样返回的重排替身（记录调用供断言）"""

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, question, documents):
        self.calls += 1
        return [
            SimpleNamespace(index=index, score=1.0 - index * 0.1)
            for index in range(len(documents))
        ]


class ScriptedRerankGateway:
    """按脚本行为失败的重排替身（每次调用执行动作）"""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def rerank(self, question, documents):
        self.calls += 1
        raise self._error


@pytest.fixture()
def env(tmp_path):
    """临时库 + 独立 v1 应用（调试检索路由开启）+ TestClient"""
    db_path, _ = fresh_db(tmp_path, "search_api.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "调试检索测试库")
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
    resolver = ContextResolver(
        index_repo=SQLiteIndexVersionRepository(conn),
        chunk_repo=SQLiteChunkRepository(conn),
        document_repo=SQLiteDocumentRepository(conn),
        version_repo=SQLiteDocumentVersionRepository(conn),
    )
    rerank = IdentityRerankGateway()

    def build_client(*, local_debug=True, rerank_gateway=None):
        application = FastAPI()
        application.include_router(
            create_api_router(
                ApiV1Dependencies(
                    orchestrator=None,
                    task_repo=None,
                    search=SearchDependencies(
                        kb_repo=SQLiteKnowledgeBaseRepository(conn),
                        retrieval_service=retrieval_service,
                        resolver=resolver,
                        rerank_gateway=(
                            rerank_gateway if rerank_gateway is not None else rerank
                        ),
                        local_debug_enabled=local_debug,
                    ),
                )
            )
        )
        return TestClient(application)

    storage = SimpleNamespace(
        conn=conn,
        kb_id=kb_id,
        vector_index=vector_index,
        keyword_index=keyword_index,
        embedder=embedder,
        rerank=rerank,
        build_client=build_client,
    )
    yield storage
    conn.close()


def _post_search(client, kb_id, question="苹果和香蕉", **overrides):
    body = {"knowledge_base_id": kb_id, "question": question}
    body.update(overrides)
    return client.post("/api/v1/search", json=body)


class TestGate:
    def test_disabled_returns_422_without_touching_search(self, env):
        client = env.build_client(local_debug=False)

        response = _post_search(client, "不存在的库")

        # 端点整体门控：调试能力未开启时即拒绝，不触达检索与知识库校验
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "DEBUG_OVERRIDE_NOT_ALLOWED"


class TestSearch:
    def test_candidates_carry_stage_scores_and_no_content(self, env):
        add_active_document(
            env,
            display_name="水果说明.txt",
            sha="sha-1",
            items=[
                ("苹果是水果", [1.0, 0.0, 0.0, 0.0], "苹果 水果"),
                ("香蕉是水果", [0.0, 1.0, 0.0, 0.0], "香蕉 水果"),
            ],
        )
        client = env.build_client()

        response = _post_search(client, env.kb_id, question="苹果")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert len(data["candidates"]) == 2
        first = data["candidates"][0]
        assert first["rrf_rank"] == 1
        assert first["rerank_rank"] is not None
        # 文件名事实来自 documents.display_name（共享 helper 的默认值），
        # 断言解析器到候选明细的事实补全链路
        assert first["file_name"] == "doc.pdf"
        assert first["version_no"] == 1
        assert "content" not in first
        stages = data["stages"]
        assert stages["fused"] == 2
        assert stages["reranked"] == 2
        assert stages["rerank_degraded"] is False
        assert stages["vector_hits"] == 2
        # 关键词路按预分词短语匹配，仅苹果切片命中
        assert stages["keyword_hits"] == 1

    def test_kb_not_found_and_deleted(self, env):
        client = env.build_client()

        missing = _post_search(client, "nonexistent-kb")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

        env.conn.execute(
            "UPDATE knowledge_bases SET deleted_at = '2026-01-01T00:00:00Z'"
            " WHERE id = ?",
            (env.kb_id,),
        )
        deleted = _post_search(client, env.kb_id)
        assert deleted.status_code == 410
        assert deleted.json()["error"]["code"] == "KNOWLEDGE_BASE_DELETED"

    def test_override_applied_and_range_validated(self, env):
        add_active_document(
            env,
            display_name="覆盖参数.txt",
            sha="sha-1",
            items=[
                ("苹果是水果", [1.0, 0.0, 0.0, 0.0], "苹果 水果"),
                ("香蕉是水果", [0.0, 1.0, 0.0, 0.0], "香蕉 水果"),
            ],
        )
        client = env.build_client()

        response = _post_search(client, env.kb_id, fused_top_k=1)

        data = response.json()["data"]
        assert len(data["candidates"]) == 1
        assert data["config"]["overridden"] == {"fused_top_k": 1}

        below = _post_search(client, env.kb_id, vector_top_k=0)
        assert below.status_code == 400
        assert below.json()["error"]["code"] == "INVALID_PARAM"

        above = _post_search(client, env.kb_id, rerank_top_n=51)
        assert above.status_code == 400
        assert above.json()["error"]["code"] == "INVALID_PARAM"

    def test_blank_question_rejected(self, env):
        client = env.build_client()

        response = _post_search(client, env.kb_id, question="   ")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_PARAM"

    def test_no_candidates_skips_rerank(self, env):
        client = env.build_client()

        response = _post_search(client, env.kb_id)

        data = response.json()["data"]
        assert data["candidates"] == []
        assert data["stages"]["fused"] == 0
        assert data["stages"]["reranked"] == 0
        assert data["stages"]["rerank_degraded"] is False
        assert env.rerank.calls == 0

    def test_transient_rerank_mirrors_production_degradation(self, env):
        add_active_document(
            env,
            display_name="降级观察.txt",
            sha="sha-1",
            items=[
                ("苹果是水果", [1.0, 0.0, 0.0, 0.0], "苹果 水果"),
                ("香蕉是水果", [0.0, 1.0, 0.0, 0.0], "香蕉 水果"),
            ],
        )
        scripted = ScriptedRerankGateway(RerankTransientError("rerank down"))
        client = env.build_client(rerank_gateway=scripted)

        response = _post_search(client, env.kb_id)

        data = response.json()["data"]
        # 与查询链路一致：瞬态内联重试后降级 RRF 前 N（候选全数保留）
        assert scripted.calls == 2
        assert data["stages"]["rerank_degraded"] is True
        assert all(candidate["rerank_rank"] is None for candidate in data["candidates"])
        assert all(
            candidate["rerank_score"] is None for candidate in data["candidates"]
        )

    def test_non_transient_rerank_error_maps_to_502(self, env):
        add_active_document(
            env,
            display_name="认证失败.txt",
            sha="sha-1",
            items=[
                ("苹果是水果", [1.0, 0.0, 0.0, 0.0], "苹果 水果"),
            ],
        )
        client = env.build_client(
            rerank_gateway=ScriptedRerankGateway(RerankAuthError("bad key"))
        )

        response = _post_search(client, env.kb_id)

        # 认证类失败不降级也不吞错：调试观察必须暴露真实失败
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "RERANK_AUTH"

    def test_debug_search_writes_nothing(self, env):
        add_active_document(
            env,
            display_name="零写入.txt",
            sha="sha-1",
            items=[
                ("苹果是水果", [1.0, 0.0, 0.0, 0.0], "苹果 水果"),
            ],
        )
        client = env.build_client()

        def _counts():
            tables = ("query_runs", "retrieval_candidates", "query_events")
            return [
                env.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            ]

        before = _counts()
        response = _post_search(client, env.kb_id)
        after = _counts()

        assert response.status_code == 200
        assert before == [0, 0, 0]
        assert after == before
