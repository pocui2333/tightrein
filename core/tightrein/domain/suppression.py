"""抑制规则的匹配(design 2.9)：按指纹，或按探针加消息正则；到期的规则不参与匹配。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from tightrein.domain.enums import Probe
from tightrein.domain.fingerprint import CURRENT_VERSION
from tightrein.domain.fingerprint import fingerprint as compute_fingerprint
from tightrein.domain.signal import Signal


@dataclass(frozen=True)
class SuppressionRule:
    """suppressions.yaml 中的一条规则。到期日当天仍有效，次日起失效。"""

    reason: str
    added_on: date
    expires_on: date
    fingerprint: str | None = None
    probe: Probe | None = None
    message_pattern: str | None = None

    def __post_init__(self) -> None:
        if self.fingerprint is not None:
            valid = self.probe is None and self.message_pattern is None
        else:
            valid = self.probe is not None and self.message_pattern is not None
        if not valid:
            raise ValueError("抑制规则的匹配条件只能是指纹，或者探针加消息正则")
        if self.expires_on < self.added_on:
            raise ValueError(f"到期日期早于添加日期：{self.expires_on}")
        if self.message_pattern is not None:
            re.compile(self.message_pattern)

    @classmethod
    def for_fingerprint(cls, value: str, reason: str, today: date, days: int) -> "SuppressionRule":
        """误报判定生成的规则：到期日期为当天加 days(thresholds.suppressionDays)。"""
        return cls(reason=reason, added_on=today, expires_on=today + timedelta(days=days), fingerprint=value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuppressionRule":
        match = data["match"]
        return cls(
            reason=data["reason"],
            added_on=date.fromisoformat(data["addedOn"]),
            expires_on=date.fromisoformat(data["expiresOn"]),
            fingerprint=match.get("fingerprint"),
            probe=Probe(match["probe"]) if "probe" in match else None,
            message_pattern=match.get("messagePattern"),
        )

    def to_dict(self) -> dict[str, Any]:
        if self.fingerprint is not None:
            match: dict[str, Any] = {"fingerprint": self.fingerprint}
        else:
            match = {"probe": self.probe.value if self.probe else None, "messagePattern": self.message_pattern}
        return {
            "match": match,
            "reason": self.reason,
            "addedOn": self.added_on.isoformat(),
            "expiresOn": self.expires_on.isoformat(),
        }

    def active(self, today: date) -> bool:
        return today <= self.expires_on

    def matches(self, signal: Signal, signal_fingerprint: str | None) -> bool:
        if self.fingerprint is not None:
            return self.fingerprint == signal_fingerprint
        return self.probe is signal.probe and re.search(str(self.message_pattern), signal.message) is not None


def match(
    signal: Signal, rules: Iterable[SuppressionRule], now: datetime, version: int = CURRENT_VERSION
) -> SuppressionRule | None:
    """返回第一条命中且未到期的规则。

    抑制在计算指纹之前执行，信号还没有指纹时按 version 现算；回归信号没有指纹，只能按消息匹配。
    """
    today = now.date()
    signal_fingerprint = signal.fingerprint or compute_fingerprint(signal, version)
    for rule in rules:
        if rule.active(today) and rule.matches(signal, signal_fingerprint):
            return rule
    return None
