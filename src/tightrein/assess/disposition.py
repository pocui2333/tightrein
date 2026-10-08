"""处理方式与去向。

- 处理方式(fix_now 立即修、fix_later 排期修、watch 观察、wont_fix 不修)用配置中的决策树(controls.assess.treatment)：
  按顺序取第一条命中的规则，每个条件为空表示不限；最后一条必须不带条件作为兜底，没有命中就报错，不悄悄给缺省值。
  项目改分支只改配置。上一次判为观察、这次又判为观察的升为排期修，不会永远停在观察；
- 去向的判定顺序：转人工 → 证据不足(先转观察，连续几次仍不足才转人工)→ 不成立(P0 须经复核且复核也不成立)→
  主干上已修(等部署)→ P0 一律建 Issue → 已接受的取舍 → 按处理方式。P0 排在取舍之前，不会被取舍吞掉；
- 预估改动命中受保护路径时，Issue 带「需要先与代码作者讨论」标签。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tightrein.assess.refute import CONFIRMING, INSUFFICIENT, REFUTED

TREATMENTS = ("fix_now", "fix_later", "watch", "wont_fix")
CONDITION_KEYS = ("verdicts", "severities", "sizes", "worth")
DISCUSS = "discuss_with_author"


class Destination(StrEnum):
    MANUAL = "manual"  # 转人工：问题保持原状态并带 manual 标记
    WATCH_EVIDENCE = "watch_evidence"  # 证据不足：问题转 watching，再出现时带新证据重新评估
    FALSE_POSITIVE = "false_positive"  # 不成立：问题关闭并生成抑制规则
    MERGED = "merged"  # 与其他问题同一根因，已并入
    AWAITING_DEPLOY = "awaiting_deploy"  # 主干上已修，等部署后由采集判为已解决
    ISSUE = "issue"
    TRADEOFF = "tradeoff"  # 命中已接受的取舍
    WATCH = "watch"  # 观察：问题忽略到再出现 N 次或严重度升级
    WONT_FIX = "wont_fix"


# 去向 → 44c 的 disposition(四种处理方式之一)
DISPOSITION = {
    Destination.MANUAL: "watch", Destination.WATCH_EVIDENCE: "watch", Destination.AWAITING_DEPLOY: "watch",
    Destination.WATCH: "watch", Destination.FALSE_POSITIVE: "wont_fix", Destination.MERGED: "wont_fix",
    Destination.TRADEOFF: "wont_fix", Destination.WONT_FIX: "wont_fix",
}


class TreatmentTreeInvalid(ValueError):
    pass


@dataclass(frozen=True)
class TreatmentRule:
    treatment: str
    verdicts: frozenset[str] = frozenset()
    severities: frozenset[str] = frozenset()
    sizes: frozenset[str] = frozenset()
    worth: frozenset[str] = frozenset()

    @property
    def unconditional(self) -> bool:
        return not (self.verdicts or self.severities or self.sizes or self.worth)

    def matches(self, verdict: str, severity: str | None, size: str | None, worth: str | None) -> bool:
        return all(not allowed or value in allowed for allowed, value in (
            (self.verdicts, verdict), (self.severities, severity), (self.sizes, size), (self.worth, worth)))


@dataclass(frozen=True)
class Facts:
    verdict: str
    severity: str | None = None
    size: str | None = None
    worth: str | None = None
    fixed_on_main: bool = False
    tradeoff_hit: bool = False
    needs_manual: bool = False
    refuter_verdict: str | None = None
    insufficient_count: int = 0  # 含本次在内，连续判为证据不足的次数
    insufficient_limit: int = 3
    observed_before: bool = False
    touches_protected: bool = False


@dataclass(frozen=True)
class Decision:
    destination: Destination
    treatment: str | None = None
    labels: tuple[str, ...] = ()

    @property
    def disposition(self) -> str:
        if self.destination is Destination.ISSUE:
            return self.treatment or "fix_later"
        return DISPOSITION[self.destination]


def tree(items: Sequence[Mapping[str, Any]]) -> list[TreatmentRule]:
    rules = []
    for index, item in enumerate(items):
        if item.get("treatment") not in TREATMENTS:
            raise TreatmentTreeInvalid(f"controls.assess.treatment[{index}].treatment 只能是 {'、'.join(TREATMENTS)}")
        rules.append(TreatmentRule(item["treatment"], *(frozenset(item.get(key) or ()) for key in CONDITION_KEYS)))
    if not rules or not rules[-1].unconditional:
        raise TreatmentTreeInvalid("controls.assess.treatment 的最后一条必须不带条件，作为兜底")
    return rules


def treatment(rules: Sequence[TreatmentRule], verdict: str, severity: str | None, size: str | None,
              worth: str | None) -> str:
    for rule in rules:
        if rule.matches(verdict, severity, size, worth):
            return rule.treatment
    raise TreatmentTreeInvalid("controls.assess.treatment 没有命中任何规则，最后一条应不带条件作为兜底")


def decide(facts: Facts, rules: Sequence[TreatmentRule]) -> Decision:
    if facts.needs_manual:
        return Decision(Destination.MANUAL)
    if facts.verdict == INSUFFICIENT:
        enough = facts.insufficient_count >= facts.insufficient_limit
        return Decision(Destination.MANUAL if enough else Destination.WATCH_EVIDENCE)
    if facts.verdict == REFUTED:
        if facts.severity == "P0" and facts.refuter_verdict != REFUTED:
            raise ValueError("P0 问题判为不成立须经证伪复核，且复核同样判为不成立")
        return Decision(Destination.FALSE_POSITIVE)
    if facts.verdict not in CONFIRMING:
        raise ValueError(f"未知的判定：{facts.verdict}")
    labels = (DISCUSS,) if facts.touches_protected else ()
    if facts.fixed_on_main:
        return Decision(Destination.AWAITING_DEPLOY)
    if facts.severity == "P0":
        return Decision(Destination.ISSUE, "fix_now", labels)
    if facts.tradeoff_hit:
        return Decision(Destination.TRADEOFF)
    chosen = treatment(rules, facts.verdict, facts.severity, facts.size, facts.worth)
    if chosen == "watch" and facts.observed_before:
        chosen = "fix_later"
    if chosen in ("fix_now", "fix_later"):
        return Decision(Destination.ISSUE, chosen, labels)
    return Decision(Destination.WATCH if chosen == "watch" else Destination.WONT_FIX, chosen)
