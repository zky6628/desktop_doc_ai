# -*- coding: utf-8 -*-
"""MinerU 产物归一化测试：结构映射、回退路径、确定性哈希与配置版本"""
import json

import pytest

from app.domain.errors import CloudProtocolViolationError
from app.domain.parsing import BlockType
from app.infrastructure.mineru import normalize as normalize_module
from app.infrastructure.mineru import normalize_archive

VERSION_ID = "01900000-0000-7000-8000-000000000001"

# 结构化内容列表样例：覆盖标题层级/正文/表格/图片/公式/未知类型
SAMPLE_ITEMS = [
    {"type": "text", "text": "第一章", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "正文段落", "page_idx": 0},
    {"type": "text", "text": "第一节", "text_level": 2, "page_idx": 1},
    {
        "type": "table",
        "table_body": "<table><tr><td>甲</td><td>乙</td></tr></table>",
        "table_caption": ["表 1"],
        "table_footnote": ["注：示例"],
        "page_idx": 1,
    },
    {"type": "image", "img_path": "images/p1.jpg", "img_caption": ["图 1"], "page_idx": 2},
    {"type": "equation", "text": "$E=mc^2$", "page_idx": 2},
    {"type": "chart", "text": "未知类型条目"},
]


def _archive(tmp_path, *, full_md="# 标题\n\n正文\n", content_list=None):
    """构造解压产物目录，返回路径文本"""
    (tmp_path / "full.md").write_text(full_md, encoding="utf-8")
    if content_list is not None:
        (tmp_path / "doc_content_list.json").write_text(
            json.dumps(content_list, ensure_ascii=False), encoding="utf-8"
        )
    return str(tmp_path)


class TestContentListNormalization:
    """结构化内容列表的主路径映射"""

    @pytest.fixture()
    def document(self, tmp_path):
        return normalize_archive(
            _archive(tmp_path, content_list=SAMPLE_ITEMS), VERSION_ID
        )

    def test_heading_levels_build_section_path(self, document):
        headings = [b for b in document.blocks if b.block_type is BlockType.HEADING]
        assert [b.text for b in headings] == ["第一章", "第一节"]
        assert headings[0].section_path == "第一章"
        assert headings[1].section_path == "第一章 > 第一节"

    def test_page_numbers_from_page_index(self, document):
        # 供应方页索引 0 起，模型页码 1 起
        assert document.blocks[0].page_no == 1
        assert document.blocks[1].page_no == 1
        assert document.blocks[2].page_no == 2

    def test_layout_bbox_captured(self, tmp_path):
        item = {
            "type": "text",
            "text": "带坐标的段落",
            "bbox": [111, 75, 346, 94],
            "page_idx": 0,
        }
        document = normalize_archive(_archive(tmp_path, content_list=[item]), VERSION_ID)
        assert document.blocks[0].bbox_json == "[111,75,346,94]"

    def test_paragraph_inherits_section(self, document):
        paragraph = document.blocks[1]
        assert paragraph.block_type is BlockType.PARAGRAPH
        assert paragraph.section_path == "第一章"
        assert paragraph.text == "正文段落"

    def test_table_evidence_fields(self, document):
        table = document.blocks[3]
        assert table.block_type is BlockType.TABLE
        assert table.text is None
        evidence = table.table
        assert evidence is not None
        assert evidence.raw_html == "<table><tr><td>甲</td><td>乙</td></tr></table>"
        assert evidence.searchable_text == "甲 乙"
        assert json.loads(evidence.structure_json) == {
            "caption": ["表 1"],
            "footnote": ["注：示例"],
        }
        assert evidence.serialization_model == "local"

    def test_image_block_carries_caption_and_locator(self, document):
        image = document.blocks[4]
        assert image.block_type is BlockType.IMAGE_OCR
        assert image.text == "图 1"
        assert json.loads(image.source_locator_json) == {"img_path": "images/p1.jpg"}
        assert image.page_no == 3

    def test_equation_becomes_paragraph(self, document):
        equation = document.blocks[5]
        assert equation.block_type is BlockType.PARAGRAPH
        assert equation.text == "$E=mc^2$"

    def test_unknown_type_skipped_with_contiguous_ordinals(self, document):
        # 未知类型不产生块，顺序号保持连续
        assert [b.ordinal for b in document.blocks] == list(range(6))
        assert len(document.blocks) == 6

    def test_identity_and_hash(self, document):
        assert document.parser_provider == "mineru"
        assert document.parser_name == "mineru_content_list"
        assert document.parser_version == normalize_module.NORMALIZATION_CONFIG_VERSION
        assert document.structure_sha256


