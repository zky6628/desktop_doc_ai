# -*- coding: utf-8 -*-
"""解析领域模型单元测试：哈希确定性与结构哈希复算"""
from dataclasses import FrozenInstanceError

import pytest

from app.domain.parsing import (
    BlockType,
    ParsedDocument,
    TableEvidence,
    make_block,
)


def _sample_document(
    document_version_id: str = "01900000-0000-7000-8000-000000000001",
    parser_version: str = "1.0.0",
) -> ParsedDocument:
    """构造固定内容的样例解析产物"""
    return ParsedDocument(
        document_version_id=document_version_id,
        parser_provider="local",
        parser_name="unit",
        parser_version=parser_version,
        blocks=(
            make_block(BlockType.HEADING, 0, text="标题"),
            make_block(BlockType.PARAGRAPH, 1, text="正文"),
        ),
    )


class TestBlockContentHash:
    """块内容哈希的确定性合同"""

    def test_same_fields_produce_identical_hash(self):
        first = make_block(BlockType.PARAGRAPH, 0, page_no=3, text="内容")
        second = make_block(BlockType.PARAGRAPH, 0, page_no=3, text="内容")
        assert first.content_hash == second.content_hash

    def test_text_change_changes_hash(self):
        first = make_block(BlockType.PARAGRAPH, 0, text="内容甲")
        second = make_block(BlockType.PARAGRAPH, 0, text="内容乙")
        assert first.content_hash != second.content_hash

    def test_ordinal_not_part_of_content_hash(self):
        # 内容相同、位置不同的块哈希一致：位置语义由结构哈希承担
        first = make_block(BlockType.PARAGRAPH, 0, text="内容")
        second = make_block(BlockType.PARAGRAPH, 7, text="内容")
        assert first.content_hash == second.content_hash

    def test_table_evidence_participates_in_hash(self):
        evidence = TableEvidence(
            raw_html="<table><tr><td>甲</td></tr></table>",
            raw_markdown="| 甲 |\n| --- |",
            structure_json='{"rows":[["甲"]]}',
            searchable_text="甲",
            serialization_model="local",
            serialization_version="1",
        )
        with_table = make_block(BlockType.TABLE, 0, table=evidence)
        modified = make_block(
            BlockType.TABLE,
            0,
            table=TableEvidence(
                raw_html=evidence.raw_html,
                raw_markdown=evidence.raw_markdown,
                structure_json=evidence.structure_json,
                searchable_text="甲 乙",
                serialization_model=evidence.serialization_model,
                serialization_version=evidence.serialization_version,
            ),
        )
        assert with_table.content_hash != modified.content_hash

    def test_hash_is_sha256_hex(self):
        block = make_block(BlockType.PARAGRAPH, 0, text="内容")
        assert len(block.content_hash) == 64
        int(block.content_hash, 16)


class TestStructureHash:
    """整篇结构哈希的复算合同"""

    def test_recomputation_is_stable(self):
        first = _sample_document().structure_sha256
        second = _sample_document().structure_sha256
        assert first == second

    def test_independent_of_document_version_id(self):
        first = _sample_document(
            document_version_id="01900000-0000-7000-8000-000000000001"
        )
        second = _sample_document(
            document_version_id="01900000-0000-7000-8000-000000000002"
        )
        assert first.structure_sha256 == second.structure_sha256

    def test_parser_version_scopes_structure_hash(self):
        first = _sample_document(parser_version="1.0.0")
        second = _sample_document(parser_version="2.0.0")
        assert first.structure_sha256 != second.structure_sha256

    def test_block_order_scopes_structure_hash(self):
        document = _sample_document()
        reordered = ParsedDocument(
            document_version_id=document.document_version_id,
            parser_provider=document.parser_provider,
            parser_name=document.parser_name,
            parser_version=document.parser_version,
            blocks=tuple(reversed(document.blocks)),
        )
        assert document.structure_sha256 != reordered.structure_sha256


class TestModelContract:
    """模型与内容表取值口径的一致性"""

    def test_block_type_values(self):
        # 取值与内容表 block_type 列注释口径一致，扩展需同步表定义
        assert {member.value for member in BlockType} == {
            "heading",
            "paragraph",
            "list",
            "table",
            "image_ocr",
            "header",
            "footer",
        }

    def test_scan_suspected_defaults_false(self):
        document = _sample_document()
        assert document.scan_suspected is False

    def test_parsed_document_is_immutable(self):
        document = _sample_document()
        with pytest.raises(FrozenInstanceError):
            document.parser_name = "other"  # type: ignore[misc]

    def test_block_is_immutable(self):
        block = make_block(BlockType.PARAGRAPH, 0, text="内容")
        with pytest.raises(FrozenInstanceError):
            block.text = "其他"  # type: ignore[misc]
