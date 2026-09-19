# -*- coding: utf-8 -*-
"""关键词索引测试：jieba 分词、FTS 命名空间重建与迁移落地"""
import sqlite3

import pytest

from app.domain.keyword import KeywordDocument
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.migrations import (
    _DEFAULT_MIGRATIONS_DIR,
    apply_migrations,
)


def test_migration_creates_fts_table_and_config_column(tmp_path):
    """迁移 0006 落地：FTS 虚拟表存在且索引版本带关键词配置列"""
    db_path = str(tmp_path / "kw_migration.db")
    apply_migrations(db_path, _DEFAULT_MIGRATIONS_DIR)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "chunks_fts" in tables
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(index_versions)")
        }
        assert "keyword_config_id" in columns
    finally:
        conn.close()


class TestJiebaTokenizer:
    """分词器：中文切词、归一化与空输入"""

    def setup_method(self) -> None:
        self.tokenizer = JiebaTokenizer()

    def test_chinese_text_splits_into_tokens(self):
        tokens = self.tokenizer.tokenize(["知识工作台检索验证"])
        assert len(tokens) == 1
        assert "".join(tokens[0]) == "知识工作台检索验证"
        assert len(tokens[0]) >= 2

    def test_ascii_normalized_to_lowercase(self):
        tokens = self.tokenizer.tokenize(["Hello World"])
        assert tokens == [["hello", "world"]]

    def test_empty_and_whitespace_texts(self):
        assert self.tokenizer.tokenize(["", "   "]) == [[], []]

    def test_order_preserved_across_batch(self):
        texts = ["第一条内容", "Second text"]
        tokens = self.tokenizer.tokenize(texts)
        assert len(tokens) == 2
        assert "".join(tokens[0]) == "第一条内容"
        assert tokens[1] == ["second", "text"]


