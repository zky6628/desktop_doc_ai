# -*- coding: utf-8 -*-
"""文档版本仓储的 SQLite 实现"""
from app.domain.clock import utc_now_iso
from app.domain.entities import DocumentVersion
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import DocumentVersionRepository as DocumentVersionRepositoryPort

from ..transactions import run_in_transaction


class SQLiteDocumentVersionRepository(DocumentVersionRepositoryPort):
    """document_versions 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        document_id: str,
        source_path: str,
        source_sha256: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        parser_mode: str | None = None,
        parser_provider: str | None = None,
        parser_version: str | None = None,
        parsed_content_sha256: str | None = None,
    ) -> DocumentVersion:
        def _create(conn) -> DocumentVersion:
            now = utc_now_iso()
            doc_exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if doc_exists is None:
                raise EntityNotFoundError(f"文档不存在: {document_id}")
            # 版本号在文档内递增：取当前最大值 +1（事务内保证并发安全）
            next_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM document_versions"
                " WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            version_id = uuid7()
            conn.execute(
                "INSERT INTO document_versions"
                " (id, document_id, version_no, source_path, source_sha256, mime_type,"
                "  size_bytes, parser_mode, parser_provider, parser_version,"
                "  parsed_content_sha256, status, active_index_version_id, created_at, activated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', NULL, ?, NULL)",
                (
                    version_id, document_id, next_no, source_path, source_sha256, mime_type,
                    size_bytes, parser_mode, parser_provider, parser_version,
                    parsed_content_sha256, now,
                ),
            )
            return DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=next_no,
                source_path=source_path,
                source_sha256=source_sha256,
                mime_type=mime_type,
                size_bytes=size_bytes,
                parser_mode=parser_mode,
                parser_provider=parser_provider,
                parser_version=parser_version,
                parsed_content_sha256=parsed_content_sha256,
                status="parsed",
                active_index_version_id=None,
                created_at=now,
                activated_at=None,
            )

        return run_in_transaction(self._conn, _create, f"创建文档版本 {document_id}")

    def get(self, version_id: str) -> DocumentVersion | None:
        row = self._conn.execute(
            "SELECT id, document_id, version_no, source_path, source_sha256, mime_type,"
            " size_bytes, parser_mode, parser_provider, parser_version,"
            " parsed_content_sha256, status, active_index_version_id, created_at, activated_at"
            " FROM document_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        return self._to_entity(row) if row is not None else None

    def list_by_document(self, document_id: str) -> list:
        rows = self._conn.execute(
            "SELECT id, document_id, version_no, source_path, source_sha256, mime_type,"
            " size_bytes, parser_mode, parser_provider, parser_version,"
            " parsed_content_sha256, status, active_index_version_id, created_at, activated_at"
            " FROM document_versions WHERE document_id = ?"
            " ORDER BY version_no",
            (document_id,),
        ).fetchall()
        return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row) -> DocumentVersion:
        """把查询行转换为领域实体"""
        return DocumentVersion(
            id=row[0],
            document_id=row[1],
            version_no=row[2],
            source_path=row[3],
            source_sha256=row[4],
            mime_type=row[5],
            size_bytes=row[6],
            parser_mode=row[7],
            parser_provider=row[8],
            parser_version=row[9],
            parsed_content_sha256=row[10],
            status=row[11],
            active_index_version_id=row[12],
            created_at=row[13],
            activated_at=row[14],
        )
