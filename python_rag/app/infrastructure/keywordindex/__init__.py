# -*- coding: utf-8 -*-
"""关键词索引适配器：jieba 分词器与 FTS5 命名空间写入"""
from .fts_keyword_index import SQLiteFtsKeywordIndex
from .jieba_tokenizer import JiebaTokenizer

__all__ = ["JiebaTokenizer", "SQLiteFtsKeywordIndex"]
