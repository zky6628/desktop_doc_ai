# -*- coding: utf-8 -*-
"""Rerank 配置测试：冻结常量与配置行内容"""
import json

from app.domain import rerank
from app.domain.rerank import rerank_config_json


def test_frozen_rerank_contract():
    """重排模型/输出数量/输入上限为冻结合同：qwen3-rerank、20→5"""
    assert rerank.RERANK_MODEL == "qwen3-rerank"
    assert rerank.RERANK_TOP_N == 5
    payload = json.loads(rerank_config_json())
    assert payload["max_input_candidates"] == 20
    assert payload["request_timeout_seconds"] == 3


def test_rerank_config_json_is_canonical_and_deterministic():
    """重排配置内容为规范 JSON：重复产出逐字节一致且字段完整"""
    payload = json.loads(rerank_config_json())
    assert payload == {
        "rerank_config_version": rerank.RERANK_CONFIG_VERSION,
        "model": "qwen3-rerank",
        "top_n": 5,
        "max_input_candidates": 20,
        "request_timeout_seconds": 3,
    }
    assert rerank_config_json() == rerank_config_json()
