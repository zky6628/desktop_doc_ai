# -*- coding: utf-8 -*-
"""MinerU 产物归一化：解压目录 -> 统一解析模型

归一化前先验证必需文件（full.md 必须存在）；结构化内容列表存在时
优先使用（携带页码、标题层级、表格 HTML 与图片引用，结构保真度
最高），缺失时回退到 full.md 的 Markdown 解析（页码信息不可得，
属可接受的降级）。未识别的条目类型确定性跳过，供应方新增类型
不会导致整体失败；未知结构性破坏（非 JSON、非数组、非对象条目）
按协议违规拒绝。规则语义变化时必须递增配置版本，使结构哈希可辨别。
"""
import json
import os
from html.parser import HTMLParser

from app.domain.errors import CloudProtocolViolationError
from app.domain.parsing import (
    TABLE_SERIALIZATION_MODEL_LOCAL,
    TABLE_SERIALIZATION_VERSION,
    Block,
    BlockType,
    ParsedDocument,
    TableEvidence,
    make_block,
)
from app.infrastructure.parsing.base import SectionPathTracker
from app.infrastructure.parsing.txt_md_parser import markdown_blocks

# 归一化规则版本：块映射语义变化时必须递增
NORMALIZATION_CONFIG_VERSION = "1"

# 必需产物与结构化内容列表的文件命名约定
_FULL_MD_NAME = "full.md"
_CONTENT_LIST_SUFFIX = "_content_list.json"

# 归一化产物身份：供应方标识 + 产物源 + 规则版本
_PROVIDER_MINERU = "mineru"
_PARSER_NAME_CONTENT_LIST = "mineru_content_list"
_PARSER_NAME_FULL_MD = "mineru_full_md"


def normalize_archive(archive_dir: str, document_version_id: str) -> ParsedDocument:
    """把解压后的结果目录归一化为统一解析模型

    :param archive_dir: 解压后的产物目录（含 full.md 与可选内容列表）
    :param document_version_id: 所属文档版本 ID（调用方生成）
    :return: 统一解析模型（结构哈希由模型即时复算）
    :raises CloudProtocolViolationError: 缺少必需文件或结构性破坏
    """
    full_md_path = os.path.join(archive_dir, _FULL_MD_NAME)
    if not os.path.isfile(full_md_path):
        raise CloudProtocolViolationError("缺少必需的解析产物 full.md")

    content_list_path = _find_content_list(archive_dir)
    if content_list_path is not None:
        blocks = _blocks_from_content_list(content_list_path)
        parser_name = _PARSER_NAME_CONTENT_LIST
    else:
        blocks = markdown_blocks(_read_text(full_md_path))
        parser_name = _PARSER_NAME_FULL_MD

    return ParsedDocument(
        document_version_id=document_version_id,
        parser_provider=_PROVIDER_MINERU,
        parser_name=parser_name,
        parser_version=NORMALIZATION_CONFIG_VERSION,
        blocks=tuple(blocks),
    )


def _find_content_list(archive_dir: str) -> str | None:
    """定位结构化内容列表文件（可能带文档哈希前缀）

    多个匹配时取文件名排序首个，保证确定性。

    :param archive_dir: 产物目录
    :return: 内容列表路径；不存在返回 None
    """
    candidates = sorted(
        name
        for name in os.listdir(archive_dir)
        if name.endswith(_CONTENT_LIST_SUFFIX)
    )
    if not candidates:
        return None
    return os.path.join(archive_dir, candidates[0])


def _blocks_from_content_list(path: str) -> list[Block]:
    """把结构化内容列表映射为块序列

    :param path: 内容列表 JSON 文件路径
    :return: 顺序号连续的块列表（未知类型条目跳过）
    :raises CloudProtocolViolationError: JSON 非法、根非数组或条目非对象
    """
    items = _load_content_items(path)
    blocks: list[Block] = []
    tracker = SectionPathTracker()
    for item in items:
        if not isinstance(item, dict):
            raise CloudProtocolViolationError("内容列表条目不是对象")
        block = _block_from_item(item, ordinal=len(blocks), tracker=tracker)
        if block is not None:
            blocks.append(block)
    return blocks


def _block_from_item(item: dict, ordinal: int, tracker: SectionPathTracker) -> Block | None:
    """把单个内容条目映射为块

    未知类型与无有效内容的条目返回 None（确定性跳过）；文本条目
    携带标题层级时进入章节栈并产出标题块。

    :param item: 内容条目对象
    :param ordinal: 候选块顺序号
    :param tracker: 章节路径栈
    :return: 块；跳过时为 None
    """
    item_type = item.get("type")
    page_no = _page_no(item)
    bbox_json = _bbox_json(item)
    if item_type == "text":
        text = _clean_text(item.get("text"))
        if not text:
            return None
        level = item.get("text_level")
        if isinstance(level, int) and level >= 1:
            return make_block(
                BlockType.HEADING,
                ordinal,
                page_no=page_no,
                section_path=tracker.enter_heading(level, text),
                text=text,
                bbox_json=bbox_json,
            )
        return make_block(
            BlockType.PARAGRAPH,
            ordinal,
            page_no=page_no,
            section_path=tracker.current_path(),
            text=text,
            bbox_json=bbox_json,
        )
    if item_type == "equation":
        text = _clean_text(item.get("text"))
        if not text:
            return None
        return make_block(
            BlockType.PARAGRAPH,
            ordinal,
            page_no=page_no,
            section_path=tracker.current_path(),
            text=text,
            bbox_json=bbox_json,
        )
    if item_type == "table":
        return _table_block(item, ordinal, page_no, tracker, bbox_json)
    if item_type == "image":
        return _image_block(item, ordinal, page_no, tracker, bbox_json)
    # 未识别的类型（供应方新增条目类型）确定性跳过，不产生块
    return None


