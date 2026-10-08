"""status 与 watch 测试共用的示例工作区：数据与 44 号计划「定下的样式」中的示例一一对应。

- 0019 停在实施·定案等你审核；0022 在实施·方案出问题；0023 正在实施·编码第 2 轮(有进行中的模型调用)；
- 0024、0025 排队；告警与 API 模糊测试不启用且没有兜底；Claude 5h 用了 62%，agy 5h 用了 34%；
- 当前运行 R-20261007T013000Z-implement 由本进程持有，上一次运行是采集。
"""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tightrein.cli.render.snapshot import Source
from tightrein.onboard.setup import MODULES
from tightrein.protocol import handoff
from tightrein.protocol.handoff import Handoff, Metrics, Status, Tokens, Versions
from tightrein.protocol.naming import FileName, FixedClock, format_iso
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.atomic import write_text
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.tables import counters, issues, problems, runs, state
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.problems import Problem
from tightrein.store.tables.runs import Run

NOW = datetime(2026, 10, 7, 3, 19, tzinfo=UTC)
RUN = "R-20261007T013000Z-implement"
HOST = "test-host"
COMMIT = "8f3a9e1" + "0" * 33
DISABLED = ("collect.alerts", "collect.api_fuzz")


@dataclass
class World:
    source: Source
    clock: FixedClock
    layout: WorkspaceLayout
    alive: set[int]
    now: datetime = NOW
    run: str = RUN
    host: str = HOST

    def at(self, minutes_ago: float) -> datetime:
        return NOW - timedelta(minutes=minutes_ago)

    def handoff(self, subject: str, point: str, **values: Any) -> Path:
        return write_handoff(self, subject, point, **values)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    clock = FixedClock(NOW)
    tool = ToolLayout(tmp_path / "tool")
    (tool.root / ".git").mkdir(parents=True)
    (tool.root / ".git" / "HEAD").write_text(COMMIT + "\n", encoding="utf-8")
    layout = tool.workspace("ai-interview-collector")
    layout.data_dir.mkdir(parents=True)
    settings = Settings.from_data(json.loads(ToolLayout.discover().defaults.read_text(encoding="utf-8")))
    conn = open_database(layout.database, clock=clock)
    alive = {os.getpid()}
    source = Source(tool=tool, layout=layout, conn=conn, settings=settings, clock=clock, zone=UTC, host=HOST,
                    alive=lambda pid: pid in alive)
    built = World(source, clock, layout, alive)
    _setup(layout)
    _runs(built)
    _issues(built)
    _problems(built)
    _quota(built)
    _events(built)
    yield built
    conn.close()


def write_handoff(world: World, subject: str, point: str, *, status: Status = Status.PASSED, summary: str = "完成",
                  facts: dict | None = None, round_: int | None = None, minutes_ago: float = 30,
                  metrics: Metrics | None = None) -> Path:
    path = world.layout.step_file(subject, FileName(point, "handoff", "json", round=round_))
    handoff.write(path, Handoff(point=point, subject=subject, run=RUN, status=status, summary=summary,
                                facts=facts or {}, metrics=metrics or Metrics(duration_ms=60_000), round=round_,
                                created_at=format_iso(world.at(minutes_ago)),
                                versions=Versions(tool="claude", model="opus")))
    return path


def _setup(layout: WorkspaceLayout) -> None:
    modules = {key: {"status": "disabled" if key in DISABLED else "enabled", "method": None, "script": None,
                     "guide": None, "reason": "没有接入" if key in DISABLED else None, "impact": None}
               for key in MODULES}
    write_json(layout.setup, {"project": layout.project, "updatedAt": "2026-10-06T00:00:00Z", "modules": modules})


def _runs(world: World) -> None:
    conn = world.source.conn
    runs.start(conn, Run("R-20261007T000000Z-collect", "collect", "schedule", "running", world.at(199),
                         heartbeat_at=world.at(190), holder_pid=os.getpid(), holder_host=HOST))
    runs.finish(conn, "R-20261007T000000Z-collect", "done", FixedClock(world.at(190)))
    runs.start(conn, Run(RUN, "implement", "schedule", "running", world.at(42), heartbeat_at=NOW - timedelta(seconds=3),
                         holder_pid=os.getpid(), holder_host=HOST))


