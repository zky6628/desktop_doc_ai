# -*- coding: utf-8 -*-
"""索引版本仓储的 SQLite 实现（含激活事务）"""
from app.domain.clock import utc_now_iso
from app.domain.entities import IndexVersion, IndexVersionStatus
from app.domain.errors import ActivationError, EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import IndexVersionRepository as IndexVersionRepositoryPort

from ..transactions import run_in_transaction

# 不允许进入激活流程的状态：退役/失败是终态，需要新建索引版本
_ACTIVATION_BLOCKED_STATUSES = (
    IndexVersionStatus.RETIRED.value,
    IndexVersionStatus.FAILED.value,
)


class SQLiteIndexVersionRepository(IndexVersionRepositoryPort):
    """index_versions 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        document_version_id: str,
        vector_collection: str | None = None,
        fts_namespace: str | None = None,
        chunking_config_id: str | None = None,
        embedding_profile_id: str | None = None,
        keyword_config_id: str | None = None,
    ) -> IndexVersion:
        def _create(conn) -> IndexVersion:
            now = utc_now_iso()
            version_exists = conn.execute(
                "SELECT 1 FROM document_versions WHERE id = ?", (document_version_id,)
            ).fetchone()
            if version_exists is None:
                raise EntityNotFoundError(f"文档版本不存在: {document_version_id}")
            next_no = conn.execute(
                "SELECT COALESCE(MAX(index_no), 0) + 1 FROM index_versions"
                " WHERE document_version_id = ?",
                (document_version_id,),
            ).fetchone()[0]
            index_id = uuid7()
            conn.execute(
                "INSERT INTO index_versions"
                " (id, document_version_id, index_no, status, parser_config_id,"
                "  chunking_config_id, embedding_profile_id, keyword_config_id,"
                "  vector_collection, fts_namespace,"
                "  chunk_count, integrity_hash, created_at, activated_at, retired_at)"
                " VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)",
                (
                    index_id, document_version_id, next_no,
                    IndexVersionStatus.STAGING.value,
                    chunking_config_id, embedding_profile_id, keyword_config_id,
                    vector_collection, fts_namespace, now,
                ),
            )
            return IndexVersion(
                id=index_id,
                document_version_id=document_version_id,
                index_no=next_no,
                status=IndexVersionStatus.STAGING,
                parser_config_id=None,
                chunking_config_id=chunking_config_id,
                embedding_profile_id=embedding_profile_id,
                vector_collection=vector_collection,
                fts_namespace=fts_namespace,
                chunk_count=None,
                integrity_hash=None,
                created_at=now,
                activated_at=None,
                retired_at=None,
            )

        return run_in_transaction(
            self._conn, _create, f"创建索引版本 {document_version_id}"
        )

    def set_vector_collection(self, index_id: str, collection_name: str) -> None:
        """登记向量集合名（方法契约见领域 Port 定义）"""

        def _set(conn) -> None:
            self._require_index(conn, index_id)
            conn.execute(
                "UPDATE index_versions SET vector_collection = ? WHERE id = ?",
                (collection_name, index_id),
            )

        run_in_transaction(self._conn, _set, f"登记向量集合 {index_id}")

    def set_fts_namespace(self, index_id: str, namespace: str) -> None:
        """登记 FTS 命名空间（方法契约见领域 Port 定义）"""

        def _set(conn) -> None:
            self._require_index(conn, index_id)
            conn.execute(
                "UPDATE index_versions SET fts_namespace = ? WHERE id = ?",
                (namespace, index_id),
            )

        run_in_transaction(self._conn, _set, f"登记 FTS 命名空间 {index_id}")

    def record_validation(
        self, index_id: str, *, chunk_count: int, integrity_hash: str
    ) -> None:
        """记录完整性验证结果（方法契约见领域 Port 定义）"""

        def _record(conn) -> None:
            self._require_index(conn, index_id)
            conn.execute(
                "UPDATE index_versions SET chunk_count = ?, integrity_hash = ?"
                " WHERE id = ?",
                (chunk_count, integrity_hash, index_id),
            )

        run_in_transaction(self._conn, _record, f"记录索引验证结果 {index_id}")

    @staticmethod
    def _require_index(conn, index_id: str) -> None:
        """校验索引版本存在；缺失抛领域错误"""
        exists = conn.execute(
            "SELECT 1 FROM index_versions WHERE id = ?", (index_id,)
        ).fetchone()
        if exists is None:
            raise EntityNotFoundError(f"索引版本不存在: {index_id}")

    def get(self, index_id: str) -> IndexVersion | None:
        row = self._get_row(self._conn, index_id)
        return self._to_entity(row) if row is not None else None

    def list_by_document_version(self, document_version_id: str) -> list:
        rows = self._conn.execute(
            "SELECT id, document_version_id, index_no, status, parser_config_id,"
            " chunking_config_id, embedding_profile_id, vector_collection, fts_namespace,"
            " chunk_count, integrity_hash, created_at, activated_at, retired_at"
            " FROM index_versions WHERE document_version_id = ?"
            " ORDER BY index_no",
            (document_version_id,),
        ).fetchall()
        return [self._to_entity(row) for row in rows]

    def activate(self, index_id: str) -> IndexVersion:
        """激活事务：退役既有 active、激活目标、回填文档版本指针，
        并在事务内校验双向一致后返回激活后的实体"""

        def _activate(conn) -> IndexVersion:
            now = utc_now_iso()
            row = self._get_row(conn, index_id)
            if row is None:
                raise EntityNotFoundError(f"索引版本不存在: {index_id}")
            document_version_id = row[1]
            status = row[3]
            if status in _ACTIVATION_BLOCKED_STATUSES:
                raise ActivationError(
                    f"索引版本 {index_id} 状态为 {status}，不允许激活"
                )

            # 既有活动索引退役（保留历史，不物理删除）
            conn.execute(
                "UPDATE index_versions"
                " SET status = ?, retired_at = ?"
                " WHERE document_version_id = ? AND status = ? AND id != ?",
                (
                    IndexVersionStatus.RETIRED.value, now,
                    document_version_id, IndexVersionStatus.ACTIVE.value, index_id,
                ),
            )
            # 目标置为 active
            conn.execute(
                "UPDATE index_versions"
                " SET status = ?, activated_at = ?, retired_at = NULL"
                " WHERE id = ?",
                (IndexVersionStatus.ACTIVE.value, now, index_id),
            )
            # 文档版本指针回填
            conn.execute(
                "UPDATE document_versions"
                " SET active_index_version_id = ? WHERE id = ?",
                (index_id, document_version_id),
            )

            # 双向一致性校验：指针必须指向目标，且目标必须是 active
            pointer = conn.execute(
                "SELECT active_index_version_id FROM document_versions WHERE id = ?",
                (document_version_id,),
            ).fetchone()[0]
            final_status = self._get_row(conn, index_id)[3]
            if pointer != index_id or final_status != IndexVersionStatus.ACTIVE.value:
                raise ActivationError(
                    f"激活后一致性校验失败: 指针={pointer}, 状态={final_status}"
                )
            return self._to_entity(self._get_row(conn, index_id))

        return run_in_transaction(self._conn, _activate, f"激活索引版本 {index_id}")

    @staticmethod
    def _get_row(conn, index_id: str):
        """按 ID 读取索引版本原始行"""
        return conn.execute(
            "SELECT id, document_version_id, index_no, status, parser_config_id,"
            " chunking_config_id, embedding_profile_id, vector_collection, fts_namespace,"
            " chunk_count, integrity_hash, created_at, activated_at, retired_at"
            " FROM index_versions WHERE id = ?",
            (index_id,),
        ).fetchone()

    @staticmethod
    def _to_entity(row) -> IndexVersion:
        """把查询行转换为领域实体"""
        return IndexVersion(
            id=row[0],
            document_version_id=row[1],
            index_no=row[2],
            status=IndexVersionStatus(row[3]),
            parser_config_id=row[4],
            chunking_config_id=row[5],
            embedding_profile_id=row[6],
            vector_collection=row[7],
            fts_namespace=row[8],
            chunk_count=row[9],
            integrity_hash=row[10],
            created_at=row[11],
            activated_at=row[12],
            retired_at=row[13],
        )
