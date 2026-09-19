# -*- coding: utf-8 -*-
"""引用快照仓储的 SQLite 实现：批量幂等写入与按消息读取

以 (助手消息, 引用序号) 为唯一键，重放冲突行忽略不覆盖；切片清理
由存储层 ON DELETE SET NULL 表达（快照行保留，切片指针置空）。
读取回填主键；写入时主键由仓储生成、创建时间取 UTC 时钟。
"""
from collections.abc import Sequence

from app.domain.citation import CitationRecord
from app.domain.clock import utc_now_iso
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import CitationRepository as CitationRepositoryPort

from ..transactions import run_in_transaction

# 引用快照列的读取顺序（与 _to_record 对应）
_RECORD_COLUMNS = (
    "id, assistant_message_id, citation_order, chunk_id,"
    " knowledge_base_id_snapshot, document_id_snapshot,"
    " document_version_id_snapshot, file_name_snapshot, version_no_snapshot,"
    " quoted_text_snapshot, content_snapshot, page_no, section_path,"
    " source_locator_json, validation_state, query_run_id,"
    " vector_score, keyword_score, fusion_score, rerank_score"
)


class SQLiteCitationRepository(CitationRepositoryPort):
    """citations 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def insert_citations(self, records: Sequence[CitationRecord]) -> int:
        """批量写入引用快照（方法契约见领域 Port 定义）"""

        def _insert(conn) -> int:
            written = 0
            for record in records:
                exists = conn.execute(
                    "SELECT 1 FROM messages WHERE id = ?",
                    (record.assistant_message_id,),
                ).fetchone()
                if exists is None:
                    raise EntityNotFoundError(
                        f"助手消息不存在: {record.assistant_message_id}"
                    )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO citations ("
                    " id, assistant_message_id, citation_order, chunk_id,"
                    " knowledge_base_id_snapshot, document_id_snapshot,"
                    " document_version_id_snapshot, file_name_snapshot,"
                    " version_no_snapshot, quoted_text_snapshot,"
                    " content_snapshot, page_no, section_path,"
                    " source_locator_json, validation_state, query_run_id,"
                    " vector_score, keyword_score, fusion_score, rerank_score,"
                    " created_at)"
                    f" VALUES ({', '.join(['?'] * 21)})",
                    (
                        uuid7(),
                        record.assistant_message_id,
                        record.citation_order,
                        record.chunk_id,
                        record.knowledge_base_id_snapshot,
                        record.document_id_snapshot,
                        record.document_version_id_snapshot,
                        record.file_name_snapshot,
                        record.version_no_snapshot,
                        record.quoted_text_snapshot,
                        record.content_snapshot,
                        record.page_no,
                        record.section_path,
                        record.source_locator_json,
                        record.validation_state,
                        record.query_run_id,
                        record.vector_score,
                        record.keyword_score,
                        record.fusion_score,
                        record.rerank_score,
                        utc_now_iso(),
                    ),
                )
                written += cursor.rowcount
            return written

        return run_in_transaction(
            self._conn, _insert, f"写入引用快照 {len(records)} 条"
        )

    def list_by_message(self, assistant_message_id: str) -> list[CitationRecord]:
        """按助手消息读取引用快照（方法契约见领域 Port 定义）"""
        rows = self._conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM citations"
            " WHERE assistant_message_id = ? ORDER BY citation_order",
            (assistant_message_id,),
        ).fetchall()
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row) -> CitationRecord:
        """把查询行转换为领域快照记录（主键回填 citation_id）"""
        return CitationRecord(
            citation_id=row[0],
            assistant_message_id=row[1],
            citation_order=row[2],
            chunk_id=row[3],
            knowledge_base_id_snapshot=row[4],
            document_id_snapshot=row[5],
            document_version_id_snapshot=row[6],
            file_name_snapshot=row[7],
            version_no_snapshot=row[8],
            quoted_text_snapshot=row[9],
            content_snapshot=row[10],
            page_no=row[11],
            section_path=row[12],
            source_locator_json=row[13],
            validation_state=row[14],
            query_run_id=row[15],
            vector_score=row[16],
            keyword_score=row[17],
            fusion_score=row[18],
            rerank_score=row[19],
        )
