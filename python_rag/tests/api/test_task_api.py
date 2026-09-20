# -*- coding: utf-8 -*-
"""任务 API 端点测试：列表筛选/分页/队列位次、详情事件、取消语义与
失败重试的父关系。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import ApiV1Dependencies, TaskDependencies, create_api_router
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from tests.infrastructure.schema_helpers import (
    fresh_db,
    insert_document,
    insert_task,
    insert_version,
)


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + v1 应用（任务域）+ TestClient"""
    db_path, _ = fresh_db(tmp_path, "task_api.db")
    conn = connect(db_path)
    tasks = SQLiteTaskRepository(conn)
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=ImportOrchestrator(
                    staging_store=UploadStagingStore(str(tmp_path / "staging")),
                    import_repo=SQLiteImportRepository(conn),
                ),
                task_repo=tasks,
                tasks=TaskDependencies(
                    task_repo=tasks,
                    document_repo=SQLiteDocumentRepository(conn),
                    version_repo=SQLiteDocumentVersionRepository(conn),
                ),
            )
        )
    )
    yield TestClient(application), conn, tasks
    conn.close()


class TestTaskList:
    def test_list_ordered_with_queue_position(self, runtime):
        client, conn, _ = runtime
        first = insert_task(conn, state="queued")
        insert_task(conn, state="succeeded")
        third = insert_task(conn, state="queued")

        response = client.get("/api/v1/tasks")
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assert len(items) == 3
        assert {item["id"] for item in items} >= {first, third}
        # 两个 queued 任务各占一个位次（顺序由 created_at/id 决定）
        positions = {
            item["id"]: item["queue_position"]
            for item in items
            if item["state"] == "queued"
        }
        assert sorted(positions.values()) == [1, 2]
        # 非排队任务的位次为空
        succeeded = next(item for item in items if item["state"] == "succeeded")
        assert succeeded["queue_position"] is None

    def test_list_filters(self, runtime):
        client, conn, _ = runtime
        insert_task(conn, task_type="import", state="queued")
        insert_task(conn, task_type="health_check", state="queued")

        by_type = client.get("/api/v1/tasks", params={"task_type": "health_check"})
        assert [t["task_type"] for t in by_type.json()["data"]["items"]] == [
            "health_check"
        ]

        by_state = client.get("/api/v1/tasks", params={"state": "succeeded"})
        assert by_state.json()["data"]["items"] == []

        invalid = client.get("/api/v1/tasks", params={"state": "nope"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "INVALID_PARAM"

    def test_list_pagination_cursor(self, runtime):
        client, conn, _ = runtime
        for _ in range(3):
            insert_task(conn, state="queued")

        first = client.get("/api/v1/tasks", params={"limit": 2})
        data = first.json()["data"]
        assert len(data["items"]) == 2 and data["next_cursor"]
        second = client.get(
            "/api/v1/tasks", params={"limit": 2, "cursor": data["next_cursor"]}
        )
        assert len(second.json()["data"]["items"]) == 1


class TestTaskDetail:
    def test_detail_with_recent_events(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="queued")
        conn.execute(
            "INSERT INTO task_events"
            " (id, task_id, event_type, state, stage, attempt_count, worker,"
            "  created_at, duration_ms, checkpoint_json, error_code, detail_json)"
            " VALUES (?, ?, ?, ?, NULL, 0, NULL, ?, NULL, NULL, NULL, NULL)",
            ("ev-1", task_id, "created", "queued", "2026-09-18T00:00:00+00:00"),
        )
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["task"]["id"] == task_id
        assert data["recent_events"][0]["event_type"] == "created"

    def test_detail_missing(self, runtime):
        client, _, _ = runtime
        response = client.get("/api/v1/tasks/no-such")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


class TestTaskCancel:
    def test_cancel_queued_task_finalizes_immediately(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="queued")
        response = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert response.status_code == 200
        body = response.json()["data"]["task"]
        assert body["state"] == "cancelled"
        assert body["finished_at"] is not None

    def test_cancel_running_moves_to_cancel_requested(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="running", stage="parsing_local")
        response = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["task"]["state"] == "cancel_requested"

    def test_cancel_terminal_rejected(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="succeeded")
        response = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_NOT_CANCELLABLE"

    def test_cancel_missing(self, runtime):
        client, _, _ = runtime
        assert client.post("/api/v1/tasks/no-such/cancel").status_code == 404


class TestTaskRetry:
    def test_retry_failed_creates_child_task(self, runtime):
        client, conn, _ = runtime
        failed_id = insert_task(conn, task_type="import", state="failed")
        conn.execute(
            "UPDATE tasks SET error_code = ? WHERE id = ?",
            ("PARSE_CORRUPTED_FILE", failed_id),
        )
        response = client.post(
            f"/api/v1/tasks/{failed_id}/retry",
            headers={"Idempotency-Key": "rt-1"},
        )
        assert response.status_code == 202
        task = response.json()["data"]["task"]
        assert task["task_type"] == "import"
        assert task["parent_task_id"] == failed_id
        assert task["state"] == "queued"

        replay = client.post(
            f"/api/v1/tasks/{failed_id}/retry",
            headers={"Idempotency-Key": "rt-1"},
        )
        assert replay.json()["data"]["task"]["id"] == task["id"]

    def test_retry_non_failed_rejected(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="queued")
        response = client.post(f"/api/v1/tasks/{task_id}/retry")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_STATE_CONFLICT"


class TestTaskViewEnrichment:
    def test_waiting_user_task_carries_document_display_fields(self, runtime):
        client, conn, _ = runtime
        kb_id = "0197kbed-kb-0000-0000-000000000001"
        conn.execute(
            "INSERT INTO knowledge_bases"
            " (id, name, description, status, deleted_at, delete_requested_at,"
            "  created_at, updated_at)"
            " VALUES (?, '库', NULL, 'active', NULL, NULL,"
            "  '2026-09-18T00:00:00+00:00', '2026-09-18T00:00:00+00:00')",
            (kb_id,),
        )
        doc_id = insert_document(conn, kb_id, "a" * 64)
        version_id = insert_version(conn, doc_id)
        insert_task(
            conn,
            task_type="import",
            state="waiting_user",
            stage="routing_parser",
            kb_id=kb_id,
            document_id=doc_id,
            document_version_id=version_id,
        )

        response = client.get(
            "/api/v1/tasks", params={"state": "waiting_user"}
        )
        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["document_display_name"] == "doc.pdf"
        assert item["document_size_bytes"] == 1024

    def test_route_fields_parsed_from_task_input(self, runtime):
        client, conn, _ = runtime
        task_id = insert_task(conn, state="waiting_user")
        conn.execute(
            "UPDATE tasks SET input_json = ? WHERE id = ?",
            (
                '{"mode":"cloud","reason":"扫描页","router_config_version":"v1","parser_preference":"auto"}',
                task_id,
            ),
        )
        item = client.get(f"/api/v1/tasks/{task_id}").json()["data"]["task"]
        assert item["route_mode"] == "cloud"
        assert item["route_reason"] == "扫描页"
