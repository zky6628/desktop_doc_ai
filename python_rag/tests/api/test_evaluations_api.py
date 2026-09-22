# -*- coding: utf-8 -*-
"""评测 API 契约测试：创建预检、独占、进度查询与取消"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import (
    ApiV1Dependencies,
    EvaluationDependencies,
    create_api_router,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteEvaluationRunRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)
from tests.infrastructure.schema_helpers import fresh_db


class StubEvaluationService:
    """预检替身：固定参评版本与切片规模（不触达内容表）"""

    def __init__(self, version_ids=None, estimated=10):
        self.version_ids = version_ids or ["v-1"]
        self.estimated = estimated

    def version_ids_for_kb(self, kb_id):
        return list(self.version_ids)

    def estimate_chunk_count(self, version_ids, params):
        return self.estimated


def build_client(conn, service):
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=None,
                task_repo=None,
                evaluations=EvaluationDependencies(
                    conn=conn,
                    kb_repo=SQLiteKnowledgeBaseRepository(conn),
                    task_repo=SQLiteTaskRepository(conn),
                    evaluation_repo=SQLiteEvaluationRunRepository(conn),
                    evaluation_service=service,
                ),
            )
        )
    )
    return TestClient(application)


@pytest.fixture()
def env(tmp_path):
    db_path, _ = fresh_db(tmp_path, "evaluations_api.db")
    conn = connect(db_path)
    kb = SQLiteKnowledgeBaseRepository(conn).create(name="评测库")
    yield conn, kb
    conn.close()


def test_create_returns_202_with_run_payload(env):
    conn, kb = env
    client = build_client(conn, StubEvaluationService())

    response = client.post(
        "/api/v1/evaluation-runs",
        json={
            "knowledge_base_id": kb.id,
            "questions": ["问题一", "问题二"],
            "param_groups": [
                {"parent_chunk_chars": 800, "child_chunk_chars": 300},
                {"parent_chunk_chars": 1200, "child_chunk_chars": 400},
            ],
        },
    )

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["state"] == "running"
    assert data["progress"] == {
        "current_group_index": None,
        "current_question_index": None,
    }
    assert len(data["param_groups"]) == 2
    assert data["questions"] == ["问题一", "问题二"]
    # 编排任务已入队
    queued = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type = 'evaluation_run'"
        " AND state = 'queued'"
    ).fetchone()[0]
    assert queued == 1


def test_create_rejects_second_running_evaluation(env):
    conn, kb = env
    client = build_client(conn, StubEvaluationService())
    body = {
        "knowledge_base_id": kb.id,
        "questions": ["问题"],
        "param_groups": [{"parent_chunk_chars": 800, "child_chunk_chars": 300}],
    }
    assert client.post("/api/v1/evaluation-runs", json=body).status_code == 202

    second = client.post("/api/v1/evaluation-runs", json=body)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EVALUATION_RUNNING"


def test_create_rejects_invalid_params(env):
    conn, kb = env
    client = build_client(conn, StubEvaluationService())

    child_over = client.post(
        "/api/v1/evaluation-runs",
        json={
            "knowledge_base_id": kb.id,
            "questions": ["问题"],
            "param_groups": [{"parent_chunk_chars": 300, "child_chunk_chars": 800}],
        },
    )
    empty_questions = client.post(
        "/api/v1/evaluation-runs",
        json={"knowledge_base_id": kb.id, "questions": ["  "], "param_groups": [
            {"parent_chunk_chars": 800, "child_chunk_chars": 300}
        ]},
    )

    assert child_over.status_code == 422
    assert empty_questions.status_code == 422


def test_create_rejects_when_chunk_estimate_exceeds_cap(env):
    conn, kb = env
    client = build_client(conn, StubEvaluationService(estimated=999_999))

    response = client.post(
        "/api/v1/evaluation-runs",
        json={
            "knowledge_base_id": kb.id,
            "questions": ["问题"],
            "param_groups": [{"parent_chunk_chars": 800, "child_chunk_chars": 300}],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EVALUATION_TOO_LARGE"


def test_get_run_not_found(env):
    conn, _kb = env
    client = build_client(conn, StubEvaluationService())

    response = client.get("/api/v1/evaluation-runs/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EVALUATION_NOT_FOUND"


def test_cancel_marks_task_cancel_requested(env):
    conn, kb = env
    client = build_client(conn, StubEvaluationService())
    created = client.post(
        "/api/v1/evaluation-runs",
        json={
            "knowledge_base_id": kb.id,
            "questions": ["问题"],
            "param_groups": [{"parent_chunk_chars": 800, "child_chunk_chars": 300}],
        },
    )
    run_id = created.json()["data"]["id"]

    cancelled = client.post(f"/api/v1/evaluation-runs/{run_id}/cancel")

    assert cancelled.status_code == 200
    # 排队中的评测尚未开跑：取消立即收尾为 cancelled 终态
    state = conn.execute(
        "SELECT t.state FROM tasks t"
        " JOIN evaluation_runs e ON e.task_id = t.id WHERE e.id = ?",
        (run_id,),
    ).fetchone()[0]
    assert state == "cancelled"
