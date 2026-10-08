"""Issue 状态 → 推进它的阶段：只在这里定义一处，调度(protocol/schedule)、实施入口、命令行的展示与测试共用。

待修与实施中的由实施推进(同时只做一个，实施中的优先)；待发布与验收中的由发布推进；待决定、已完成、取消与手动接管
不在表中，不自动推进(带停止原因的 Issue 要先由用户确认，见 transitions.py 的 hold 与 APPROVE)。
"""

from __future__ import annotations

IMPLEMENT, RELEASE = "implement", "release"
# 同一阶段内按先后：前面的优先(实施中的先于待修)
STAGE_FOR_STATUS: dict[str, str] = {
    "implementing": IMPLEMENT,
    "todo": IMPLEMENT,
    "releasing": RELEASE,
    "accepting": RELEASE,
}


def stage_for(status: str | None) -> str | None:
    """该状态的 Issue 由哪个阶段推进；不自动推进的为 None。"""
    return STAGE_FOR_STATUS.get(status or "")


def statuses_for(stage: str) -> tuple[str, ...]:
    """某阶段推进的 Issue 状态，按优先顺序。"""
    return tuple(status for status, owner in STAGE_FOR_STATUS.items() if owner == stage)
