# -*- coding: utf-8 -*-
"""解析路由策略：纯规则决策，不触碰文件系统与数据库

路由分两阶段：预路由在解析发生前按扩展名与用户偏好决定本地或
云端；解析后复核在本地解析产出扫描件信号时要求用户确认升级。
路由结果必须携带决策理由与配置版本并写入任务输入，保证任何一次
自动决策可追溯、可复现。
"""
from dataclasses import dataclass
from enum import StrEnum

from app.domain.errors import UnsupportedFormatError

# 路由配置版本：决策表语义变化时必须递增，使历史任务的路由依据可辨别
ROUTER_CONFIG_VERSION = "1"

# 用户可见的解析偏好取值
PARSER_PREFERENCE_VALUES = ("auto", "local", "mineru")


class ParserPreference(StrEnum):
    """用户声明的解析偏好"""

    AUTO = "auto"
    LOCAL = "local"
    MINERU = "mineru"


class ParserRouteMode(StrEnum):
    """预路由决策：本地解析或提交云端解析"""

    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class ParserRouteDecision:
    """预路由决策及其可追溯依据"""

    mode: ParserRouteMode
    reason: str
    router_config_version: str = ROUTER_CONFIG_VERSION


# 各扩展名的默认决策：文本与 Office 文档本地解析即可满足；
# 文本型 PDF 同样本地优先，扫描件在解析后经复核阶段升级确认。
# 云端解析的实际提交由其适配器负责，路由只做模式决策
_DEFAULT_DECISIONS: dict[str, ParserRouteDecision] = {
    "txt": ParserRouteDecision(
        mode=ParserRouteMode.LOCAL,
        reason="纯文本内容，本地解析即可",
    ),
    "md": ParserRouteDecision(
        mode=ParserRouteMode.LOCAL,
        reason="Markdown 内容，本地解析即可",
    ),
    "markdown": ParserRouteDecision(
        mode=ParserRouteMode.LOCAL,
        reason="Markdown 内容，本地解析即可",
    ),
    "docx": ParserRouteDecision(
        mode=ParserRouteMode.LOCAL,
        reason="Word 文档结构在本地解析器支持范围内",
    ),
    "pdf": ParserRouteDecision(
        mode=ParserRouteMode.LOCAL,
        reason="文本型 PDF 默认本地解析，扫描件信号在解析后复核",
    ),
}

# 用户明确指定云端解析时的决策理由
_MINERU_PREFERENCE_REASON = "用户明确指定云端解析"


def decide_parser_route(
    extension: str, preference: ParserPreference | str
) -> ParserRouteDecision:
    """按扩展名与用户偏好做出预路由决策

    偏好为 auto 时按默认决策表；明确指定 local/mineru 时服从用户
    选择（上传暂存已保证扩展名在白名单内，未知扩展名属于调用方
    违约，防御性拒绝）。

    :param extension: 暂存文件扩展名（不含点，小写）
    :param preference: 用户解析偏好
    :return: 路由决策（含理由与配置版本）
    :raises app.domain.errors.UnsupportedFormatError: 扩展名不在白名单
    """
    normalized = extension.lower().lstrip(".")
    decision = _DEFAULT_DECISIONS.get(normalized)
    if decision is None:
        raise UnsupportedFormatError(f"不支持的文件类型: .{normalized}")

    pref = ParserPreference(preference)
    if pref is ParserPreference.MINERU:
        return ParserRouteDecision(
            mode=ParserRouteMode.CLOUD, reason=_MINERU_PREFERENCE_REASON
        )
    if pref is ParserPreference.LOCAL:
        return ParserRouteDecision(
            mode=ParserRouteMode.LOCAL, reason="用户明确指定本地解析"
        )
    return decision


def requires_cloud_confirmation(
    *, scan_suspected: bool, preference: ParserPreference | str
) -> bool:
    """解析后复核：扫描件信号是否需要用户确认升级云端解析

    本地解析器无法从扫描件提取文本，继续本地路线没有产出，
    因此只要出现扫描件信号即要求确认；用户已指定云端解析时
    无需再确认。

    :param scan_suspected: 解析产物携带的扫描件信号
    :param preference: 用户解析偏好
    :return: 需要用户确认时为 True
    """
    return scan_suspected and ParserPreference(preference) is not ParserPreference.MINERU
