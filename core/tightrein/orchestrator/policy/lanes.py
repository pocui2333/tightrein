"""修复的流程表(redesign/05-fix.md 第 1、3 节；配置 fix 段)：「类型 × 档」到通道，以及各通道经过哪些步骤。

只有纯函数，只读配置：
- tier_of：按 thresholds.tiers 的门槛给出规模档(分诊的预估与修复的实际改动共用)；
- route：查 fix.lanes.<类型>.<档>，类型没有单独一行时取 default；规模档未知(用户需求)按中档查；超限返回 None；
- needs_scout：B 通道在 Issue 缺根因位置或范围，或类型在 fix.scout.taskTypes 时勘察；
- repro_mode：fix.repro.skipTypes 不写复现测试，independentTypes 由另一会话的 repro-writer 写，其余与写代码同一会话；
- expects_pass：fix.repro.passOnBaseTypes 写表征测试，在基准版本上须通过，其余须失败；
- review_modes：A 轻量(实际改动为微档且检查都通过、fix.review.skipMicro 为真时跳过)；B 轻量，高风险另加深度；
  C 不直接评审(各子 Issue 走 A 或 B)。
"""

from __future__ import annotations

from enum import Enum

from tightrein.config.project import ProjectConfig
from tightrein.domain import sizing
from tightrein.domain.enums import Lane, ReviewMode, SizeTier, TaskType
from tightrein.domain.sizing import TierLimit

DEFAULT_ROW = "default"
TIERED = (SizeTier.MICRO, SizeTier.SMALL, SizeTier.MEDIUM, SizeTier.LARGE)


class ReproMode(str, Enum):
    SKIP = "skip"
    INDEPENDENT = "independent"
    SAME_SESSION = "same-session"


def tier_limits(config: ProjectConfig) -> dict[SizeTier, TierLimit]:
    return {tier: TierLimit(config.whole_threshold(f"tiers.{tier.value}.maxFiles"),
                            config.whole_threshold(f"tiers.{tier.value}.maxLines")) for tier in TIERED}


def tier_of(config: ProjectConfig, files: int, lines: int) -> SizeTier:
    """文件数与行数都不含测试文件。"""
    return sizing.size_tier(files, lines, tier_limits(config))


def _types(config: ProjectConfig, key: str) -> set[TaskType]:
    return {TaskType(item) for item in config.get(key)}


def route(config: ProjectConfig, task_type: TaskType, tier: SizeTier | None) -> Lane | None:
    if tier is SizeTier.OVERSIZE:
        return None
    table = config.get("fix.lanes")
    row = table.get(task_type.value) or table[DEFAULT_ROW]
    return Lane(row[(tier or SizeTier.MEDIUM).value])


def needs_scout(config: ProjectConfig, task_type: TaskType, *, has_root_cause: bool, has_scope: bool) -> bool:
    return not has_root_cause or not has_scope or task_type in _types(config, "fix.scout.taskTypes")


def repro_mode(config: ProjectConfig, task_type: TaskType) -> ReproMode:
    if task_type in _types(config, "fix.repro.skipTypes"):
        return ReproMode.SKIP
    if task_type in _types(config, "fix.repro.independentTypes"):
        return ReproMode.INDEPENDENT
    return ReproMode.SAME_SESSION


def writes_repro_test(config: ProjectConfig, task_type: TaskType | None) -> bool:
    """这类任务写复现测试(验收标准第一条)；任务类型未知时按写。"""
    return task_type is None or repro_mode(config, task_type) is not ReproMode.SKIP


def expects_pass(config: ProjectConfig, task_type: TaskType) -> bool:
    return task_type in _types(config, "fix.repro.passOnBaseTypes")


def review_modes(config: ProjectConfig, lane: Lane, *, actual_tier: SizeTier, checks_passed: bool,
                 high_risk: bool) -> tuple[ReviewMode, ...]:
    if lane is Lane.FAST:
        skip = bool(config.get("fix.review.skipMicro")) and actual_tier is SizeTier.MICRO and checks_passed
        return () if skip else (ReviewMode.LIGHT,)
    if lane is Lane.STANDARD:
        return (ReviewMode.LIGHT, ReviewMode.DEEP) if high_risk else (ReviewMode.LIGHT,)
    return ()
