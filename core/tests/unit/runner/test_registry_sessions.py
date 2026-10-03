from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from runner_samples import task

from tightrein.config.capabilities import ModelChoice
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
    config = make_config(roleCapabilities={"claim-verifier": "deep"})
    assert choose(config, task()) == ModelChoice("claude", "claude-opus", "deep")
    assert choose(config, task(), runner_override="codex") == ModelChoice("codex", "gpt-5", "deep")
    assert choose(config, task(tool="claude"), runner_override="codex") == ModelChoice("claude", "claude-opus", "deep")
    assert choose(config, task(), model_override="claude-haiku") == ModelChoice("claude", "claude-haiku")
    assert choose(config, task(model="claude-sonnet"), model_override="claude-haiku") == (
        ModelChoice("claude", "claude-sonnet")
    )
    session = task(stage=Stage.FIX, interactive=True, output_schema=None, role="fix-session")
    assert choose(config, session) == ModelChoice("claude", None)
    with pytest.raises(RunnerConfigError):
        choose(config, task(stage=Stage.LEARN))


def test_roles_take_their_default_capability_and_effort(make_config):
    standard = make_config().data["capabilities"]["standard"]  # 共用配置的 refuter 用 standard 档
    capabilities = {"deep": {"claude": {"model": "claude-opus", "inputUsdPerMTok": 15, "outputUsdPerMTok": 75},
                             "codex": {"model": "gpt-5", "inputUsdPerMTok": 1.25, "outputUsdPerMTok": 10}},
                    "standard": standard,
                    "strong": {"claude": {"model": "claude-max", "effort": "high", "inputUsdPerMTok": 15,
                                          "outputUsdPerMTok": 75}}}
    config = make_config(capabilities=capabilities)
    assert choose(config, task()) == ModelChoice("claude", "claude-max", "strong", "high")
    assert choose(config, task(role="unlisted-role")) == ModelChoice("claude", "claude-opus", "deep")
    assert choose(config, task(effort="low")) == ModelChoice("claude", "claude-max", "strong", "low")
    assert choose(config, task(model="claude-haiku")) == ModelChoice("claude", "claude-haiku")
    overridden = make_config(capabilities=capabilities,
                             stages={"triage": {"tool": "claude", "roles": {"claim-verifier": {"capability": "deep"}}}})
    assert choose(overridden, task()) == ModelChoice("claude", "claude-opus", "deep")
    with pytest.raises(RunnerConfigError, match="capabilities.strong.claude"):
        choose(make_config(capabilities={"deep": capabilities["deep"], "standard": standard}), task())


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
