# -*- coding: utf-8 -*-
"""Rerank 配置常量与配置行内容

重排模型、输出数量与请求超时在此统一定义：配置行进流水线配置表
（查询链路的单一事实源），网关从这里取值。输入候选上限沿用检索
融合的冻结候选数（RRF 产出即重排输入），不另立常量。参数语义变化
必须递增配置版本，使配置行与既有查询可辨别。
"""
from dataclasses import dataclass

from app.domain import retrieval
from app.domain.parsing import canonical_json

# Rerank 配置版本：模型/输出数量/超时语义变化时必须递增
RERANK_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
RERANK_CONFIG_TYPE = "rerank"

# 重排模型与输出数量（候选 20 → 5 的冻结口径）
RERANK_MODEL = "qwen3-rerank"
RERANK_TOP_N = 5

# 单请求超时（秒）：供应方 SDK 默认 300 秒远超重排延迟预算
RERANK_TIMEOUT_SECONDS = 3


def rerank_config_json() -> str:
    """产出当前重排参数集的规范 JSON（配置行的内容与哈希来源）

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "rerank_config_version": RERANK_CONFIG_VERSION,
            "model": RERANK_MODEL,
            "top_n": RERANK_TOP_N,
            "max_input_candidates": retrieval.FUSED_TOP_K,
            "request_timeout_seconds": RERANK_TIMEOUT_SECONDS,
        }
    )


@dataclass(frozen=True)
class RerankHit:
    """重排命中：输入文档下标与相关性分数

    index 指向重排输入的文档下标（由链路映射回切片）；score 为
    供应方相关性原始分，越高越相关，不做归一化转换
    """

    index: int
    score: float
