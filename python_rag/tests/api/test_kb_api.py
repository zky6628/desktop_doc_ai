# -*- coding: utf-8 -*-
"""知识库 API 端点测试：创建/列表/详情/重命名/删除/健康检查的信封、
状态码、keyset 分页与删除事务语义（队列满回滚保持未删除）。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import (
    ApiV1Dependencies,
    KnowledgeBaseDependencies,
    create_api_router,
)
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteDeletionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from tests.infrastructure.schema_helpers import fresh_db, insert_task

# 队列 pending 上限（与任务引擎 3/50/53 口径一致）
MAX_PENDING = 50


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + v1 应用（知识库域）+ TestClient，返回 (client, 连接, 仓储集)"""
    db_path, _ = fresh_db(tmp_path, "kb_api.db")
    conn = connect(db_path)
    kbs = SQLiteKnowledgeBaseRepository(conn)
    tasks = SQLiteTaskRepository(conn)
    deletions = SQLiteDeletionRepository(conn)
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=ImportOrchestrator(
                    staging_store=UploadStagingStore(str(tmp_path / "staging")),
                    import_repo=SQLiteImportRepository(conn),
                ),
                task_repo=tasks,
                knowledge_bases=KnowledgeBaseDependencies(
                    kb_repo=kbs, deletion_repo=deletions, task_repo=tasks
                ),
            )
        )
    )
    yield (
        TestClient(application),
        conn,
        {"kbs": kbs, "tasks": tasks, "deletions": deletions},
    )
    conn.close()