class TestFallbackAndValidation:
    """回退路径与结构性拒绝"""

    def test_missing_content_list_falls_back_to_full_md(self, tmp_path):
        document = normalize_archive(_archive(tmp_path), VERSION_ID)
        assert document.parser_name == "mineru_full_md"
        # full.md 的 ATX 标题构建章节路径（无页码概念）
        assert document.blocks[0].block_type is BlockType.HEADING
        assert document.blocks[0].text == "标题"
        assert document.blocks[0].page_no is None
        assert document.blocks[1].section_path == "标题"

    def test_missing_full_md_rejected(self, tmp_path):
        (tmp_path / "doc_content_list.json").write_text("[]", encoding="utf-8")
        with pytest.raises(CloudProtocolViolationError):
            normalize_archive(str(tmp_path), VERSION_ID)

    def test_invalid_content_list_json_rejected(self, tmp_path):
        (tmp_path / "full.md").write_text("# 标题", encoding="utf-8")
        (tmp_path / "doc_content_list.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(CloudProtocolViolationError):
            normalize_archive(str(tmp_path), VERSION_ID)

    def test_non_array_root_rejected(self, tmp_path):
        (tmp_path / "full.md").write_text("# 标题", encoding="utf-8")
        (tmp_path / "doc_content_list.json").write_text('{"a": 1}', encoding="utf-8")
        with pytest.raises(CloudProtocolViolationError):
            normalize_archive(str(tmp_path), VERSION_ID)

    def test_non_object_item_rejected(self, tmp_path):
        (tmp_path / "full.md").write_text("# 标题", encoding="utf-8")
        (tmp_path / "doc_content_list.json").write_text('["junk"]', encoding="utf-8")
        with pytest.raises(CloudProtocolViolationError):
            normalize_archive(str(tmp_path), VERSION_ID)

    def test_empty_content_list_yields_empty_document(self, tmp_path):
        document = normalize_archive(_archive(tmp_path, content_list=[]), VERSION_ID)
        assert document.blocks == ()

    def test_error_messages_carry_no_paths(self, tmp_path):
        with pytest.raises(CloudProtocolViolationError) as exc_info:
            normalize_archive(str(tmp_path), VERSION_ID)
        assert str(tmp_path) not in str(exc_info.value)


class TestDeterminism:
    """确定性合同：同目录重复归一化输出逐字段一致"""

    def test_repeated_normalization_identical(self, tmp_path):
        archive_dir = _archive(tmp_path, content_list=SAMPLE_ITEMS)
        first = normalize_archive(archive_dir, VERSION_ID)
        second = normalize_archive(archive_dir, VERSION_ID)
        assert first == second
        assert first.structure_sha256 == second.structure_sha256

    def test_config_version_scopes_structure_hash(self, tmp_path, monkeypatch):
        archive_dir = _archive(tmp_path, content_list=SAMPLE_ITEMS)
        first = normalize_archive(archive_dir, VERSION_ID)
        monkeypatch.setattr(normalize_module, "NORMALIZATION_CONFIG_VERSION", "2")
        second = normalize_archive(archive_dir, VERSION_ID)
        assert first.parser_version == "1"
        assert second.parser_version == "2"
        assert first.structure_sha256 != second.structure_sha256
