# -*- coding: utf-8 -*-
"""文本型 PDF 解析器测试：页码、文本层、扫描信号、损坏/加密与确定性"""
import io

import pytest
from pypdf import PdfReader, PdfWriter

from app.domain.errors import (
    DocumentCorruptedError,
    EmptyContentError,
    EncryptedDocumentError,
)
from app.domain.parsing import BlockType
from app.infrastructure.parsing.pdf_parser import PdfTextParser

VERSION_ID = "01900000-0000-7000-8000-000000000001"


def _build_pdf(page_line_groups: list[list[str]]) -> bytes:
    """程序化构造最小合法 PDF

    每页文本行各占一个文本对象（视觉上一行一 Tj），行间距固定；
    xref 偏移按实际字节位置计算，保证 pypdf 可完整解析。
    空行组表示该页没有文本层（空内容流）。

    :param page_line_groups: 每页的文本行列表
    :return: 合法 PDF 字节流
    """
    page_count = len(page_line_groups)
    # 对象布局：1 目录 / 2 页面树 / 3 字体，每页依次占页对象与内容对象
    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(page_count))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, lines in enumerate(page_line_groups):
        content = "".join(
            f"BT /F1 12 Tf 72 {720 - line_no * 20} Td ({line}) Tj ET\n"
            for line_no, line in enumerate(lines)
        )
        content_bytes = content.encode("ascii")
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> /Contents "
                f"{5 + index * 2} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(content_bytes)).encode("ascii")
            + b" >>\nstream\n" + content_bytes + b"endstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_position = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _write_pdf(path, page_line_groups: list[list[str]]) -> str:
    path.write_bytes(_build_pdf(page_line_groups))
    return str(path)


class TestTextLayer:
    """文本层提取与页码语义"""

    def test_pages_and_paragraphs(self, tmp_path):
        parser = PdfTextParser()
        path = _write_pdf(
            tmp_path / "a.pdf",
            [["first line", "second line"], ["page two body"]],
        )
        document = parser.parse(path, VERSION_ID)
        assert [block.block_type for block in document.blocks] == [
            BlockType.PARAGRAPH,
            BlockType.PARAGRAPH,
        ]
        assert document.blocks[0].page_no == 1
        assert document.blocks[0].text == "first line\nsecond line"
        assert document.blocks[1].page_no == 2
        assert document.blocks[1].text == "page two body"
        assert [block.ordinal for block in document.blocks] == [0, 1]
        assert document.scan_suspected is False

    def test_empty_page_between_text_pages_skipped(self, tmp_path):
        parser = PdfTextParser()
        path = _write_pdf(
            tmp_path / "a.pdf", [["filled"], [], ["filled again"]]
        )
        document = parser.parse(path, VERSION_ID)
        assert [block.page_no for block in document.blocks] == [1, 3]
        assert document.scan_suspected is False


class TestScanSignal:
    """扫描件信号：有页面但无文本层时正常返回，不做路由决策"""

    def test_no_text_layer_returns_scan_signal(self, tmp_path):
        parser = PdfTextParser()
        path = _write_pdf(tmp_path / "scan.pdf", [[], []])
        document = parser.parse(path, VERSION_ID)
        assert document.blocks == ()
        assert document.scan_suspected is True
        assert document.parser_name == "pdf_text"

    def test_zero_pages_rejected_as_empty(self, tmp_path):
        parser = PdfTextParser()
        path = _write_pdf(tmp_path / "void.pdf", [])
        with pytest.raises(EmptyContentError):
            parser.parse(path, VERSION_ID)


class TestRejection:
    """损坏/加密的明确拒绝"""

    def test_encrypted_rejected(self, tmp_path):
        source = PdfReader(io.BytesIO(_build_pdf([["secret"]])))
        writer = PdfWriter()
        writer.append(source)
        writer.encrypt("password")
        buffer = io.BytesIO()
        writer.write(buffer)
        path = tmp_path / "enc.pdf"
        path.write_bytes(buffer.getvalue())
        with pytest.raises(EncryptedDocumentError):
            PdfTextParser().parse(str(path), VERSION_ID)

    def test_garbage_rejected_as_corrupted(self, tmp_path):
        path = tmp_path / "bad.pdf"
        path.write_bytes(b"%PDF-1.4\nthis is not a real pdf body")
        with pytest.raises(DocumentCorruptedError):
            PdfTextParser().parse(str(path), VERSION_ID)

    def test_truncated_rejected_as_corrupted(self, tmp_path):
        path = tmp_path / "cut.pdf"
        path.write_bytes(_build_pdf([["content"]])[:200])
        with pytest.raises(DocumentCorruptedError):
            PdfTextParser().parse(str(path), VERSION_ID)

    def test_error_messages_contain_no_document_text(self, tmp_path):
        path = tmp_path / "enc.pdf"
        source = PdfReader(io.BytesIO(_build_pdf([["classified body"]])))
        writer = PdfWriter()
        writer.append(source)
        writer.encrypt("password")
        buffer = io.BytesIO()
        writer.write(buffer)
        path.write_bytes(buffer.getvalue())
        try:
            PdfTextParser().parse(str(path), VERSION_ID)
        except EncryptedDocumentError as exc:
            message = str(exc)
            assert "classified" not in message
            assert str(path) not in message


class TestDeterminism:
    """确定性合同：同文件重复解析输出逐字段一致"""

    def test_repeated_parse_identical(self, tmp_path):
        parser = PdfTextParser()
        path = _write_pdf(
            tmp_path / "a.pdf", [["alpha"], ["beta one", "beta two"]]
        )
        first = parser.parse(path, VERSION_ID)
        second = parser.parse(path, VERSION_ID)
        assert first == second
        assert first.structure_sha256 == second.structure_sha256
