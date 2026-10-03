"""链路健康检查(architecture/08 第 7 节，design 8.6)：六项检查，每项返回若干 HealthItem。

没有发现异常的检查返回一条 passed 为真的记录；notify 为真的项由编排合并进本次运行的通知，learn 不直接发送。
漏跑、异常退出与部署停滞只看最近 thresholds.health.lookbackDays 天。
"""

from __future__ import annotations

import shlex
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import (
    DeploymentStatus,
    Probe,
    ProbeLevel,
    RunStage,
    RunStatus,
)
from tightrein.domain.run import Run
from tightrein.pipeline.learn.steps.weeks import due_times
from tightrein.store.migrations.runner import initialized_at
from tightrein.store import locks
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import deployments, runs, signals

MISSED = "missed-runs"
ABNORMAL = "abnormal-exit"
IDLE = "idle-probe"
DEPLOY_STALL = "deploy-stall"
ACCOUNT = "account-unavailable"
DATA_DIR = "data-dir-size"
CHECK_LABELS = {MISSED: "探针漏跑", ABNORMAL: "探针异常退出", IDLE: "探针长期没有产出", DEPLOY_STALL: "部署检测停滞",
                ACCOUNT: "测试账号异常", DATA_DIR: "数据目录膨胀"}
PROBE_OPTION = "--probe"


@dataclass(frozen=True)
class HealthItem:
    check: str
    passed: bool
    notify: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "notify": self.notify, "detail": self.detail}


@dataclass(frozen=True)
class HealthContext:
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    config: ProjectConfig
    now: datetime
    zone: tzinfo | None
    current_run_id: str | None = None
    pid_alive: Callable[[int], bool] = locks.process_alive

    def since(self) -> datetime:
        return self.now - timedelta(days=self.config.whole_threshold("health.lookbackDays"))

    def local(self, at: datetime) -> str:
        return (at.astimezone(self.zone) if self.zone is not None else at.astimezone()).strftime("%Y-%m-%d %H:%M %z")


def _ok(check: str) -> list[HealthItem]:
    return [HealthItem(check, True, False, "")]


def _target(command: str) -> tuple[RunStage, Probe | None] | None:
    """定时任务命令对应的环节与探针；第一个词不是环节时返回 None。"""
    words = shlex.split(command)
    if not words or words[0] not in {stage.value for stage in RunStage}:
        return None
    probe = words[words.index(PROBE_OPTION) + 1] if PROBE_OPTION in words[:-1] else None
    return RunStage(words[0]), Probe(probe) if probe is not None else None


def missed_runs(ctx: HealthContext) -> list[HealthItem]:
    tasks = ctx.config.data.get("schedule", {}).get("tasks", [])
    window = timedelta(minutes=ctx.config.whole_threshold("health.missedWindowMinutes"))
    notify_count = ctx.config.whole_threshold("health.missedNotifyCount")
    all_runs = runs.find(ctx.conn)
    created = initialized_at(ctx.conn)
    found = []
    for task in tasks:
        target = _target(task["command"])
        if target is None:
            continue
        stage, probe = target
        own = [run.started_at for run in all_runs if run.stage is stage and (probe is None or run.probe is probe)]
        # 从任务第一次执行(从未执行时为工作区建立)起才计漏跑，新建的工作区不把回看期内的全部时刻都算作漏跑
        start = min(own) if own else created
        since = ctx.since() if start is None else max(ctx.since(), start)
        due = due_times([task], ctx.config.non_working_days(), since, ctx.now - window, ctx.zone)
        moments = [item.at for item in due]
        missed = [moment for moment in moments if not any(
            run.stage is stage and (probe is None or run.probe is probe) and moment <= run.started_at <= moment + window
            for run in all_runs)]
        if missed:
            recent = len(moments) >= notify_count and all(moment in missed for moment in moments[-notify_count:])
            times = "、".join(ctx.local(moment) for moment in missed)
            found.append(HealthItem(MISSED, False, recent, f"定时任务 {task['name']} 漏跑 {len(missed)} 次：{times}"))
    return found or _ok(MISSED)


def _log_hint(ctx: HealthContext, run: Run) -> str:
    log = ctx.layout.events_log(run.started_at.date())
    return f"日志 {ctx.layout.relative(log)}、{ctx.layout.relative(ctx.layout.raw_dir(run.id))}/"


