"""评级(retro/README.md「评级」)：由程序按「影响大小 × 出现次数」计算，不调用模型。

| 评级 | 条件 |
|---|---|
| P0 | 导致整轮失败、卡死或需要用户救场(影响 severe)，且出现次数达到 repeat |
| P1 | 只出现一次的 severe；明显的大量浪费、误判(major)；或偶发的问题出现次数达到 frequent(频繁返工) |
| P2 | 偶发的浪费或失败(minor) |
| P3 | 小问题、措辞、体验，及按设计停下的关卡(trivial) |
"""

from __future__ import annotations

from dataclasses import dataclass

from tightrein.retro.records import Impact, Record
from tightrein.settings.load import Settings

SECTION = "retro"


@dataclass(frozen=True)
class RatingRule:
    repeat: int  # severe 反复出现(P0)的次数
    frequent: int  # minor 算作频繁(P1)的次数

    @classmethod
    def from_settings(cls, settings: Settings) -> RatingRule:
        values = settings.section(SECTION)
        return cls(repeat=int(values["repeatCount"]), frequent=int(values["frequentCount"]))


def rate(record: Record, rule: RatingRule) -> str:
    impact, count = record.impact, record.count
    if impact is Impact.SEVERE:
        return "P0" if count >= rule.repeat else "P1"
    if impact is Impact.MAJOR:
        return "P1"
    if impact is Impact.MINOR:
        return "P1" if count >= rule.frequent else "P2"
    return "P3"
