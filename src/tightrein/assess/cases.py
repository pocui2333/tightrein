"""分情况：同样是「判断问题是否存在」，按采集时已经知道多少决定做多少。

| 情况 | 例子 | 怎么做 |
|---|---|---|
| 已取证(verified) | 静态巡检中已由模型逐条取证成立的 | 复用采集时的取证，不再从零调查，只补报告与评估(assess.confirm) |
| 能复现(reproducible) | API 模糊测试的 5xx，去重时已重放确认 | 确定存在，只读代码找出错位置，不再证明存在 |
| 完整取证(full) | 确定性来源但没取证过的、预估 P0 的 | 四步取证(找到代码、读懂逻辑、找触发条件、找反证) |
| 轻量推测(light) | 偶发的运行时报错、日志中的异常 | 只看报错指向的代码，推测触发条件；不做反证与证伪 |

已取证：采集时的取证判成立或条件成立、带代码事实与反证的才算。输出已带报告与评估的直接复用(仍要通过证据检查，
见 evidence.py)；只有取证结论的(静态巡检的取证不写报告与评估)交给 assess.confirm，拿着这份证据与程序截取的
当前代码补报告与评估，不再从零调查。用户请求的重新评估：带了补充信息的不复用，重新取证(补充的信息可能推翻采集时的
结论)；没带的照样复用(多半是转人工之后想再自动评估一次，代码可能已经变了，确认会对照当前代码)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from tightrein.assess.claims import severity_hint, signal_evidence
from tightrein.store.tables.occurrences import Occurrence

PREPARED_KEY = "verification"
PREPARED_REQUIRED = ("facts", "counterEvidence")
PREPARED_VERDICTS = frozenset({"confirmed", "conditional"})
COMPLETE_REQUIRED = ("report", "assessment")


class Case(StrEnum):
    VERIFIED = "verified"
    REPRODUCIBLE = "reproducible"
    FULL = "full"
    LIGHT = "light"


def latest(found: Sequence[Occurrence]) -> Occurrence:
    return max(found, key=lambda item: (item.seen_at, item.id or 0))


def classify(found: Sequence[Occurrence], *, retriage: bool, user_notes: bool = False) -> Case:
    item = latest(found)
    if not (retriage and user_notes) and item.evidence.get("verified") and prepared(item) is not None:
        return Case.VERIFIED
    if any(occurrence.evidence.get("reproducible") for occurrence in found):
        return Case.REPRODUCIBLE
    if item.evidence.get("deterministic") or severity_hint(item) == "P0" or retriage:
        return Case.FULL
    return Case.LIGHT


def prepared(item: Occurrence) -> Mapping[str, Any] | None:
    """采集时已有的取证输出：判成立或条件成立、带代码事实与反证的才复用。"""
    value = signal_evidence(item).get(PREPARED_KEY)
    if not isinstance(value, Mapping) or value.get("verdict") not in PREPARED_VERDICTS \
            or any(not value.get(key) for key in PREPARED_REQUIRED):
        return None
    return value


def complete(value: Mapping[str, Any]) -> bool:
    """取证输出已带报告与评估：可以不调用模型直接用。"""
    return all(value.get(key) for key in COMPLETE_REQUIRED)
