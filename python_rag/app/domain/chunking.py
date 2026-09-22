# -*- coding: utf-8 -*-
"""层次切片：解析块序列到父子切片结构的确定性转换

切片模型分两级：父切片保存章节内的完整上下文（供候选命中后展开），
子切片是直接参与召回的检索单元；表格块以可检索文本参与切片并保留
到原始表格结构的定位关系。转换是纯函数：相同块序列与相同配置参数
必然得到逐字段一致的结果，切片序号在索引版本内连续（父子共用），
内容哈希覆盖切片语义字段且不含位置主键——位置稳定性由仓储的
按序号 upsert 语义承担，哈希只表达内容事实。
"""
import hashlib
import json
from dataclasses import dataclass

from app.domain.parsing import Block, BlockType, canonical_json

# 切片配置版本：参数语义或算法行为变化时必须递增，使配置行与
# 既有切片哈希可辨别
CHUNKING_CONFIG_VERSION = "1"

# 配置类型取值：与流水线配置表的 config_type 口径一致
CHUNKING_CONFIG_TYPE = "chunking"

# 父切片字符预算默认值：组内素材累计超过该值即开新父切片（单素材
# 超预算时独占切片，不硬切以保持解析块完整）
PARENT_CHUNK_CHARS = 1200

# 子切片字符预算默认值：父切片内的再切分粒度（直接召回单元）
CHILD_CHUNK_CHARS = 400

# 素材文本拼接分隔符：段落语义的确定性连接
CHUNK_CONTENT_SEPARATOR = "\n\n"

# 定位关系类型：切片由其链接的解析块原文构成（引用定位链的起点）
CHUNK_BLOCK_RELATION_EXACT = "exact"

# 系统设置键：切片参数覆盖（缺省即默认预算）
CHUNKING_OVERRIDE_SETTING_KEY = "chunking_override"


@dataclass(frozen=True)
class ChunkingParams:
    """切片参数：父/子切片字符预算（在役值由系统设置解析）"""

    parent_chunk_chars: int
    child_chunk_chars: int


DEFAULT_CHUNKING_PARAMS = ChunkingParams(
    parent_chunk_chars=PARENT_CHUNK_CHARS,
    child_chunk_chars=CHILD_CHUNK_CHARS,
)


def validate_chunking_params(parent: object, child: object) -> ChunkingParams:
    """校验并构造切片参数

    入参按未信任值处理（可来自 JSON 反序列化），运行时完整校验。
    结构不变式：正整数、子不超父；违反即拒绝而非静默调整——参数
    错误应在写入端暴露，不该在切片端被掩盖

    :raises ValueError: 不变式不满足
    """
    # JSON 反序列化后的类型不符按参数值错误统一以 ValueError 表达
    # （调用方单一错误族处理，不区分异常类型）
    if not isinstance(parent, int) or isinstance(parent, bool):
        raise ValueError("parent_chunk_chars 必须为正整数")  # noqa: TRY004
    if not isinstance(child, int) or isinstance(child, bool):
        raise ValueError("child_chunk_chars 必须为正整数")  # noqa: TRY004
    if parent < 1 or child < 1:
        raise ValueError("parent_chunk_chars 与 child_chunk_chars 必须为正整数")
    if child > parent:
        raise ValueError("child_chunk_chars 不能大于 parent_chunk_chars")
    return ChunkingParams(parent_chunk_chars=parent, child_chunk_chars=child)


def resolve_chunking_params(value_json: str | None) -> ChunkingParams:
    """从系统设置 JSON 解析切片参数

    缺省（None/空白）即默认预算；值必须为含 parent/child 的对象且
    满足结构不变式，非法输入拒绝

    :raises ValueError: JSON 非法、字段缺失或不变式不满足
    """
    if value_json is None or not value_json.strip():
        return DEFAULT_CHUNKING_PARAMS
    try:
        content = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"切片参数 JSON 非法: {exc}") from exc
    if not isinstance(content, dict):
        raise TypeError("切片参数必须是对象")
    return validate_chunking_params(
        content.get("parent_chunk_chars"),
        content.get("child_chunk_chars"),
    )


def chunking_config_json(params: ChunkingParams) -> str:
    """产出切片参数集的规范 JSON（配置行的内容与哈希来源）

    :return: 键排序、紧凑分隔的配置 JSON 文本
    """
    return canonical_json(
        {
            "chunking_config_version": CHUNKING_CONFIG_VERSION,
            "parent_chunk_chars": params.parent_chunk_chars,
            "child_chunk_chars": params.child_chunk_chars,
            "content_separator": CHUNK_CONTENT_SEPARATOR,
        }
    )


@dataclass(frozen=True)
class StoredBlock:
    """已落库解析块：切片输入需要数据库主键建立定位关系

    切片从持久化的解析事实读取而非内存解析模型，主键用于写
    切片-块定位关系；领域 Block 保持无主键的纯内容语义
    """

    id: str
    block: Block


@dataclass(frozen=True)
class Chunk:
    """切片：索引版本内的检索单元（父切片或子切片）

    parent_ordinal 为空表示父切片；子切片引用其父的序号，仓储写
    入时解析为实际主键。content_hash 不含 ordinal 与所属索引版本，
    block_ids 只参与定位关系、不参与哈希
    """

    ordinal: int
    content: str
    content_hash: str
    parent_ordinal: int | None
    section_path: str | None
    page_start: int | None
    page_end: int | None
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class StoredChunk:
    """已落库切片：向量写入需要数据库主键作为记录 ID

    切片主键经按序号 upsert 保持跨重放稳定，向量化以它为向量 ID，
    检索命中后经主键回查业务事实
    """

    id: str
    chunk: Chunk


