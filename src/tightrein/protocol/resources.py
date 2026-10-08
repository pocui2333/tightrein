"""资源(protocol/resources.md)：订阅额度、每个 Issue 的用量上限、并发。

按订阅额度管，不按美元：两个工具都按订阅计费，真正的限制是 5 小时窗口与每周额度。官方没有查询剩余额度的接口，
只读工具输出中的信号(claude 的 rate_limit_event、额度用完的报错)，存在 state 表，跨运行有效。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta

from tightrein.agents.result import RateLimit
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.naming import Clock, format_iso, parse_iso
from tightrein.settings.load import Settings
from tightrein.store.tables import counters, state

QUOTA_KEY = "quota"
WEEKLY_WINDOWS = frozenset({"weekly", "opus", "sonnet"})  # 每周额度(含分模型的)，按每周的余量门槛
RATES = frozenset({"platformRps"})  # resources.concurrency 中按速率而非并发数计的项


class Quota:
    """state 表 `quota` = {工具: {窗口: {status, usedRatio, resetsAt, seenAt}}}；新信号覆盖同一窗口的旧信号。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, settings: Settings) -> None:
        self.conn = conn
        self.clock = clock
        self.reserve_five_hour = float(settings.get("resources.quota.reserveFiveHour"))
        self.reserve_weekly = float(settings.get("resources.quota.reserveWeekly"))
        self.unknown_wait = timedelta(seconds=settings.duration("resources.quota.unknownResetWait"))

    def update(self, limits: Sequence[RateLimit]) -> None:
        if not limits:
            return
        data = state.get(self.conn, QUOTA_KEY) or {}
        seen = format_iso(self.clock.now())
        for item in limits:
            data.setdefault(item.tool, {})[item.window] = {
                "status": item.status, "usedRatio": item.used_ratio, "resetsAt": item.resets_at, "seenAt": seen}
        state.put(self.conn, QUOTA_KEY, data, self.clock)

    def current(self) -> list[RateLimit]:
        """还没到重置时间的信号；重置时间不明的，看到之后 unknownResetWait 内有效。"""
        now = self.clock.now()
        found = []
        for tool, windows in (state.get(self.conn, QUOTA_KEY) or {}).items():
            for window, item in windows.items():
                if self._until(item) > now:
                    found.append(RateLimit(tool, window, item["status"], item["usedRatio"], item["resetsAt"]))
        return found

    def reserve_reached(self) -> bool:
        """给用户自己留余量：任一条件满足即不再开始新的 Issue(手上这一步做完)。"""
        return bool(self.reserve_reasons())

    def reserve_reasons(self) -> list[str]:
        reasons = []
        for item in self.current():
            limit = self.reserve_weekly if item.window in WEEKLY_WINDOWS else self.reserve_five_hour
            if item.status == "rejected" or (item.used_ratio is not None and item.used_ratio >= limit):
                used = "已用完" if item.status == "rejected" else f"已用 {item.used_ratio:.0%}"
                reasons.append(f"{item.tool} 的 {item.window} 额度{used}(留余量门槛 {limit:.0%})")
        return reasons

    def halted_until(self, tool: str | None = None) -> datetime | None:
        """额度用完的工具(不给则任一工具)到什么时候重置；没有用完的返回 None。"""
        moments = [self._until(item) for name, windows in (state.get(self.conn, QUOTA_KEY) or {}).items()
                   if tool is None or name == tool
                   for item in windows.values() if item["status"] == "rejected"]
        now = self.clock.now()
        moments = [moment for moment in moments if moment > now]
        return max(moments) if moments else None

    def _until(self, item: dict[str, object]) -> datetime:
        resets = item.get("resetsAt")
        if isinstance(resets, str):
            return parse_iso(resets)
        return parse_iso(str(item["seenAt"])) + self.unknown_wait


class IssueBudget:
    """每个对象(Issue 或问题)累计的 token，缓存读取按 cacheReadWeight(缺省 1/10)计；超过 issueTokens 即停下。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, settings: Settings) -> None:
        self.conn = conn
        self.clock = clock
        self.limit = float(settings.get("resources.issueTokens"))
        self.cache_read_weight = float(settings.get("resources.cacheReadWeight"))

    def add(self, subject: str, tokens: Tokens) -> float:
        return counters.add(self.conn, _budget(subject), tokens.weighted(self.cache_read_weight), self.clock)

    def used(self, subject: str) -> float:
        return counters.get(self.conn, _budget(subject))

    def remaining(self, subject: str) -> float:
        return self.limit - self.used(subject)

    def exceeded(self, subject: str) -> bool:
        return self.used(subject) >= self.limit


class Slots:
    """全局同时进行的模型调用、采集并行的来源等：每项一个信号量，数目取 resources.concurrency。"""

    def __init__(self, settings: Settings) -> None:
        concurrency = settings.get("resources.concurrency")
        self._semaphores = {name: threading.BoundedSemaphore(int(value)) for name, value in concurrency.items()
                            if name not in RATES}

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        with self._semaphores[name]:
            yield


def _budget(subject: str) -> str:
    return f"issue_tokens.{subject}"
