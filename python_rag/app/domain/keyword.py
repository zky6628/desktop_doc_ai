# -*- coding: utf-8 -*-
"""关键词索引配置常量与分词规则版本

中文关键词检索依赖分词规则：jieba 精确模式 + 归一化规则产出空格
分隔的预分词文本，写入 FTS5（unicode61 tokenizer 按空格切词）。
分词规则版本与 jieba 版本一并进入配置行——任一变化都会改变分词
产出，经配置版本化使重建后的索引可辨别。
"""
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

from app.domain.parsing import canonical_json


@dataclass(frozen=True)
class KeywordDocument:
    """关键词索引文档：切片主键与其预分词文本

    content 为分词器产出的空格分隔词元文本（非原始正文——原始正文
    以 chunks.content 为唯一事实源，本表只承载倒排内容）
    """

    chunk_id: str
    content: str


# 关键词索引配置版本：分词规则/归一化/停用词语义变化时必须递增
KEYWORD_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
KEYWORD_CONFIG_TYPE = "keyword"

# 分词引擎：jieba 精确模式（可重切、无歧义冗余）
TOKENIZER_NAME = "jieba"
TOKENIZER_MODE = "accurate"

# 停用词表版本：v1 为空集（无停用词过滤）；引入停用词时递增配置版本
STOPWORDS_VERSION = "empty-v1"


def jieba_version() -> str:
    """读取运行时 jieba 版本号（未知时以 unknown 标记）"""
    try:
        return _package_version("jieba")
    except PackageNotFoundError:
        return "unknown"


def keyword_config_json() -> str:
    """产出当前关键词索引参数集的规范 JSON（配置行的内容与哈希来源）

    分词规则版本、jieba 版本与停用词版本共同构成配置内容：任一升级
    都会改变配置哈希，触发新配置版本行。

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "keyword_config_version": KEYWORD_CONFIG_VERSION,
            "tokenizer": TOKENIZER_NAME,
            "tokenizer_mode": TOKENIZER_MODE,
            "tokenizer_version": jieba_version(),
            "stopwords_version": STOPWORDS_VERSION,
        }
    )
