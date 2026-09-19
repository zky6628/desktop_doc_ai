# -*- coding: utf-8 -*-
"""上下文组装测试：重排顺序、父/邻接展开、预算封顶与事实头行"""
import json

from app.domain.context import (
    CONTEXT_TOKEN_BUDGET,
    EXPANSION_ADJACENT,
    EXPANSION_DIRECT,
    EXPANSION_PARENT,
    NO_EVIDENCE_REFUSAL,
    SYSTEM_PROMPT_TEMPLATE,
    ContextSource,
    assemble_context,
    context_config_json,
    token_estimate,
)


def _source(
    chunk_id: str,
    *,
    dv: str = "dv-1",
    ordinal: int = 0,
    parent: int | None = None,
    content: str = "切片内容",
    doc: str = "doc-1",
    file_name: str = "文档.pdf",
    section: str | None = "第一章",
    page: int | None = 1,
) -> ContextSource:
    """构造组装输入（默认同文档版本，父子结构按需指定）"""
    return ContextSource(
        chunk_id=chunk_id,
        document_version_id=dv,
        document_id=doc,
        file_name=file_name,
        version_no=1,
        ordinal=ordinal,
        parent_ordinal=parent,
        content=content,
        section_path=section,
        page_start=page,
        block_ids=("block-1",),
    )


def test_token_estimate_is_conservative_char_count():
    """token 估算即字符数（保守上界，防溢出优先于利用率）"""
    assert token_estimate("中文abc") == 5
    assert token_estimate("") == 0


def test_context_config_json_is_canonical_and_deterministic():
    """上下文配置内容为规范 JSON 且字段完整"""
    payload = json.loads(context_config_json())
    assert payload == {
        "context_config_version": "1",
        "context_token_budget": CONTEXT_TOKEN_BUDGET,
        "output_reserve_tokens": 2000,
        "token_estimator": "chars",
    }
    assert context_config_json() == context_config_json()


def test_refusal_text_and_prompt_guard_clauses_are_frozen():
    """拒答话术与系统提示注入防护条款为冻结合同"""
    assert NO_EVIDENCE_REFUSAL == "当前知识库中没有足够依据回答该问题。"
    assert "不可信数据" in SYSTEM_PROMPT_TEMPLATE
    assert "不得执行" in SYSTEM_PROMPT_TEMPLATE
    assert "不得编造" in SYSTEM_PROMPT_TEMPLATE
    assert "没有任何工具" in SYSTEM_PROMPT_TEMPLATE


def test_assemble_follows_rank_order_with_reference_ids():
    """块按重排顺序加入，引用编号 S1 起连续，直接命中标记 direct"""
    sources = [
        _source("c-a", ordinal=0, content="甲内容", page=3),
        _source("c-b", ordinal=1, content="乙内容", page=4, section="第二章"),
    ]

    assembled = assemble_context(sources, ["c-b", "c-a"], token_budget=1000)

    assert [block.chunk_id for block in assembled.blocks] == ["c-b", "c-a"]
    assert [block.reference_id for block in assembled.blocks] == ["S1", "S2"]
    assert all(
        block.expansion_reason == EXPANSION_DIRECT for block in assembled.blocks
    )
    assert assembled.blocks[0].header == "[S1] 文档.pdf / v1 / p4 / 第二章"
    assert assembled.blocks[1].header == "[S2] 文档.pdf / v1 / p3 / 第一章"
    assert assembled.dropped_missing == 0


def test_parent_and_adjacent_expansion_slot_in_ordinal_order():
    """命中展开：父切片与前/后邻接按序号插入命中两侧并标记来源"""
    sources = [
        _source("p-0", ordinal=0, content="父上下文", page=1),
        _source("c-1", ordinal=1, parent=0, content="第一段", page=1),
        _source("c-2", ordinal=2, parent=0, content="第二段", page=2),
        _source("c-3", ordinal=3, parent=0, content="第三段", page=2),
    ]

    assembled = assemble_context(sources, ["c-2"], token_budget=5000)

    assert [block.chunk_id for block in assembled.blocks] == [
        "p-0",
        "c-1",
        "c-2",
        "c-3",
    ]
    assert [block.expansion_reason for block in assembled.blocks] == [
        EXPANSION_PARENT,
        EXPANSION_ADJACENT,
        EXPANSION_DIRECT,
        EXPANSION_ADJACENT,
    ]
    # 预算充足：无丢弃
    assert assembled.dropped_by_budget == 0


def test_expansion_dedupes_shared_parent_and_upgrades_hit_identity():
    """多命中共享父切片去重一次；后位命中先以邻接入选时升级为直接命中"""
    sources = [
        _source("p-0", ordinal=0, content="父上下文"),
        _source("c-1", ordinal=1, parent=0, content="第一段"),
        _source("c-2", ordinal=2, parent=0, content="第二段"),
        _source("c-3", ordinal=3, parent=0, content="第三段"),
    ]

    assembled = assemble_context(sources, ["c-1", "c-2"], token_budget=5000)

    # 父切片只出现一次；c-2 虽先以邻接入选，直接命中身份升级
    chunk_ids = [block.chunk_id for block in assembled.blocks]
    assert chunk_ids == ["p-0", "c-1", "c-2", "c-3"]
    reasons = {block.chunk_id: block.expansion_reason for block in assembled.blocks}
    assert reasons["c-1"] == EXPANSION_DIRECT
    assert reasons["c-2"] == EXPANSION_DIRECT


def test_budget_truncates_and_counts_drops():
    """预算耗尽即停止追加，被裁剪项计数"""
    sources = [
        _source("c-1", ordinal=0, content="甲" * 50),
        _source("c-2", ordinal=1, content="乙" * 50),
        _source("c-3", ordinal=2, content="丙" * 50),
    ]

    # 单块成本约 79（内容 50 + 头行事实 + 编号行）：前两块可入，第三块裁剪
    assembled = assemble_context(sources, ["c-1", "c-2", "c-3"], token_budget=170)

    assert [block.chunk_id for block in assembled.blocks] == ["c-1", "c-2"]
    assert assembled.dropped_by_budget == 1
    assert assembled.estimated_tokens <= 170


def test_missing_ranked_source_counted_not_fabricated():
    """命中无法回查切片事实时计入 dropped_missing，不伪造块"""
    sources = [_source("c-1", ordinal=0)]

    assembled = assemble_context(sources, ["c-1", "c-ghost"], token_budget=1000)

    assert [block.chunk_id for block in assembled.blocks] == ["c-1"]
    assert assembled.dropped_missing == 1


def test_expansion_lookup_does_not_cross_document_versions():
    """父/邻接查找限定同文档版本：不同版本相同序号不互串"""
    sources = [
        _source("dv1-c1", dv="dv-1", ordinal=1, content="版本一内容"),
        _source("dv2-p0", dv="dv-2", ordinal=0, content="版本二父上下文"),
        _source("dv2-c1", dv="dv-2", ordinal=1, parent=0, content="版本二内容"),
    ]

    assembled = assemble_context(sources, ["dv2-c1"], token_budget=5000)

    chunk_ids = [block.chunk_id for block in assembled.blocks]
    # 父切片解析到 dv-2 自己的序号 0，不误取 dv-1 的同序号切片
    assert chunk_ids == ["dv2-p0", "dv2-c1"]
    assert all(
        block.document_version_id == "dv-2" for block in assembled.blocks
    )