def _table_block(
    item: dict,
    ordinal: int,
    page_no: int | None,
    tracker: SectionPathTracker,
    bbox_json: str | None,
) -> Block | None:
    """把表格条目映射为携带结构证据的表格块

    原始 HTML 是引用证据；可检索文本由 HTML 内容按确定性规则抽取
    （剥离标签、压平空白），不丢失也不加解释。

    :param item: 内容条目对象
    :param ordinal: 块顺序号
    :param page_no: 页码
    :param tracker: 章节路径栈
    :param bbox_json: 版面坐标 JSON 文本
    :return: 表格块；无有效内容时为 None
    """
    raw_html = item.get("table_body")
    caption = _string_list(item.get("table_caption"))
    footnote = _string_list(item.get("table_footnote"))
    if not raw_html and not caption:
        return None
    searchable = _html_to_text(raw_html or "")
    evidence = TableEvidence(
        raw_html=raw_html or None,
        raw_markdown=None,
        structure_json=json.dumps(
            {"caption": caption, "footnote": footnote},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        searchable_text=searchable,
        serialization_model=TABLE_SERIALIZATION_MODEL_LOCAL,
        serialization_version=TABLE_SERIALIZATION_VERSION,
    )
    return make_block(
        BlockType.TABLE,
        ordinal,
        page_no=page_no,
        section_path=tracker.current_path(),
        bbox_json=bbox_json,
        table=evidence,
    )


def _image_block(
    item: dict,
    ordinal: int,
    page_no: int | None,
    tracker: SectionPathTracker,
    bbox_json: str | None,
) -> Block | None:
    """把图片条目映射为带图注的图片块

    图片本体不读取，仅在来源定位中记录其在压缩包内的相对路径。

    :param item: 内容条目对象
    :param ordinal: 块顺序号
    :param page_no: 页码
    :param tracker: 章节路径栈
    :param bbox_json: 版面坐标 JSON 文本
    :return: 图片块；无图注且无路径时为 None
    """
    caption = _string_list(item.get("img_caption"))
    img_path = item.get("img_path")
    if not caption and not img_path:
        return None
    text = "\n".join(caption) if caption else None
    locator = (
        json.dumps({"img_path": img_path}, ensure_ascii=False, separators=(",", ":"))
        if img_path
        else None
    )
    return make_block(
        BlockType.IMAGE_OCR,
        ordinal,
        page_no=page_no,
        section_path=tracker.current_path(),
        text=text,
        bbox_json=bbox_json,
        source_locator_json=locator,
    )


def _load_content_items(path: str) -> list:
    """读取并校验内容列表根结构

    :param path: 内容列表 JSON 文件路径
    :return: 条目数组
    :raises CloudProtocolViolationError: JSON 非法或根不是数组
    """
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError) as exc:
        raise CloudProtocolViolationError("内容列表不是有效的 JSON 文档") from exc
    if not isinstance(items, list):
        raise CloudProtocolViolationError("内容列表根结构不是数组")
    return items


def _read_text(path: str) -> str:
    """读取 UTF-8 文本产物

    :param path: 文本文件路径
    :return: 文本内容
    :raises CloudProtocolViolationError: 文件不是合法 UTF-8 文本
    """
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise CloudProtocolViolationError("文本产物不是合法的 UTF-8 内容") from exc


def _page_no(item: dict) -> int | None:
    """从条目读取 1 起页码（供应方页索引 0 起）

    :param item: 内容条目对象
    :return: 页码；缺失或非整数返回 None
    """
    page_idx = item.get("page_idx")
    if isinstance(page_idx, int) and page_idx >= 0:
        return page_idx + 1
    return None


def _bbox_json(item: dict) -> str | None:
    """把条目的版面坐标规整为 JSON 文本

    :param item: 内容条目对象
    :return: 坐标数组（四元数值）的紧凑 JSON；缺失或形态不符返回 None
    """
    bbox = item.get("bbox")
    if (
        isinstance(bbox, list)
        and len(bbox) == 4
        and all(isinstance(v, (int, float)) for v in bbox)
    ):
        return json.dumps(bbox, separators=(",", ":"))
    return None


def _clean_text(value) -> str | None:
    """清理条目文本：去除首尾空白，空串归一为 None

    :param value: 条目文本字段
    :return: 清理后的文本；无有效内容返回 None
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _string_list(value) -> list[str]:
    """把条目的字符串数组字段规整为非空字符串列表

    :param value: 条目数组字段（如图注/表注）
    :return: 非空字符串列表（保持原顺序）
    """
    if not isinstance(value, list):
        return []
    return [part.strip() for part in value if isinstance(part, str) and part.strip()]


class _HtmlTextExtractor(HTMLParser):
    """HTML 纯文本抽取器：只收集文本节点，标签结构不参与输出"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)


def _html_to_text(html_text: str) -> str | None:
    """把 HTML 片段确定性抽取为纯文本（空白压平、单空格连接）

    :param html_text: HTML 片段（如表格单元格结构）
    :return: 纯文本；无文本内容返回 None
    """
    extractor = _HtmlTextExtractor()
    extractor.feed(html_text)
    if not extractor.parts:
        return None
    return " ".join(extractor.parts)
