# -*- coding: utf-8 -*-
"""TXT 与 Markdown 本地解析器

输入是已通过上传安全校验的纯文本暂存文件。编码按 BOM -> UTF-8 ->
GBK 顺序探测，全文以增量解码器分块读取（不整文件读入内存）；
换行统一归一化为 LF 后再识别结构。Markdown 识别 ATX 标题与列表项
并构建章节路径，TXT 只按空行分段；两种模式产出的都是无页面概念的
paragraph/heading/list 块序列。
"""
import codecs

from app.domain.errors import EmptyContentError, TextEncodingError
from app.domain.parsing import Block, BlockType, ParsedDocument, make_block
from app.infrastructure.parsing.base import (
    LocalFileParser,
    SectionPathTracker,
    build_paragraph_blocks,
    group_paragraphs,
    is_markdown_fence,
    normalize_newlines,
    parse_markdown_line,
)

# 解码分块大小（字节）：按块读取文件并喂给增量解码器，
# 多字节字符跨块时由解码器内部缓冲处理
DECODE_CHUNK_BYTES = 64 * 1024

# 编码探测的采样字节数：只需头部样本即可区分 UTF-8 与 GBK
_ENCODING_SAMPLE_BYTES = DECODE_CHUNK_BYTES

# UTF-8 BOM：存在时以带 BOM 的 UTF-8 解码（解码器自动剥离 BOM）
_UTF8_BOM = b"\xef\xbb\xbf"


class TxtMarkdownParser(LocalFileParser):
    """TXT 与 Markdown 文件的确定性解析器

    markdown_mode 由调用方按扩展名决定：.txt 为纯文本分段，
    .md/.markdown 启用标题与列表结构识别
    """

    parser_name = "txt_md"
    parser_version = "1.0.0"

    def __init__(self, *, markdown_mode: bool) -> None:
        """创建解析器实例

        :param markdown_mode: True 按 Markdown 识别标题/列表，False 按纯文本分段
        """
        self._markdown_mode = markdown_mode

    def parse(self, staged_path: str, document_version_id: str) -> ParsedDocument:
        """解析文本文件为块序列

        :param staged_path: 暂存文件路径
        :param document_version_id: 所属文档版本 ID
        :return: 统一解析模型（无页面概念，page_no 恒为 None）
        :raises EmptyContentError: 文件为空或内容全部为空白
        :raises TextEncodingError: 编码无法识别或文件中段出现非法字节
        """
        encoding = self._detect_encoding(staged_path)
        text = self._decode_file(staged_path, encoding)
        normalized = normalize_newlines(text)
        if not normalized.strip():
            raise EmptyContentError("文件内容为空或全部为空白")

        blocks = (
            self._parse_markdown(normalized)
            if self._markdown_mode
            else self._parse_plain(normalized)
        )
        return ParsedDocument(
            document_version_id=document_version_id,
            parser_provider="local",
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            blocks=tuple(blocks),
        )

    @staticmethod
    def _detect_encoding(staged_path: str) -> str:
        """按头部样本探测文本编码

        探测顺序：UTF-8 BOM -> 严格 UTF-8 -> 严格 GBK；两种编码
        都无法解码样本时判定为不可识别（多为二进制内容）。

        :param staged_path: 暂存文件路径
        :return: 编码名称（utf-8-sig/utf-8/gbk）
        :raises TextEncodingError: 样本无法按任何候选编码解码
        """
        with open(staged_path, "rb") as f:
            sample = f.read(_ENCODING_SAMPLE_BYTES)
        if sample.startswith(_UTF8_BOM):
            return "utf-8-sig"
        for candidate in ("utf-8", "gbk"):
            try:
                sample.decode(candidate)
            except UnicodeDecodeError:
                continue
            return candidate
        raise TextEncodingError("无法识别文本编码，仅支持 UTF-8 或 GBK")

    @staticmethod
    def _decode_file(staged_path: str, encoding: str) -> str:
        """以增量解码器分块读取全文

        分块读取保证内存占用只与块大小相关；探测阶段只覆盖文件头部，
        文件中段出现非法字节时同样按编码错误拒绝。

        :param staged_path: 暂存文件路径
        :param encoding: 探测得到的编码名称
        :return: 完整解码文本
        :raises TextEncodingError: 任一分块解码失败
        """
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        parts: list[str] = []
        with open(staged_path, "rb") as f:
            while chunk := f.read(DECODE_CHUNK_BYTES):
                try:
                    parts.append(decoder.decode(chunk))
                except UnicodeDecodeError as exc:
                    raise TextEncodingError(
                        "文件内容不是合法的目标编码文本"
                    ) from exc
        # final=True 冲刷解码器内部缓冲：文件在多字节字符中段截断时同样报错
        try:
            parts.append(decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise TextEncodingError("文件内容不是合法的目标编码文本") from exc
        return "".join(parts)

    def _parse_plain(self, normalized: str) -> list[Block]:
        """纯文本模式：空行分段的 paragraph 块序列"""
        paragraphs = group_paragraphs(normalized.split("\n"))
        return build_paragraph_blocks(
            paragraphs, start_ordinal=0, page_no=None, section_path=None
        )

    def _parse_markdown(self, normalized: str) -> list[Block]:
        """Markdown 模式：委托模块级块构建（归一化回退复用同一实现）"""
        return markdown_blocks(normalized)


def markdown_blocks(normalized: str) -> list[Block]:
    """把 Markdown 文本构建为块序列

    行结构识别规则：ATX 标题（1~6 个 #）构建章节路径、无序/有序
    列表项按连续段合并、围栏代码块（围栏内部一律视为普通文本行）；
    标题与列表之外的连续非空行按空行边界合并为 paragraph 块。

    :param normalized: 已归一化换行（仅 LF）的 Markdown 文本
    :return: 顺序号连续的块列表（无页面概念，page_no 为 None）
    """
    blocks: list[Block] = []
    tracker = SectionPathTracker()
    ordinal = 0

    paragraph_lines: list[str] = []
    list_items: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        nonlocal ordinal
        if paragraph_lines:
            text = "\n".join(paragraph_lines)
            blocks.append(
                make_block(
                    BlockType.PARAGRAPH,
                    ordinal,
                    section_path=tracker.current_path(),
                    text=text,
                )
            )
            ordinal += 1
            paragraph_lines.clear()

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

    for line in normalized.split("\n"):
        if is_markdown_fence(line):
            # 围栏边界切换代码模式，围栏内部行按普通文本累积
            in_fence = not in_fence
            paragraph_lines.append(line.strip())
            continue
        if in_fence:
            paragraph_lines.append(line.strip())
            continue

        line_type, content = parse_markdown_line(line)
        if line_type == "heading":
            flush_paragraph()
            flush_list()
            assert content is not None
            section_path = tracker.enter_heading(
                len(line) - len(line.lstrip("#")), content
            )
            blocks.append(
                make_block(
                    BlockType.HEADING,
                    ordinal,
                    section_path=section_path,
                    text=content,
                )
            )
            ordinal += 1
        elif line_type == "list_item":
            flush_paragraph()
            assert content is not None
            list_items.append(content)
        else:
            flush_list()
            if line.strip():
                paragraph_lines.append(line.strip())

    flush_paragraph()
    flush_list()
    return blocks
