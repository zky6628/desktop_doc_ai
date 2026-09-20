# -*- coding: utf-8 -*-
"""文档 API 端点测试：列表/详情/版本/解析块/替换/重建/删除的信封、
状态码、守卫语义与替换的新版本建立。"""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import (
    ApiV1Dependencies,
    DocumentDependencies,
    KnowledgeBaseDependencies,
    create_api_router,
)
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteContentRepository,
    SQLiteDeletionRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
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

# 队列 pending 上限（与任务引擎口径一致）
MAX_PENDING = 50


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + v1 应用（知识库/文档域）+ TestClient

    预置一个知识库与一个带活动版本的文档（版本 sha 为 'a'*64），
    返回 (client, 连接, 仓储集, kb_id, doc_id, active_version_id)
    """
    db_path, _ = fresh_db(tmp_path, "doc_api.db")
    conn = connect(db_path)
    kbs = SQLiteKnowledgeBaseRepository(conn)
    tasks = SQLiteTaskRepository(conn)
    deletions = SQLiteDeletionRepository(conn)
    documents = SQLiteDocumentRepository(conn)
    versions = SQLiteDocumentVersionRepository(conn)
    indexes = SQLiteIndexVersionRepository(conn)
    contents = SQLiteContentRepository(conn)
    chunks = SQLiteChunkRepository(conn)
    imports = SQLiteImportRepository(conn)
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=ImportOrchestrator(
                    staging_store=UploadStagingStore(str(tmp_path / "staging")),
                    import_repo=imports,
                ),
                task_repo=tasks,
                knowledge_bases=KnowledgeBaseDependencies(
                    kb_repo=kbs, deletion_repo=deletions, task_repo=tasks
                ),
                documents=DocumentDependencies(
                    orchestrator=ImportOrchestrator(
                        staging_store=UploadStagingStore(
                            str(tmp_path / "staging")
                        ),
                        import_repo=imports,
                    ),
                    kb_repo=kbs,
                    document_repo=documents,
                    version_repo=versions,
                    index_repo=indexes,
                    content_repo=contents,
                    task_repo=tasks,
                    deletion_repo=deletions,
                    import_repo=imports,
                ),
            )
        )
    )
    kb_id = kbs.create(name="文档测试库").id
    doc_id = insert_document(conn, kb_id, "a" * 64, status="ready")
    active_version_id = insert_version(conn, doc_id, version_no=1, status="parsed")
    conn.execute(
        "UPDATE documents SET active_document_version_id = ? WHERE id = ?",
        (active_version_id, doc_id),
    )
    yield (
        TestClient(application),
        conn,
        {
            "documents": documents,
            "versions": versions,
            "indexes": indexes,
            "contents": contents,
            "chunks": chunks,
            "tasks": tasks,
        },
        kb_id,
        doc_id,
        active_version_id,
    )
    conn.close()


class TestDocumentList:
    def test_list_enriches_and_hides_deleted(self, runtime):
        client, conn, _, kb_id, doc_id, version_id = runtime
        insert_document(conn, kb_id, "b" * 64, deleted_at="2026-09-18T00:00:00+00:00")

        response = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["next_cursor"] is None
        ids = [item["id"] for item in data["items"]]
        assert ids == [doc_id]
        assert data["items"][0]["active_version"]["id"] == version_id
        assert data["items"][0]["chunk_count"] is None

        include = client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            params={"include_deleted": True},
        )
        assert len(include.json()["data"]["items"]) == 2

    def test_list_pagination(self, runtime):
        client, conn, _, kb_id, _, _ = runtime
        insert_document(conn, kb_id, "c" * 64)

        first = client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", params={"limit": 1}
        )
        data = first.json()["data"]
        assert len(data["items"]) == 1 and data["next_cursor"]
        second = client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            params={"limit": 1, "cursor": data["next_cursor"]},
        )
        assert len(second.json()["data"]["items"]) == 1
        assert second.json()["data"]["items"][0]["id"] != data["items"][0]["id"]

    def test_list_missing_kb_rejected(self, runtime):
        client, _, _, _, _, _ = runtime
        response = client.get("/api/v1/knowledge-bases/no-such/documents")
        assert response.status_code == 404


class TestDocumentDetail:
    def test_detail_with_active_version_and_recent_tasks(self, runtime):
        client, conn, _, _, doc_id, version_id = runtime
        insert_task(conn, document_id=doc_id, task_type="import", state="succeeded")
        insert_task(conn, document_id=doc_id, task_type="rebuild_index", state="queued")

        response = client.get(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["active_version"]["id"] == version_id
        assert data["active_version"]["version_no"] == 1
        assert "source_path" not in data["active_version"]
        assert len(data["recent_tasks"]) == 2
        assert data["recent_tasks"][0]["task_type"] == "rebuild_index"

    def test_detail_missing_rejected(self, runtime):
        client, _, _, _, _, _ = runtime
        assert client.get("/api/v1/documents/no-such").status_code == 404

    def test_versions_sorted_ascending(self, runtime):
        client, conn, _, _, doc_id, version_id = runtime
        second = insert_version(conn, doc_id, version_no=2)
        response = client.get(f"/api/v1/documents/{doc_id}/versions")
        data = response.json()["data"]["items"]
        assert [v["version_no"] for v in data] == [1, 2]
        assert data[0]["id"] == version_id
        assert data[1]["id"] == second

    def test_blocks_preview_includes_table_without_raw_html(self, runtime):
        from tests.infrastructure.schema_helpers import insert_block, insert_table

        client, conn, _, _, doc_id, version_id = runtime
        table_block = insert_block(conn, version_id, block_type="table", ordinal=1)
        insert_table(conn, table_block)
        insert_block(conn, version_id, ordinal=2)

        response = client.get(f"/api/v1/documents/{doc_id}/blocks")
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assert [b["ordinal"] for b in items] == [1, 2]
        assert items[0]["table"]["raw_markdown"] == "| 列 |"
        assert "raw_html" not in items[0]["table"]

        paged = client.get(
            f"/api/v1/documents/{doc_id}/blocks", params={"limit": 1}
        )
        page = paged.json()["data"]
        assert len(page["items"]) == 1 and page["next_cursor"] == "1"
        follow = client.get(
            f"/api/v1/documents/{doc_id}/blocks",
            params={"limit": 1, "cursor": page["next_cursor"]},
        )
        assert follow.json()["data"]["items"][0]["ordinal"] == 2


class TestReplaceEndpoint:
    def test_replace_creates_new_version_and_updates_identity(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        content = "# 替换后的新内容".encode()
        response = client.post(
            f"/api/v1/documents/{doc_id}/replace",
            files=[("file", ("新版.md", content))],
            data={"parser_preference": "auto"},
            headers={"Idempotency-Key": "rep-1"},
        )
        assert response.status_code == 202, response.json()
        result = response.json()["data"]["result"]
        assert result["document_id"] == doc_id
        assert result["task_state"] == "queued"

        # 新版本建立在目标文档上且身份列跟随新内容
        row = conn.execute(
            "SELECT d.source_sha256, MAX(v.version_no) FROM documents d"
            " JOIN document_versions v ON v.document_id = d.id WHERE d.id = ?",
            (doc_id,),
        ).fetchone()
        assert row[0] == hashlib.sha256(content).hexdigest()
        assert row[1] == 2

        replay = client.post(
            f"/api/v1/documents/{doc_id}/replace",
            files=[("file", ("新版.md", content))],
            headers={"Idempotency-Key": "rep-1"},
        )
        assert replay.json()["data"]["result"]["task_id"] == result["task_id"]

    def test_replace_same_content_rejected(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        content = b"identical content"
        conn.execute(
            "UPDATE document_versions SET source_sha256 = ? WHERE id = ?",
            (hashlib.sha256(content).hexdigest(), conn.execute(
                "SELECT active_document_version_id FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()[0]),
        )
        response = client.post(
            f"/api/v1/documents/{doc_id}/replace",
            files=[("file", ("same.md", content))],
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_FILE"

    def test_replace_with_pending_task_conflict(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        insert_task(conn, document_id=doc_id, state="queued")
        response = client.post(
            f"/api/v1/documents/{doc_id}/replace",
            files=[("file", ("next.md", "其他内容".encode()))],
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_replace_missing_document_rejected(self, runtime):
        client, _, _, _, _, _ = runtime
        response = client.post(
            "/api/v1/documents/no-such/replace",
            files=[("file", ("x.md", b"x"))],
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


class TestRebuildEndpoint:
    def test_rebuild_creates_task_for_active_version(self, runtime):
        client, _, _, kb_id, doc_id, version_id = runtime
        response = client.post(
            f"/api/v1/documents/{doc_id}/rebuild",
            headers={"Idempotency-Key": "rb-1"},
        )
        assert response.status_code == 202
        task = response.json()["data"]["task"]
        assert task["task_type"] == "rebuild_index"
        assert task["document_version_id"] == version_id
        assert task["knowledge_base_id"] == kb_id

        # 幂等键重放返回同一任务
        replay = client.post(
            f"/api/v1/documents/{doc_id}/rebuild",
            headers={"Idempotency-Key": "rb-1"},
        )
        assert replay.json()["data"]["task"]["id"] == task["id"]

    def test_rebuild_without_active_version_rejected(self, runtime):
        client, conn, _, kb_id, _, _ = runtime
        doc_id = insert_document(conn, kb_id, "d" * 64, status="queued")
        response = client.post(f"/api/v1/documents/{doc_id}/rebuild")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_rebuild_with_pending_task_rejected(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        insert_task(conn, document_id=doc_id, state="queued")
        response = client.post(f"/api/v1/documents/{doc_id}/rebuild")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_rebuild_queue_full_rejected(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        for _ in range(MAX_PENDING):
            insert_task(conn, state="queued")
        response = client.post(f"/api/v1/documents/{doc_id}/rebuild")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TASK_QUEUE_FULL"


class TestDocumentDeleteEndpoint:
    def test_delete_soft_deletes_and_creates_task(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        response = client.delete(
            f"/api/v1/documents/{doc_id}",
            headers={"Idempotency-Key": "dd-1"},
        )
        assert response.status_code == 202
        task = response.json()["data"]["task"]
        assert task["task_type"] == "delete_document"
        row = conn.execute(
            "SELECT deleted_at, status FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        assert row[0] is not None and row[1] == "deleted"

        replay = client.delete(
            f"/api/v1/documents/{doc_id}",
            headers={"Idempotency-Key": "dd-1"},
        )
        assert replay.json()["data"]["task"]["id"] == task["id"]

    def test_delete_with_pending_task_rejected(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        insert_task(conn, document_id=doc_id, state="queued")
        response = client.delete(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 409

    def test_delete_queue_full_rolls_back(self, runtime):
        client, conn, _, _, doc_id, _ = runtime
        for _ in range(MAX_PENDING):
            insert_task(conn, state="queued")
        response = client.delete(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 429
        row = conn.execute(
            "SELECT deleted_at FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        assert row[0] is None
