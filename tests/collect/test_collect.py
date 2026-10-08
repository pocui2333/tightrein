"""采集一轮：假来源、真实数据库与去重，git 与事件日志用替身。"""

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.agents.call import AgentContext
from tightrein.collect import collect
from tightrein.collect.collect import SCHEDULE_KEY, run, select
from tightrein.collect.common.signals import Signal
from tightrein.collect.common.source import SourceResult, SourceStatus, SourceUnavailable
from tightrein.onboard.setup import MODULES, ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.handoff import Metrics, read
from tightrein.protocol.naming import FileName, FixedClock
from tightrein.protocol.resources import Slots
from tightrein.protocol.runtime import Runtime
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import state

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RUN = "R-20261007T120000Z-collect"
DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"
ENABLED = ("collect.platform_errors", "collect.alerts", "collect.static", "collect.project_probes", "collect.api_fuzz")


@dataclass
class FakeEvents:
    emitted: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, **event: Any) -> None:
        self.emitted.append(event)


class FakeGit:
    repo = Path("/repo")

    def head(self) -> SimpleNamespace:
        return SimpleNamespace(commit="c1", branch="main")

    def is_ancestor(self, commit: str, of: str) -> bool | None:
        return None


def setup_with(enabled: tuple[str, ...], deploy: bool = False) -> Setup:
    def module(key: str) -> ModuleSetup:
        on = key in enabled or (deploy and key == "release.deploy")
        return ModuleSetup(key, ModuleStatus.ENABLED if on else ModuleStatus.DISABLED, None, None, None,
                           None if on else "测试", None)

    return Setup("demo", "2026-10-07T00:00:00Z", {key: module(key) for key in MODULES})


@pytest.fixture
def runtime(tmp_path):
    clock = FixedClock(NOW)
    layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    conn = open_database(layout.database, clock=clock)
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    settings = Settings.from_data(defaults, {"controls": {"collect.alerts": {"sourceTimeout": "200ms"}}})
    events = FakeEvents()
    agents = AgentContext(settings, layout, conn, clock, None, None, events, {}, None, None, None)
    built = Runtime(tool=None, workspace=layout, settings=settings, setup=setup_with(ENABLED), conn=conn, clock=clock,
                    runner=None, redactor=None, secrets={}, environ={}, run=RUN, events=events, agents=agents,
                    git=FakeGit(), github=None, slots=Slots(settings))
    yield built
    conn.close()


def signal(source: str, key: str) -> Signal:
    return Signal(id="S-0001", run=RUN, source=source, check_type="alert", location=None, symbol=None, message="down",
                  evidence={}, occurred_at="2026-10-07T11:00:00Z", commit=None, environment=None, severity_hint=None,
                  group_key=key, deterministic=True, verified=False, reproducible=False)


def fake_sources(main_conn, seen_connections):
    released = threading.Event()

    def platform(runtime):
        seen_connections.append(runtime.conn is not main_conn and runtime.agents.conn is runtime.conn)
        runtime.conn.execute("SELECT COUNT(*) FROM problems").fetchone()
        return SourceResult("collect.platform_errors", SourceStatus.DONE, [signal("collect.platform_errors", "g1")], 1,
                            None, None, {"collect.platform_errors:sentry": {"until": "x"}}, Metrics(),
                            coverage=["sentry"])

    def stuck(runtime):
        released.wait(2)
        return SourceResult("collect.alerts", SourceStatus.DONE, [], 0, None, None, {}, Metrics())

    def broken(runtime):
        raise SourceUnavailable("连不上")

    def crashing(runtime):
        raise RuntimeError("bug")

    table = {"collect.platform_errors": platform, "collect.alerts": stuck, "collect.static": broken,
             "collect.project_probes": crashing}
    return (lambda name: table[name]), released


