"""运行时限中「失败后怎么办」的唯一一处(protocol/limits.md)：按失败类型处理、退避与抖动、熔断、没有进展就停。

各模块只抛出或返回带类型的失败，不各自重试；重试只在这里决定，且只在一层(agents/call.py)执行，不层层叠加。
"""

from __future__ import annotations

import random as system_random
import sqlite3
from collections.abc import Callable, Sequence
from enum import StrEnum

from tightrein.agents.result import CallStatus
from tightrein.protocol.naming import Clock
from tightrein.settings.load import Settings
from tightrein.store.tables import counters

FORMAT_RETRIES = 1  # 格式不符：续接同一会话带原因重试 1 次


class Action(StrEnum):
    RETRY = "retry"  # 退避后重试同一次调用
    RESUME_WITH_REASON = "resume_with_reason"  # 续接同一会话，带上不合格的原因
    FALLBACK = "fallback"  # 换备用模型新开会话
    STOP = "stop"  # 停下，交给上层(复盘记一条)
    HALT_ALL = "halt_all"  # 订阅额度用完：全部停下，到重置时间再接着做


def decide(status: CallStatus, attempt: int, *, fallback_used: bool, settings: Settings) -> Action:
    """「按失败类型处理」表。attempt 为同一次 call 中这种状态第几次出现(从 1 起)。

    | 状态 | 处理 |
    |---|---|
    | 临时错误(限流、5xx、网络) | 退避重试，最多 limits.retry.attempts 次(缺省 2，共 3 次) |
    | 格式不符 | 续接同一会话带原因重试 1 次 |
    | 被拒绝、工具不可用 | 换备用模型 1 次，再失败就停 |
    | 订阅额度用完 | 全部停下；不换到另一个工具 |
    | 超时、轮数到限、费用到限、认证失败、越界、其余失败 | 停下(认证失败另由调用方触发依赖熔断) |
    """
    if status is CallStatus.TRANSIENT:
        return Action.RETRY if attempt <= int(settings.get("limits.retry.attempts")) else Action.STOP
    if status is CallStatus.SCHEMA_INVALID:
        return Action.RESUME_WITH_REASON if attempt <= FORMAT_RETRIES else Action.STOP
    if status in (CallStatus.REFUSED, CallStatus.UNAVAILABLE):
        return Action.STOP if fallback_used else Action.FALLBACK
    if status is CallStatus.QUOTA_EXHAUSTED:
        return Action.HALT_ALL
    return Action.STOP


def backoff_s(attempt: int, *, retry_after_s: float | None = None, overloaded: bool = False, settings: Settings,
              random: Callable[[], float] = system_random.random) -> float:
    """所有重试的等待时间都从这里取(模型调用、只读 git/gh 命令的临时错误)。attempt 为第几次重试(从 1 起)。

    全抖动：在 0 到 min(上限, 起始间隔 × 2^(attempt-1)) 之间随机，避免多个调用同时重试(AWS Builders' Library)。
    对方给了 retry-after 就照它等(Google SRE)；服务过载(529)时上限放宽到 overloadMax。"""
    if retry_after_s is not None:
        return retry_after_s
    base = settings.duration("limits.retry.base")
    cap = settings.duration("limits.retry.overloadMax" if overloaded else "limits.retry.max")
    return random() * min(cap, base * 2 ** (attempt - 1))


class Breaker:
    """依赖熔断与对象熔断，计数在 counters 表(跨进程、跨运行有效)。

    依赖熔断按连续失败计(调用量少，按比例统计用不上)：连续 dependencyFailures 次失败即打开，暂停 pause 后放一次
    试探调用；试探成功即关闭，失败再暂停。对象熔断：同一对象连续失败 objectFailures 次即不再处理它。
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock, settings: Settings) -> None:
        self.conn = conn
        self.clock = clock
        self.failures = int(settings.get("limits.breaker.dependencyFailures"))
        self.pause_s = settings.duration("limits.breaker.pause")
        self.object_failures = int(settings.get("limits.breaker.objectFailures"))

    def allow(self, dependency: str) -> bool:
        if counters.get(self.conn, _failures(dependency)) < self.failures:
            return True
        if self.clock.now().timestamp() - counters.get(self.conn, _opened(dependency)) < self.pause_s:
            return False
        self._open(dependency)  # 放一次试探：重新计时，试探结束前其余调用仍被挡住
        return True

    def record(self, dependency: str, ok: bool) -> None:
        if ok:
            counters.reset(self.conn, _failures(dependency), self.clock)
            counters.reset(self.conn, _opened(dependency), self.clock)
            return
        if counters.add(self.conn, _failures(dependency), 1, self.clock) >= self.failures:
            self._open(dependency)

    def trip(self, dependency: str) -> None:
        """立即打开(认证失败：再试也不会好，不必等满连续失败次数)。"""
        counters.reset(self.conn, _failures(dependency), self.clock)
        counters.add(self.conn, _failures(dependency), self.failures, self.clock)
        self._open(dependency)

    def object_failed(self, subject: str) -> bool:
        """记一次失败；返回 True 表示已到对象熔断，不再处理这个对象。"""
        return counters.add(self.conn, _object(subject), 1, self.clock) >= self.object_failures

    def object_progressed(self, subject: str) -> None:
        counters.reset(self.conn, _object(subject), self.clock)

    def _open(self, dependency: str) -> None:
        counters.reset(self.conn, _opened(dependency), self.clock)
        counters.add(self.conn, _opened(dependency), self.clock.now().timestamp(), self.clock)


def no_progress(previous: Sequence[tuple[str, str]], current: Sequence[tuple[str, str]], previous_diff: str | None,
                current_diff: str | None) -> bool:
    """没有进展就停：本轮 diff(哈希)与上一轮相同，或连续两轮阻断项(位置, 类型)相同。不耗满轮数。"""
    if previous_diff is not None and previous_diff == current_diff:
        return True
    return bool(current) and set(previous) == set(current)


def _failures(dependency: str) -> str:
    return f"breaker.dependency.{dependency}.failures"


def _opened(dependency: str) -> str:
    return f"breaker.dependency.{dependency}.opened"


def _object(subject: str) -> str:
    return f"breaker.object.{subject}"
