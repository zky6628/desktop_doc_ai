# -*- coding: utf-8 -*-
"""文档仓储的 SQLite 实现"""
from app.domain.clock import utc_now_iso
from app.domain.entities import Document, DocumentStatus
from app.domain.errors import (
    DuplicateActiveContentError,
    EntityNotFoundError,
)
from app.domain.ids import uuid7
from app.domain.ports import DocumentRepository as DocumentRepositoryPort

from ..transactions import run_in_transaction


class SQLiteDocumentRepository(DocumentRepositoryPort):
    """documents 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(self, kb_id: str, display_name: str, source_sha256: str) -> Document:
        def _create(conn) -> Document:
            now = utc_now_iso()
            kb_exists = conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
            if kb_exists is None:
                raise EntityNotFoundError(f"知识库不存在: {kb_id}")
            duplicate = conn.execute(
                "SELECT 1 FROM documents"
                " WHERE knowledge_base_id = ? AND source_sha256 = ? AND deleted_at IS NULL",
                (kb_id, source_sha256),
            ).fetchone()
            if duplicate is not None:
                raise DuplicateActiveContentError(
                    f"知识库内已存在相同内容的活动文档: {source_sha256}"
                )
            doc_id = uuid7()
            conn.execute(
                "INSERT INTO documents"
                " (id, knowledge_base_id, display_name, source_sha256, status,"
                "  active_document_version_id, deleted_at, delete_requested_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (doc_id, kb_id, display_name, source_sha256, DocumentStatus.QUEUED.value, now, now),
            )
            return Document(
                id=doc_id,
                knowledge_base_id=kb_id,
                display_name=display_name,
                source_sha256=source_sha256,
                status=DocumentStatus.QUEUED,
                active_document_version_id=None,
                deleted_at=None,
                delete_requested_at=None,
                created_at=now,
                updated_at=now,
            )

        return run_in_transaction(self._conn, _create, f"创建文档 {display_name}")

    def get(self, doc_id: str) -> Document | None:
        row = self._conn.execute(
            "SELECT id, knowledge_base_id, display_name, source_sha256, status,"
            " active_document_version_id, deleted_at, delete_requested_at, created_at, updated_at"
            " FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        return self._to_entity(row) if row is not None else None

    def list_by_kb(self, kb_id: str, include_deleted: bool = False) -> list:
        sql = (
            "SELECT id, knowledge_base_id, display_name, source_sha256, status,"
            " active_document_version_id, deleted_at, delete_requested_at, created_at, updated_at"
            " FROM documents WHERE knowledge_base_id = ?"
        )
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY created_at DESC, id"
        rows = self._conn.execute(sql, (kb_id,)).fetchall()
        return [self._to_entity(row) for row in rows]

    def soft_delete(self, doc_id: str) -> None:
        def _soft_delete(conn) -> None:
            now = utc_now_iso()
            cursor = conn.execute(
                "UPDATE documents"
                " SET deleted_at = ?, delete_requested_at = ?,"
                "     status = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (now, now, DocumentStatus.DELETED.value, now, doc_id),
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError(f"文档不存在或已删除: {doc_id}")

        run_in_transaction(self._conn, _soft_delete, f"软删除文档 {doc_id}")

    def set_active_version(self, doc_id: str, version_id: str) -> None:
        def _set_active(conn) -> None:
            now = utc_now_iso()
            version_exists = conn.execute(
                "SELECT 1 FROM document_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if version_exists is None:
                raise EntityNotFoundError(f"文档版本不存在: {version_id}")
            cursor = conn.execute(
                "UPDATE documents"
                " SET active_document_version_id = ?, status = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (version_id, DocumentStatus.READY.value, now, doc_id),
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError(f"文档不存在或已删除: {doc_id}")

        run_in_transaction(self._conn, _set_active, f"设置文档活动版本 {doc_id}")

    @staticmethod
    def _to_entity(row) -> Document:
        """把查询行转换为领域实体"""
        return Document(
            id=row[0],
            knowledge_base_id=row[1],
            display_name=row[2],
            source_sha256=row[3],
            status=DocumentStatus(row[4]),
            active_document_version_id=row[5],
            deleted_at=row[6],
            delete_requested_at=row[7],
            created_at=row[8],
            updated_at=row[9],
        )
