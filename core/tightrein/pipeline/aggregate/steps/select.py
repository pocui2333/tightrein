"""选出待聚合的运行与信号(architecture/05 3.3)。

待聚合的运行是已结束、aggregated_at 为空的 collect 运行(包括没有信号的 blocked 运行，聚合时只记为已聚合)，按开始
时间升序；--select 可以只取某个运行或某个探针。--input 给出一份 collect 交接文档时，运行与信号都从文件构造：
交接文档只有覆盖范围的计数，构造出的运行不带覆盖范围。另有此前遗留、需要重新重放的待确认问题。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import Probe, ProbeLevel, ProblemStatus, ReproduceStrategy
from tightrein.domain.enums import RunStage, RunStatus, SignalAggregateState
from tightrein.domain.problem import Problem
from tightrein.domain.reproduce import strategy
from tightrein.domain.run import EnvironmentDetail, Run
from tightrein.domain.signal import Signal
from tightrein.pipeline.collect.steps.handoff import read_signals
from tightrein.store.files import handoff_files
from tightrein.store.repos import problems, runs, signals

RUN_TIME_FORMAT = "%Y%m%d-%H%M%S"
NO_COVERAGE = "输入的交接文档只有覆盖范围的计数，本次不按覆盖范围判定解决与复现"


def parse_selectors(values: Sequence[str]) -> tuple[str | None, Probe | None]:
    """`run:<运行编号>` 或 `probe:<探针>`，各至多一个；其他写法抛出 ValueError。"""
    run_id: str | None = None
    probe: Probe | None = None
    for value in values:
        kind, _, argument = value.partition(":")
        if kind == "run" and argument and run_id is None:
            run_id = argument
        elif kind == "probe" and argument and probe is None:
            probe = Probe(argument)
        else:
            raise ValueError(f"无法识别的选择器：{value}，只能是 run:<运行编号> 或 probe:<探针>，各一次")
    return run_id, probe


def pending_runs(conn: sqlite3.Connection, run_id: str | None = None, probe: Probe | None = None) -> list[Run]:
    return [run for run in runs.unaggregated(conn)
            if (run_id is None or run.id == run_id) and (probe is None or run.probe is probe)]


def pending_signals(conn: sqlite3.Connection, run: Run) -> list[Signal]:
    found = signals.find(conn, run_id=run.id, aggregate_state=SignalAggregateState.PENDING)
    return sorted(found, key=lambda signal: (signal.occurred_at, signal.id))


def run_time(run_id: str) -> datetime:
    return datetime.strptime(run_id[2:17], RUN_TIME_FORMAT).replace(tzinfo=timezone.utc)


def from_input(path: Path) -> tuple[Run, list[Signal]]:
    document = handoff_files.read(path)
    if document["stage"] != RunStage.COLLECT.value:
        raise ValueError(f"{path} 不是 collect 的交接文档")
    outputs = document["outputs"]
    run = Run(
        id=document["runId"], stage=RunStage.COLLECT, started_at=run_time(document["runId"]),
        status=RunStatus(outputs["runStatus"]), probe=Probe(outputs["probe"]),
        level=ProbeLevel(outputs["level"]) if outputs["level"] else None, ended_at=parse_iso(document["createdAt"]),
        target_commit=outputs["target"]["release"],
        environment_detail=EnvironmentDetail.from_dict(outputs["environment"]),
    )
    return run, sorted(read_signals(path, outputs), key=lambda signal: (signal.occurred_at, signal.id))


def latest_signal(conn: sqlite3.Connection, problem: Problem) -> Signal | None:
    found = signals.get_many(conn, problems.signal_ids(conn, problem.id))
    return found[-1] if found else None


def pending_replays(conn: sqlite3.Connection) -> list[Problem]:
    """待确认、复现方式为重放的问题：上次重放时目标不可用，下次聚合重试。"""
    result = []
    for problem in problems.find(conn, statuses=[ProblemStatus.PENDING]):
        signal = latest_signal(conn, problem)
        if signal is not None and strategy(problem.probe, signal.check) is ReproduceStrategy.REPLAY:
            result.append(problem)
    return result
