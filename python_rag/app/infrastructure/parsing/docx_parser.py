# -*- coding: utf-8 -*-
"""DOCX 本地解析器

以文档主体 XML 的子节点顺序遍历，段落/标题/列表/表格的交错顺序
完整保真（文档对象模型的 paragraphs 属性会丢失表格与顺序，故不使用）。
标题按样式名（Heading N / 标题 N）识别并构建章节路径；列表按样式名
或编号属性识别并按连续段合并。只读取内容，不执行宏、不解析外部关系
（python-docx 的打开过程天然不执行 VBA、不访问外部链接目标）。
"""
import html
import json
import re
import zipfile

from docx import Document as open_document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

from app.domain.errors import (
    DocumentCorruptedError,
    EmptyContentError,
    EncryptedDocumentError,
)
from app.domain.parsing import (
    TABLE_SERIALIZATION_MODEL_LOCAL,
    TABLE_SERIALIZATION_VERSION,
    Block,
    BlockType,
    ParsedDocument,
    TableEvidence,
    make_block,
)
from app.infrastructure.parsing.base import LocalFileParser, SectionPathTracker

# 内建标题样式名：中文 Word 文档的样式名同样以 Heading N 存储于
# styles.xml，个别模板使用本地化的"标题 N"，一并识别
_HEADING_STYLE_PATTERN = re.compile(r"^(?:Heading|标题)\s*(\d+)$", re.IGNORECASE)

# OLE 复合文档 magic：加密的 OOXML 文档会被保存为 OLE 容器，
# 以此区分"已加密"与"一般损坏"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# OLE magic 判定的读取字节数
_MAGIC_BYTES = 8


