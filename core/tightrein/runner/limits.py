"""轮数、单次费用、每日预算与三层总预算(architecture/02 2.7，design 11.3，redesign/09-loop.md 第 3 节)。

- 每日预算：启动前读取 budget_usage 中该环节当天的累计，达到 stages.<环节>.budgetPerDay 时不启动；运行结束后把
  本次费用累加进去。「当天」按本机时区的日期计算，时区可以注入。没有配置 budgetPerDay 的环节不限。
- 总预算(budget 段)：全部环节合计的每次运行、每天、每周上限。执行器启动任务前检查每天与每周；编排在每一步之前检查
  三层(每次运行的已用为当前总额减去运行开始时的总额)。每周从周一开始；perWeekUsd 为空时按 perWeekPercent ×
  subscriptionWeekUsd 换算。
- 费用估算：工具不返回费用时，按 capabilities 中该模型的每百万 token 价格计算，costEstimated 为真；没有价格时费用为空。
- 工具不支持原生轮数或费用上限时，核心逐行读取事件：tool-call 计数超过 maxTurns、累计费用超过 maxCostUsd 时终止。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta, tzinfo

from tightrein.config.capabilities import Capabilities
from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import Stage
from tightrein.runner.result import COST_LIMIT, TURN_LIMIT, Usage
from tightrein.runner.task import Limits
from tightrein.runner.transcript import TOOL_CALL, EventDraft
from tightrein.store.repos import budget_usage


def stage_limits(config: ProjectConfig, stage: Stage) -> Limits:
    """stages.<环节>.limits 中的缺省上限。"""
    data = config.data.get("stages", {}).get(stage.value, {}).get("limits", {})
    return Limits.from_dict(data)


def budget_per_day(config: ProjectConfig, stage: Stage) -> float | None:
    value = config.data.get("stages", {}).get(stage.value, {}).get("budgetPerDay")
    return None if value is None else float(value)


def with_cost(usage: Usage, capabilities: Capabilities, tool: str, model: str | None) -> Usage:
    """工具没有给出费用时按价格估算。"""
    if usage.cost_usd is not None or usage.input_tokens is None or usage.output_tokens is None:
        return usage
    cost = capabilities.estimate_cost(tool, model, usage.input_tokens, usage.output_tokens)
    return usage if cost is None else replace(usage, cost_usd=cost, cost_estimated=True)


class DailyBudget:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, zone: tzinfo | None = None) -> None:
        self.conn = conn
        self.clock = clock
        self.zone = zone

    def spent(self, stage: Stage) -> float:
        usage = budget_usage.get(self.conn, stage, local_date(self.clock.now(), self.zone))
        return 0.0 if usage is None else usage.cost_usd

    def exhausted(self, stage: Stage, limit: float | None) -> bool:
        return limit is not None and self.spent(stage) >= limit

    def add(self, stage: Stage, usage: Usage) -> None:
        if usage.cost_usd is None and usage.input_tokens is None and usage.output_tokens is None:
            return
        budget_usage.add(self.conn, stage, local_date(self.clock.now(), self.zone), usage.cost_usd or 0.0,
                         usage.input_tokens or 0, usage.output_tokens or 0, usage.cost_estimated, self.clock)


@dataclass(frozen=True)
class BudgetLimits:
    per_run: float | None
    per_day: float | None
    per_week: float | None

    @classmethod
    def from_config(cls, config: ProjectConfig) -> BudgetLimits:
        def number(key: str) -> float | None:
            value = config.get(f"budget.{key}")
            return None if value is None else float(value)

        week = number("perWeekUsd")
        percent, subscription = number("perWeekPercent"), number("subscriptionWeekUsd")
        if week is None and percent is not None and subscription is not None:
            week = round(subscription * percent / 100, 2)
        return cls(number("perRunUsd"), number("perDayUsd"), week)


class GlobalBudget:
    """全部环节合计的费用；exceeded 返回到达的那一层与已用、上限，没有到达时为空。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, limits: BudgetLimits,
                 zone: tzinfo | None = None) -> None:
        self.conn = conn
        self.clock = clock
        self.limits = limits
        self.zone = zone

    def total(self) -> float:
        return budget_usage.total(self.conn)

    def spent(self) -> dict[str, float]:
        today = local_date(self.clock.now(), self.zone)
        monday = today - timedelta(days=today.weekday())
        return {"day": budget_usage.total(self.conn, today), "week": budget_usage.total(self.conn, monday)}

    def exceeded(self, run_baseline: float | None = None) -> str | None:
        spent = self.spent()
        layers = [("每天", spent["day"], self.limits.per_day), ("每周", spent["week"], self.limits.per_week)]
        if run_baseline is not None:
            layers.insert(0, ("本次运行", self.total() - run_baseline, self.limits.per_run))
        for name, used, limit in layers:
            if limit is not None and used >= limit:
                return f"{name}的费用已达上限：已用 {used:.2f} 美元，上限 {limit:.2f} 美元"
        return None


class RunWatch:
    """逐行观察统一事件，超出轮数或费用上限时给出终止原因；为 None 的上限不检查(由工具自身保证或不限)。"""

    def __init__(self, *, max_turns: int | None, max_cost_usd: float | None,
                 cost: Callable[[Usage], Usage]) -> None:
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        self.cost = cost
        self.turns = 0
        self.usage = Usage()

    def observe(self, draft: EventDraft) -> str | None:
        if draft.type == TOOL_CALL:
            self.turns += 1
            if self.max_turns is not None and self.turns > self.max_turns:
                return TURN_LIMIT
        if draft.usage is not None:
            self.usage = self.usage + self.cost(draft.usage)
            spent = self.usage.cost_usd
            if self.max_cost_usd is not None and spent is not None and spent > self.max_cost_usd:
                return COST_LIMIT
        return None
