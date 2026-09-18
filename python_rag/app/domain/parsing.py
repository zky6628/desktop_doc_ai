# -*- coding: utf-8 -*-
"""解析产物领域模型：本地解析与云端解析归一的统一输出

模型字段与业务表列一一对应：块序列对应 content_blocks 列，表格结构
证据对应 tables 列。所有哈希基于字段内容的规范 JSON 序列化计算，
相同输入与相同解析器版本必然得到逐字段相同的结果；整篇结构哈希
由块序列即时复算得出，不落存储、不随实例保存。
"""
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

# 解析提供方标识：本地解析器统一取值；云端解析提供方由其适配器定义
PARSER_PROVIDER_LOCAL = "local"

# 本地表格序列化标识：searchable_text 由确定性规则产出（无 LLM 参与），
# 规则语义变化时必须递增版本号，使已有哈希失效
TABLE_SERIALIZATION_MODEL_LOCAL = "local"
TABLE_SERIALIZATION_VERSION = "1"

# 规范 JSON 序列化：紧凑分隔符 + 键排序 + 保留非 ASCII 字符，
# 保证哈希输入在不同运行环境间逐字节一致；切片等其他需要确定性
# 哈希的领域模块共用本函数，避免各处序列化口径漂移
def canonical_json(payload: object) -> str:
    """把哈希输入载荷序列化为规范 JSON 文本

    :param payload: 可 JSON 序列化的哈希输入
    :return: 紧凑、键排序、保留非 ASCII 字符的 JSON 文本
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class BlockType(StrEnum):
    """解析块类型：与内容表 block_type 列的取值口径一致

    image_ocr/header/footer 仅由云端解析归一化产出；本地解析器
    只产出 heading/paragraph/list/table
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE_OCR = "image_ocr"
    HEADER = "header"
    FOOTER = "footer"


@dataclass(frozen=True)
class TableEvidence:
    """表格块的结构证据：与 tables 表列一一对应

    raw_html/raw_markdown/structure_json 是不可变的引用证据；
    searchable_text 只是检索辅助文本，由 serialization_model 标识的
    序列化规则产出
    """

    raw_html: str | None = None
    raw_markdown: str | None = None
    structure_json: str | None = None
    searchable_text: str | None = None
    serialization_model: str | None = None
    serialization_version: str | None = None


@dataclass(frozen=True)
class Block:
    """解析块：一篇文档内顺序排列的最小结构单元

    ordinal 为版本内顺序号（0 起）；page_no 为 1 起的自然页码，
    无页面概念的文本（TXT/Markdown/DOCX 主体）为 None。
    content_hash 覆盖块的全部语义字段（不含 ordinal 与所属版本），
    由 make_block 工厂统一计算，禁止手工填写
    """

    block_type: BlockType
    ordinal: int
    content_hash: str
    page_no: int | None = None
    section_path: str | None = None
    text: str | None = None
    bbox_json: str | None = None
    source_locator_json: str | None = None
    table: TableEvidence | None = None


def make_block(
    block_type: BlockType,
    ordinal: int,
    *,
    page_no: int | None = None,
    section_path: str | None = None,
    text: str | None = None,
    bbox_json: str | None = None,
    source_locator_json: str | None = None,
    table: TableEvidence | None = None,
) -> Block:
    """构造块并计算内容哈希

    哈希输入为块语义字段的规范 JSON（表格证据整体参与），不含
    ordinal 与所属版本：同一内容在不同位置/不同版本哈希一致，
    位置与整体结构由结构哈希负责。

    :param block_type: 块类型
    :param ordinal: 版本内顺序号（0 起）
    :param page_no: 1 起的自然页码（无页面概念时为 None）
    :param section_path: 所属章节路径（无章节上下文时为 None）
    :param text: 文本内容（表格块可空，结构在表格证据中）
    :param bbox_json: 版面坐标 JSON 文本
    :param source_locator_json: 原始来源定位 JSON 文本
    :param table: 表格结构证据（仅表格块携带）
    :return: 已填充内容哈希的不可变块
    """
    payload = {
        "block_type": block_type.value,
        "page_no": page_no,
        "section_path": section_path,
        "text": text,
        "bbox_json": bbox_json,
        "source_locator_json": source_locator_json,
        "table": None
        if table is None
        else {
            "raw_html": table.raw_html,
            "raw_markdown": table.raw_markdown,
            "structure_json": table.structure_json,
            "searchable_text": table.searchable_text,
            "serialization_model": table.serialization_model,
            "serialization_version": table.serialization_version,
        },
    }
    canonical = canonical_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Block(
        block_type=block_type,
        ordinal=ordinal,
        content_hash=digest,
        page_no=page_no,
        section_path=section_path,
        text=text,
        bbox_json=bbox_json,
        source_locator_json=source_locator_json,
        table=table,
    )


@dataclass(frozen=True)
class ParsedDocument:
    """解析产物：解析器元信息与有序块序列

    document_version_id 由调用方传入（解析器不负责生成业务 ID）。
    scan_suspected 表示输入是有页面但无文本层的扫描件信号，
    供后续路由决策使用；正常解析恒为 False。
    structure_sha256 为即时复算的整篇结构哈希：覆盖解析器身份与
    有序块哈希序列，不包含 document_version_id（同一内容在不同
    版本下结构哈希一致，可用于幂等检测）。
    """

    document_version_id: str
    parser_provider: str
    parser_name: str
    parser_version: str
    blocks: tuple[Block, ...]
    scan_suspected: bool = False

    @property
    def structure_sha256(self) -> str:
        """复算整篇结构哈希

        :return: 解析器身份与全部块（顺序 + 内容哈希）的 SHA-256 十六进制文本
        """
        payload = {
            "parser_provider": self.parser_provider,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "scan_suspected": self.scan_suspected,
            "blocks": [
                {"ordinal": block.ordinal, "content_hash": block.content_hash}
                for block in self.blocks
            ],
        }
        canonical = canonical_json(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
