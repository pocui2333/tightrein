"""第 0 步 分流(redesign/05-fix.md 第 2 节)：按任务类型、规模档与处理标签查流程表(orchestrator/policy/lanes.py)，
决定通道，写入 data/fixes/<编号>/route.json；之后的升档(A 转 B、计划重评为更大的档)只升不降，同样记在这里。

类型、档与处理标签依次取 Issue 头信息、第一个关联问题的分诊结论；用户需求的 Issue 没有类型时取 fix.manualTaskType，
分诊结论没有类型(旧结论)时按缺陷处理；档未知时按中档查表。超限时 lane 为空，由调用方转待决定。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import sizing
from tightrein.domain.enums import Lane, SizeTier, TaskType, Treatment
from tightrein.domain.issue import Issue
from tightrein.orchestrator.policy import lanes
from tightrein.store.files import atomic

FILE = "route.json"
LANE_ORDER = (Lane.FAST, Lane.STANDARD, Lane.LARGE)


@dataclass(frozen=True)
class Route:
    lane: Lane | None
    task_type: TaskType
    tier: SizeTier | None
    treatment: Treatment | None
    changes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def oversize(self) -> bool:
        return self.lane is None

    def to_dict(self) -> dict[str, Any]:
        return {"lane": None if self.lane is None else self.lane.value, "taskType": self.task_type.value,
                "tier": None if self.tier is None else self.tier.value,
                "treatment": None if self.treatment is None else self.treatment.value, "changes": list(self.changes)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Route:
        return cls(Lane(data["lane"]) if data["lane"] else None, TaskType(data["taskType"]),
                   SizeTier(data["tier"]) if data["tier"] else None,
                   Treatment(data["treatment"]) if data["treatment"] else None, tuple(data.get("changes") or ()))

    def text(self) -> str:
        lane = self.lane.label if self.lane is not None else "超限，不接"
        tier = self.tier.label if self.tier is not None else "未知"
        return f"{lane}(类型 {self.task_type.label}，规模档 {tier})"


def decide(config: ProjectConfig, issue: Issue, triaged: Mapping[str, Any]) -> Route:
    task_type = issue.task_type or (TaskType(triaged["taskType"]) if triaged.get("taskType") else None)
    if task_type is None:
        task_type = TaskType(config.get("fix.manualTaskType")) if issue.is_manual else TaskType.BUG
    tier = issue.size_tier or (SizeTier(triaged["sizeTier"]) if triaged.get("sizeTier") else None)
    treatment = issue.treatment or (Treatment(triaged["treatment"]) if triaged.get("treatment") else None)
    return Route(lanes.route(config, task_type, tier), task_type, tier, treatment)


def upgrade(config: ProjectConfig, route: Route, tier: SizeTier, reason: str, *,
            lane: Lane | None = None) -> Route:
    """按新的规模档重新查表(档只升不降)；lane 给出时至少升到该通道。超限时 lane 为空。"""
    new_tier = tier if route.tier is None else sizing.larger(route.tier, tier)
    found = lanes.route(config, route.task_type, new_tier)
    if found is not None and lane is not None and LANE_ORDER.index(lane) > LANE_ORDER.index(found):
        found = lane
    if found is not None and route.lane is not None and LANE_ORDER.index(route.lane) > LANE_ORDER.index(found):
        found = route.lane
    if (found, new_tier) == (route.lane, route.tier):
        return route
    changed = replace(route, lane=found, tier=new_tier)
    return replace(changed, changes=(*route.changes, f"{route.text()} → {changed.text()}：{reason}"))


def load(directory: Path) -> Route | None:
    path = directory / FILE
    return Route.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else None


def save(directory: Path, route: Route) -> None:
    atomic.write_text(directory / FILE, json.dumps(route.to_dict(), ensure_ascii=False, indent=2) + "\n")
