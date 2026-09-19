# -*- coding: utf-8 -*-
"""生成配置常量、配置行内容与重排降级策略

生成模型与输出上限在此统一定义：配置行进流水线配置表（查询链路的
单一事实源），网关与编排从这里取值。重排降级策略为冻结口径：重排
瞬态重试一次仍失败时使用 RRF 前 5 继续生成并记录降级（认证/配额类
失败不降级，直接失败终态）。
"""
from app.domain.parsing import canonical_json

# 生成配置版本：模型/输出上限/超时语义变化时必须递增
GENERATION_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
GENERATION_CONFIG_TYPE = "generation"

# 生成模型与输出 token 上限（上下文预算的输出预留同源）
GENERATION_MODEL = "qwen-plus"
GENERATION_MAX_OUTPUT_TOKENS = 2000

# 单次流式请求超时（秒）：约束整条流的吞吐下限（约 16.7 token/秒），
# 病态慢生成被切断进失败终态
GENERATION_TIMEOUT_SECONDS = 120

# 重排瞬态失败进程内重试次数（重试耗尽即降级）
RERANK_INLINE_RETRIES = 1


def generation_config_json() -> str:
    """产出当前生成参数集的规范 JSON（配置行的内容与哈希来源）

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "generation_config_version": GENERATION_CONFIG_VERSION,
            "model": GENERATION_MODEL,
            "max_output_tokens": GENERATION_MAX_OUTPUT_TOKENS,
            "request_timeout_seconds": GENERATION_TIMEOUT_SECONDS,
            "rerank_degradation_policy": "rrf_top5",
            "rerank_inline_retries": RERANK_INLINE_RETRIES,
        }
    )