def integrity_hash(chunks: list[Chunk]) -> str:
    """计算检索单元完整性哈希（索引验证与重建比对的基准）

    哈希输入为子切片的序号与内容哈希的规范 JSON 序列（父切片不参与
    向量召回，不计入完整性口径）；输入顺序须按序号升序

    :param chunks: 已按序号升序排列的切片
    :return: SHA-256 十六进制文本
    """
    payload = [
        {"ordinal": chunk.ordinal, "content_hash": chunk.content_hash}
        for chunk in chunks
        if chunk.parent_ordinal is not None
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_chunk(
    *,
    ordinal: int,
    content: str,
    parent_ordinal: int | None,
    section_path: str | None,
    page_start: int | None,
    page_end: int | None,
    block_ids: tuple[str, ...],
) -> Chunk:
    """构造切片并计算内容哈希

    哈希输入为切片语义字段的规范 JSON：内容、章节路径、页码范围与
    父子结构（父序号）。相同输入在不同索引版本下哈希一致。

    :return: 已填充内容哈希的不可变切片
    """
    payload = {
        "content": content,
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_end,
        "parent_ordinal": parent_ordinal,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return Chunk(
        ordinal=ordinal,
        content=content,
        content_hash=digest,
        parent_ordinal=parent_ordinal,
        section_path=section_path,
        page_start=page_start,
        page_end=page_end,
        block_ids=block_ids,
    )


@dataclass(frozen=True)
class _Unit:
    """切片素材：一个参与切片的解析块及其文本"""

    block_id: str
    page_no: int | None
    text: str


def _units_from_blocks(blocks: list[StoredBlock]) -> list[tuple[str | None, _Unit]]:
    """按块序产出切片素材：普通块取文本，表格块取可检索文本

    无文本或全空白的块不产生素材（无内容可检索）；表格块的可检索
    文本由解析阶段的确定性序列化产出，空表格证据同样跳过
    """
    units: list[tuple[str | None, _Unit]] = []
    for stored in blocks:
        block = stored.block
        text = block.text
        if block.block_type is BlockType.TABLE and block.table is not None:
            text = block.table.searchable_text
        if text is None or not text.strip():
            continue
        units.append((block.section_path, _Unit(stored.id, block.page_no, text)))
    return units


def _aggregate(
    units: list[_Unit], budget: int
) -> list[list[_Unit]]:
    """按字符预算把素材聚合为切片（保块完整，不硬切）

    逐素材累计：加入当前切片会超出预算且当前非空时开新切片；
    单素材超预算时独占一个切片。结果可为空（输入为空时）
    """
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_len = 0
    for unit in units:
        addition = len(unit.text)
        if current:
            addition += len(CHUNK_CONTENT_SEPARATOR)
            if current_len + addition > budget:
                groups.append(current)
                current = []
                current_len = 0
                addition = len(unit.text)
        current.append(unit)
        current_len += addition
    if current:
        groups.append(current)
    return groups


def _build_chunk(
    ordinal: int,
    parent_ordinal: int | None,
    section_path: str | None,
    units: list[_Unit],
) -> Chunk:
    """由素材组构造切片：内容拼接、页码范围取素材并集"""
    page_nos = [unit.page_no for unit in units if unit.page_no is not None]
    return make_chunk(
        ordinal=ordinal,
        content=CHUNK_CONTENT_SEPARATOR.join(unit.text for unit in units),
        parent_ordinal=parent_ordinal,
        section_path=section_path,
        page_start=min(page_nos) if page_nos else None,
        page_end=max(page_nos) if page_nos else None,
        block_ids=tuple(unit.block_id for unit in units),
    )


def chunk_blocks(
    blocks: list[StoredBlock], params: ChunkingParams
) -> tuple[Chunk, ...]:
    """把已落库解析块序列转换为父子切片结构

    转换规则：素材按章节路径的连续段分组（父切片不跨章节路径，同
    路径中断后重现视为新段）；段内按父预算聚合父切片；父内素材按
    子预算聚合子切片。切片序号父子共用连续计数，父先于其子，子
    切片引用父序号。相同块序列与相同参数必然得到逐字段一致的结果。

    :param blocks: 已落库解析块（按序号升序）
    :param params: 切片参数（父/子字符预算）
    :return: 父先子后的切片序列（序号 0 起连续）
    """
    chunks: list[Chunk] = []
    ordinal = 0
    runs: list[tuple[str | None, list[_Unit]]] = []
    for section_path, unit in _units_from_blocks(blocks):
        if runs and runs[-1][0] == section_path:
            runs[-1][1].append(unit)
        else:
            runs.append((section_path, [unit]))

    for section_path, run_units in runs:
        for parent_units in _aggregate(run_units, params.parent_chunk_chars):
            parent_ordinal = ordinal
            chunks.append(
                _build_chunk(ordinal, None, section_path, parent_units)
            )
            ordinal += 1
            for child_units in _aggregate(parent_units, params.child_chunk_chars):
                chunks.append(
                    _build_chunk(ordinal, parent_ordinal, section_path, child_units)
                )
                ordinal += 1
    return tuple(chunks)
