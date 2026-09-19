# -*- coding: utf-8 -*-
"""Embedding 配置常量与配置行内容

模型、维度与批量参数在此统一定义：配置行进流水线配置表（Index 构建侧
的单一事实源），网关与后续向量写入都从这里取值，禁止各自内联；模型
绑定档案表留给查询侧使用，两侧口径不混用。参数语义变化必须递增配置
版本，使配置行与既有向量可辨别。
"""
from app.domain.parsing import canonical_json

# Embedding 配置版本：模型/维度/批量语义变化时必须递增
EMBEDDING_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
EMBEDDING_CONFIG_TYPE = "embedding"

# 文档侧向量模型与维度：维度决定向量索引形态，变更即新配置版本
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMENSIONS = 1024

# 单次请求文本数上限（供应方批量接口约束）
EMBEDDING_BATCH_SIZE = 10

# 文本侧别：文档构建固定 document；查询侧检索固定 query，两侧口径
# 不混用（查询侧由查询嵌入端口实现固定，与实例配置无关）
EMBEDDING_TEXT_TYPE = "document"
EMBEDDING_QUERY_TEXT_TYPE = "query"


def embedding_config_json() -> str:
    """产出当前 Embedding 参数集的规范 JSON（配置行的内容与哈希来源）

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "embedding_config_version": EMBEDDING_CONFIG_VERSION,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMENSIONS,
            "batch_size": EMBEDDING_BATCH_SIZE,
            "text_type": EMBEDDING_TEXT_TYPE,
        }
    )
