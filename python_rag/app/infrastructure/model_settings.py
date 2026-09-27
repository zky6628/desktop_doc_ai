# -*- coding: utf-8 -*-
"""模型身份与调用参数的装配期单一事实源

网关、编排器与运维端点都从这里取值，避免模型名在各处内联后出现口径
不一致（曾经网关用一种模型、配置行记录另一种模型）。解析结果是一次
性的不可变快照：进程启动时读一次环境变量，运行期不再变化。

三类角色的可切换范围不同：生成与重排是逐请求调用，换模型只影响新
请求；文本向量化与向量索引形态绑定（模型与维度写入索引配置行，并
参与向量维度校验与嵌入缓存键），运行期更换会让既有索引与缓存失去
可比性，因此仍为固定常量，更换属于重建索引级别的变更。
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.domain import embedding, generation, rerank

# 生成供应商取值
PROVIDER_DASHSCOPE = "dashscope"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

# 本地兼容端点缺省地址（Ollama 的 OpenAI 兼容入口）
_LOCAL_DEFAULT_BASE_URL = "http://localhost:11434/v1"

# 兼容端点通常不校验密钥，缺省占位值只用于满足 SDK 参数要求
_LOCAL_PLACEHOLDER_KEY = "local"

# 云端兼容端点地址（与生成适配器缺省值同源）
_CLOUD_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_VALID_PROVIDERS = frozenset({PROVIDER_DASHSCOPE, PROVIDER_OPENAI_COMPATIBLE})


def _env_text(env: Mapping[str, str], key: str, default: str) -> str:
    """读取文本型环境变量：未设置或仅空白一律按未设置处理

    空白值若被原样采纳会产出空模型名或空地址，错误要到调用供应方时
    才以难以定位的形式暴露，因此在装配期即回退缺省值
    """
    return (env.get(key) or "").strip() or default


@dataclass(frozen=True)
class ModelSettings:
    """已解析的模型身份与调用参数

    dashscope_api_key 为云端凭据（向量化与重排始终由云端承载），
    generation_api_key 为所选生成供应商实际使用的凭据；
    generation_enable_thinking 为 None 表示端点不识别思考开关，
    请求体不携带该字段。
    """

    embedding_model: str
    embedding_dimensions: int
    rerank_model: str
    dashscope_api_key: str
    generation_provider: str
    generation_model: str
    generation_base_url: str
    generation_api_key: str
    generation_enable_thinking: bool | None

    @classmethod
    def defaults(cls) -> "ModelSettings":
        """域常量口径（无环境依赖）：测试与未配置环境的等价基线"""
        return cls(
            embedding_model=embedding.EMBEDDING_MODEL,
            embedding_dimensions=embedding.EMBEDDING_DIMENSIONS,
            rerank_model=rerank.RERANK_MODEL,
            dashscope_api_key="",
            generation_provider=PROVIDER_DASHSCOPE,
            generation_model=generation.GENERATION_MODEL,
            generation_base_url=_CLOUD_COMPAT_BASE_URL,
            generation_api_key="",
            generation_enable_thinking=generation.GENERATION_ENABLE_THINKING,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ModelSettings":
        """按环境变量解析模型设置，缺省值与域常量完全一致

        :param environ: 环境变量映射（缺省取进程环境）
        :raises ValueError: 供应商取值不在支持范围内
        """
        env = os.environ if environ is None else environ
        provider = (env.get("WORKBENCH_GENERATION_PROVIDER") or "").strip().lower()
        if not provider:
            provider = PROVIDER_DASHSCOPE
        if provider not in _VALID_PROVIDERS:
            raise ValueError(
                "不支持的生成供应商: "
                f"{provider}（可选 {PROVIDER_DASHSCOPE} / {PROVIDER_OPENAI_COMPATIBLE}）"
            )

        dashscope_api_key = (env.get("DASHSCOPE_API_KEY") or "").strip()
        if provider == PROVIDER_OPENAI_COMPATIBLE:
            default_base_url = _LOCAL_DEFAULT_BASE_URL
            default_api_key = _LOCAL_PLACEHOLDER_KEY
            # 本地推理服务不识别思考开关，省略该字段避免被判无效请求
            default_thinking: bool | None = None
        else:
            default_base_url = _CLOUD_COMPAT_BASE_URL
            default_api_key = dashscope_api_key
            default_thinking = generation.GENERATION_ENABLE_THINKING

        return cls(
            embedding_model=embedding.EMBEDDING_MODEL,
            embedding_dimensions=embedding.EMBEDDING_DIMENSIONS,
            rerank_model=_env_text(env, "WORKBENCH_RERANK_MODEL", rerank.RERANK_MODEL),
            dashscope_api_key=dashscope_api_key,
            generation_provider=provider,
            generation_model=_env_text(
                env, "WORKBENCH_GENERATION_MODEL", generation.GENERATION_MODEL
            ),
            generation_base_url=_env_text(
                env, "WORKBENCH_GENERATION_BASE_URL", default_base_url
            ),
            generation_api_key=_env_text(
                env, "WORKBENCH_GENERATION_API_KEY", default_api_key
            ),
            generation_enable_thinking=default_thinking,
        )

    @property
    def generation_available(self) -> bool:
        """生成是否可用：兼容端点带占位密钥同样可用"""
        return bool(self.generation_api_key)

    @property
    def rerank_available(self) -> bool:
        """重排是否可用：重排仍由云端供应方承载，需云端凭据"""
        return bool(self.dashscope_api_key)