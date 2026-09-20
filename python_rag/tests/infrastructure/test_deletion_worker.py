# -*- coding: utf-8 -*-
"""删除与健康检查任务的 Worker 测试：delete_document / delete_kb 的
物理清理（源文件/解析产物/切片/向量集合/FTS 命名空间）与
health_check 的结论落盘。复用 conftest 的 Worker 环境夹具。"""
import json
import os

from tests.infrastructure.schema_helpers import import_file


def _import_and_process(env, filename="说明.md", content=None):
    """经真实管线导入一个文件并处理完成，返回 (document_id, version_id)"""
    if content is None:
        content = "# 标题\n\n正文段落。".encode()
    task_id = import_file(env, filename, content)
    assert env.build().process_next() is True  # 导入任务
    row = env.conn.execute(
        "SELECT document_id, document_version_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return row[0], row[1]


def _drain_tasks(env) -> None:
    """处理队列中剩余任务直到无任务可领取（删除/清理任务链）"""
    worker = env.build()
    while worker.process_next():
        pass


class TestDocumentDeletionTask:
    def test_deletes_physical_assets_and_succeeds(self, env):
        document_id, version_id = _import_and_process(env)
        from app.infrastructure.sqlite.repositories import SQLiteDeletionRepository

        deletions = SQLiteDeletionRepository(env.conn)
        delete_task = deletions.create_document_deletion(document_id)

        # 清理前：派生资产在场
        index_id = env.conn.execute(
            "SELECT active_index_version_id FROM document_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0]
        assert env.repos["chunks"].count_index_chunks(index_id) > 0
        source_path = env.conn.execute(
            "SELECT source_path FROM document_versions WHERE id = ?", (version_id,)
        ).fetchone()[0]
        assert os.path.exists(source_path)

        _drain_tasks(env)
        finished = env.repos["tasks"].get(delete_task.id)
        assert finished.state.value == "succeeded", (
            finished.error_code,
            finished.error_message,
        )

        # 清理后：切片/解析产物/源文件/派生索引全部消失
        assert env.repos["chunks"].count_index_chunks(index_id) == 0
        assert (
            env.repos["content"].list_document_blocks(version_id) == []
        )
        assert not os.path.exists(source_path)
        assert env.vector_index.list_collections() == []
        namespace = env.conn.execute(
            "SELECT fts_namespace FROM index_versions WHERE id = ?", (index_id,)
        ).fetchone()[0]
        assert env.keyword_index.count_documents(namespace) == 0

        # 审计摘要在场
        events = env.repos["tasks"].list_events(delete_task.id)
        assert any(event.event_type == "deletion_summary" for event in events)

    def test_kb_row_stays_as_tombstone(self, env):
        document_id, _ = _import_and_process(env)
        from app.infrastructure.sqlite.repositories import SQLiteDeletionRepository

        deletions = SQLiteDeletionRepository(env.conn)
        delete_task = deletions.create_document_deletion(document_id)
        _drain_tasks(env)
        assert env.repos["tasks"].get(delete_task.id).state.value == "succeeded"
        # 文档行保留为墓碑，物理资产已清
        row = env.conn.execute(
            "SELECT deleted_at, active_document_version_id FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        assert row[0] is not None and row[1] is not None


class TestKnowledgeBaseDeletionTask:
    def test_cleans_all_documents_of_kb(self, env):
        first_doc, _ = _import_and_process(env, "一.md", b"# one")
        second_doc, _ = _import_and_process(env, "二.md", b"# two")
        from app.infrastructure.sqlite.repositories import SQLiteDeletionRepository

        deletions = SQLiteDeletionRepository(env.conn)
        delete_task = deletions.create_kb_deletion(env.kb_id)

        _drain_tasks(env)
        finished = env.repos["tasks"].get(delete_task.id)
        assert finished.state.value == "succeeded", (
            finished.error_code,
            finished.error_message,
        )

        for document_id in (first_doc, second_doc):
            row = env.conn.execute(
                "SELECT deleted_at FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            assert row[0] is not None
        # KB 行保留为墓碑（历史会话可追溯名称）
        kb_row = env.conn.execute(
            "SELECT deleted_at FROM knowledge_bases WHERE id = ?", (env.kb_id,)
        ).fetchone()
        assert kb_row[0] is not None
        assert env.vector_index.list_collections() == []

    def test_delete_task_with_no_documents_succeeds(self, env):
        from app.infrastructure.sqlite.repositories import SQLiteDeletionRepository

        deletions = SQLiteDeletionRepository(env.conn)
        delete_task = deletions.create_kb_deletion(env.kb_id)
        _drain_tasks(env)
        assert env.repos["tasks"].get(delete_task.id).state.value == "succeeded"


class TestHealthCheckTask:
    def test_reports_healthy_after_import(self, env):
        _import_and_process(env)
        task = env.repos["tasks"].create("health_check", knowledge_base_id=env.kb_id)
        _drain_tasks(env)
        finished = env.repos["tasks"].get(task.id)
        assert finished.state.value == "succeeded", (
            finished.error_code,
            finished.error_message,
        )
        events = env.repos["tasks"].list_events(task.id)
        summary_event = next(
            event for event in events if event.event_type == "health_summary"
        )
        summary = json.loads(summary_event.detail_json)
        assert summary["healthy"] is True
        assert summary["checked"] >= 1

    def test_empty_kb_reports_zero_checked(self, env):
        task = env.repos["tasks"].create("health_check", knowledge_base_id=env.kb_id)
        assert env.build().process_next() is True
        events = env.repos["tasks"].list_events(task.id)
        summary = json.loads(
            next(
                event for event in events if event.event_type == "health_summary"
            ).detail_json
        )
        assert summary == {"healthy": True, "checked": 0, "findings": []}
