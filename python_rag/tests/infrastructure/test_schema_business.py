# -*- coding: utf-8 -*-
"""
业务表 schema 测试（对真实 migrations/ 目录应用 0001-0004）：

- 全新 SQLite 可按顺序创建全部表与索引
- runner 幂等；仅含基建表的旧库可升级
- 部分唯一索引：软删除后允许名称/内容复用
- 外键 RESTRICT 与活动版本指针生效
"""
import sqlite3

import pytest

from app.domain.ids import uuid7
from app.infrastructure.sqlite.migrations import (
    _DEFAULT_MIGRATIONS_DIR,
    SCHEMA_MIGRATIONS_DDL,
    apply_migrations,
)

from .schema_helpers import (
    FIXED_TIME,
    fresh_db,
    insert_document,
    insert_index_version,
    insert_kb,
    insert_version,
)


def _fresh_db(tmp_path):
    """创建应用过全部迁移的临时库（测试辅助）"""
    db_path, applied = fresh_db(tmp_path, name="schema_business.db")
    assert applied == 4
    return db_path


def test_migrations_apply_in_order(tmp_path):
    """全新 SQLite 文件可按 0001->0004 顺序创建全部表与索引"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "pipeline_configs", "model_profiles", "knowledge_bases",
            "documents", "document_versions", "index_versions",
            "content_blocks", "tables", "chunks", "chunk_block_links",
            "schema_migrations",
        } <= tables

        indexes = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "uq_knowledge_bases_active_name",
            "uq_documents_active_source",
            "uq_document_versions_doc_version",
            "uq_pipeline_configs_type_version",
            "uq_index_versions_one_active",
            "uq_chunks_version_ordinal",
        } <= indexes

        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        assert versions == [1, 2, 3, 4]
    finally:
        conn.close()


def test_migrations_idempotent_on_real_dir(tmp_path):
    """migration 可重复检测且不可重复应用（重复运行返回 0）"""
    db_path = _fresh_db(tmp_path)
    assert apply_migrations(db_path, _DEFAULT_MIGRATIONS_DIR) == 0


def test_upgrade_from_legacy_schema(tmp_path):
    """仅含 schema_migrations 基建表的旧库可升级到 0001-0004"""
    db_path = str(tmp_path / "legacy_runner.db")

    # 模拟历史产物：仅有 runner 基建表
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_MIGRATIONS_DDL)
    finally:
        conn.close()

    assert apply_migrations(db_path, _DEFAULT_MIGRATIONS_DIR) == 4


def test_kb_active_name_unique_and_soft_delete_reuse(tmp_path):
    """活动名称唯一；软删除后允许同名重建"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        insert_kb(conn, "项目资料")
        with pytest.raises(sqlite3.IntegrityError):
            insert_kb(conn, "项目资料")

        # 软删除后同名可复用
        conn.execute(
            "UPDATE knowledge_bases SET deleted_at = ?, status = 'deleted' WHERE name = ?",
            (FIXED_TIME, "项目资料"),
        )
        reused_id = insert_kb(conn, "项目资料")
        assert reused_id
    finally:
        conn.close()


def test_document_active_source_unique_and_reimport(tmp_path):
    """同知识库活动内容唯一；软删除后允许相同内容重新导入"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        kb_id = insert_kb(conn, "kb")
        sha = "c" * 64
        insert_document(conn, kb_id, sha)
        with pytest.raises(sqlite3.IntegrityError):
            insert_document(conn, kb_id, sha)

        # 软删除后同内容可重新导入
        conn.execute(
            "UPDATE documents SET deleted_at = ?, status = 'deleted'"
            " WHERE knowledge_base_id = ? AND source_sha256 = ?",
            (FIXED_TIME, kb_id, sha),
        )
        reimported = insert_document(conn, kb_id, sha)
        assert reimported

        # 不同知识库的同内容互不影响（去重范围是 KB 内）
        other_kb = insert_kb(conn, "other")
        insert_document(conn, other_kb, sha)
    finally:
        conn.close()


def test_kb_delete_restrict_when_documents_exist(tmp_path):
    """documents.knowledge_base_id ON DELETE RESTRICT：物理删除被禁止"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        kb_id = insert_kb(conn, "kb")
        insert_document(conn, kb_id, "d" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
    finally:
        conn.close()


def test_document_version_unique_per_document(tmp_path):
    """同一文档内 version_no 唯一；不同文档各自独立编号"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        kb_id = insert_kb(conn, "kb")
        doc_a = insert_document(conn, kb_id, "e" * 64)
        doc_b = insert_document(conn, kb_id, "f" * 64)

        insert_version(conn, doc_a, 1)
        with pytest.raises(sqlite3.IntegrityError):
            insert_version(conn, doc_a, 1)

        # 不同文档的 version_no 相互独立
        insert_version(conn, doc_b, 1)
    finally:
        conn.close()


def test_document_active_version_fk_enforced(tmp_path):
    """documents.active_document_version_id 外键生效"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        kb_id = insert_kb(conn, "kb")
        with pytest.raises(sqlite3.IntegrityError):
            insert_document(conn, kb_id, "0" * 64, active_version_id=uuid7())

        # 正常链路：先建版本，再回填活动指针（首次导入激活流程）
        doc_id = insert_document(conn, kb_id, "0" * 64)
        version_id = insert_version(conn, doc_id, 1)
        conn.execute(
            "UPDATE documents SET active_document_version_id = ? WHERE id = ?",
            (version_id, doc_id),
        )
        # 版本被活动指针引用时物理删除被 RESTRICT 阻止
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM document_versions WHERE id = ?", (version_id,))
    finally:
        conn.close()


def test_index_version_one_active_per_document_version(tmp_path):
    """同一 DocumentVersion 不能有两个 active IndexVersion；
    staging/retired 等非活动状态不受限"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        kb_id = insert_kb(conn, "kb")
        doc_id = insert_document(conn, kb_id, "2" * 64)
        version_id = insert_version(conn, doc_id, 1)

        active_id = insert_index_version(conn, version_id, status="active")

        # 第二个 active 违反部分唯一索引
        with pytest.raises(sqlite3.IntegrityError):
            insert_index_version(conn, version_id, status="active")

        # staging 与 retired 并存合法
        insert_index_version(conn, version_id, status="staging")
        insert_index_version(conn, version_id, status="retired")

        # 旧 active 退役后可激活新版本
        conn.execute("UPDATE index_versions SET status = 'retired' WHERE id = ?", (active_id,))
        insert_index_version(conn, version_id, status="active")
    finally:
        conn.close()


def test_foreign_key_check_clean(tmp_path):
    """PRAGMA foreign_key_check 无违规（前向 FK 的父表全部就位）"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        kb_id = insert_kb(conn, "kb")
        doc_id = insert_document(conn, kb_id, "1" * 64)
        version_id = insert_version(conn, doc_id, 1)
        conn.execute(
            "UPDATE documents SET active_document_version_id = ? WHERE id = ?",
            (version_id, doc_id),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
