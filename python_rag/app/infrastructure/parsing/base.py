# -*- coding: utf-8 -*-
"""本地解析器公共契约与文本块辅助逻辑

抽象基类统一三个本地解析器的输入输出契约：输入是已通过上传安全
校验的暂存文件路径与所属文档版本 ID，输出是统一解析模型；调用方
（解析路由）负责按扩展名选择解析器，解析器不再重复做格式白名单校验。
行级文本辅助逻辑供纯文本类输入（TXT/Markdown 与 PDF 文本层）复用。
"""
import re
from abc import ABC, abstractmethod

from app.domain.parsing import Block, BlockType, ParsedDocument, make_block


class LocalFileParser(ABC):
    """本地解析器基类

    parser_name/parser_version 是解析器身份常量：版本号表述输出语义，
    输出结构变化时必须递增，否则确定性哈希无法区分新旧产物
    """

    parser_name: str
    parser_version: str

    @abstractmethod
    def parse(self, staged_path: str, document_version_id: str) -> ParsedDocument:
        """解析暂存文件并输出统一解析模型

        :param staged_path: 已通过上传安全校验的暂存文件路径
        :param document_version_id: 所属文档版本 ID（调用方生成）
        :return: 统一解析模型
        :raises app.domain.errors.ParsingError: 空内容/损坏/加密/编码失败
        """


def normalize_newlines(text: str) -> str:
    """把 CRLF 与孤立 CR 统一归一化为 LF

    :param text: 原始文本
    :return: 仅含 LF 换行的文本
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def group_paragraphs(lines: list[str]) -> list[str]:
    """把行序列按空行边界分组为段落文本

    连续非空行合并为一个段落（行内保留原有换行），空白行视为段落
    分隔；每行去除首尾空白后丢弃空行。

    :param lines: 已归一化换行的文本行
    :return: 段落文本列表（保持出现顺序）
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            current.append(stripped)
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def build_paragraph_blocks(
    paragraphs: list[str],
    *,
    start_ordinal: int,
    page_no: int | None,
    section_path: str | None,
) -> list[Block]:
    """把段落文本列表构造为顺序 paragraph 块

    :param paragraphs: 段落文本列表
    :param start_ordinal: 起始顺序号
    :param page_no: 全部段落共用的页码（无页面概念时为 None）
    :param section_path: 全部段落共用的章节路径
    :return: 顺序号连续递增的块列表
    """
    return [
        make_block(
            BlockType.PARAGRAPH,
            start_ordinal + index,
            page_no=page_no,
            section_path=section_path,
            text=text,
        )
        for index, text in enumerate(paragraphs)
    ]


class SectionPathTracker:
    """标题层级栈：按出现顺序构建各块的章节路径

    章节路径由当前各级标题文本按层级以 " > " 连接；遇到更浅层级
    标题时回退栈顶，同层级标题替换栈顶。
    """

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def enter_heading(self, level: int, title: str) -> str | None:
        """登记一个标题并返回其完整章节路径

        :param level: 标题层级（1 起）
        :param title: 标题文本
        :return: 该标题的章节路径（无标题上下文时为 None）
        """
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, title))
        return self.current_path()

    def current_path(self) -> str | None:
        """返回当前章节路径（尚未出现任何标题时为 None）"""
        if not self._stack:
            return None
        return " > ".join(title for _, title in self._stack)


# Markdown ATX 标题行：1~6 个 # 后接标题文本
_MD_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# Markdown 列表项：无序（- * +）或有序（数字后跟 . 或 )）
_MD_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")

# 围栏代码块边界行（``` 或 ~~~ 开头）
_MD_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")


def parse_markdown_line(line: str) -> tuple[str, str | None]:
    """识别单个 Markdown 行的结构类型

    :param line: 原始文本行（已归一化换行）
    :return: (行类型, 提取内容)；行类型为 heading/list_item/text，
        heading 返回标题文本，list_item 返回去除列表标记的项文本，
        text 返回 None（原始行即内容）
    """
    heading = _MD_HEADING_PATTERN.match(line)
    if heading:
        return "heading", heading.group(2).strip()
    list_item = _MD_LIST_ITEM_PATTERN.match(line)
    if list_item:
        return "list_item", list_item.group(1).strip()
    return "text", None


def is_markdown_fence(line: str) -> bool:
    """判断行是否为围栏代码块边界

    :param line: 原始文本行
    :return: 是围栏边界行时为 True
    """
    return _MD_FENCE_PATTERN.match(line) is not None
