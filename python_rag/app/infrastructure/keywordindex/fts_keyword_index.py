# -*- coding: utf-8 -*-
"""FTS5 关键词索引适配器：命名空间隔离的重建式写入与短语查询

每个索引版本对应一个命名空间；重建先清空该命名空间再插入本次输入
（幂等，重放不产生重复行）。文档内容为分词器产出的空格分隔文本，
由 unicode61 tokenizer 建立倒排；查询以词元序列构成单短语精确匹配，
unicode61 对查询短语与索引内容做同样的词元切分（标点两侧一致地
按分隔符处理）。命名空间内删除与计数依赖虚拟表的行扫描（v1 规模
可接受）。
"""
import sqlite3
from collections.abc import Sequence

from app.domain.keyword import KeywordDocument
from app.domain.ports import KeywordIndexGateway as KeywordIndexGatewayPort
from app.domain.retrieval import IndexHit

from ..sqlite.transactions import run_in_transaction


class SQLiteFtsKeywordIndex(KeywordIndexGatewayPort):
    """chunks_fts 虚拟表的写入与查询实现

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

    def query_keywords(
        self, namespace: str, tokens: Sequence[str], top_k: int
    ) -> list[IndexHit]:
        """按预分词词元取短语命中（方法契约见领域 Port 定义）"""
        # 纯标点词元在 unicode61 下不产生索引词元（两侧一致按分隔符
        # 处理），过滤避免空短语查询
        phrase_tokens = [
            token for token in tokens if any(char.isalnum() for char in token)
        ]
        if not phrase_tokens:
            return []
        # 短语语法：词元序列构成单短语；词元内双引号按 FTS5 字符串
        # 语法转义，保证任意词元组合的查询语法合法
        escaped = [token.replace('"', '""') for token in phrase_tokens]
        match_phrase = '"' + " ".join(escaped) + '"'
        rows = self._conn.execute(
            "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts"
            " WHERE chunks_fts MATCH ? AND fts_namespace = ?"
            " ORDER BY bm25(chunks_fts)"
            " LIMIT ?",
            (match_phrase, namespace, top_k),
        ).fetchall()
        # bm25 原始值为负且越小越匹配，取负即为越高越相关
        return [
            IndexHit(chunk_id=row[0], raw_score=float(row[1]), score=-float(row[1]))
            for row in rows
        ]

    def count_documents(self, namespace: str) -> int:
        """返回命名空间内文档数量（方法契约见领域 Port 定义）"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE fts_namespace = ?", (namespace,)
        ).fetchone()
        return row[0]

    def list_namespaces(self) -> list[str]:
        """返回虚拟表内已存在的全部命名空间（方法契约见领域 Port 定义）"""
        rows = self._conn.execute(
            "SELECT DISTINCT fts_namespace FROM chunks_fts"
        ).fetchall()
        return [row[0] for row in rows]
