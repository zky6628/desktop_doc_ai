# -*- coding: utf-8 -*-
"""FTS5 关键词索引适配器：命名空间隔离的重建式写入与析取式匹配查询

每个索引版本对应一个命名空间；重建先清空该命名空间再插入本次输入
（幂等，重放不产生重复行）。文档内容为分词器产出的空格分隔文本，
由 unicode61 tokenizer 建立倒排。

查询采用析取（OR）匹配：任一预分词词元命中即召回，按 bm25 相关度
排序截断。选择析取而非合取的原因：文档各自独立命名空间，自然语言
提问必然携带该文档中不存在的词元（疑问词、助词、同义表述），只要
求全词元命中就会因单个词元缺失而整体归零，使该路长期空转；而 bm25
的词频-逆文档频率权重本就把"同时含多个稀有词元"的切片排在只命中
常见词元的切片之前，精确措辞的强信号无需额外的短语级过滤即可保留。
召回的宽窄由 top_k 截断，最终取舍交给融合与重排裁决。

查询与索引内容由 unicode61 做同样的词元切分（标点两侧一致地按分隔符
处理）。命名空间内删除与计数依赖虚拟表的行扫描（v1 规模可接受）。
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
        """按预分词词元取析取匹配命中（方法契约见领域 Port 定义）"""
        # 纯标点词元在 unicode61 下不产生索引词元（两侧一致按分隔符
        # 处理），过滤避免无意义的空查询
        match_tokens = [
            token for token in tokens if any(char.isalnum() for char in token)
        ]
        if not match_tokens:
            return []
        # 词元内双引号按 FTS5 字符串语法转义，保证任意词元组合的查询
        # 语法合法
        escaped = [token.replace('"', '""') for token in match_tokens]
        rows = self._query(
            namespace, " OR ".join(f'"{token}"' for token in escaped), top_k
        )
        # bm25 原始值为负且越小越匹配，取负即为越高越相关
        return [
            IndexHit(chunk_id=row[0], raw_score=float(row[1]), score=-float(row[1]))
            for row in rows
        ]

    def _query(self, namespace: str, match_expr: str, top_k: int) -> list:
        """按 MATCH 表达式查询命名空间并按 bm25 相关度降序截断"""
        return self._conn.execute(
            "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts"
            " WHERE chunks_fts MATCH ? AND fts_namespace = ?"
            " ORDER BY bm25(chunks_fts)"
            " LIMIT ?",
            (match_expr, namespace, top_k),
        ).fetchall()

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
