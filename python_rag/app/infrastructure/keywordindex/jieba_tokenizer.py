# -*- coding: utf-8 -*-
"""jieba 分词器适配器：中文关键词检索的预分词

精确模式产出可重切的词元序列；归一化（小写、去首尾空白、丢弃空白
词元）保证查询与写入两侧口径一致。分词在本地内存完成，无网络调用。
"""
from collections.abc import Sequence

import jieba

from app.domain.keyword import TOKENIZER_MODE
from app.domain.ports import TextTokenizer as TextTokenizerPort


class JiebaTokenizer(TextTokenizerPort):
    """基于 jieba 的文本分词器

    jieba 词典在进程内惰性初始化（首调用加载），此后线程安全；
    桌面单用户场景下初始化开销可接受
    """

    def __init__(self) -> None:
        # 精确模式为本适配器唯一模式：规格冻结，不暴露切词模式开关
        self._mode = TOKENIZER_MODE

    def tokenize(self, texts: Sequence[str]) -> list[list[str]]:
        """分词并归一化（方法契约见领域 Port 定义）"""
        results: list[list[str]] = []
        for text in texts:
            tokens = jieba.cut(text, cut_all=False, HMM=True)
            normalized = [token.strip().lower() for token in tokens]
            results.append([token for token in normalized if token])
        return results