def abnormal_exits(ctx: HealthContext) -> list[HealthItem]:
    holders: dict[str, list[int]] = {}
    for lock in locks.TABLE.find(ctx.conn):
        if lock.run_id is not None:
            holders.setdefault(lock.run_id, []).append(lock.holder_pid)
    found = []
    for run in runs.find(ctx.conn):
        if run.id == ctx.current_run_id or run.started_at < ctx.since():
            continue
        if run.status is RunStatus.FAILED:
            found.append(HealthItem(ABNORMAL, False, True, f"运行 {run.id} 以失败结束；{_log_hint(ctx, run)}"))
        elif run.ended_at is None and not any(ctx.pid_alive(pid) for pid in holders.get(run.id, [])):
            found.append(HealthItem(ABNORMAL, False, True,
                                    f"运行 {run.id} 没有结束时间且持有进程已不存在；{_log_hint(ctx, run)}"))
    return found or _ok(ABNORMAL)


def _coverage_text(run: Run) -> str:
    coverage = run.coverage
    return (f"接口 {len(coverage.tested_endpoints())} 个、文件 {len(coverage.files)} 个、"
            f"来源 {len(coverage.sources)} 个")


def idle_probes(ctx: HealthContext) -> list[HealthItem]:
    size = ctx.config.whole_threshold("health.idleRuns")
    found = []
    for probe in Probe:
        recent = runs.find(ctx.conn, stage=RunStage.COLLECT, probe=probe, status=RunStatus.OK)[-size:]
        if len(recent) == size and not any(signals.find(ctx.conn, run_id=run.id) for run in recent):
            found.append(HealthItem(IDLE, False, False,
                                    f"{probe.label} 最近 {size} 次运行都没有产出信号，可能已失效(例如被拦在登录页)；"
                                    f"最近一次 {recent[-1].id} 覆盖{_coverage_text(recent[-1])}"))
    return found or _ok(IDLE)


def deploy_stall(ctx: HealthContext) -> list[HealthItem]:
    limit = ctx.now - timedelta(minutes=ctx.config.whole_threshold("health.deployToShallowMinutes"))
    shallow = {run.target_commit for run in runs.find(ctx.conn, stage=RunStage.COLLECT)
               if run.level is ProbeLevel.SHALLOW}
    found = [HealthItem(DEPLOY_STALL, False, True,
                        f"部署 {item.commit[:12]} 于 {ctx.local(item.detected_at)} 检测到，至今没有部署后浅跑")
             for item in deployments.find(ctx.conn, status=DeploymentStatus.SUCCEEDED)
             if ctx.since() <= item.detected_at <= limit and item.commit not in shallow]
    return found or _ok(DEPLOY_STALL)


def account_unavailable(ctx: HealthContext) -> list[HealthItem]:
    """最近一次带账号的采集运行(api-fuzz)中登录失败的角色。"""
    collected = [run for run in runs.find(ctx.conn, stage=RunStage.COLLECT) if run.probe is Probe.API_FUZZ]
    if not collected:
        return _ok(ACCOUNT)
    latest = max(collected, key=lambda run: run.started_at)
    found = [HealthItem(ACCOUNT, False, True, f"运行 {latest.id} 中角色 {role} 登录失败，请检查该角色的测试账号")
             for role in latest.environment_detail.failed_roles]
    return found or _ok(ACCOUNT)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def data_dir_size(ctx: HealthContext) -> list[HealthItem]:
    root = ctx.layout.data_dir()
    if not root.is_dir():
        return _ok(DATA_DIR)
    parts = {item.name: _size(item) for item in sorted(root.iterdir())}
    total, limit = sum(parts.values()), ctx.config.whole_threshold("health.dataDirMaxBytes")
    if total <= limit:
        return _ok(DATA_DIR)
    listing = "、".join(f"{name} {size} 字节" for name, size in sorted(parts.items(), key=lambda item: -item[1]))
    return [HealthItem(DATA_DIR, False, False,
                       f"data/ 共 {total} 字节，超过 {limit} 字节；各子目录：{listing}；按保留期清理")]


CHECKS: tuple[Callable[[HealthContext], list[HealthItem]], ...] = (
    missed_runs, abnormal_exits, idle_probes, deploy_stall, account_unavailable, data_dir_size,
)


def check(ctx: HealthContext) -> list[HealthItem]:
    return [item for function in CHECKS for item in function(ctx)]
