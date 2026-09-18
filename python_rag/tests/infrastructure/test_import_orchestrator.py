# -*- coding: utf-8 -*-
"""批量导入编排测试：逐文件独立成败、路由决策与错误码映射"""
import json

import pytest

from app.infrastructure.ingest import ImportFileInput, ImportOrchestrator
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from tests.infrastructure.schema_helpers import fresh_db, insert_kb


@pytest.fixture()
def orchestrator(tmp_path):
    """临时库 + 受控暂存目录 + 编排器，返回 (编排器, 连接, kb_id)"""
    db_path, _ = fresh_db(tmp_path, "ingest.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "编排测试库")
    store = UploadStagingStore(str(tmp_path / "staging"))
    repo = SQLiteImportRepository(conn)
    yield ImportOrchestrator(store, repo), conn, kb_id
    conn.close()


def _text_file(name: str, text: str) -> ImportFileInput:
    """构造文本文件的批次输入"""
    return ImportFileInput(
        display_name=name,
        chunks=iter([text.encode("utf-8")]),
        declared_mime="text/plain",
    )


class TestBatchOrchestration:
    """批次行为与结果形状"""

    def test_mixed_batch_per_file_independent(self, orchestrator):
        orch, conn, kb_id = orchestrator
        results = orch.import_batch(
            kb_id,
            [
                _text_file("好文件.txt", "正文内容"),
                _text_file("坏格式.docx", " pretending to be docx "),
            ],
        )
        assert [result.accepted for result in results] == [True, False]
        assert results[1].error_code == "UNSUPPORTED_FORMAT"
        assert results[1].document_id is None
        # 好文件不受坏文件影响：文档/版本/任务均已建立
        assert (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_empty_file_rejected_with_code(self, orchestrator):
        orch, _, kb_id = orchestrator
        results = orch.import_batch(
            kb_id,
            [ImportFileInput(display_name="空.txt", chunks=iter([b""]))],
        )
        assert results[0].accepted is False
        assert results[0].error_code == "EMPTY_FILE"

    def test_route_decision_recorded_in_task_input(self, orchestrator):
        orch, conn, kb_id = orchestrator
        results = orch.import_batch(
            kb_id,
            [_text_file("说明.md", "# 标题\n\n正文")],
            parser_preference="auto",
        )
        assert results[0].accepted is True
        assert results[0].route_mode == "local"
        task_input = conn.execute(
            "SELECT input_json FROM tasks WHERE id = ?",
            (results[0].task_id,),
        ).fetchone()[0]
        route = json.loads(task_input)
        assert route["mode"] == "local"
        assert route["reason"]
        assert route["parser_preference"] == "auto"
        assert "router_config_version" in route

    def test_duplicate_skip_rejected_but_batch_continues(self, orchestrator):
        orch, _, kb_id = orchestrator
        first = ImportFileInput(
            display_name="第一.txt", chunks=iter([b"same bytes content"])
        )
        second = ImportFileInput(
            display_name="第二.txt", chunks=iter([b"same bytes content"])
        )
        results = orch.import_batch(kb_id, [first, second])
        assert results[0].accepted is True
        assert results[1].accepted is False
        assert results[1].error_code == "DUPLICATE_FILE"

    def test_duplicate_new_version_accepted(self, orchestrator):
        orch, conn, kb_id = orchestrator
        results = orch.import_batch(
            kb_id,
            [
                ImportFileInput(
                    display_name="第一.txt", chunks=iter([b"same bytes content"])
                ),
                ImportFileInput(
                    display_name="第二.txt", chunks=iter([b"same bytes content"])
                ),
            ],
            duplicate_policy="new_version",
        )
        assert all(result.accepted for result in results)
        assert results[0].document_id == results[1].document_id
        versions = conn.execute(
            "SELECT COUNT(*) FROM document_versions WHERE document_id = ?",
            (results[0].document_id,),
        ).fetchone()[0]
        assert versions == 2

    def test_staged_bytes_reach_staging_directory(self, orchestrator, tmp_path):
        orch, _, kb_id = orchestrator
        results = orch.import_batch(kb_id, [_text_file("落盘.txt", "正文")])
        assert results[0].accepted is True
        staging_files = list((tmp_path / "staging").iterdir())
        assert len(staging_files) == 1
        assert staging_files[0].read_bytes() == "正文".encode()