class DocxParser(LocalFileParser):
    """DOCX 文件的确定性解析器

    表格块同步产出结构证据（HTML/Markdown/单元格矩阵/检索文本），
    全部由确定性规则生成；全空表格不产出块
    """

    parser_name = "docx"
    parser_version = "1.0.0"

    def parse(self, staged_path: str, document_version_id: str) -> ParsedDocument:
        """解析 DOCX 文件为块序列

        :param staged_path: 暂存文件路径
        :param document_version_id: 所属文档版本 ID
        :return: 统一解析模型（无页面概念，page_no 恒为 None）
        :raises EncryptedDocumentError: 文件为 OLE 容器（已加密文档）
        :raises DocumentCorruptedError: ZIP 容器损坏或不是有效的 Word 文档
        :raises EmptyContentError: 文档没有任何有效内容
        """
        self._reject_encrypted(staged_path)
        blocks = self._parse_body(staged_path)
        if not blocks:
            raise EmptyContentError("文档没有任何有效内容")
        return ParsedDocument(
            document_version_id=document_version_id,
            parser_provider="local",
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            blocks=tuple(blocks),
        )

    @staticmethod
    def _reject_encrypted(staged_path: str) -> None:
        """OLE magic 前缀判定：加密文档明确拒绝，不做解密尝试

        :param staged_path: 暂存文件路径
        :raises EncryptedDocumentError: 文件头为 OLE 复合容器
        """
        with open(staged_path, "rb") as f:
            head = f.read(_MAGIC_BYTES)
        if head.startswith(_OLE_MAGIC):
            raise EncryptedDocumentError("暂不支持加密的 DOCX 文件")

    def _parse_body(self, staged_path: str) -> list[Block]:
        """按主体子节点顺序产出块序列

        打开容器的一切失败统一翻译为损坏错误；ZIP 校验在打开瞬间完成，
        内容读取阶段不再出现容器级异常。

        :param staged_path: 暂存文件路径
        :return: 顺序号连续的块列表
        :raises DocumentCorruptedError: 容器损坏或内容不是 Word 文档结构
        """
        try:
            document = open_document(staged_path)
        except zipfile.BadZipFile as exc:
            raise DocumentCorruptedError("DOCX 容器损坏，无法打开") from exc
        except Exception as exc:
            # python-docx 对缺目录/坏结构抛包级异常，统一按损坏处理
            raise DocumentCorruptedError("文件不是有效的 DOCX 文档") from exc

        blocks: list[Block] = []
        tracker = SectionPathTracker()
        ordinal = 0
        list_items: list[str] = []

        def flush_list() -> None:
            nonlocal ordinal
            if list_items:
                blocks.append(
                    make_block(
                        BlockType.LIST,
                        ordinal,
                        section_path=tracker.current_path(),
                        text="\n".join(list_items),
                    )
                )
                ordinal += 1
                list_items.clear()

        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = DocxParagraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                heading_level = self._heading_level(paragraph)
                if heading_level is not None:
                    flush_list()
                    section_path = tracker.enter_heading(heading_level, text)
                    blocks.append(
                        make_block(
                            BlockType.HEADING,
                            ordinal,
                            section_path=section_path,
                            text=text,
                        )
                    )
                    ordinal += 1
                elif self._is_list_item(paragraph):
                    list_items.append(text)
                else:
                    flush_list()
                    blocks.append(
                        make_block(
                            BlockType.PARAGRAPH,
                            ordinal,
                            section_path=tracker.current_path(),
                            text=text,
                        )
                    )
                    ordinal += 1
            elif isinstance(child, CT_Tbl):
                flush_list()
                table_block = self._table_block(
                    DocxTable(child, document),
                    ordinal,
                    section_path=tracker.current_path(),
                )
                if table_block is not None:
                    blocks.append(table_block)
                    ordinal += 1

        flush_list()
        return blocks

    @staticmethod
    def _style_name(paragraph: DocxParagraph) -> str:
        """读取段落样式名；样式引用缺失（模板损坏的边角情况）按普通文本

        :param paragraph: 文档段落
        :return: 样式名文本（读取失败时为空串）
        """
        try:
            style = paragraph.style
        except KeyError:
            return ""
        if style is None:
            return ""
        return (style.name or "").strip()

    @classmethod
    def _heading_level(cls, paragraph: DocxParagraph) -> int | None:
        """从样式名解析标题层级

        :param paragraph: 文档段落
        :return: 标题层级（1 起）；非标题样式返回 None
        """
        matched = _HEADING_STYLE_PATTERN.match(cls._style_name(paragraph))
        return int(matched.group(1)) if matched else None

    @classmethod
    def _is_list_item(cls, paragraph: DocxParagraph) -> bool:
        """判定段落是否为列表项：列表样式名或携带编号属性均视为列表

        :param paragraph: 文档段落
        :return: 是列表项时为 True
        """
        if cls._style_name(paragraph).lower().startswith("list"):
            return True
        properties = paragraph._p.pPr
        return properties is not None and properties.numPr is not None

    def _table_block(
        self, table: DocxTable, ordinal: int, *, section_path: str | None
    ) -> Block | None:
        """把文档表格构造为携带结构证据的表格块

        单元格文本一律去除首尾空白；全空表格（没有任何非空单元格）
        不产出块。合并单元格按网格坐标重复出现，证据如实记录。

        :param table: 文档表格
        :param ordinal: 块顺序号
        :param section_path: 表格所在章节路径
        :return: 表格块；全空表格返回 None
        """
        rows: list[list[str]] = []
        for row in table.rows:
            rows.append([cell.text.strip() for cell in row.cells])
        if not any(text for row in rows for text in row):
            return None

        evidence = TableEvidence(
            raw_html=self._table_html(rows),
            raw_markdown=self._table_markdown(rows),
            structure_json=json.dumps(
                {"rows": rows}, ensure_ascii=False, separators=(",", ":")
            ),
            searchable_text="\n".join(" | ".join(row) for row in rows),
            serialization_model=TABLE_SERIALIZATION_MODEL_LOCAL,
            serialization_version=TABLE_SERIALIZATION_VERSION,
        )
        return make_block(
            BlockType.TABLE, ordinal, section_path=section_path, table=evidence
        )

    @staticmethod
    def _table_html(rows: list[list[str]]) -> str:
        """把单元格矩阵序列化为紧凑 HTML 表格

        :param rows: 单元格矩阵（已去除首尾空白）
        :return: 单行 <table> HTML 文本
        """
        body = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
            + "</tr>"
            for row in rows
        )
        return f"<table>{body}</table>"

    @staticmethod
    def _table_markdown(rows: list[list[str]]) -> str:
        """把单元格矩阵序列化为 Markdown 管道表格

        单元格内的竖线转义、换行压平为空格，保证单行确定性输出。

        :param rows: 单元格矩阵（已去除首尾空白）
        :return: 含表头分隔行的 Markdown 表格文本
        """
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [
            [cell.replace("\n", " ").replace("|", "\\|") for cell in row]
            + [""] * (width - len(row))
            for row in rows
        ]
        lines = ["| " + " | ".join(row) + " |" for row in normalized]
        separator = "| " + " | ".join(["---"] * width) + " |"
        return "\n".join([lines[0], separator, *lines[1:]])
