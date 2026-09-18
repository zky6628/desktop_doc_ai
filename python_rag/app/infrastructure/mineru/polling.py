# -*- coding: utf-8 -*-
"""批量轮询节奏与超时策略

供应方结果查询按固定节奏执行：提交后的首分钟内每 3 秒一次，
之后每 8 秒一次；单个文件自轮询开始起 20 分钟仍未进入终态即
超时失败。sleep 与时钟可注入，节奏与超时行为可独立测试。
"""
import time
from collections.abc import Callable

from app.domain.errors import CloudTimeoutError
from app.infrastructure.mineru.dto import BatchPollResult, ProviderFileStatus

# 首分钟内的轮询间隔与快速窗口时长（秒）
POLL_INTERVAL_INITIAL_SECONDS = 3
POLL_FAST_WINDOW_SECONDS = 60
# 快速窗口之后的稳定轮询间隔（秒）
POLL_INTERVAL_STEADY_SECONDS = 8
# 单文件轮询超时（秒）：自轮询开始起 20 分钟
PER_FILE_TIMEOUT_SECONDS = 20 * 60


def next_poll_interval(elapsed_seconds: float) -> int:
    """按已流逝时间计算下一次轮询间隔

    :param elapsed_seconds: 自轮询开始起的流逝秒数
    :return: 下一次轮询前应等待的秒数
    """
    if elapsed_seconds < POLL_FAST_WINDOW_SECONDS:
        return POLL_INTERVAL_INITIAL_SECONDS
    return POLL_INTERVAL_STEADY_SECONDS


def poll_batch_until_done(
    client,
    batch_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_poll: Callable[[BatchPollResult, float], None] | None = None,
) -> dict[str, ProviderFileStatus]:
    """轮询批次直到全部文件进入终态或触发超时

    每次轮询后通过回调暴露状态快照与流逝时间，供持久化层记录
    轮询事实；全部文件 done/failed 即返回，任一文件超过时限
    则按超时失败终止。

    :param client: 提供 poll_batch 的适配器客户端
    :param batch_id: 批次引用
    :param sleep: 等待函数（测试可注入空实现）
    :param clock: 单调时钟（测试可注入推进函数）
    :param on_poll: 每次轮询后的回调（快照, 流逝秒）
    :return: 源引用到终态状态的映射
    :raises CloudTimeoutError: 任一文件超过轮询时限仍未终态
    """
    started = clock()
    while True:
        result = client.poll_batch(batch_id)
        elapsed = clock() - started
        if on_poll is not None:
            on_poll(result, elapsed)
        statuses = {status.source_ref: status for status in result.files}
        for status in statuses.values():
            if not status.is_terminal and elapsed > PER_FILE_TIMEOUT_SECONDS:
                raise CloudTimeoutError("云端解析轮询超时")
        if all(status.is_terminal for status in statuses.values()):
            return statuses
        sleep(next_poll_interval(elapsed))
