# -*- coding: utf-8 -*-
"""生成配置与提示组装/引用提取测试"""
import json

from app.domain import generation
from app.domain.citation import extract_citation_drafts
from app.domain.context import (
    SYSTEM_PROMPT_TEMPLATE,
    ContextBlock,
    build_prompt_messages,
)
from app.domain.generation import generation_config_json


def test_frozen_generation_contract():
    """生成模型/输出上限/降级策略为冻结合同"""
    assert generation.GENERATION_MODEL == "qwen3.8-max"
    assert generation.GENERATION_MAX_OUTPUT_TOKENS == 2000
    payload = json.loads(generation_config_json())
    assert payload["enable_thinking"] is False
    assert payload["rerank_degradation_policy"] == "rrf_top5"
    assert payload["rerank_inline_retries"] == 1
    assert generation_config_json() == generation_config_json()


def _block(reference_id: str, content: str) -> ContextBlock:
    return ContextBlock(
        reference_id=reference_id,
        chunk_id=f"chunk-{reference_id}",
        document_id="doc-1",
        document_version_id="dv-1",
        file_name="手册.pdf",
        version_no=1,
        page_no=1,
        section_path="年假",
        header=f"[{reference_id}] 手册.pdf / v1 / p1 / 年假",
        content=content,
        block_ids=(),
        expansion_reason="direct",
    )


def test_prompt_messages_separate_instructions_from_data():
    """指令与数据物理分离：system 仅规则，user 为定界数据加问题"""
    messages = build_prompt_messages(
        [_block("S1", "年假内容"), _block("S2", "其他内容")], "年假有几天？"
    )

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "年假内容" not in messages[0]["content"]
    assert messages[0]["content"] == SYSTEM_PROMPT_TEMPLATE
    user = messages[1]["content"]
    assert "以下是知识库检索到的文档内容" in user
    assert user.index("[S1] 手册.pdf") < user.index("年假内容")
    assert user.index("年假内容") < user.index("文档内容结束")
    assert user.endswith("年假有几天？")


def test_extract_citation_drafts_dedupes_in_order():
    """引用提取按首次出现排序去重，无标记返回空"""
    drafts = extract_citation_drafts("开头[S2]中[S1]间[S2]再[S10]尾[S1]")
    assert [draft.reference_id for draft in drafts] == ["S2", "S1", "S10"]
    assert all(draft.quoted_text == "" for draft in drafts)
    assert extract_citation_drafts("无标记回答") == []
