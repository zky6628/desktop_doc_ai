# -*- coding: utf-8 -*-
"""本地解析器：TXT/Markdown、DOCX 与文本型 PDF

三个解析器共享统一解析契约（LocalFileParser），输出领域层定义的
ParsedDocument；扩展名到解析器的映射由解析路由负责，不在此定义
"""
from .base import LocalFileParser
from .docx_parser import DocxParser
from .pdf_parser import PdfTextParser
from .txt_md_parser import TxtMarkdownParser

__all__ = ["DocxParser", "LocalFileParser", "PdfTextParser", "TxtMarkdownParser"]
