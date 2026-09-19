# -*- coding: utf-8 -*-
"""切片仓储的 SQLite 实现：按序号幂等 upsert 与定位关系维护

写入以 (索引版本, 序号) 为行级键：已存在的切片原地更新并保留首次
写入的主键——下游向量/关键词索引以切片主键为记录 ID，主键不漂移
使重试重写天然幂等，无需确定性 ID。定位关系按切片整体替换；切片
序号恒为 0..n-1 连续，超出本次输入范围的残留行（只可能来自中断的
旧写入）一并清除，关系行随切片删除级联消失。
"""
from collections.abc import Sequence

from app.domain.chunking import CHUNK_BLOCK_RELATION_EXACT, Chunk, StoredChunk
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import ChunkRepository as ChunkRepositoryPort

from ..transactions import run_in_transaction

# 切片与定位关系列的读取顺序（与 _to_stored_chunk 对应）
_CHUNK_COLUMNS = (
    "id, parent_chunk_id, ordinal, content, content_hash, section_path,"
    " page_start, page_end"
)


class SQLiteChunkRepository(ChunkRepositoryPort):
    """chunks / chunk_block_links 表的写入实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def replace_index_chunks(
        self, index_version_id: str, chunks: Sequence[Chunk]
    ) -> int:
        """按序号幂等写入切片（方法契约见领域 Port 定义）"""

        def _replace(conn) -> int:
            exists = conn.execute(
                "SELECT 1 FROM index_versions WHERE id = ?", (index_version_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"索引版本不存在: {index_version_id}")

            written = 0
            for chunk in chunks:
                chunk_id = self._upsert_chunk(conn, index_version_id, chunk)
                self._replace_links(conn, chunk_id, chunk)
                written += 1

            # 尾部超界清理：序号连续约定下，>= 输入数量的既有行均为残留
            conn.execute(
                "DELETE FROM chunks"
                " WHERE index_version_id = ? AND ordinal >= ?",
                (index_version_id, len(chunks)),
            )
            return written

        return run_in_transaction(
            self._conn,
            _replace,
            f"写入索引版本 {index_version_id} 的切片",
        )

    @staticmethod
    def _upsert_chunk(conn, index_version_id: str, chunk: Chunk) -> str:
        """写入单个切片：命中既有序号则保留主键原地更新，否则新插入

        子切片的父主键按父序号解析（父切片先于子切片写入）；父序号
        无对应行属结构破坏，明确拒绝而非依赖外键报错
        """
        parent_id = None
        if chunk.parent_ordinal is not None:
            parent_row = conn.execute(
                "SELECT id FROM chunks WHERE index_version_id = ? AND ordinal = ?",
                (index_version_id, chunk.parent_ordinal),
            ).fetchone()
            if parent_row is None:
                raise EntityNotFoundError(
                    f"父切片不存在: 序号 {chunk.parent_ordinal}"
                )
            parent_id = parent_row[0]

        existing = conn.execute(
            "SELECT id FROM chunks WHERE index_version_id = ? AND ordinal = ?",
            (index_version_id, chunk.ordinal),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE chunks SET parent_chunk_id = ?, content = ?,"
                " content_hash = ?, section_path = ?, page_start = ?,"
                " page_end = ? WHERE id = ?",
                (
                    parent_id, chunk.content, chunk.content_hash,
                    chunk.section_path, chunk.page_start, chunk.page_end,
                    existing[0],
                ),
            )
            return existing[0]

        chunk_id = uuid7()
        conn.execute(
            "INSERT INTO chunks"
            " (id, index_version_id, parent_chunk_id, ordinal, content,"
            "  token_count, section_path, page_start, page_end, content_hash,"
            "  metadata_json)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL)",
            (
                chunk_id, index_version_id, parent_id, chunk.ordinal,
                chunk.content, chunk.section_path, chunk.page_start,
                chunk.page_end, chunk.content_hash,
            ),
        )
        return chunk_id

    @staticmethod
    def _replace_links(conn, chunk_id: str, chunk: Chunk) -> None:
        """按切片整体替换定位关系（同键主键约束下先清后插保证幂等）"""
        conn.execute(
            "DELETE FROM chunk_block_links WHERE chunk_id = ?", (chunk_id,)
        )
        for block_id in chunk.block_ids:
            conn.execute(
                "INSERT INTO chunk_block_links (chunk_id, block_id, relation_type)"
                " VALUES (?, ?, ?)",
                (chunk_id, block_id, CHUNK_BLOCK_RELATION_EXACT),
            )

    def list_index_chunks(self, index_version_id: str) -> list[StoredChunk]:
        """按序号升序读取切片（方法契约见领域 Port 定义）"""
        exists = self._conn.execute(
            "SELECT 1 FROM index_versions WHERE id = ?", (index_version_id,)
        ).fetchone()
        if exists is None:
            raise EntityNotFoundError(f"索引版本不存在: {index_version_id}")

        rows = self._conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunks"
            " WHERE index_version_id = ? ORDER BY ordinal",
            (index_version_id,),
        ).fetchall()
        # 主键到序号的映射：父子引用按序号语义还原为领域值对象
        id_to_ordinal = {row[0]: row[2] for row in rows}
        links: dict[str, list[str]] = {}
        for chunk_id, block_id in self._conn.execute(
            "SELECT chunk_id, block_id FROM chunk_block_links"
            " WHERE chunk_id IN (SELECT id FROM chunks WHERE index_version_id = ?)"
            " ORDER BY rowid",
            (index_version_id,),
        ).fetchall():
            links.setdefault(chunk_id, []).append(block_id)
        return [self._to_stored_chunk(row, id_to_ordinal, links) for row in rows]

    @staticmethod
    def _to_stored_chunk(
        row, id_to_ordinal: dict[str, int], links: dict[str, list[str]]
    ) -> StoredChunk:
        """把查询行转换为带主键的领域切片（父引用还原为父序号）"""
        parent_ordinal = (
            id_to_ordinal.get(row[1]) if row[1] is not None else None
        )
        chunk = Chunk(
            ordinal=row[2],
            content=row[3],
            content_hash=row[4],
            parent_ordinal=parent_ordinal,
            section_path=row[5],
            page_start=row[6],
            page_end=row[7],
            block_ids=tuple(links.get(row[0], [])),
        )
        return StoredChunk(id=row[0], chunk=chunk)
