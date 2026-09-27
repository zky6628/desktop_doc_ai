# -*- coding: utf-8 -*-
"""生成适配器包：OpenAI 兼容端点的流式生成网关与装配工厂"""
from app.domain.ports import GenerationGateway
from app.infrastructure.model_settings import ModelSettings

from .openai_compatible_gateway import OpenAICompatibleGenerationGateway


def build_generation_gateway(
    settings: ModelSettings,
) -> GenerationGateway | None:
    """按已解析的模型设置构造生成网关

    缺少可用凭据时返回 None：调用方据此以"未配置"状态运行，
    而不是在每次查询时才暴露认证失败。

    :param settings: 装配期解析的模型设置
    :return: 生成网关；凭据缺失时为 None
    """
    if not settings.generation_available:
        return None
    return OpenAICompatibleGenerationGateway(
        api_key=settings.generation_api_key,
        model=settings.generation_model,
        base_url=settings.generation_base_url,
        enable_thinking=settings.generation_enable_thinking,
    )


__all__ = ["OpenAICompatibleGenerationGateway", "build_generation_gateway"]