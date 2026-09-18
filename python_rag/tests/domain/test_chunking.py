# -*- coding: utf-8 -*-
"""层次切片纯函数测试：确定性、父子结构、表格参与与预算边界"""
from app.domain.chunking import (
    CHILD_CHUNK_CHARS,
    PARENT_CHUNK_CHARS,
    StoredBlock,
    chunk_blocks,
    chunking_config_json,
)
from app.domain.parsing import BlockType, TableEvidence, make_block

# 统一假块主键：定位关系只要求块有数据库主键，内容语义无关
_BLOCK_ID = "01900000-0000-7000-8000-0000000000ff"


def _stored(
    block_type: BlockType,
    ordinal: int,
    *,
    text: str | None = None,
    section_path: str | None = None,
    page_no: int | None = None,
    block_id: str = _BLOCK_ID,
    table: TableEvidence | None = None,
) -> StoredBlock:
    """构造切片输入的已落库块"""
    return StoredBlock(
        id=block_id,
        block=make_block(
            block_type,
            ordinal,
            text=text,
            section_path=section_path,
            page_no=page_no,
            table=table,
        ),
    )


def test_deterministic_output_for_same_input():
    """相同块序列两次切片逐字段一致（含序号、哈希与定位关系）"""
    blocks = [
        _stored(BlockType.HEADING, 0, text="第一章", section_path="第一章"),
        _stored(BlockType.PARAGRAPH, 1, text="正文内容", section_path="第一章"),
    ]

    first = chunk_blocks(blocks)
    second = chunk_blocks(list(blocks))

    assert first == second


def test_parent_child_structure_and_ordinal_sequence():
    """父子两级结构：父先于子、序号连续、子引用父序号"""
    blocks = [
        _stored(BlockType.HEADING, 0, text="标题", section_path="标题"),
        _stored(BlockType.PARAGRAPH, 1, text="段落一", section_path="标题"),
        _stored(BlockType.PARAGRAPH, 2, text="段落二", section_path="标题"),
    ]

    chunks = chunk_blocks(blocks)

    # 小文档聚合为单父单子：父保存完整上下文，子为召回单元
    assert len(chunks) == 2
    parent, child = chunks
    assert parent.parent_ordinal is None
    assert child.parent_ordinal == parent.ordinal
    assert [c.ordinal for c in chunks] == [0, 1]
    assert parent.block_ids == tuple(block.id for block in blocks)
    assert child.block_ids == parent.block_ids
    # 父内容与子内容在小文档下一致（同一批素材）
    assert parent.content == "标题\n\n段落一\n\n段落二"
    assert child.content == parent.content


def test_table_searchable_text_becomes_chunk_content():
    """表格块以可检索文本参与切片，定位关系指向原始表格块"""
    table = TableEvidence(
        raw_html="<table><tr><td>甲</td></tr></table>",
        raw_markdown="| 甲 |",
        structure_json="{}",
        searchable_text="表头甲 表头乙 数据甲 数据乙",
        serialization_model="local",
        serialization_version="1",
    )
    blocks = [
        _stored(BlockType.HEADING, 0, text="表", section_path="表"),
        _stored(BlockType.TABLE, 1, section_path="表", table=table),
    ]

    chunks = chunk_blocks(blocks)

    child = chunks[-1]
    assert "表头甲 表头乙 数据甲 数据乙" in child.content
    assert "甲</td>" not in child.content
    assert blocks[1].id in child.block_ids


def test_section_runs_isolated_even_when_path_reappears():
    """章节路径按连续段分组：路径中断重现后不与旧段合并"""
    blocks = [
        _stored(BlockType.HEADING, 0, text="甲", section_path="甲"),
        _stored(BlockType.PARAGRAPH, 1, text="甲段", section_path="甲"),
        _stored(BlockType.HEADING, 2, text="乙", section_path="乙"),
        _stored(BlockType.HEADING, 3, text="复现甲", section_path="甲"),
    ]

    chunks = chunk_blocks(blocks)

    parents = [chunk for chunk in chunks if chunk.parent_ordinal is None]
    # 三个连续段各产生一个父切片
    assert len(parents) == 3
    assert [parent.section_path for parent in parents] == ["甲", "乙", "甲"]


def test_budget_creates_multiple_parents_and_children():
    """预算切分：超预算聚合新切片，单素材超预算独占不硬切"""
    long_text = "字" * (PARENT_CHUNK_CHARS + 1)
    blocks = [
        _stored(BlockType.PARAGRAPH, 0, text=long_text, page_no=1),
        _stored(BlockType.PARAGRAPH, 1, text="段落", page_no=2),
    ]

    chunks = chunk_blocks(blocks)

    parents = [chunk for chunk in chunks if chunk.parent_ordinal is None]
    children = [chunk for chunk in chunks if chunk.parent_ordinal is not None]
    # 长段落独占父切片，短段落开新父
    assert len(parents) == 2
    assert parents[0].content == long_text
    # 超子预算的素材独占子切片
    assert len(children) == 2
    assert children[0].content == long_text
    # 页码范围取素材并集
    assert parents[1].page_start == 2
    assert parents[1].page_end == 2


def test_empty_and_whitespace_blocks_skipped():
    """无文本块不产生素材；全空输入产出空切片序列"""
    blocks = [
        _stored(BlockType.PARAGRAPH, 0, text=None),
        _stored(BlockType.PARAGRAPH, 1, text="   \n "),
    ]

    assert chunk_blocks(blocks) == ()
    assert chunk_blocks([]) == ()


def test_config_json_is_canonical_and_binds_parameters():
    """配置 JSON 确定性产出并绑定参数集（键排序、含版本常量）"""
    first = chunking_config_json()
    second = chunking_config_json()

    assert first == second
    assert '"chunking_config_version":"1"' in first
    assert f'"parent_chunk_chars":{PARENT_CHUNK_CHARS}' in first
    assert f'"child_chunk_chars":{CHILD_CHUNK_CHARS}' in first
