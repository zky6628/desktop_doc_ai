# -*- coding: utf-8 -*-
"""文本型 PDF 本地解析器

按页流式读取：打开文件句柄后逐页提取文本层并即时归一化分组，
不把整份 PDF 的解析结果一次性堆积后再处理。每页文本按空行边界
分组为 paragraph 块并携带 1 起的自然页码。加密文件明确拒绝、
不做解密尝试；有页面但提取不出任何文本层时按扫描件信号正常返回
（空块序列 + scan_suspected 标记），是否转云端解析由路由决策。
"""
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.domain.errors import (
    DocumentCorruptedError,
    EmptyContentError,
    EncryptedDocumentError,
)
from app.domain.parsing import Block, ParsedDocument
from app.infrastructure.parsing.base import (
    LocalFileParser,
    build_paragraph_blocks,
    group_paragraphs,
    normalize_newlines,
)


class PdfTextParser(LocalFileParser):
    """文本型 PDF 的确定性解析器

    只依赖文本层，不解析版面坐标与图像；复杂表格与扫描件属于
    云端解析的升级场景，本解析器只负责给出扫描件信号
    """

    parser_name = "pdf_text"
    parser_version = "1.0.0"

    def parse(self, staged_path: str, document_version_id: str) -> ParsedDocument:
        """解析 PDF 文件为按页编号的块序列

        :param staged_path: 暂存文件路径
        :param document_version_id: 所属文档版本 ID
        :return: 统一解析模型；无文本层时 blocks 为空且 scan_suspected 为 True
        :raises EncryptedDocumentError: 文档声明加密
        :raises DocumentCorruptedError: 文档结构损坏无法读取
        :raises EmptyContentError: 文档没有任何页面
        """
        try:
            with open(staged_path, "rb") as f:
                reader = PdfReader(f)
                if reader.is_encrypted:
                    raise EncryptedDocumentError("暂不支持加密的 PDF 文件")
                blocks = self._extract_blocks(reader)
        except EncryptedDocumentError:
            raise
        except PdfReadError as exc:
            raise DocumentCorruptedError("PDF 文档损坏，无法读取") from exc
        except (OSError, ValueError) as exc:
            # pypdf 对截断/伪造成因的输入可能以裸 ValueError 暴露
            raise DocumentCorruptedError("PDF 文档损坏，无法读取") from exc

        return ParsedDocument(
            document_version_id=document_version_id,
            parser_provider="local",
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            blocks=tuple(blocks),
            scan_suspected=not blocks,
        )

    def _extract_blocks(self, reader: PdfReader) -> list[Block]:
        """逐页提取文本层并分组为块序列

        每页独立提取：单页文本为空只跳过该页；页面遍历过程中的
        解析失败按损坏处理。

        :param reader: 已打开的 PDF 读取器
        :return: 顺序号连续、页码 1 起递增的块列表
        :raises DocumentCorruptedError: 页面内容无法解析
        """
        blocks: list[Block] = []
        try:
            page_count = len(reader.pages)
            if page_count == 0:
                raise EmptyContentError("PDF 文档没有任何页面")
            for page_index in range(page_count):
                page_text = reader.pages[page_index].extract_text() or ""
                paragraphs = group_paragraphs(
                    normalize_newlines(page_text).split("\n")
                )
                blocks.extend(
                    build_paragraph_blocks(
                        paragraphs,
                        start_ordinal=len(blocks),
                        page_no=page_index + 1,
                        section_path=None,
                    )
                )
        except PdfReadError as exc:
            raise DocumentCorruptedError("PDF 页面内容损坏，无法读取") from exc
        return blocks
