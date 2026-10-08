"""按时间窗口增量读取(平台错误、访问日志共用)：起点接着上次保存的终点，读取位置随信号同事务保存。

- 起点为上次保存的终点；没有记录时从终点往前回看 initialLookback；终点为现在(去掉秒以下)；
- 起点取 `min(上次终点, 现在)`：本机时钟回拨时窗口不倒置，只是这次读一个空窗口；
- 读满条数上限时读取位置停在实际读到的最后时间(与窗口终点取较早的)，没读到的部分下次接着读，不跳过；
- 平台能查到的最早时间晚于窗口起点时写「可能漏读」，提醒两次运行的间隔要短于平台保留期；
- 读取位置只经 SourceResult.state 返回，由去重在写信号的同一个事务里保存；读取失败或回滚时位置不前进。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tightrein.protocol.naming import format_iso, parse_iso
from tightrein.store.tables import state

UNTIL = "until"
PARSE_STATE = "parseState"
# 平台给的秒以下部分可能多于 6 位(Alertmanager 给 9 位纳秒)，fromisoformat 解析不了，先去掉
_FRACTION = re.compile(r"\.\d+(?=(Z|[+-]\d{2}:?\d{2})?$)")


@dataclass(frozen=True)
class Window:
    key: str  # state 表中的键：<来源控制键>:<子来源>
    since: datetime
    until: datetime
    previous: dict[str, Any] | None  # 上次保存的读取位置；第一次为 None

    def describe(self) -> str:
        return f"{format_iso(self.since)} 到 {format_iso(self.until)}"

    @property
    def parse_state(self) -> dict[str, Any] | None:
        """上次日志解析留下的状态(只有时刻的写法靠它补日期)，原样交回解析方法。"""
        if self.previous is None:
            return None
        found = self.previous.get(PARSE_STATE)
        return dict(found) if isinstance(found, Mapping) else None

    def gap(self, oldest: datetime | None) -> str | None:
        """平台的最早可查时间晚于窗口起点时的说明。"""
        if oldest is None or oldest <= self.since:
            return None
        return (f"可能漏读：{self.key} 在 {format_iso(self.since)} 到 {format_iso(oldest)} 之间的内容已超出平台保留期；"
                "两次运行的间隔应短于保留期")

    def advance(self, *, reached: datetime | None = None, parse_state: Mapping[str, Any] | None = None,
                extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """要保存的读取位置。reached 给出时(读满上限，只读到窗口中间)停在它与窗口终点中较早的一个。"""
        end = self.until if reached is None else min(reached, self.until)
        value: dict[str, Any] = {UNTIL: format_iso(end), **(extra or {})}
        if parse_state is not None:
            value[PARSE_STATE] = dict(parse_state)
        return value


def plan(conn: sqlite3.Connection, key: str, now: datetime, lookback_s: float) -> Window:
    previous = state.get(conn, key)
    until = now.replace(microsecond=0)
    if isinstance(previous, Mapping) and previous.get(UNTIL):
        since = parse_iso(previous[UNTIL])
    else:
        previous = None
        since = until - timedelta(seconds=lookback_s)
    return Window(key, min(since, until), until, None if previous is None else dict(previous))


def state_key(source: str, part: str | None = None) -> str:
    """读取位置在 state 表中的键：一个来源有几个子来源就各用一个。"""
    return source if part is None else f"{source}:{part}"


def oldest_available(now: datetime, retention_days: int) -> datetime:
    """按平台的保留天数估算还能查到的最早时间。"""
    return now.replace(microsecond=0) - timedelta(days=retention_days)


def platform_time(value: str) -> datetime:
    """平台给出的带时区时间 → UTC，去掉秒以下的部分；解析不了抛 ValueError。"""
    return parse_iso(_FRACTION.sub("", value))