def _issues(world: World) -> None:
    conn, clock = world.source.conn, world.clock
    issues.save(conn, Issue("0019", "needs_decision", "笔记自动保存", "feature", "problem", severity="P2", gate="design",
                            stage="implement", step="approve"), clock)
    write_handoff(world, "0019", "implement.approve", status=Status.PENDING, summary="方案需确认：保存改为防抖 2s",
                  minutes_ago=48)
    pending = world.layout.human_document("0019", "pending")
    write_text(pending, "# 待审核\n")
    _touch(pending, world.at(48))

    issues.save(conn, Issue("0022", "implementing", "题型排序", "bug", "problem", severity="P1", stage="implement",
                            step="design"), clock)
    design = write_handoff(world, "0022", "implement.design", status=Status.FAILED,
                           summary="被安全分类拒绝，备用模型也拒绝", minutes_ago=75)
    _touch(design, world.at(75))
    failure = world.layout.human_document("0022", "failure")
    write_text(failure, "# 出问题\n")
    _touch(failure, world.at(75))

    issues.save(conn, Issue("0023", "implementing", "删除追问显示", "bug", "problem", severity="P2", stage="implement",
                            step="code", round=2), clock)
    for point, minutes, duration in (("implement.prepare", 40, 60), ("implement.locate", 38, 120),
                                     ("implement.design", 34, 240)):
        write_handoff(world, "0023", point, minutes_ago=minutes, metrics=Metrics(duration_ms=duration * 1000, calls=2))
    write_handoff(world, "0023", "implement.approve", facts={"auto": True}, minutes_ago=33,
                  metrics=Metrics(duration_ms=1000))
    write_handoff(world, "0023", "implement.code", round_=1, minutes_ago=20,
                  facts={"diffHash": "a1", "linesAdded": 48, "linesDeleted": 12, "highRiskPaths": ["package.json"]},
                  metrics=Metrics(duration_ms=300_000, calls=3, files_changed=4, lines_changed=60,
                                  tokens=Tokens(500_000, 9_000, 380_000, 0)))
    write_handoff(world, "0023", "implement.check", round_=1, summary="lint、类型检查、测试 12/12", minutes_ago=15)
    write_handoff(world, "0023", "implement.review", round_=1, status=Status.FAILED, minutes_ago=12,
                  summary="不通过 2 条，交回修改",
                  facts={"blockers": [{"location": "src/notes.ts:88", "kind": "error", "summary": "未处理保存失败"},
                                      {"location": "src/api.ts:31", "kind": "type", "summary": "缺参数"}]},
                  metrics=Metrics(duration_ms=60_000, calls=1))
    marker = world.layout.step_file("0023", FileName("implement.code", "started", "json", round=2))
    write_json(marker, {"point": "implement.code", "subject": "0023", "run": RUN, "tool": "claude", "model": "opus",
                        "effort": "high", "startedAt": format_iso(world.at(8)), "endedAt": None, "status": None,
                        "durationMs": None})
    counters.add(conn, "issue_tokens.0023", 1_200_000, clock)

    for identifier, severity in (("0024", "P2"), ("0025", "P3")):
        issues.save(conn, Issue(identifier, "todo", f"排队 {identifier}", "bug", "problem", severity=severity), clock)


def _problems(world: World) -> None:
    counts = {"new": 14, "watching": 4, "muted": 3, "regressed": 2}
    number = 0
    for status, count in counts.items():
        for _ in range(count):
            number += 1
            verdict = ("confirmed", "conditional", "refuted", "insufficient")[number % 4]
            severity = ("P0", "P1", "P2", "P3")[number % 4]
            problems.save(world.source.conn, Problem(
                f"P-{number:04d}", f"fp{number}", "collect.platform_errors", "error", status, f"问题 {number}",
                world.at(30), world.at(5), extra={"verdict": verdict, "severity": severity}), world.clock)


def _quota(world: World) -> None:
    def window(used: float, reset: timedelta) -> dict:
        return {"status": "allowed", "usedRatio": used, "resetsAt": format_iso(NOW + reset), "seenAt": format_iso(NOW)}

    state.put(world.source.conn, "quota", {
        "claude": {"five_hour": window(0.62, timedelta(hours=1, minutes=18)),
                   "weekly": window(0.41, timedelta(days=3))},
        "agy": {"five_hour": window(0.34, timedelta(hours=3, minutes=40)), "weekly": window(0.20, timedelta(days=5))},
    }, world.clock)


def _events(world: World) -> None:
    lines = [
        (world.at(5.1), None, "protocol.limits", "effect", "临时错误 429，第 1 次，10s 后重试", {}),
        (world.at(4), "0023", "implement.check", "action", "lint、类型检查、测试 12/12",
         {"handoff": "36-implement.check.r1-handoff.json"}),
        (world.at(1.5), "0023", "implement.review", "action", "claude/opus：failed，不通过 2 条，交回修改",
         {"started": "37-implement.review.r1-started.json"}),
        (world.at(0.1), "0023", "implement.code", "action", "改 src/notes.ts",
         {"started": "35-implement.code.r2-started.json"}),
    ]
    path = world.layout.events(RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"at": format_iso(at), "run": RUN, "subject": subject, "point": point,
                                        "kind": kind, "summary": summary, "refs": refs}, ensure_ascii=False) + "\n"
                            for at, subject, point, kind, summary, refs in lines), encoding="utf-8")


def _touch(path: Path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))
