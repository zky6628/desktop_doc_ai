# -*- coding: utf-8 -*-
"""引用校验：模型引用草案到不可变快照

生成后（或流式结束）校验模型产出的引用：编号必须存在于上下文允许
集合，引文经空白规范化后必须来自对应块内容；页码/章节/文件/版本
等事实一律取服务端上下文块，绝不采信模型输出。通过校验的引用组装
为快照记录供持久化；未通过的引用记录拒绝状态（编号未知时文档事实
不可知），不作为有效引用返回。切片清理后快照行保留（存储层置空
切片指针），历史会话仍可完整展示。
"""
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.context import ContextBlock
from app.domain.parsing import canonical_json

# 校验状态与拒绝原因（机读，供评测导出与 UI 呈现）
STATE_VALIDATED = "validated"
STATE_REJECTED = "rejected"
REJECT_UNKNOWN_REFERENCE = "unknown_reference"
REJECT_QUOTE_MISMATCH = "quote_mismatch"

# 空白规范化：连续空白折叠为单空格并去首尾（引文比对口径）
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """空白规范化：连续空白折叠为单空格并去首尾"""
    return _WHITESPACE_RUN.sub(" ", text).strip()


@dataclass(frozen=True)
class CitationDraft:
    """模型产出的引用草案：引用编号与引文文本（两者均不可信）"""

    reference_id: str
    quoted_text: str = ""


@dataclass(frozen=True)
class CitationValidation:
    """单条引用的校验结论：通过时携带其上下文块（快照事实来源）

    编号未知时 block 为空（文档事实不可知）；引文不匹配时 block
    仍指向被引块（编号有效，仅引文不可信）
    """

    reference_id: str
    quoted_text: str
    state: str
    reason: str | None
    block: ContextBlock | None


@dataclass(frozen=True)
class CitationRecord:
    """待持久化的引用快照行（校验结论与候选分数的组装产物）

    citation_id 由仓储写入时生成、读取时回填，组装阶段为空
    """

    citation_id: str | None = None
    assistant_message_id: str = ""
    citation_order: int = 0
    chunk_id: str | None = None
    knowledge_base_id_snapshot: str = ""
    document_id_snapshot: str | None = None
    document_version_id_snapshot: str | None = None
    file_name_snapshot: str | None = None
    version_no_snapshot: int | None = None
    quoted_text_snapshot: str = ""
    content_snapshot: str | None = None
    page_no: int | None = None
    section_path: str | None = None
    source_locator_json: str | None = None
    validation_state: str = STATE_VALIDATED
    query_run_id: str | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None


# 行内引用标记：[S编号]（首次出现按序去重）
_CITATION_MARKER = re.compile(r"\[(S\d+)\]")


def extract_citation_drafts(answer_text: str) -> list[CitationDraft]:
    """从回答全文提取引用草案（流式结束后调用）

    按标记在正文中首次出现的位置排序去重；引文文本留空（仅标注编号
    口径，引文比对机制保留待后续迭代开启）

    :param answer_text: 累积的模型回答全文
    :return: 引用草案序列（无标记时为空列表）
    """
    drafts: list[CitationDraft] = []
    seen: set[str] = set()
    for match in _CITATION_MARKER.finditer(answer_text):
        reference_id = match.group(1)
        if reference_id in seen:
            continue
        seen.add(reference_id)
        drafts.append(CitationDraft(reference_id=reference_id))
    return drafts


def validate_citations(
    drafts: Sequence[CitationDraft],
    blocks: Sequence[ContextBlock],
) -> list[CitationValidation]:
    """校验模型引用草案（规则见模块说明）

    空引文视为仅标注编号的引用（无引文可验证即通过，快照记空文本）；
    重复编号按草案逐条校验，去重归生成链路负责

    :param drafts: 模型产出的引用草案序列
    :param blocks: 上下文允许引用集合
    :return: 与草案同序的校验结论序列
    """
    by_reference = {block.reference_id: block for block in blocks}
    validations: list[CitationValidation] = []
    for draft in drafts:
        quoted = normalize_whitespace(draft.quoted_text)
        block = by_reference.get(draft.reference_id)
        if block is None:
            validations.append(
                CitationValidation(
                    reference_id=draft.reference_id,
                    quoted_text=quoted,
                    state=STATE_REJECTED,
                    reason=REJECT_UNKNOWN_REFERENCE,
                    block=None,
                )
            )
            continue
        if quoted and quoted not in normalize_whitespace(block.content):
            validations.append(
                CitationValidation(
                    reference_id=draft.reference_id,
                    quoted_text=quoted,
                    state=STATE_REJECTED,
                    reason=REJECT_QUOTE_MISMATCH,
                    block=block,
                )
            )
            continue
        validations.append(
            CitationValidation(
                reference_id=draft.reference_id,
                quoted_text=quoted,
                state=STATE_VALIDATED,
                reason=None,
                block=block,
            )
        )
    return validations


def build_citation_record(
    validation: CitationValidation,
    *,
    assistant_message_id: str,
    citation_order: int,
    knowledge_base_id: str,
    query_run_id: str | None = None,
    vector_score: float | None = None,
    keyword_score: float | None = None,
    fusion_score: float | None = None,
    rerank_score: float | None = None,
) -> CitationRecord:
    """把校验结论组装为待持久化快照行

    文档事实与正文快照取自上下文块（编号未知时为空）；来源定位以
    规范 JSON 记录该切片链接的解析块主键，使快照自包含

    :param validation: 引用校验结论
    :param assistant_message_id: 关联的助手消息主键
    :param citation_order: 消息内引用序号（1 起）
    :param knowledge_base_id: 查询范围知识库主键（查询范围事实）
    :param query_run_id: 查询运行主键（评测关联）
    :param vector_score 等: 该引用对应候选的四类分数快照
    :return: 待持久化的快照行
    """
    block = validation.block
    locator_json = None
    if block is not None and block.block_ids:
        locator_json = canonical_json({"block_ids": list(block.block_ids)})
    return CitationRecord(
        assistant_message_id=assistant_message_id,
        citation_order=citation_order,
        chunk_id=block.chunk_id if block is not None else None,
        knowledge_base_id_snapshot=knowledge_base_id,
        document_id_snapshot=block.document_id if block is not None else None,
        document_version_id_snapshot=(
            block.document_version_id if block is not None else None
        ),
        file_name_snapshot=block.file_name if block is not None else None,
        version_no_snapshot=block.version_no if block is not None else None,
        quoted_text_snapshot=validation.quoted_text,
        content_snapshot=block.content if block is not None else None,
        page_no=block.page_no if block is not None else None,
        section_path=block.section_path if block is not None else None,
        source_locator_json=locator_json,
        validation_state=validation.state,
        query_run_id=query_run_id,
        vector_score=vector_score,
        keyword_score=keyword_score,
        fusion_score=fusion_score,
        rerank_score=rerank_score,
    )