class TestCreateEndpoint:
    def test_create_returns_201_envelope(self, runtime):
        client, _, _ = runtime
        response = client.post(
            "/api/v1/knowledge-bases",
            json={"name": "合同库", "description": "说明"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True and body["request_id"]
        assert body["data"]["name"] == "合同库"
        assert body["data"]["description"] == "说明"
        assert body["data"]["status"] == "active"
        assert body["data"]["deleted_at"] is None

    def test_create_blank_name_rejected(self, runtime):
        client, _, _ = runtime
        response = client.post("/api/v1/knowledge-bases", json={"name": "  "})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_PARAM"

    def test_create_duplicate_active_name_conflict(self, runtime):
        client, _, _ = runtime
        assert client.post("/api/v1/knowledge-bases", json={"name": "A"}).status_code == 201
        response = client.post("/api/v1/knowledge-bases", json={"name": "A"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NAME_CONFLICT"


class TestListEndpoint:
    def test_list_pagination_with_cursor(self, runtime):
        client, _, _ = runtime
        for index in range(3):
            client.post("/api/v1/knowledge-bases", json={"name": f"库{index}"})

        first = client.get("/api/v1/knowledge-bases", params={"limit": 2})
        assert first.status_code == 200
        body = first.json()["data"]
        assert len(body["items"]) == 2
        assert body["next_cursor"]

        second = client.get(
            "/api/v1/knowledge-bases",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        second_body = second.json()["data"]
        assert len(second_body["items"]) == 1
        assert second_body["next_cursor"] is None
        # 两页合并覆盖全部且无重复
        names = [item["name"] for item in body["items"] + second_body["items"]]
        assert sorted(names) == ["库0", "库1", "库2"]

    def test_list_invalid_cursor_rejected(self, runtime):
        client, _, _ = runtime
        response = client.get(
            "/api/v1/knowledge-bases", params={"cursor": "not-a-cursor"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_PARAM"


class TestGetAndRename:
    def test_get_detail_and_missing(self, runtime):
        client, _, _ = runtime
        created = client.post(
            "/api/v1/knowledge-bases", json={"name": "详情库"}
        ).json()["data"]
        got = client.get(f"/api/v1/knowledge-bases/{created['id']}")
        assert got.status_code == 200
        assert got.json()["data"]["name"] == "详情库"

        missing = client.get("/api/v1/knowledge-bases/no-such-id")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    def test_rename_updates_and_conflict(self, runtime):
        client, _, _ = runtime
        first = client.post("/api/v1/knowledge-bases", json={"name": "甲"}).json()["data"]
        client.post("/api/v1/knowledge-bases", json={"name": "乙"})

        renamed = client.patch(
            f"/api/v1/knowledge-bases/{first['id']}",
            json={"name": "甲二", "description": "新描述"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["data"]["name"] == "甲二"
        assert renamed.json()["data"]["description"] == "新描述"

        conflict = client.patch(
            f"/api/v1/knowledge-bases/{first['id']}", json={"name": "乙"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "NAME_CONFLICT"

        missing = client.patch(
            "/api/v1/knowledge-bases/no-such-id", json={"name": "丙"}
        )
        assert missing.status_code == 404


class TestDeleteEndpoint:
    def test_delete_returns_task_and_soft_deletes(self, runtime):
        client, conn, _ = runtime
        created = client.post("/api/v1/knowledge-bases", json={"name": "待删"}).json()["data"]

        response = client.delete(
            f"/api/v1/knowledge-bases/{created['id']}",
            headers={"Idempotency-Key": "del-1"},
        )
        assert response.status_code == 202
        body = response.json()["data"]["task"]
        assert body["task_type"] == "delete_kb"
        assert body["state"] == "queued"
        assert body["cancellable"] is True
        assert body["retryable"] is False

        row = conn.execute(
            "SELECT deleted_at, status FROM knowledge_bases WHERE id = ?",
            (created["id"],),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "deleted"

        # 幂等键重放返回同一任务
        replay = client.delete(
            f"/api/v1/knowledge-bases/{created['id']}",
            headers={"Idempotency-Key": "del-1"},
        )
        assert replay.status_code == 202
        assert replay.json()["data"]["task"]["id"] == body["id"]

    def test_delete_missing_and_already_deleted(self, runtime):
        client, _, _ = runtime
        assert (
            client.delete("/api/v1/knowledge-bases/no-such-id").status_code == 404
        )
        created = client.post("/api/v1/knowledge-bases", json={"name": "再删"}).json()["data"]
        assert (
            client.delete(f"/api/v1/knowledge-bases/{created['id']}").status_code
            == 202
        )
        again = client.delete(f"/api/v1/knowledge-bases/{created['id']}")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_delete_queue_full_rolls_back(self, runtime):
        client, conn, _ = runtime
        created = client.post("/api/v1/knowledge-bases", json={"name": "挤满"}).json()["data"]
        for _ in range(MAX_PENDING):
            insert_task(conn, state="queued")

        response = client.delete(f"/api/v1/knowledge-bases/{created['id']}")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TASK_QUEUE_FULL"
        # 容量回滚：知识库保持未删除
        row = conn.execute(
            "SELECT deleted_at FROM knowledge_bases WHERE id = ?", (created["id"],)
        ).fetchone()
        assert row[0] is None


class TestHealthCheckEndpoint:
    def test_creates_health_check_task(self, runtime):
        client, _, _ = runtime
        created = client.post("/api/v1/knowledge-bases", json={"name": "体检"}).json()["data"]
        response = client.post(
            f"/api/v1/health/knowledge-bases/{created['id']}",
            headers={"Idempotency-Key": "hc-1"},
        )
        assert response.status_code == 202
        task = response.json()["data"]["task"]
        assert task["task_type"] == "health_check"
        assert task["state"] == "queued"

        replay = client.post(
            f"/api/v1/health/knowledge-bases/{created['id']}",
            headers={"Idempotency-Key": "hc-1"},
        )
        assert replay.json()["data"]["task"]["id"] == task["id"]

    def test_missing_kb_rejected(self, runtime):
        client, _, _ = runtime
        response = client.post("/api/v1/health/knowledge-bases/no-such-id")
        assert response.status_code == 404

    def test_queue_full_rejected(self, runtime):
        client, conn, _ = runtime
        created = client.post("/api/v1/knowledge-bases", json={"name": "满"}).json()["data"]
        for _ in range(MAX_PENDING):
            insert_task(conn, state="queued")
        response = client.post(f"/api/v1/health/knowledge-bases/{created['id']}")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TASK_QUEUE_FULL"
