"""去重测试共用：真实数据库与缺省配置，git、事件日志用替身；信号与来源结果用工厂造。"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.collect.common.signals import Signal
from tightrein.collect.common.source import SourceResult, SourceStatus
from tightrein.protocol.handoff import Metrics
from tightrein.protocol.naming import FixedClock
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RUN = "R-20261007T120000Z-collect"
DEFAULTS = Path(__file__).resolve().parents[3] / "settings" / "defaults.json"
ORDER = ["c1", "c2", "c3", "c4"]  # 依次更新的 commit；不在其中的(本地没同步)关系未知


class FakeGit:
    def __init__(self, head: str | None = "c1") -> None:
        self.current = head
        self.queries: list[tuple[str, str]] = []

    def is_ancestor(self, commit: str, of: str) -> bool | None:
        self.queries.append((commit, of))
        if commit not in ORDER or of not in ORDER:
            return None
        return ORDER.index(commit) <= ORDER.index(of)

    def head(self) -> SimpleNamespace:
        return SimpleNamespace(commit=self.current, branch="main")


@dataclass
class FakeEvents:
    emitted: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, **event: Any) -> None:
        self.emitted.append(event)


def settings_with(**dedup: Any) -> Settings:
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    return Settings.from_data(defaults, {"controls": {"collect.dedup": dedup}})


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "demo")


@pytest.fixture
def conn(layout: WorkspaceLayout, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(layout.database, clock=clock)
    yield connection
    connection.close()


@pytest.fixture
def runtime(conn: sqlite3.Connection, clock: FixedClock, layout: WorkspaceLayout) -> SimpleNamespace:
    return SimpleNamespace(conn=conn, clock=clock, workspace=layout, run=RUN, settings=settings_with(),
                           git=FakeGit(), events=FakeEvents(), secrets={}, environ={}, github=None, language="zh")


@pytest.fixture
def make_signal() -> Callable[..., Signal]:
    counter = iter(range(1, 10_000))

    def make(*, source: str = "collect.platform_errors", check_type: str = "error", location: str | None = "app.py:10",
             message: str = "boom", occurred_at: str = "2026-10-07T11:00:00Z", commit: str | None = "c1",
             evidence: dict[str, Any] | None = None, group_key: str | None = None, symbol: str | None = None,
             deterministic: bool = False, verified: bool = False, reproducible: bool = False,
             run: str = RUN) -> Signal:
        return Signal(id=f"S-{next(counter):04d}", run=run, source=source, check_type=check_type, location=location,
                      symbol=symbol, message=message, evidence=dict(evidence or {}), occurred_at=occurred_at,
                      commit=commit, environment="staging", severity_hint=None, group_key=group_key,
                      deterministic=deterministic, verified=verified, reproducible=reproducible)

    return make


@pytest.fixture
def make_result() -> Callable[..., SourceResult]:
    def make(source: str, signals: list[Signal] = (), *, status: str = "done", coverage: list[str] = (),
             state: dict[str, Any] | None = None, reason: str | None = None) -> SourceResult:
        return SourceResult(source, SourceStatus(status), list(signals), len(signals), None,
                            reason or (None if status == "done" else "原因"), dict(state or {}), Metrics(),
                            coverage=list(coverage))

    return make


@pytest.fixture
def configure(runtime: SimpleNamespace) -> Callable[..., None]:
    """改 controls."collect.dedup" 的取值(覆盖缺省)。"""

    def apply(**dedup: Any) -> None:
        runtime.settings = settings_with(**dedup)

    return apply
