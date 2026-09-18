# -*- coding: utf-8 -*-
"""内容仓储的 SQLite 实现：解析产物事实的幂等重建

解析事实以文档版本为界整体替换：单事务内先删除该版本的表格证据
与内容块，再按块序写入，重放同一解析产物不会产生重复块。块的
物理 ID 每次重建重新生成，幂等性由全量替换保证，不依赖块 ID 跨
重放稳定；同一版本的重新解析只发生在解析从未成功的重试路径上，
因此重建时不存在引用块 ID 的切片关联。
"""
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.parsing import ParsedDocument
from app.domain.ports import ContentRepository as ContentRepositoryPort

from ..transactions import run_in_transaction


class SQLiteContentRepository(ContentRepositoryPort):
    """content_blocks / tables 表的写入实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def replace_document_content(
        self, document_version_id: str, parsed: ParsedDocument
    ) -> int:
        """幂等重建版本的解析事实（方法契约见领域 Port 定义）"""

        def _replace(conn) -> int:
            exists = conn.execute(
                "SELECT 1 FROM document_versions WHERE id = ?",
                (document_version_id,),
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"文档版本不存在: {document_version_id}")

            # 先删扩展证据再删块：tables.block_id 引用 content_blocks，
            # 删除顺序保证外键约束始终满足
            conn.execute(
                "DELETE FROM tables WHERE block_id IN"
                " (SELECT id FROM content_blocks WHERE document_version_id = ?)",
                (document_version_id,),
            )
            conn.execute(
                "DELETE FROM content_blocks WHERE document_version_id = ?",
                (document_version_id,),
            )
            for block in parsed.blocks:
                block_id = uuid7()
                # parent_block_id 暂为空：统一解析模型尚无层级块，
                # 字段随层级结构启用后再写入
                conn.execute(
                    "INSERT INTO content_blocks"
                    " (id, document_version_id, parent_block_id, block_type, ordinal,"
                    "  page_no, section_path, content_text, bbox_json,"
                    "  source_locator_json, content_hash)"
                    " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        block_id, document_version_id, block.block_type.value,
                        block.ordinal, block.page_no, block.section_path,
                        block.text, block.bbox_json, block.source_locator_json,
                        block.content_hash,
                    ),
                )
                if block.table is not None:
                    conn.execute(
                        "INSERT INTO tables"
                        " (block_id, raw_html, raw_markdown, structure_json,"
                        "  searchable_text, serialization_model, serialization_version)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            block_id, block.table.raw_html, block.table.raw_markdown,
                            block.table.structure_json, block.table.searchable_text,
                            block.table.serialization_model,
                            block.table.serialization_version,
                        ),
                    )
            return len(parsed.blocks)

        return run_in_transaction(
            self._conn,
            _replace,
            f"重建文档版本 {document_version_id} 的解析内容",
        )
