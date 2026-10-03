"""证伪复核的触发条件与两次结论的合并(architecture/06 4.7，redesign/03-triage.md)。

只在高风险时复核：取证判为成立或条件成立，且严重度、任务类型或影响类别命中 triage.refute 的任一项；或预估为 P0
(越权检查失败)而取证判为不成立。其余由修复前的复现测试兜底。
合并规则：
- 取证判为成立或条件成立：复核同样判为成立或条件成立时采用取证；复核判为不成立或证据不足(或复核没有通过证据检查，
  second 为空)时保留取证的判定并转人工；
- 取证判为不成立：复核也判为不成立才算不成立；复核判为成立或条件成立时采用复核并转人工；复核证据不足时判为证据不足
  并转人工。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import ImpactKind, Severity, TaskType, Verdict

CONFIRMING = frozenset({Verdict.CONFIRMED, Verdict.CONDITIONAL})


@dataclass(frozen=True)
class RefuteRule:
    severities: frozenset[Severity]
    task_types: frozenset[TaskType]
    impact_kinds: frozenset[ImpactKind]

    @classmethod
    def from_config(cls, config: ProjectConfig) -> RefuteRule:
        return cls(frozenset(Severity(item) for item in config.get("triage.refute.severities")),
                   frozenset(TaskType(item) for item in config.get("triage.refute.taskTypes")),
                   frozenset(ImpactKind(item) for item in config.get("triage.refute.impactKinds")))


def _hit(allowed: Collection[object], value: object | None) -> bool:
    return value is not None and value in allowed


def needed(rule: RefuteRule, verdict: Verdict, *, p0: bool, severity: Severity | None, task_type: TaskType | None,
           impact_kind: ImpactKind | None) -> bool:
    if verdict is Verdict.REFUTED:
        return p0
    if verdict not in CONFIRMING:
        return False
    return _hit(rule.severities, severity) or _hit(rule.task_types, task_type) or _hit(rule.impact_kinds, impact_kind)


def combine(first: Verdict, second: Verdict | None) -> tuple[Verdict, bool]:
    """返回 (最终判定, 是否转人工)。"""
    if first in CONFIRMING:
        return first, second not in CONFIRMING
    if first is not Verdict.REFUTED:
        raise ValueError(f"证据不足的结论不做证伪复核：{first.value}")
    if second is Verdict.REFUTED:
        return Verdict.REFUTED, False
    if second in CONFIRMING:
        return second, True
    return Verdict.INSUFFICIENT, True
