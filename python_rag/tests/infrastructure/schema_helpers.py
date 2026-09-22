# -*- coding: utf-8 -*-
"""
测试公共辅助：schema 数据链插入、临时库构建与跨文件共享的行为替身。

插入函数直接执行 SQL，字段值尽量提供默认，测试按需覆盖关键字段；
模块后半部分为导入/维护类测试共享的替身与驱动辅助（pytest fixture
无法跨文件导入，需共享的 fixture 放 conftest.py，其余普通辅助放此）。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.domain.ids import uuid7
from app.domain.parser_routing import decide_parser_route
from app.infrastructure.sqlite.migrations import (
    _DEFAULT_MIGRATIONS_DIR,
    apply_migrations,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore

# 统一的固定时间戳（UTC ISO-8601 文本），避免测试依赖当前时间
FIXED_TIME = "2026-09-17T00:00:00+00:00"


def fresh_db(tmp_path, name="schema.db"):
    """对临时路径应用全部迁移，返回 (db_path, applied_count)"""
    db_path = str(tmp_path / name)
    applied = apply_migrations(db_path, _DEFAULT_MIGRATIONS_DIR)
    return db_path, applied


def insert_kb(conn: sqlite3.Connection, name, kb_id=None, status="active", deleted_at=None) -> str:
    """插入知识库行，返回 id"""
    kb_id = kb_id or uuid7()
    conn.execute(
        "INSERT INTO knowledge_bases"
        " (id, name, description, status, deleted_at, delete_requested_at, created_at, updated_at)"
        " VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
        (kb_id, name, status, deleted_at, FIXED_TIME, FIXED_TIME),
    )
    return kb_id


def insert_document(
    conn: sqlite3.Connection, kb_id, sha256, doc_id=None,
    status="ready", active_version_id=None, deleted_at=None,
) -> str:
    """插入文档行，返回 id"""
    doc_id = doc_id or uuid7()
    conn.execute(
        "INSERT INTO documents"
        " (id, knowledge_base_id, display_name, source_sha256, status,"
        "  active_document_version_id, deleted_at, delete_requested_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (doc_id, kb_id, "doc.pdf", sha256, status, active_version_id, deleted_at, FIXED_TIME, FIXED_TIME),
    )
    return doc_id


def insert_version(
    conn: sqlite3.Connection, doc_id, version_no=1, version_id=None, status="parsed",
) -> str:
    """插入文档版本行，返回 id"""
    version_id = version_id or uuid7()
    conn.execute(
        "INSERT INTO document_versions"
        " (id, document_id, version_no, source_path, source_sha256, mime_type, size_bytes,"
        "  parser_mode, parser_provider, parser_version, parsed_content_sha256, status,"
        "  active_index_version_id, created_at, activated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
        (
            version_id, doc_id, version_no, "uploads/doc.pdf", "a" * 64, "application/pdf",
            1024, "local", "local", "file_parser-1.0", "b" * 64, status, FIXED_TIME,
        ),
    )
    return version_id


def insert_index_version(
    conn: sqlite3.Connection, document_version_id, status="staging", index_no=1, index_id=None,
) -> str:
    """插入索引版本行，返回 id"""
    index_id = index_id or uuid7()
    conn.execute(
        "INSERT INTO index_versions"
        " (id, document_version_id, index_no, status, parser_config_id,"
        "  chunking_config_id, embedding_profile_id, vector_collection, fts_namespace,"
        "  chunk_count, integrity_hash, created_at, activated_at, retired_at)"
        " VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL)",
        (index_id, document_version_id, index_no, status, FIXED_TIME),
    )
    return index_id


def insert_block(
    conn: sqlite3.Connection, document_version_id, block_type="paragraph", ordinal=1,
    parent_block_id=None, content_text="块文本内容",
) -> str:
    """插入解析块行，返回 id"""
    block_id = uuid7()
    conn.execute(
        "INSERT INTO content_blocks"
        " (id, document_version_id, parent_block_id, block_type, ordinal, page_no,"
        "  section_path, content_text, bbox_json, source_locator_json, content_hash)"
        " VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, ?)",
        (block_id, document_version_id, parent_block_id, block_type, ordinal,
         "第一章", content_text, f"hash-{block_id}"),
    )
    return block_id


def insert_table(conn: sqlite3.Connection, block_id) -> str:
    """插入表格扩展证据行，返回 block_id"""
    conn.execute(
        "INSERT INTO tables"
        " (block_id, raw_html, raw_markdown, structure_json, searchable_text,"
        "  serialization_model, serialization_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            block_id, "<table></table>", "| 列 |", "{}", "表格可检索文本",
            "qwen-plus", "v1",
        ),
    )
    return block_id


def insert_chunk(
    conn: sqlite3.Connection, index_version_id, ordinal=1, parent_chunk_id=None,
    content="切片文本内容",
) -> str:
    """插入切片行，返回 id"""
    chunk_id = uuid7()
    conn.execute(
        "INSERT INTO chunks"
        " (id, index_version_id, parent_chunk_id, ordinal, content, token_count,"
        "  section_path, page_start, page_end, content_hash, metadata_json)"
        " VALUES (?, ?, ?, ?, ?, 32, ?, 1, 2, ?, NULL)",
        (chunk_id, index_version_id, parent_chunk_id, ordinal, content,
         "第一章", f"hash-{chunk_id}"),
    )
    return chunk_id


def insert_chunk_block_link(
    conn: sqlite3.Connection, chunk_id, block_id, relation_type="exact",
) -> None:
    """插入切片-块定位关系"""
    conn.execute(
        "INSERT INTO chunk_block_links (chunk_id, block_id, relation_type) VALUES (?, ?, ?)",
        (chunk_id, block_id, relation_type),
    )


def insert_task(
    conn: sqlite3.Connection, task_id=None, task_type="import", state="queued",
    stage=None, kb_id=None, document_id=None, document_version_id=None,
    index_version_id=None, idempotency_key=None, parent_task_id=None,
    retry_origin=None, progress=0.0, priority=0, created_at=FIXED_TIME,
) -> str:
    """插入任务行，返回 id"""
    task_id = task_id or uuid7()
    conn.execute(
        "INSERT INTO tasks"
        " (id, task_type, knowledge_base_id, document_id, document_version_id,"
        "  index_version_id, state, stage, progress, priority, idempotency_key,"
        "  retry_count, max_retries, attempt_count, stage_attempt, total_attempt_count,"
        "  next_retry_at, lease_owner, lease_expires_at, heartbeat_at,"
        "  cancel_requested_at, checkpoint_json, parent_task_id, retry_origin,"
        "  error_code, error_message, input_json, created_at, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 3, 0, 0, 0,"
        "  NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)",
        (
            task_id, task_type, kb_id, document_id, document_version_id,
            index_version_id, state, stage, progress, priority, idempotency_key,
            parent_task_id, retry_origin, created_at,
        ),
    )
    return task_id


def insert_task_event(
    conn: sqlite3.Connection, task_id, event_type="state_changed", state="queued",
    stage=None, error_code=None, created_at=FIXED_TIME,
) -> str:
    """插入任务审计事件行，返回 id"""
    event_id = uuid7()
    conn.execute(
        "INSERT INTO task_events"
        " (id, task_id, event_type, state, stage, attempt_count, worker,"
        "  created_at, duration_ms, checkpoint_json, error_code, detail_json)"
        " VALUES (?, ?, ?, ?, ?, 0, NULL, ?, NULL, NULL, ?, NULL)",
        (event_id, task_id, event_type, state, stage, created_at, error_code),
    )
    return event_id


def insert_external_task(
    conn: sqlite3.Connection, task_id, provider="mineru", provider_batch_ref="batch-1",
    source_ref="source-1", provider_task_id=None, state="submitted",
) -> str:
    """插入外部任务行，返回 id"""
    external_id = uuid7()
    conn.execute(
        "INSERT INTO external_tasks"
        " (id, task_id, provider, provider_batch_ref, source_ref, provider_task_id,"
        "  upload_url_expires_at, remote_cancel_state, provider_status_summary,"
        "  state, poll_count, last_polled_at, request_summary_json,"
        "  result_uri, result_sha256, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 0, NULL, NULL, NULL, NULL, NULL)",
        (external_id, task_id, provider, provider_batch_ref, source_ref,
         provider_task_id, state),
    )
    return external_id


# ===================== 导入/维护类测试共享的替身与驱动辅助 =====================


class FakeEmbeddingGateway:
    """确定性向量化替身：记录调用文本，可选首调挂钩（取消场景用）"""

    def __init__(self, on_first_call=None) -> None:
        self.embedded_texts: list[str] = []
        self._on_first_call = on_first_call

    def embed_texts(self, texts):
        if self._on_first_call is not None:
            hook, self._on_first_call = self._on_first_call, None
            hook()
        self.embedded_texts.extend(texts)
        return [[float(len(text) % 9 + 1)] * 4 for text in texts]


class BlockedDeleteVectorIndex:
    """对指定集合名的删除抛文件系统占用错误，其余方法委托真实适配器"""

    def __init__(self, inner, blocked_names) -> None:
        self._inner = inner
        self.blocked = blocked_names

    def delete_collection(self, collection_name: str) -> None:
        if collection_name in self.blocked:
            raise OSError("[WinError 32] 另一个程序正在使用此文件")
        self._inner.delete_collection(collection_name)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def import_file(env, filename: str, content: bytes, preference: str = "auto",
                duplicate_policy: str = "skip") -> str:
    """经真实暂存与导入事务建立导入任务，返回任务 ID

    env 为 conftest.py 提供的 Worker 环境夹具（需含 kb_id/repos/staging）
    """
    staged = UploadStagingStore(env.staging).stage(iter([content]), filename)
    route = decide_parser_route(staged.extension, preference)
    outcome = env.repos["imports"].create_import(
        env.kb_id,
        display_name=staged.display_name,
        source_path=staged.staging_path,
        source_sha256=staged.sha256,
        mime_type=staged.mime_type,
        size_bytes=staged.size_bytes,
        duplicate_policy=duplicate_policy,
        parser_mode=route.mode.value,
        parser_route_json=json.dumps(
            {
                "mode": route.mode.value,
                "reason": route.reason,
                "router_config_version": route.router_config_version,
                "parser_preference": preference,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return outcome.task.id


def ensure_pipeline_configs(config_repo) -> tuple[str, str, str]:
    """确保切片/嵌入/关键词三类配置行存在，返回三个配置 ID"""
    from app.domain.chunking import (
        DEFAULT_CHUNKING_PARAMS,
        chunking_config_json,
    )
    from app.domain.embedding import embedding_config_json
    from app.domain.keyword import keyword_config_json

    return (
        config_repo.ensure_config(
            "chunking", chunking_config_json(DEFAULT_CHUNKING_PARAMS)
        ),
        config_repo.ensure_config("embedding", embedding_config_json()),
        config_repo.ensure_config("keyword", keyword_config_json()),
    )


def claimed_task(repo, priority: int = 0):
    """创建并领取一个任务（进入 running，持有租约）"""
    task = repo.create("import", priority=priority)
    claimed = repo.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == task.id
    return claimed


def backdate_lease(conn: sqlite3.Connection, task_id: str, *, seconds_ago: int = 31) -> None:
    """把任务租约到期时间改到过去指定秒数（构造租约失效现场）"""
    expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (expires_at, task_id)
    )