class TestSqliteFtsKeywordIndex:
    """FTS 适配器：重建幂等、命名空间隔离与缺失零值"""

    def _fresh(self, tmp_path):
        db_path = str(tmp_path / "kw_index.db")
        apply_migrations(db_path, _DEFAULT_MIGRATIONS_DIR)
        conn = connect(db_path)
        return conn, SQLiteFtsKeywordIndex(conn)

    def _documents(self, count: int) -> list:
        return [
            KeywordDocument(chunk_id=f"chunk-{number}", content=f"词元{number} 文本")
            for number in range(count)
        ]

    def test_rebuild_and_count_roundtrip(self, tmp_path):
        conn, index = self._fresh(tmp_path)
        try:
            written = index.rebuild_namespace("fts-a", self._documents(3))
            assert written == 3
            assert index.count_documents("fts-a") == 3
        finally:
            conn.close()

    def test_rebuild_is_idempotent(self, tmp_path):
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace("fts-a", self._documents(3))
            index.rebuild_namespace("fts-a", self._documents(3))
            assert index.count_documents("fts-a") == 3
        finally:
            conn.close()

    def test_rebuild_replaces_previous_content(self, tmp_path):
        """重建完全取代既有内容：旧 chunk_id 不残留"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a", [KeywordDocument(chunk_id="old-1", content="旧内容")]
            )
            index.rebuild_namespace("fts-a", self._documents(2))
            rows = conn.execute(
                "SELECT chunk_id FROM chunks_fts WHERE fts_namespace = 'fts-a'"
                " ORDER BY chunk_id"
            ).fetchall()
            assert [row[0] for row in rows] == ["chunk-0", "chunk-1"]
        finally:
            conn.close()

    def test_namespaces_are_isolated(self, tmp_path):
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace("fts-a", self._documents(2))
            index.rebuild_namespace("fts-b", self._documents(1))
            index.rebuild_namespace("fts-a", self._documents(1))
            assert index.count_documents("fts-a") == 1
            assert index.count_documents("fts-b") == 1
            assert index.count_documents("fts-missing") == 0
        finally:
            conn.close()

    def test_pretokenized_content_is_matchable(self, tmp_path):
        """空格分隔的预分词文本可被 FTS MATCH 命中"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a",
                [
                    KeywordDocument(chunk_id="c1", content="知识 工作台 向量"),
                    KeywordDocument(chunk_id="c2", content="其他 内容"),
                ],
            )
            hits = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH '知识'"
                )
            ]
            assert hits == ["c1"]
        finally:
            conn.close()

    def test_query_keywords_orders_by_relevance_and_keeps_raw_score(self, tmp_path):
        """短语查询按相关度降序：score = -bm25 且原始值保留"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a",
                [
                    KeywordDocument(chunk_id="c1", content="苹果 是 水果"),
                    KeywordDocument(chunk_id="c2", content="苹果 苹果 苹果"),
                    KeywordDocument(chunk_id="c3", content="香蕉 是 水果"),
                ],
            )
            hits = index.query_keywords("fts-a", ["苹果"], top_k=10)

            assert [hit.chunk_id for hit in hits] == ["c2", "c1"]
            assert hits[0].score > hits[1].score >= 0
            for hit in hits:
                assert hit.score == pytest.approx(-hit.raw_score)
        finally:
            conn.close()

    def test_query_keywords_multi_token_phrase_requires_adjacency(self, tmp_path):
        """多词元查询构成单短语，要求词元在索引内容中相邻"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a",
                [
                    KeywordDocument(chunk_id="c1", content="年假 制度 规定"),
                    KeywordDocument(chunk_id="c2", content="年假 其他 制度"),
                ],
            )
            hits = index.query_keywords("fts-a", ["年假", "制度"], top_k=10)

            assert [hit.chunk_id for hit in hits] == ["c1"]
        finally:
            conn.close()

    def test_query_keywords_empty_tokens_returns_empty(self, tmp_path):
        """空词元序列不发起查询，直接返回空列表"""
        conn, index = self._fresh(tmp_path)
        try:
            assert index.query_keywords("fts-a", [], top_k=10) == []
        finally:
            conn.close()

    def test_query_keywords_punctuation_tokens_dropped(self, tmp_path):
        """纯标点词元在索引侧无词元对应，过滤后短语仍按词元命中"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a", [KeywordDocument(chunk_id="c1", content="苹果 香蕉")]
            )

            hits = index.query_keywords("fts-a", ["苹果", "，", "香蕉"], top_k=10)

            assert [hit.chunk_id for hit in hits] == ["c1"]
        finally:
            conn.close()

    def test_query_keywords_quote_tokens_keep_syntax_valid(self, tmp_path):
        """含引号词元转义后语法合法（无命中但不报错）"""
        conn, index = self._fresh(tmp_path)
        try:
            assert index.query_keywords("fts-a", ['a"b'], top_k=10) == []
        finally:
            conn.close()

    def test_query_keywords_namespaces_are_isolated(self, tmp_path):
        """查询按命名空间隔离：跨命名空间内容互不可见"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a", [KeywordDocument(chunk_id="c1", content="苹果 是 水果")]
            )
            index.rebuild_namespace(
                "fts-b", [KeywordDocument(chunk_id="c2", content="香蕉 是 水果")]
            )

            assert [hit.chunk_id for hit in index.query_keywords("fts-a", ["苹果"], top_k=10)] == ["c1"]
            assert index.query_keywords("fts-b", ["苹果"], top_k=10) == []
        finally:
            conn.close()

    def test_query_keywords_respects_top_k(self, tmp_path):
        """查询返回数量不超过 top_k"""
        conn, index = self._fresh(tmp_path)
        try:
            index.rebuild_namespace(
                "fts-a",
                [
                    KeywordDocument(chunk_id="c1", content="苹果 有 营养"),
                    KeywordDocument(chunk_id="c2", content="苹果 很 常见"),
                    KeywordDocument(chunk_id="c3", content="其他 内容"),
                ],
            )

            hits = index.query_keywords("fts-a", ["苹果"], top_k=1)

            assert len(hits) == 1
        finally:
            conn.close()
