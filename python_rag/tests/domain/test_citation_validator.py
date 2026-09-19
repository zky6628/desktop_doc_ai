# -*- coding: utf-8 -*-
"""引用校验测试：空白规范化、事实快照来源与拒绝路径"""
import json

from app.domain.citation import (
    REJECT_QUOTE_MISMATCH,
    REJECT_UNKNOWN_REFERENCE,
    STATE_REJECTED,
    STATE_VALIDATED,
    CitationDraft,
    build_citation_record,
    normalize_whitespace,
    validate_citations,
)
from app.domain.context import ContextBlock


def _block(reference_id: str, content: str) -> ContextBlock:
    """构造上下文块（文档事实为服务端事实）"""
    return ContextBlock(
        reference_id=reference_id,
        chunk_id=f"chunk-{reference_id}",
        document_id="doc-1",
        document_version_id="dv-1",
        file_name="员工手册.pdf",
        version_no=2,
        page_no=12,
        section_path="休假制度/年假",
        header=f"[{reference_id}] 员工手册.pdf / v2 / p12 / 休假制度/年假",
        content=content,
        block_ids=("block-a", "block-b"),
        expansion_reason="direct",
    )


def test_normalize_whitespace_collapses_runs():
    """空白规范化：连续空白折叠为单空格并去首尾"""
    assert normalize_whitespace("  员工  累计\n工作\t满一年  ") == "员工 累计 工作 满一年"


def test_validated_quote_with_whitespace_difference():
    """引文与块内容仅空白差异时通过校验"""
    block = _block("S1", "员工累计工作满一年后，可 享受带薪年假。")

    validations = validate_citations(
        [CitationDraft(reference_id="S1", quoted_text="员工累计工作满一年后，可 享受带薪年假。 ")],
        [block],
    )

    assert validations[0].state == STATE_VALIDATED
    assert validations[0].reason is None
    assert validations[0].block is block


def test_rejected_quote_mismatch_keeps_block_fact():
    """引文不在块内容中：拒绝且记录原因，但编号有效的块事实保留"""
    block = _block("S1", "员工累计工作满一年后，可享受带薪年假。")

    validations = validate_citations(
        [CitationDraft(reference_id="S1", quoted_text="合同期内随时可以离职")],
        [block],
    )

    assert validations[0].state == STATE_REJECTED
    assert validations[0].reason == REJECT_QUOTE_MISMATCH
    assert validations[0].block is block


def test_rejected_unknown_reference_has_no_block():
    """编号不存在于允许集合：拒绝且文档事实不可知"""
    validations = validate_citations(
        [CitationDraft(reference_id="S9", quoted_text="任意引文")],
        [_block("S1", "内容")],
    )

    assert validations[0].state == STATE_REJECTED
    assert validations[0].reason == REJECT_UNKNOWN_REFERENCE
    assert validations[0].block is None


def test_empty_quote_is_marker_only_citation():
    """空引文视为仅标注编号的引用：通过且快照记空文本"""
    validations = validate_citations(
        [CitationDraft(reference_id="S1")], [_block("S1", "内容")]
    )

    assert validations[0].state == STATE_VALIDATED
    assert validations[0].quoted_text == ""


def test_build_record_snapshots_block_facts_and_scores():
    """快照行事实全部取自上下文块，四类分数与来源定位一并定格"""
    block = _block("S1", "员工累计工作满一年后，可享受带薪年假。")
    validation = validate_citations(
        [CitationDraft(reference_id="S1", quoted_text="可享受带薪年假")], [block]
    )[0]

    record = build_citation_record(
        validation,
        assistant_message_id="msg-1",
        citation_order=1,
        knowledge_base_id="kb-1",
        vector_score=0.81,
        keyword_score=7.42,
        fusion_score=0.031,
        rerank_score=0.92,
    )

    assert record.citation_id is None
    assert record.assistant_message_id == "msg-1"
    assert record.citation_order == 1
    assert record.chunk_id == "chunk-S1"
    assert record.knowledge_base_id_snapshot == "kb-1"
    assert record.document_id_snapshot == "doc-1"
    assert record.document_version_id_snapshot == "dv-1"
    assert record.file_name_snapshot == "员工手册.pdf"
    assert record.version_no_snapshot == 2
    assert record.quoted_text_snapshot == "可享受带薪年假"
    assert record.page_no == 12
    assert record.section_path == "休假制度/年假"
    assert json.loads(record.source_locator_json) == {"block_ids": ["block-a", "block-b"]}
    assert record.validation_state == STATE_VALIDATED
    assert (record.vector_score, record.keyword_score) == (0.81, 7.42)
    assert (record.fusion_score, record.rerank_score) == (0.031, 0.92)


def test_build_record_for_unknown_reference_blanks_document_facts():
    """编号未知的拒绝快照：文档事实为空，知识库与拒绝状态保留"""
    validation = validate_citations(
        [CitationDraft(reference_id="S9", quoted_text="编造的引文")],
        [_block("S1", "内容")],
    )[0]

    record = build_citation_record(
        validation,
        assistant_message_id="msg-1",
        citation_order=2,
        knowledge_base_id="kb-1",
    )

    assert record.chunk_id is None
    assert record.document_id_snapshot is None
    assert record.file_name_snapshot is None
    assert record.version_no_snapshot is None
    assert record.quoted_text_snapshot == "编造的引文"
    assert record.validation_state == STATE_REJECTED
    assert record.knowledge_base_id_snapshot == "kb-1"
