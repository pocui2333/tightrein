"""证伪复核：只在高风险时触发，两次结论按合并表定最终判定。

触发：预估 P0 或取证给出 P0 的问题被判为不成立(要直接关掉、不进修复，后面没有检查兜底)；判成立的只在严重度、
任务类型或影响类别命中配置(controls.assess.refute)时复核，缺省三项都为空，即不复核：成立与否交给修复后的验证
(复现测试先失败、修复后通过)，那是确定性证据，比再请一个模型读一遍代码可靠(换厂商的模型复核，错误相关性仍高)，
也不再让每个高危问题在评估里多花十几分钟。证据不足不复核。
合并表：
- 取证成立：复核也成立才采用取证；复核不成立、证据不足或没通过检查时保留取证的判定并转人工；
- 取证不成立：复核也不成立才算不成立；复核判成立就采用复核并转人工；复核证据不足就判证据不足并转人工。
复核是盲审(见 prompts/claim_verifier.py)，模型与取证不同(settings 的 independence 检查)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CONFIRMING = frozenset({"confirmed", "conditional"})
REFUTED = "refuted"
INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class RefuteRule:
    severities: frozenset[str]
    task_types: frozenset[str]
    impact_kinds: frozenset[str]

    @classmethod
    def from_settings(cls, section: Mapping[str, Any]) -> RefuteRule:
        rule = section["refute"]
        return cls(frozenset(rule["severities"]), frozenset(rule["taskTypes"]), frozenset(rule["impactKinds"]))


def needed(rule: RefuteRule, verdict: str, *, p0: bool, severity: str | None, task_type: str | None,
           impact_kind: str | None) -> bool:
    if verdict == REFUTED:
        # 判不成立的 P0(预估或取证给的严重度)都要复核：去向那一步要求 P0 的不成立须经复核且复核也不成立
        return p0 or severity == "P0"
    if verdict not in CONFIRMING:
        return False
    return severity in rule.severities or task_type in rule.task_types or impact_kind in rule.impact_kinds


def combine(first: str, second: str | None) -> tuple[str, bool]:
    """返回(最终判定, 是否转人工)；second 为空表示复核没有通过检查。"""
    if first in CONFIRMING:
        return first, second not in CONFIRMING
    if first != REFUTED:
        raise ValueError(f"证据不足的结论不做证伪复核：{first}")
    if second == REFUTED:
        return REFUTED, False
    if second in CONFIRMING:
        return str(second), True
    return INSUFFICIENT, True
