# -*- coding: utf-8 -*-
"""解析路由策略测试：决策矩阵与解析后复核规则"""
import pytest

from app.domain.errors import UnsupportedFormatError
from app.domain.parser_routing import (
    PARSER_PREFERENCE_VALUES,
    ROUTER_CONFIG_VERSION,
    ParserPreference,
    decide_parser_route,
    requires_cloud_confirmation,
)

# 白名单内全部扩展名 × 全部偏好的决策矩阵期望：
# auto 按默认决策表，local/mineru 服从用户指定
_MATRIX_EXPECTATIONS = {
    ("txt", "auto"): "local",
    ("md", "auto"): "local",
    ("markdown", "auto"): "local",
    ("docx", "auto"): "local",
    ("pdf", "auto"): "local",
    ("txt", "local"): "local",
    ("docx", "local"): "local",
    ("pdf", "local"): "local",
    ("txt", "mineru"): "cloud",
    ("md", "mineru"): "cloud",
    ("markdown", "mineru"): "cloud",
    ("docx", "mineru"): "cloud",
    ("pdf", "mineru"): "cloud",
}


class TestPreRoute:
    """预路由决策矩阵"""

    @pytest.mark.parametrize(
        ("extension", "preference"), sorted(_MATRIX_EXPECTATIONS)
    )
    def test_decision_matrix(self, extension, preference):
        decision = decide_parser_route(extension, preference)
        assert decision.mode.value == _MATRIX_EXPECTATIONS[(extension, preference)]
        assert decision.reason
        assert decision.router_config_version == ROUTER_CONFIG_VERSION

    def test_auto_reason_records_default_basis(self):
        decision = decide_parser_route("pdf", ParserPreference.AUTO)
        assert "扫描件" in decision.reason

    def test_mineru_reason_records_user_choice(self):
        decision = decide_parser_route("txt", ParserPreference.MINERU)
        assert "用户" in decision.reason

    def test_unknown_extension_rejected(self):
        with pytest.raises(UnsupportedFormatError):
            decide_parser_route("exe", "auto")

    def test_preference_values_contract(self):
        # 用户可见取值与枚举保持一致
        assert set(PARSER_PREFERENCE_VALUES) == {"auto", "local", "mineru"}


class TestPostParseReview:
    """解析后复核：扫描件信号与用户偏好的组合规则"""

    def test_scan_signal_requires_confirmation(self):
        assert requires_cloud_confirmation(
            scan_suspected=True, preference=ParserPreference.AUTO
        )
        assert requires_cloud_confirmation(
            scan_suspected=True, preference=ParserPreference.LOCAL
        )

    def test_mineru_preference_already_cloud(self):
        assert not requires_cloud_confirmation(
            scan_suspected=True, preference=ParserPreference.MINERU
        )

    def test_no_signal_no_confirmation(self):
        assert not requires_cloud_confirmation(
            scan_suspected=False, preference=ParserPreference.AUTO
        )
