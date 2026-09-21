# -*- coding: utf-8 -*-
"""运维 API 测试：健康探针与无密钥配置概览的响应契约

覆盖三态推导（healthy/degraded/数据库不可用）、组件降级来源、队列
水位与容量常量一致性、配置摘要白名单与密钥不外泄。健康探针不外呼
云服务，凭据只表达配置存在性。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import ApiV1Dependencies, OpsDependencies, create_api_router
from app.domain import file_policy, task_state
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteConfigRepository
from tests.infrastructure.schema_helpers import fresh_db, insert_task


class HeartbeatChromaClient:
    """可编程心跳替身：按标志抛异常模拟向量库不可达"""

    def __init__(self) -> None:
        self.fail = False

    def heartbeat(self):
        if self.fail:
            raise RuntimeError("chroma down")
        return 1


def build_client(
    conn,
    *,
    chroma=None,
    worker_enabled=True,
    worker_alive=lambda: True,
    mineru=True,
    dashscope=True,
    local_debug=False,
):
    """装配仅含运维路由的独立 v1 应用"""
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=None,
                task_repo=None,
                ops=OpsDependencies(
                    conn=conn,
                    chroma_client=(
                        chroma if chroma is not None else HeartbeatChromaClient()
                    ),
                    worker_enabled=worker_enabled,
                    worker_alive=worker_alive,
                    mineru_configured=mineru,
                    dashscope_configured=dashscope,
                    local_debug_enabled=local_debug,
                ),
            )
        )
    )
    return TestClient(application)


@pytest.fixture()
def env(tmp_path):
    db_path, _ = fresh_db(tmp_path, "ops_api.db")
    return connect(db_path)


class TestHealth:
    def test_all_components_ok_is_healthy(self, env):
        client = build_client(env)
        response = client.get("/api/v1/health")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["status"] == "healthy"
        assert data["degraded"] == []
        assert data["components"]["sqlite"] == {"state": "ok"}
        assert data["components"]["chroma"]["state"] == "ok"
        assert data["components"]["fts"]["state"] == "ok"
        assert data["components"]["worker"] == {
            "state": "ok",
            "worker_enabled": True,
        }

    def test_queue_counts_reflect_tasks_with_capacity_constants(self, env):
        insert_task(env, task_id="task-running-1", state="running")
        insert_task(env, task_id="task-running-2", state="running")
        insert_task(env, task_id="task-queued-1", state="queued")
        insert_task(env, task_id="task-queued-2", state="queued")
        insert_task(env, task_id="task-queued-3", state="queued")
        insert_task(env, task_id="task-done", state="succeeded")
        client = build_client(env)

        data = client.get("/api/v1/health").json()["data"]

        assert data["queue"]["running"] == 2
        assert data["queue"]["pending"] == 3
        assert data["queue"]["capacity_running"] == task_state.MAX_RUNNING
        assert data["queue"]["capacity_pending"] == task_state.MAX_PENDING
        assert data["queue"]["capacity_non_terminal"] == task_state.MAX_NON_TERMINAL

    def test_worker_disabled_degrades_as_stopped(self, env):
        client = build_client(env, worker_enabled=False, worker_alive=lambda: False)

        data = client.get("/api/v1/health").json()["data"]

        assert data["status"] == "degraded"
        assert data["degraded"] == ["worker"]
        assert data["components"]["worker"] == {
            "state": "stopped",
            "worker_enabled": False,
        }

    def test_worker_thread_dead_degrades_as_error(self, env):
        client = build_client(env, worker_alive=lambda: False)

        data = client.get("/api/v1/health").json()["data"]

        assert data["status"] == "degraded"
        assert data["components"]["worker"]["state"] == "error"

    def test_chroma_failure_degrades(self, env):
        chroma = HeartbeatChromaClient()
        chroma.fail = True
        client = build_client(env, chroma=chroma)

        data = client.get("/api/v1/health").json()["data"]

        assert data["status"] == "degraded"
        assert data["components"]["chroma"]["state"] == "error"
        assert "chroma" in data["degraded"]

    def test_fts_broken_degrades(self, env):
        env.execute("DROP TABLE chunks_fts")
        client = build_client(env)

        data = client.get("/api/v1/health").json()["data"]

        assert data["status"] == "degraded"
        assert data["components"]["fts"]["state"] == "error"

    def test_provider_unconfigured_keeps_healthy_status(self, env):
        client = build_client(env, mineru=False, dashscope=False)

        data = client.get("/api/v1/health").json()["data"]

        # 凭据未配置不影响整体状态（本地路线完整可用是合法运行形态）
        assert data["status"] == "healthy"
        assert data["components"]["providers"]["mineru"] == {"configured": False}
        assert data["components"]["providers"]["dashscope"] == {
            "configured": False
        }

    def test_database_unavailable_returns_503_envelope(self, env):
        env.close()
        client = build_client(env)

        response = client.get("/api/v1/health")

        assert response.status_code == 503
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["error"]["retryable"] is True


class TestPublicConfig:
    def test_shape_with_constants_and_features(self, env):
        client = build_client(env, mineru=True, dashscope=True, local_debug=True)

        response = client.get("/api/v1/config/public")

        assert response.status_code == 200
        data = response.json()["data"]
        assert {profile["role"] for profile in data["model_profiles"]} == {
            "embedding",
            "rerank",
            "generation",
        }
        assert data["limits"] == {
            "max_running": task_state.MAX_RUNNING,
            "max_pending": task_state.MAX_PENDING,
            "max_non_terminal": task_state.MAX_NON_TERMINAL,
            "max_file_mb": file_policy.MAX_FILE_BYTES // (1024 * 1024),
            "max_batch_files": file_policy.MAX_BATCH_FILES,
        }
        assert data["features"] == {
            "worker_enabled": True,
            "local_debug_enabled": True,
            "cloud_parsing_available": True,
        }

    def test_summary_whitelist_by_config_type(self, env):
        config_repo = SQLiteConfigRepository(env)
        config_repo.ensure_config("retrieval", '{"rrf_k": 60, "vector_top_k": 20}')
        config_repo.ensure_config("generation", '{"max_output_tokens": 2000}')
        config_repo.ensure_config("prompt", '{"system_prompt_template": "SECRET"}')
        client = build_client(env)

        data = client.get("/api/v1/config/public").json()["data"]

        by_type = {row["config_type"]: row for row in data["pipeline_configs"]}
        # prompt 类型不在白名单内，整行跳过（模板大文本不外泄）
        assert "prompt" not in by_type
        assert by_type["retrieval"]["config_summary"] == {
            "rrf_k": 60,
            "vector_top_k": 20,
        }
        assert by_type["generation"]["config_summary"] == {
            "max_output_tokens": 2000
        }

    def test_response_carries_no_secret_material(self, env):
        client = build_client(env, mineru=True, dashscope=True)

        text = client.get("/api/v1/config/public").text

        assert "api_key" not in text
        assert "MINERU_API_TOKEN" not in text
        assert "DASHSCOPE_API_KEY" not in text
