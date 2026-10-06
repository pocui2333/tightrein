from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest
from runner_samples import task

from tightrein.config.routes import ModelChoice
from tightrein.config.user import UserConfig
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import AgentSessionStatus, Stage
from tightrein.runner import sessions
from tightrein.runner.adapters.claude import ClaudeAdapter
from tightrein.runner.registry import Registry, choose, default_adapters
from tightrein.runner.result import RunnerConfigError
from tightrein.store.migrations.runner import open_database

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


def test_adapters_and_executables(tmp_path):
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    user = UserConfig(tmp_path / "config.yaml", tools={"claude": claude, "codex": tmp_path / "missing"})
    registry = Registry(default_adapters(tmp_path), user, which=lambda name: f"/usr/local/bin/{name}")
    assert isinstance(registry.adapter("claude"), ClaudeAdapter)
    assert registry.adapter("claude").home == tmp_path / ".claude"
    with pytest.raises(RunnerConfigError, match="没有工具 cursor 的适配器"):
        registry.adapter("cursor")
    assert registry.executable("claude") == str(claude)
    assert registry.executable("codex") is None
    assert registry.executable("agy") == "/usr/local/bin/agy"


def test_tool_and_model_choice(make_config):
    config = make_config()
    assert choose(config, task()) == ModelChoice("claude", "claude-opus", None, "claude-opus")
    assert choose(config, task(), runner_override="codex") == ModelChoice("codex", None)
    assert choose(config, task(), runner_override="claude") == ModelChoice("claude", "claude-opus", None, "claude-opus")
    assert choose(config, task(), runner_override="codex", model_override="gpt-5") == ModelChoice("codex", "gpt-5")
    assert choose(config, task(), model_override="claude-haiku") == ModelChoice("claude", "claude-haiku")
    session = task(stage=Stage.FIX, interactive=True, output_schema=None, role="fix-session", route="fix.session")
    assert choose(config, session) == ModelChoice("claude", None, None, "claude")
    with pytest.raises(RunnerConfigError, match="routes.default 或 routes.learn.rule-writer"):
        choose(config, task(stage=Stage.LEARN, route="learn.rule-writer"))
    with pytest.raises(RunnerConfigError, match="没有调用点"):
        choose(config, task(route=None))


def test_conditions_pick_the_conditional_route_and_effort(make_config):
    models = {**make_config().data["models"],
              "claude-max": {"tool": "claude", "model": "claude-max", "effort": "high"}}
    routes = {**make_config().data["routes"], "fix.planner.high-risk": "claude-max", "fix.planner.large": "claude-opus"}
    config = make_config(models=models, routes=routes)
    planner = task(stage=Stage.FIX, role="fix-planner", route="fix.planner")
    assert choose(config, planner).model == "gpt-5"
    assert choose(config, replace(planner, conditions=("high-risk", "large"))) == (
        ModelChoice("claude", "claude-max", "high", "claude-max"))
    assert choose(config, replace(planner, conditions=("large",))).model == "claude-opus"


def test_session_records(tmp_path):
    clock = FixedClock(NOW)
    conn = open_database(tmp_path / "tightrein.db", clock)
    session_task = task(stage=Stage.FIX, role="fix-session", workdir=Path("/ws/worktrees/fix-0007"))
    first = sessions.open_session(conn, session_task, "codex", NOW, None)
    assert (first.status, first.session_id, first.workdir) == (AgentSessionStatus.OPEN, None, "/ws/worktrees/fix-0007")
    attached = sessions.attach(conn, first, "0199b000")
    assert sessions.attach(conn, attached, None) == attached
    closed = sessions.close(conn, attached, NOW + timedelta(minutes=30))
    assert (closed.status, closed.ended_at) == (AgentSessionStatus.CLOSED, NOW + timedelta(minutes=30))
    second = sessions.open_session(conn, session_task, "codex", NOW + timedelta(hours=1), "0199b001")
    assert sessions.latest(conn, session_task) == second
    assert sessions.latest(conn, task()) is None
    conn.close()