def test_a_round_runs_due_sources_in_parallel_and_isolates_failures(runtime):
    seen: list[bool] = []
    loader, released = fake_sources(runtime.conn, seen)
    outcome = run(runtime, loader=loader)
    released.set()
    statuses = {item.source: (item.status.value, item.reason) for item in outcome.results}
    assert [item.source for item in outcome.results] == [
        "collect.project_probes", "collect.platform_errors", "collect.alerts", "collect.static"]
    assert statuses["collect.platform_errors"] == ("done", None)
    assert statuses["collect.alerts"][0] == "failed" and "不再等它" in statuses["collect.alerts"][1]
    assert statuses["collect.static"] == ("failed", "unavailable：连不上")
    assert statuses["collect.project_probes"][0] == "failed" and "RuntimeError" in statuses["collect.project_probes"][1]
    assert outcome.skipped == {"collect.api_fuzz": "还没有部署记录"}
    # 没有启用的来源连同原因列出，不是静默跳过
    assert outcome.disabled == {key: "测试" for key in MODULES if key.startswith("collect.") and key not in ENABLED}
    assert outcome.disabled
    assert outcome.new == ["P-0001"] and seen == [True]
    assert state.get(runtime.conn, "collect.platform_errors:sentry") == {"until": "x"}
    marked = state.get(runtime.conn, SCHEDULE_KEY.format(source="collect.platform_errors"))
    assert marked["at"] == "2026-10-07T12:00:00Z"
    assert state.get(runtime.conn, SCHEDULE_KEY.format(source="collect.static")) is None
    handoff = read(runtime.workspace.step_file(RUN, FileName("collect.platform_errors", "handoff", "json")))
    assert handoff.status.value == "passed" and handoff.facts["signals"][0]["groupKey"] == "g1"
    failed = read(runtime.workspace.step_file(RUN, FileName("collect.static", "handoff", "json")))
    assert failed.status.value == "failed"
    assert runtime.workspace.step_file(RUN, FileName("collect.dedup", "handoff", "json")).is_file()


def test_sources_wait_for_their_interval_or_a_new_commit(runtime):
    for source, value in [("collect.platform_errors", {"at": "2026-10-07T11:30:00Z", "marker": None}),
                          ("collect.alerts", {"at": "2026-10-07T11:00:00Z", "marker": None}),
                          ("collect.static", {"at": "2026-10-07T11:00:00Z", "marker": "c1"})]:
        state.put(runtime.conn, SCHEDULE_KEY.format(source=source), value, runtime.clock)
    selected, skipped = select(runtime)
    assert [(item.source, item.missed) for item in selected] == [("collect.project_probes", 0), ("collect.alerts", 3)]
    assert skipped["collect.platform_errors"].startswith("未到点")
    assert skipped["collect.static"].startswith("没有新提交")


def test_a_new_deployment_makes_api_fuzz_due(runtime):
    state.put(runtime.conn, "release.deploy.deployments",
              [{"commit": "d2", "status": "succeeded", "deployedAt": "2026-10-07T10:00:00Z",
                "detectedAt": "2026-10-07T10:05:00Z"}], runtime.clock)
    state.put(runtime.conn, SCHEDULE_KEY.format(source="collect.api_fuzz"), {"at": "x", "marker": "d1"}, runtime.clock)
    selected, _ = select(runtime)
    assert ("collect.api_fuzz", "d2") in [(item.source, item.marker) for item in selected]


def test_manual_runs_ignore_the_schedule_and_nothing_due_skips_dedup(runtime):
    selected, _ = select(runtime, only=["collect.static"])
    assert [(item.source, item.marker) for item in selected] == [("collect.static", "c1")]
    runtime.setup = setup_with(())
    outcome = run(runtime, loader=lambda name: pytest.fail("不该运行任何来源"))
    assert outcome.results == [] and outcome.dedup is None
    assert set(outcome.disabled) == {key for key in MODULES if key.startswith("collect.")}


def test_a_failing_deploy_refresh_is_noted_and_the_round_goes_on(runtime):
    runtime.setup = setup_with((), deploy=True)
    outcome = run(runtime, loader=lambda name: pytest.fail("不该运行任何来源"))
    assert outcome.notes and outcome.notes[0].startswith("部署记录没有刷新")


def test_sources_are_loaded_from_their_module():
    assert collect.SOURCE_MODULE.format(source="collect.alerts") == "tightrein.collect.alerts.source"
