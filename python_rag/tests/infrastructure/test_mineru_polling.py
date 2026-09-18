# -*- coding: utf-8 -*-
"""轮询节奏测试：间隔序列、终态返回与超时策略（时钟可注入）"""
import pytest

from app.domain.errors import CloudTimeoutError
from app.infrastructure.mineru.dto import BatchPollResult, ProviderFileStatus
from app.infrastructure.mineru.polling import (
    PER_FILE_TIMEOUT_SECONDS,
    poll_batch_until_done,
)


class FakeClock:
    """可手动推进的单调时钟"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ScriptedClient:
    """按脚本逐次返回轮询快照的假客户端"""

    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)
        self.poll_count = 0

    def poll_batch(self, batch_id: str) -> BatchPollResult:
        self.poll_count += 1
        return self._snapshots[min(self.poll_count, len(self._snapshots)) - 1]


def _result(*statuses: ProviderFileStatus) -> BatchPollResult:
    return BatchPollResult(batch_id="batch-1", files=statuses)


def _status(source_ref: str, state: str) -> ProviderFileStatus:
    return ProviderFileStatus(source_ref=source_ref, state=state)


class TestPollSchedule:
    """轮询间隔与终态返回"""

    def test_returns_when_all_terminal_without_sleep(self):
        snapshots = [_result(_status("a", "done"), _status("b", "failed"))]
        sleeps: list[float] = []
        clock = FakeClock()
        statuses = poll_batch_until_done(
            ScriptedClient(snapshots),
            "batch-1",
            sleep=sleeps.append,
            clock=clock,
        )
        assert statuses["a"].state == "done"
        assert statuses["b"].state == "failed"
        assert sleeps == []

    def test_interval_transitions_from_fast_to_steady(self):
        # 恒为 running 的快照触发持续轮询：前段 3 秒，跨过快速窗口后 8 秒
        running = _result(_status("a", "running"))
        sleeps: list[float] = []
        clock = FakeClock()

        def advance(seconds: float) -> None:
            clock.now += seconds
            sleeps.append(seconds)

        with pytest.raises(CloudTimeoutError):
            poll_batch_until_done(
                ScriptedClient([running] * 500), "batch-1", sleep=advance, clock=clock
            )
        # 首分钟内为 3 秒间隔；跨过 60 秒窗口后为 8 秒间隔
        fast_count = sleeps.count(3)
        assert 18 <= fast_count <= 21
        assert 8 in sleeps
        assert all(interval == 8 for interval in sleeps[fast_count:])

    def test_timeout_terminates_polling(self):
        running = _result(_status("a", "running"))
        clock = FakeClock()

        def advance(seconds: float) -> None:
            clock.now += seconds

        with pytest.raises(CloudTimeoutError):
            poll_batch_until_done(
                ScriptedClient([running] * 1000), "batch-1", sleep=advance, clock=clock
            )
        # 超时恰在时限跨过时触发，不无限轮询
        assert clock.now <= PER_FILE_TIMEOUT_SECONDS + 10

    def test_on_poll_receives_snapshot_and_elapsed(self):
        snapshots = [
            _result(_status("a", "running")),
            _result(_status("a", "done")),
        ]
        seen: list[tuple[bool, float]] = []
        clock = FakeClock()

        def on_poll(result, elapsed: float) -> None:
            seen.append((result.files[0].state == "done", elapsed))

        poll_batch_until_done(
            ScriptedClient(snapshots),
            "batch-1",
            sleep=lambda seconds: setattr(clock, "now", clock.now + 3),
            clock=clock,
            on_poll=on_poll,
        )
        assert [done for done, _ in seen] == [False, True]
        assert seen[-1][1] >= 3
