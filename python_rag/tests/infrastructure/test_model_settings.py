# -*- coding: utf-8 -*-
"""模型设置解析测试：缺省口径、供应商差异、覆盖与非法输入拒绝

解析结果决定网关装配参数与配置行登记内容，因此断言分三层：缺省值
必须与域常量逐项一致；供应商之间的差异（端点、密钥、思考开关）必须
明确；显式配置必须完整生效且非法取值不做兜底。
"""
import pytest

from app.domain import embedding, generation, rerank
from app.infrastructure.model_settings import (
    PROVIDER_DASHSCOPE,
    PROVIDER_OPENAI_COMPATIBLE,
    ModelSettings,
)

_LOCAL_ENDPOINT = "http://localhost:11434/v1"
_CLOUD_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class TestDefaults:
    """缺省口径：无环境依赖的域常量基线"""

    def test_defaults_match_domain_constants(self):
        settings = ModelSettings.defaults()

        assert settings.embedding_model == embedding.EMBEDDING_MODEL
        assert settings.embedding_dimensions == embedding.EMBEDDING_DIMENSIONS
        assert settings.rerank_model == rerank.RERANK_MODEL
        assert settings.generation_provider == PROVIDER_DASHSCOPE
        assert settings.generation_model == generation.GENERATION_MODEL
        assert settings.generation_base_url == _CLOUD_ENDPOINT
        assert settings.generation_enable_thinking is False

    def test_defaults_without_credentials_are_unavailable(self):
        # 无凭据时按"未配置"表达，而不是等到首次调用才暴露认证失败
        settings = ModelSettings.defaults()

        assert not settings.generation_available
        assert not settings.rerank_available

    def test_defaults_ignores_process_environment(self, monkeypatch):
        # defaults() 是与环境无关的等价基线，不受进程环境变量影响
        monkeypatch.setenv("WORKBENCH_GENERATION_MODEL", "环境里的模型")
        monkeypatch.setenv("WORKBENCH_GENERATION_PROVIDER", PROVIDER_OPENAI_COMPATIBLE)

        settings = ModelSettings.defaults()

        assert settings.generation_model == generation.GENERATION_MODEL
        assert settings.generation_provider == PROVIDER_DASHSCOPE

    def test_empty_environment_equals_defaults(self):
        # 未配置任何变量时，解析结果与域常量基线完全相同
        assert ModelSettings.from_env({}) == ModelSettings.defaults()


class TestCloudProvider:
    """云端供应商：兼容端点、思考开关与凭据来源"""

    def test_cloud_key_flows_to_generation_and_rerank(self):
        settings = ModelSettings.from_env({"DASHSCOPE_API_KEY": "sk-cloud"})

        assert settings.generation_provider == PROVIDER_DASHSCOPE
        assert settings.dashscope_api_key == "sk-cloud"
        assert settings.generation_api_key == "sk-cloud"
        assert settings.generation_base_url == _CLOUD_ENDPOINT
        assert settings.generation_enable_thinking is False
        assert settings.generation_available
        assert settings.rerank_available

    def test_missing_cloud_key_keeps_generation_unavailable(self):
        settings = ModelSettings.from_env({"WORKBENCH_GENERATION_MODEL": "any"})

        assert not settings.generation_available
        assert not settings.rerank_available

    def test_dedicated_generation_key_overrides_cloud_key(self):
        settings = ModelSettings.from_env(
            {
                "DASHSCOPE_API_KEY": "sk-cloud",
                "WORKBENCH_GENERATION_API_KEY": "sk-dedicated",
            }
        )

        # 生成用专用密钥；向量化与重排仍用云端密钥
        assert settings.generation_api_key == "sk-dedicated"
        assert settings.dashscope_api_key == "sk-cloud"


