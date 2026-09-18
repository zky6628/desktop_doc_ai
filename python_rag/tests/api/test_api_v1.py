# -*- coding: utf-8 -*-
"""API v1 端点测试：上传批次与云端确认的信封、状态码与错误码"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import ApiV1Dependencies, create_api_router
from app.domain.entities import TaskStage, TaskStatus
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.sqlite.repositories.task_repository import (
    SQLiteTaskRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from tests.infrastructure.schema_helpers import fresh_db, insert_kb


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + 独立 v1 应用 + TestClient，返回 (client, 连接, 导入/任务仓储, kb_id)"""
    db_path, _ = fresh_db(tmp_path, "api.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "API 测试库")
    imports = SQLiteImportRepository(conn)
    tasks = SQLiteTaskRepository(conn)
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=ImportOrchestrator(
                    staging_store=UploadStagingStore(str(tmp_path / "staging")),
                    import_repo=imports,
                ),
                task_repo=tasks,
            )
        )
    )
    yield TestClient(application), conn, imports, tasks, kb_id
    conn.close()


def _upload(client, kb_id, files, headers=None, **data):
    """发起批量上传请求"""
    return client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files=files,
        data=data,
        headers=headers,
    )


class TestUploadEndpoint:
    """批量上传端点"""

    def test_upload_envelope_and_accepted_result(self, runtime):
        client, _, _, _, kb_id = runtime
        response = _upload(
            client,
            kb_id,
            [("files", ("说明.md", "# 标题\n\n正文".encode()))],
            parser_preference="auto",
            duplicate_policy="skip",
        )
        assert response.status_code == 202
        body = response.json()
        assert body["success"] is True
        assert body["request_id"]
        assert body["error"] is None
        result = body["data"]["results"][0]
        assert result["accepted"] is True
        assert result["display_name"] == "说明.md"
        assert result["document_id"] and result["document_version_id"]
        assert result["task_id"] and result["task_state"] == "queued"
        assert result["route"]["mode"] == "local"
        assert result["error"] is None

    def test_mixed_batch_reports_per_file(self, runtime):
        client, _, _, _, kb_id = runtime
        response = _upload(
            client,
            kb_id,
            [
                ("files", ("好.txt", b"plain text body")),
                ("files", ("伪装.exe", b"MZ binary")),
            ],
        )
        results = response.json()["data"]["results"]
        assert [item["accepted"] for item in results] == [True, False]
        assert results[1]["error"]["code"] == "UNSUPPORTED_FORMAT"

    def test_invalid_preference_returns_400(self, runtime):
        client, _, _, _, kb_id = runtime
        response = _upload(
            client,
            kb_id,
            [("files", ("a.txt", b"x"))],
            parser_preference="magic",
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_PARAM"

    def test_batch_over_limit_returns_413(self, runtime):
        client, _, _, _, kb_id = runtime
        files = [("files", (f"f{index}.txt", b"x")) for index in range(51)]
        response = _upload(client, kb_id, files)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "BATCH_TOO_LARGE"

    def test_missing_kb_rejects_every_file(self, runtime):
        client, _, _, _, _ = runtime
        response = _upload(
            client, "no-such-kb", [("files", ("a.txt", b"x"))]
        )
        assert response.status_code == 202
        result = response.json()["data"]["results"][0]
        assert result["accepted"] is False
        assert result["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    def test_idempotency_key_replay_returns_same_task(self, runtime):
        client, _, _, _, kb_id = runtime
        files = [("files", ("again.txt", b"repeatable body"))]
        headers = {"Idempotency-Key": "batch-42"}
        first = _upload(client, kb_id, files, headers=headers)
        replay = _upload(client, kb_id, files, headers=headers)
        assert first.json()["data"]["results"][0]["task_id"] == (
            replay.json()["data"]["results"][0]["task_id"]
        )


class TestCloudConfirmationEndpoint:
    """云端确认端点"""

    @staticmethod
    def _make_waiting_task(imports, tasks, kb_id, sha: str):
        """构造等待确认任务：导入 -> 领取 -> 转等待确认（停留路由阶段）

        每个用例独享空队列：领取到的即为本次导入创建的任务
        """
        outcome = imports.create_import(
            kb_id,
            display_name=f"{sha[:6]}.pdf",
            source_path="staging-path",
            source_sha256=sha,
            parser_mode="local",
        )
        claimed = tasks.claim_next("worker-api")
        assert claimed is not None and claimed.id == outcome.task.id
        tasks.transition(
            outcome.task.id,
            TaskStatus.WAITING_USER,
            stage=TaskStage.ROUTING_PARSER,
        )
        return outcome.task.id

    def test_approve_returns_queued_for_cloud_submission(self, runtime):
        client, _, imports, tasks, kb_id = runtime
        task_id = self._make_waiting_task(imports, tasks, kb_id, "a" * 64)
        approved = client.post(
            f"/api/v1/tasks/{task_id}/cloud-confirmation",
            json={"decision": "approve"},
        )
        assert approved.status_code == 200
        task = approved.json()["data"]["task"]
        assert task["state"] == "queued"
        assert task["stage"] == "submitting_cloud"

    def test_reject_cancels_task(self, runtime):
        client, _, imports, tasks, kb_id = runtime
        task_id = self._make_waiting_task(imports, tasks, kb_id, "b" * 64)
        rejected = client.post(
            f"/api/v1/tasks/{task_id}/cloud-confirmation",
            json={"decision": "reject"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["task"]["state"] == "cancelled"

    def test_unknown_decision_returns_400(self, runtime):
        client, _, imports, tasks, kb_id = runtime
        task_id = self._make_waiting_task(imports, tasks, kb_id, "c" * 64)
        response = client.post(
            f"/api/v1/tasks/{task_id}/cloud-confirmation",
            json={"decision": "maybe"},
        )
        assert response.status_code == 400

    def test_confirmed_task_reconfirmation_conflict(self, runtime):
        client, _, imports, tasks, kb_id = runtime
        task_id = self._make_waiting_task(imports, tasks, kb_id, "d" * 64)
        client.post(
            f"/api/v1/tasks/{task_id}/cloud-confirmation",
            json={"decision": "approve"},
        )
        second = client.post(
            f"/api/v1/tasks/{task_id}/cloud-confirmation",
            json={"decision": "approve"},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "CONFIRMATION_CONFLICT"

    def test_missing_task_returns_404(self, runtime):
        client, _, _, _, _ = runtime
        response = client.post(
            "/api/v1/tasks/nonexistent/cloud-confirmation",
            json={"decision": "approve"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
