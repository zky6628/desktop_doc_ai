# -*- coding: utf-8 -*-
"""FTS5 关键词索引适配器：命名空间隔离的重建式写入

每个索引版本对应一个命名空间；重建先清空该命名空间再插入本次输入
（幂等，重放不产生重复行）。文档内容为分词器产出的空格分隔文本，
由 unicode61 tokenizer 建立倒排；命名空间内删除与计数依赖虚拟表
的行扫描（v1 规模可接受）。检索查询属检索里程碑能力，不在本适配器。
"""
import sqlite3
from collections.abc import Sequence

from app.domain.keyword import KeywordDocument
from app.domain.ports import KeywordIndexGateway as KeywordIndexGatewayPort

from ..sqlite.transactions import run_in_transaction


class SQLiteFtsKeywordIndex(KeywordIndexGatewayPort):
    """chunks_fts 虚拟表的写入实现

    :param conn: 工作台 SQLite 连接（autocommit 模式，事务由执行器管理）
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def rebuild_namespace(
        self, namespace: str, documents: Sequence[KeywordDocument]
    ) -> int:
        """重建命名空间（方法契约见领域 Port 定义）"""

        def _rebuild(conn: sqlite3.Connection) -> int:
            conn.execute("DELETE FROM chunks_fts WHERE fts_namespace = ?", (namespace,))
            for document in documents:
                conn.execute(
                    "INSERT INTO chunks_fts (fts_namespace, chunk_id, content)"
                    " VALUES (?, ?, ?)",
                    (namespace, document.chunk_id, document.content),
                )
            return len(documents)

        return run_in_transaction(self._conn, _rebuild, f"重建关键词索引 {namespace}")

    def count_documents(self, namespace: str) -> int:
        """返回命名空间内文档数量（方法契约见领域 Port 定义）"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE fts_namespace = ?", (namespace,)
        ).fetchone()
        return row[0]
