"""抑制：命中抑制规则(以前判为误报)的信号标记掉，不再往下走。

规则两处来源：项目写在工作区 settings.json 的 controls."collect.dedup".suppress；评估判为误报时由 for_fingerprint
生成、add 存进 state 表(带到期日，不是永久抑制)。抑制在算指纹之前执行，指纹由调用方现算后传入。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from tightrein.collect.common.signals import Signal
from tightrein.protocol.naming import Clock, local_date
from tightrein.store.tables import state

STATE_KEY = "collect.dedup.suppress"


class SuppressionInvalid(ValueError):
    pass


@dataclass(frozen=True)
class SuppressionRule:
    """匹配条件只能二选一：指纹，或来源加消息正则。到期日当天仍有效，次日起失效。"""

    reason: str
    added_on: date
    expires_on: date
    fingerprint: str | None = None
    source: str | None = None
    message_pattern: re.Pattern[str] | None = None

    def __post_init__(self) -> None:
        by_fingerprint = self.fingerprint is not None and self.source is None and self.message_pattern is None
        by_message = self.fingerprint is None and self.source is not None and self.message_pattern is not None
        if not (by_fingerprint or by_message):
            raise SuppressionInvalid("抑制规则的匹配条件只能是指纹，或者来源加消息正则")
        if self.expires_on < self.added_on:
            raise SuppressionInvalid(f"到期日早于添加日：{self.expires_on}")

    @classmethod
    def for_fingerprint(cls, value: str, reason: str, today: date, days: int) -> SuppressionRule:
        """评估判为误报时的规则：到期日为当天加 suppressionDays。"""
        return cls(reason=reason, added_on=today, expires_on=today + timedelta(days=days), fingerprint=value)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SuppressionRule:
        match = data["match"]
        pattern = match.get("messagePattern")
        return cls(
            reason=data["reason"], added_on=date.fromisoformat(data["addedOn"]),
            expires_on=date.fromisoformat(data["expiresOn"]), fingerprint=match.get("fingerprint"),
            source=match.get("source"), message_pattern=re.compile(pattern) if pattern is not None else None,
        )

    def to_json(self) -> dict[str, Any]:
        match: dict[str, Any] = (
            {"fingerprint": self.fingerprint} if self.fingerprint is not None
            else {"source": self.source,
                  "messagePattern": self.message_pattern.pattern if self.message_pattern else None}
        )
        return {"match": match, "reason": self.reason, "addedOn": self.added_on.isoformat(),
                "expiresOn": self.expires_on.isoformat()}

    def active(self, today: date) -> bool:
        return today <= self.expires_on

    def matches(self, signal: Signal, fingerprint: str | None) -> bool:
        """fingerprint 为 None 的是回归信号：只能按消息匹配。"""
        if self.fingerprint is not None:
            return fingerprint is not None and self.fingerprint == fingerprint
        return self.source == signal.source and self.message_pattern is not None \
            and self.message_pattern.search(signal.message) is not None


def rules_from(items: Iterable[Mapping[str, Any]], where: str) -> tuple[SuppressionRule, ...]:
    """一次列出全部写错的规则，不跳过。"""
    rules, errors = [], []
    for index, item in enumerate(items):
        try:
            rules.append(SuppressionRule.from_json(item))
        except (KeyError, TypeError, ValueError, re.error) as error:
            errors.append(f"{where}[{index}]：{error}")
    if errors:
        raise SuppressionInvalid("；".join(errors))
    return tuple(rules)


def load(conn: sqlite3.Connection, configured: Iterable[Mapping[str, Any]]) -> tuple[SuppressionRule, ...]:
    """项目规则在前，评估生成的在后(第一条命中的生效)。"""
    return (rules_from(configured, 'controls."collect.dedup".suppress')
            + rules_from(state.get(conn, STATE_KEY) or [], STATE_KEY))


def add(conn: sqlite3.Connection, rule: SuppressionRule, clock: Clock) -> None:
    """评估判为误报时调用；同一指纹的旧规则被新规则替换。"""
    kept = [item for item in state.get(conn, STATE_KEY) or []
            if rule.fingerprint is None or item["match"].get("fingerprint") != rule.fingerprint]
    state.put(conn, STATE_KEY, [*kept, rule.to_json()], clock)


def match(signal: Signal, fingerprint: str | None, rules: Sequence[SuppressionRule],
          now: datetime) -> SuppressionRule | None:
    """第一条未到期且命中的规则；「今天」按本机时区(与评估生成规则时的日期一致)。"""
    today = local_date(now)
    for rule in rules:
        if rule.active(today) and rule.matches(signal, fingerprint):
            return rule
    return None


def apply(signals: Sequence[Signal], rules: Sequence[SuppressionRule], now: datetime,
          fingerprint_of: Callable[[Signal], str | None]) -> tuple[list[Signal], int]:
    """返回(仍要往下走的信号, 抑制掉的条数)。"""
    if not rules:
        return list(signals), 0
    remaining = [signal for signal in signals if match(signal, fingerprint_of(signal), rules, now) is None]
    return remaining, len(signals) - len(remaining)