class TestLocalProvider:
    """本地兼容端点：缺省地址、占位密钥与思考开关省略"""

    def test_local_defaults_to_local_endpoint(self):
        settings = ModelSettings.from_env(
            {"WORKBENCH_GENERATION_PROVIDER": PROVIDER_OPENAI_COMPATIBLE}
        )

        assert settings.generation_provider == PROVIDER_OPENAI_COMPATIBLE
        assert settings.generation_base_url == _LOCAL_ENDPOINT
        assert settings.generation_model == generation.GENERATION_MODEL
        # 本地端点不校验密钥，占位值只用于满足 SDK 参数要求
        assert settings.generation_api_key == "local"
        assert settings.generation_available

    def test_local_omits_thinking_switch(self):
        # 端点不识别思考开关时省略该字段，避免被判为无效请求
        settings = ModelSettings.from_env(
            {"WORKBENCH_GENERATION_PROVIDER": PROVIDER_OPENAI_COMPATIBLE}
        )

        assert settings.generation_enable_thinking is None

    def test_local_provider_keeps_cloud_credentials_separate(self):
        settings = ModelSettings.from_env(
            {
                "WORKBENCH_GENERATION_PROVIDER": PROVIDER_OPENAI_COMPATIBLE,
                "DASHSCOPE_API_KEY": "sk-cloud",
            }
        )

        # 云端密钥不流入生成参数，但向量化与重排仍可用
        assert settings.generation_api_key == "local"
        assert settings.rerank_available

    def test_local_provider_without_cloud_key_disables_rerank(self):
        settings = ModelSettings.from_env(
            {"WORKBENCH_GENERATION_PROVIDER": PROVIDER_OPENAI_COMPATIBLE}
        )

        # 生成可离线，重排仍由云端承载：无云端凭据即不可用
        assert settings.generation_available
        assert not settings.rerank_available

    def test_provider_value_is_normalized(self):
        settings = ModelSettings.from_env(
            {"WORKBENCH_GENERATION_PROVIDER": "  OpenAI_Compatible "}
        )

        assert settings.generation_provider == PROVIDER_OPENAI_COMPATIBLE


class TestOverrides:
    """显式配置：模型名、端点地址与专用密钥完整生效"""

    def test_explicit_values_take_precedence(self):
        settings = ModelSettings.from_env(
            {
                "DASHSCOPE_API_KEY": "sk-cloud",
                "WORKBENCH_RERANK_MODEL": "custom-rerank",
                "WORKBENCH_GENERATION_MODEL": "custom-generation",
                "WORKBENCH_GENERATION_BASE_URL": "http://127.0.0.1:9000/v1",
            }
        )

        assert settings.rerank_model == "custom-rerank"
        assert settings.generation_model == "custom-generation"
        assert settings.generation_base_url == "http://127.0.0.1:9000/v1"

    def test_surrounding_whitespace_is_trimmed(self):
        settings = ModelSettings.from_env(
            {"WORKBENCH_GENERATION_MODEL": "  custom-generation  "}
        )

        assert settings.generation_model == "custom-generation"

    @pytest.mark.parametrize(
        "key",
        [
            "WORKBENCH_RERANK_MODEL",
            "WORKBENCH_GENERATION_MODEL",
            "WORKBENCH_GENERATION_BASE_URL",
        ],
    )
    def test_blank_values_fall_back_to_defaults(self, key):
        # 仅空白视为未设置：空白值若被采纳会产出空模型名或空地址
        settings = ModelSettings.from_env({key: "   "})

        assert settings == ModelSettings.defaults()

    def test_embedding_identity_is_not_environment_driven(self):
        # 嵌入模型与维度写入索引配置行并参与缓存键，不随环境变量漂移
        settings = ModelSettings.from_env(
            {"WORKBENCH_EMBEDDING_MODEL": "另一个模型"}
        )

        assert settings.embedding_model == embedding.EMBEDDING_MODEL
        assert settings.embedding_dimensions == embedding.EMBEDDING_DIMENSIONS

    def test_process_environment_used_when_not_injected(self, monkeypatch):
        monkeypatch.delenv("WORKBENCH_GENERATION_BASE_URL", raising=False)
        monkeypatch.setenv("WORKBENCH_GENERATION_PROVIDER", PROVIDER_OPENAI_COMPATIBLE)
        monkeypatch.setenv("WORKBENCH_GENERATION_MODEL", "qwen3:8b")

        settings = ModelSettings.from_env()

        assert settings.generation_provider == PROVIDER_OPENAI_COMPATIBLE
        assert settings.generation_model == "qwen3:8b"
        assert settings.generation_base_url == _LOCAL_ENDPOINT


class TestInvalidProvider:
    """非法供应商：启动期即拒绝，不静默回退到缺省供应商"""

    def test_unsupported_provider_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            ModelSettings.from_env({"WORKBENCH_GENERATION_PROVIDER": "openai"})

        message = str(exc_info.value)
        assert "openai" in message
        assert PROVIDER_DASHSCOPE in message
        assert PROVIDER_OPENAI_COMPATIBLE in message