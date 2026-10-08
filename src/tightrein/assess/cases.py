"""分情况：同样是「判断问题是否存在」，按采集时已经知道多少决定做多少。

| 情况 | 例子 | 怎么做 |
|---|---|---|
| 已取证(verified) | 静态巡检中已由模型逐条取证成立的 | 复用采集时的取证，跳过判断，只定严重度与去向 |
| 能复现(reproducible) | API 模糊测试的 5xx，去重时已重放确认 | 确定存在，只读代码找出错位置，不再证明存在 |
| 完整取证(full) | 确定性来源但没取证过的、预估 P0 的 | 四步取证(找到代码、读懂逻辑、找触发条件、找反证) |
| 轻量推测(light) | 偶发的运行时报错、日志中的异常 | 只看报错指向的代码，推测触发条件；不做反证与证伪 |

已取证的只在采集时的输出带报告、分析与评估时才复用(且之后还要通过证据检查，见 evidence.py)；用户请求的
重新评估一律不复用，重新取证。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from tightrein.assess.claims import severity_hint, signal_evidence
from tightrein.store.tables.occurrences import Occurrence

PREPARED_KEY = "verification"
PREPARED_REQUIRED = ("analysis", "report", "assessment")


class Case(StrEnum):
    VERIFIED = "verified"
    REPRODUCIBLE = "reproducible"
    FULL = "full"
    LIGHT = "light"


def latest(found: Sequence[Occurrence]) -> Occurrence:
    return max(found, key=lambda item: (item.seen_at, item.id or 0))


def classify(found: Sequence[Occurrence], *, retriage: bool) -> Case:
    item = latest(found)
    if not retriage and item.evidence.get("verified") and prepared(item) is not None:
        return Case.VERIFIED
    if any(occurrence.evidence.get("reproducible") for occurrence in found):
        return Case.REPRODUCIBLE
    if item.evidence.get("deterministic") or severity_hint(item) == "P0" or retriage:
        return Case.FULL
    return Case.LIGHT


def prepared(item: Occurrence) -> Mapping[str, Any] | None:
    """采集时已有的取证输出；缺少分析、报告或评估的(旧格式)不复用。"""
    value = signal_evidence(item).get(PREPARED_KEY)
    if not isinstance(value, Mapping) or any(not value.get(key) for key in PREPARED_REQUIRED):
        return None
    return value
