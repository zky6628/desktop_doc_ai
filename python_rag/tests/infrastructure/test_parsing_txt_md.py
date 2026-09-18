# -*- coding: utf-8 -*-
"""TXT/Markdown 解析器测试：编码探测、结构与确定性"""
import pytest

from app.domain.errors import EmptyContentError, TextEncodingError
from app.domain.parsing import BlockType
from app.infrastructure.parsing.txt_md_parser import TxtMarkdownParser

VERSION_ID = "01900000-0000-7000-8000-000000000001"


def _write_bytes(path, raw: bytes):
    path.write_bytes(raw)
    return str(path)


def _write_text(path, text: str, encoding: str = "utf-8"):
    # newline="" 关闭写入端换行翻译，保证样例字节与解析输入一致
    path.write_text(text, encoding=encoding, newline="")
    return str(path)


class TestPlainTextParsing:
    """纯文本模式的分段与编码行为"""

    def test_utf8_paragraph_grouping(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_text(tmp_path / "a.txt", "第一段第一行\n第一段第二行\n\n第二段\n")
        document = parser.parse(path, VERSION_ID)
        assert [block.block_type for block in document.blocks] == [
            BlockType.PARAGRAPH,
            BlockType.PARAGRAPH,
        ]
        assert document.blocks[0].text == "第一段第一行\n第一段第二行"
        assert document.blocks[1].text == "第二段"
        assert all(block.page_no is None for block in document.blocks)
        assert all(block.section_path is None for block in document.blocks)

    def test_crlf_normalized(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_text(tmp_path / "a.txt", "甲\r\n乙\r\n\r\n丙")
        document = parser.parse(path, VERSION_ID)
        assert [block.text for block in document.blocks] == ["甲\n乙", "丙"]

    def test_gbk_file_parsed(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_text(tmp_path / "a.txt", "中文内容第一段\n\n第二段", encoding="gbk")
        document = parser.parse(path, VERSION_ID)
        assert [block.text for block in document.blocks] == [
            "中文内容第一段",
            "第二段",
        ]

    def test_bom_stripped(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_bytes(tmp_path / "a.txt", b"\xef\xbb\xbfBOM content\n")
        document = parser.parse(path, VERSION_ID)
        assert document.blocks[0].text == "BOM content"

    def test_multibyte_across_chunk_boundary(self, tmp_path):
        # GBK 中文为双字节编码：构造大于解码块（64KB）的文件，
        # 让块边界必然落在多字节字符中间，验证增量解码器的跨块续读
        parser = TxtMarkdownParser(markdown_mode=False)
        text = "中文" * 40000
        path = _write_text(tmp_path / "a.txt", text, encoding="gbk")
        document = parser.parse(path, VERSION_ID)
        assert len(document.blocks) == 1
        assert document.blocks[0].text == text

    def test_invalid_encoding_rejected(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_bytes(tmp_path / "a.txt", b"\xff\xfe\x00\x01binary garbage")
        with pytest.raises(TextEncodingError):
            parser.parse(path, VERSION_ID)

    def test_invalid_tail_rejected_after_valid_sample(self, tmp_path):
        # 头部样本合法但尾部字节非法：文件中段的解码失败同样按编码错误拒绝
        parser = TxtMarkdownParser(markdown_mode=False)
        raw = b"a" * 70000 + b"\xff"
        path = _write_bytes(tmp_path / "a.txt", raw)
        with pytest.raises(TextEncodingError):
            parser.parse(path, VERSION_ID)

    def test_truncated_multibyte_tail_rejected(self, tmp_path):
        # 截断的多字节尾字节仅在解码器冲刷时暴露
        parser = TxtMarkdownParser(markdown_mode=False)
        raw = b"a" * 70000 + "中".encode()[:2]
        path = _write_bytes(tmp_path / "a.txt", raw)
        with pytest.raises(TextEncodingError):
            parser.parse(path, VERSION_ID)

    def test_empty_file_rejected(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_bytes(tmp_path / "a.txt", b"")
        with pytest.raises(EmptyContentError):
            parser.parse(path, VERSION_ID)

    def test_whitespace_only_rejected(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=False)
        path = _write_bytes(tmp_path / "a.txt", b"\n \n\t \n")
        with pytest.raises(EmptyContentError):
            parser.parse(path, VERSION_ID)


class TestMarkdownStructure:
    """Markdown 模式的标题/列表结构"""

    def test_heading_hierarchy_builds_section_path(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=True)
        content = "# 第一章\n\n导语内容\n\n## 第一节\n\n正文内容\n\n# 第二章\n\n另一段\n"
        path = _write_text(tmp_path / "a.md", content)
        document = parser.parse(path, VERSION_ID)
        types = [block.block_type for block in document.blocks]
        assert types == [
            BlockType.HEADING,
            BlockType.PARAGRAPH,
            BlockType.HEADING,
            BlockType.PARAGRAPH,
            BlockType.HEADING,
            BlockType.PARAGRAPH,
        ]
        assert document.blocks[0].section_path == "第一章"
        assert document.blocks[1].section_path == "第一章"
        assert document.blocks[2].section_path == "第一章 > 第一节"
        assert document.blocks[3].section_path == "第一章 > 第一节"
        assert document.blocks[4].section_path == "第二章"

    def test_consecutive_list_items_grouped(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=True)
        content = "- 甲\n- 乙\n\n1. 丙\n2. 丁\n"
        path = _write_text(tmp_path / "a.md", content)
        document = parser.parse(path, VERSION_ID)
        types = [block.block_type for block in document.blocks]
        assert types == [BlockType.LIST, BlockType.LIST]
        assert document.blocks[0].text == "甲\n乙"
        assert document.blocks[1].text == "丙\n丁"

    def test_list_under_section_path(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=True)
        content = "# 标题\n\n- 项一\n- 项二\n"
        path = _write_text(tmp_path / "a.md", content)
        document = parser.parse(path, VERSION_ID)
        assert document.blocks[1].block_type is BlockType.LIST
        assert document.blocks[1].section_path == "标题"

    def test_fence_content_not_treated_as_markup(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=True)
        content = "```md\n# 不是标题\n```\n"
        path = _write_text(tmp_path / "a.md", content)
        document = parser.parse(path, VERSION_ID)
        types = [block.block_type for block in document.blocks]
        assert types == [BlockType.PARAGRAPH]
        assert "# 不是标题" in document.blocks[0].text

    def test_ordinals_contiguous(self, tmp_path):
        parser = TxtMarkdownParser(markdown_mode=True)
        content = "# 甲\n\n段落\n\n- 项\n"
        path = _write_text(tmp_path / "a.md", content)
        document = parser.parse(path, VERSION_ID)
        assert [block.ordinal for block in document.blocks] == [0, 1, 2]


class TestDeterminism:
    """确定性合同：同文件重复解析输出逐字段一致"""

    @pytest.mark.parametrize("markdown_mode", [False, True], ids=["txt", "md"])
    def test_repeated_parse_identical(self, tmp_path, markdown_mode):
        parser = TxtMarkdownParser(markdown_mode=markdown_mode)
        content = "# 标题\n\n第一段\n第二行\n\n- 列表项\n\n尾段\n"
        path = _write_text(tmp_path / "a.md", content)
        first = parser.parse(path, VERSION_ID)
        second = parser.parse(path, VERSION_ID)
        assert first == second
        assert first.structure_sha256 == second.structure_sha256
