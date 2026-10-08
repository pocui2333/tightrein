import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tightrein.protocol.limits import Breaker
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import Quota
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
RUN = "R-20261008T030000Z-run"
DEFAULTS = json.loads((Path(__file__).resolve().parents[3] / "settings" / "defaults.json").read_text(encoding="utf-8"))
ALL_DAY = {"schedule": {"window": {"from": "00:00", "to": "23:59", "days": ["mon", "tue", "wed", "thu", "fri",
                                                                           "sat", "sun"]}}}


class Stages:
    """各阶段入口的假实现：记下调用，按测试给的函数返回。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.collect: Callable[..., Any] = lambda runtime, only: SimpleNamespace(results=[], new=[], regressed=[],
                                                                                 skipped={}, disabled={})
        self.assess_pending: Callable[..., Any] = lambda runtime: []
        self.assess: Callable[..., Any] = lambda runtime, problem: assessed(problem)
        self.implement: Callable[..., Any] = lambda runtime, issue: stepped(issue, "implement.deliver", None)
        self.release: Callable[..., Any] = lambda runtime, issue: stepped(issue, "release.accept", None)
        self.queue: Callable[..., Any] = lambda runtime: []
        self.retro: Callable[..., Any] = lambda runtime: SimpleNamespace()
        self.sources = ["collect.alerts"]

    def record(self, name: str, value: Any, result: Any) -> Any:
        self.calls.append((name, value))
        return result

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def stepped(subject: str, point: str, next_point: str | None, status: str = "passed") -> SimpleNamespace:
    return SimpleNamespace(subject=subject, point=point, status=status, summary=f"{point} {status}",
                           next_point=next_point)


def assessed(problem: str, status: str = "passed") -> SimpleNamespace:
    return SimpleNamespace(problem=problem, verdict="confirmed", disposition="fix_now", issue=None, status=status,
                           reason=None)


def module(name: str, **functions: Callable[..., Any]) -> ModuleType:
    found = ModuleType(name)
    for key, value in functions.items():
        setattr(found, key, value)
    return found


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> Stages:
    fake = Stages()
    modules = {
        "tightrein.collect.collect": module(
            "collect", run=lambda runtime, only=None: fake.record("collect", only, fake.collect(runtime, only)),
            sources=lambda runtime: fake.sources,
            select=lambda runtime, only=None: ([SimpleNamespace(source=name, missed=0) for name in fake.sources], {})),
        "tightrein.assess.assess": module(
            "assess", assess=lambda runtime, problem: fake.record("assess", problem, fake.assess(runtime, problem)),
            assess_pending=lambda runtime: fake.record("assess_pending", None, fake.assess_pending(runtime))),
        "tightrein.implement.implement": module(
            "implement",
            implement=lambda runtime, issue: fake.record("implement", issue, fake.implement(runtime, issue))),
        "tightrein.release.release": module(
            "release", release=lambda runtime, issue: fake.record("release", issue, fake.release(runtime, issue))),
        "tightrein.release.queue": module(
            "queue", queue=lambda runtime: fake.record("queue", None, fake.queue(runtime))),
        "tightrein.retro.retro": module("retro", retro=lambda runtime: fake.record("retro", None, fake.retro(runtime))),
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
        package, _, attribute = name.rpartition(".")
        monkeypatch.setattr(sys.modules[package], attribute, value, raising=False)
    return fake


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "shop")


@pytest.fixture
def conn(layout: WorkspaceLayout, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(layout.database, clock=clock)
    yield connection
    connection.close()


@pytest.fixture
def make_runtime(layout: WorkspaceLayout, conn: sqlite3.Connection,
                 clock: FixedClock) -> Callable[..., SimpleNamespace]:
    def make(*overrides: dict[str, Any], run: str = RUN) -> SimpleNamespace:
        settings = Settings.from_data(DEFAULTS, ALL_DAY, *overrides)
        redactor = Redactor()
        agents = SimpleNamespace(breaker=Breaker(conn, clock, settings), quota=Quota(conn, clock, settings))
        return SimpleNamespace(workspace=layout, conn=conn, clock=clock, settings=settings, redactor=redactor,
                               events=EventLog(layout.events(run), redactor, clock), run=run, agents=agents)

    return make


@pytest.fixture
def kit() -> SimpleNamespace:
    """测试文件要用的构造函数与常量(importlib 导入模式下测试文件不能直接 import conftest)。"""
    return SimpleNamespace(NOW=NOW, DEFAULTS=DEFAULTS, stepped=stepped, assessed=assessed)
