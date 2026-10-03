"""第 2 步 前置条件(architecture/05 2.4，redesign/02-aggregate.md 第 2 节)：api-fuzz 的健康检查与 static 的每日预算。

其余条件(扩展没有实现、只读 worktree 不在目标 commit、接口描述缓存)由探针自己判定。--reparse 不访问目标环境，
不做检查。健康检查不通过时本次不采集(blocked，不产出信号)，结果写入运行的 environment_detail 与运行摘要。
没有目标地址时不做检查，由方法返回
skipped；没有配置 target.healthcheck 时不做健康检查，通过的 Gate 以 note 写明。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import RunStatus, Stage
from tightrein.domain.run import EnvironmentDetail, HealthCheck
from tightrein.pipeline.collect.steps.target import TargetInfo
from tightrein.sources.common.http import HttpRequest, Transport
from tightrein.sources.common.session import join_url
from tightrein.runner.limits import DailyBudget, budget_per_day

HEALTH_CHECKED = frozenset({ProbeKind.API_FUZZ})
UNAVAILABLE = "staging 不可用"
UNAVAILABLE_HINT = "确认 staging 已启动、健康检查接口可以访问后重新运行"
BUDGET_EXHAUSTED = "今日静态巡检预算已用尽"
BUDGET_HINT = "明天再运行，或调高 stages.collect.budgetPerDay"
HEALTH_UNCONFIGURED = "未配置 target.healthcheck，未做健康检查"


@dataclass(frozen=True)
class Gate:
    passed: bool
    status: RunStatus | None = None
    reason: str | None = None
    hint: str | None = None
    environment: EnvironmentDetail = field(default_factory=EnvironmentDetail)
    note: str | None = None


def health(config: ProjectConfig, base_url: str, transport: Transport) -> HealthCheck:
    url = join_url(base_url, config.get("target.healthcheck"))
    response = transport(HttpRequest("GET", url, timeout_seconds=float(config.get("target.healthTimeoutSeconds"))))
    return HealthCheck(response.status, response.elapsed_ms)


def check(config: ProjectConfig, conn: sqlite3.Connection, probe: ProbeKind, info: TargetInfo, *,
          transport: Transport, clock: Clock, reparse: bool = False) -> Gate:
    if reparse:
        return Gate(True)
    if probe in HEALTH_CHECKED:
        if info.base_url is None:
            return Gate(True)
        if config.data.get("target", {}).get("healthcheck") is None:
            return Gate(True, note=HEALTH_UNCONFIGURED)
        result = health(config, info.base_url, transport)
        detail = EnvironmentDetail(health=result)
        if not result.ok:
            return Gate(False, RunStatus.BLOCKED, UNAVAILABLE, UNAVAILABLE_HINT, detail)
        return Gate(True, environment=detail)
    if probe is ProbeKind.STATIC and DailyBudget(conn, clock).exhausted(Stage.COLLECT,
                                                                        budget_per_day(config, Stage.COLLECT)):
        return Gate(False, RunStatus.BLOCKED, BUDGET_EXHAUSTED, BUDGET_HINT)
    return Gate(True)
