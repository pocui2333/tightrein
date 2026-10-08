import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from tightrein.onboard import check
from tightrein.onboard.check import FAILED, PASSED, SKIPPED, Report, Trial, interpret
from tightrein.onboard.setup import MODULES, ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import state

SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "required": ["signals"],
          "properties": {"signals": {"type": "array", "items": {"type": "object", "required": ["message"]}}}}


class FakeRunner:
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        self.commands: list[Command] = []
        self.scratch_existed: bool | None = None

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        self.scratch_existed = Path(json.loads(command.stdin)["scratch"]).is_dir()
        return self.outcome


def outcome(stdout: str, exit_code: int | None = 0, stderr: str = "", **extra: str | None) -> Outcome:
    return Outcome(exit_code=exit_code, stdout=stdout, stderr_tail=stderr, duration_ms=5,
                   stopped_by=extra.get("stopped_by"), start_error=extra.get("start_error"))


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout(tmp_path / "workspaces" / "shop")
    layout.scripts_dir.mkdir(parents=True)
    (layout.scripts_dir / "probe.py").write_text("print('{}')\n", encoding="utf-8")
    return layout


@pytest.fixture
def conn(workspace: WorkspaceLayout, clock) -> Iterator[sqlite3.Connection]:
    connection = open_database(workspace.database, clock=clock)
    yield connection
    connection.close()


@pytest.fixture
def guide(tmp_path: Path) -> str:
    path = tmp_path / "methods" / "02-db.md"
    path.parent.mkdir()
    path.write_text("# 方法\n", encoding="utf-8")
    path.with_suffix(".schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    return str(path)  # 绝对路径：接在包根目录之后仍是它自己


def custom(guide: str, secrets: tuple[str, ...] = ()) -> ModuleSetup:
    return ModuleSetup("collect.access_log", ModuleStatus.CUSTOM, None, "scripts/probe.py", guide, "自己的库", None,
                       secrets)


def runtime(workspace: WorkspaceLayout, runner: FakeRunner, modules: dict[str, ModuleSetup], conn=None, clock=None,
            secrets: dict[str, str] | None = None) -> SimpleNamespace:
    settings = Settings.from_data({"limits": {"timeouts": {"command": "5m", "tests": "15m"}}})
    return SimpleNamespace(workspace=workspace, runner=runner, settings=settings, redactor=Redactor(),
                           environ={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-x"}, secrets=secrets or {},
                           setup=Setup("shop", "2026-10-08T00:00:00Z", modules), conn=conn, clock=clock)


# 结果归类


def test_an_error_response_is_taken_even_with_a_nonzero_exit():
    trial = interpret("m", 3, json.dumps({"signals": []}), "boom", start_error=None, stopped_by=None, schema=SCHEMA)
    assert trial.status == PASSED and "以响应为准" in trial.detail


def test_protocol_errors():
    trial = interpret("m", 0, "not json\n", "", start_error=None, stopped_by=None, schema=None)
    assert trial == Trial("m", FAILED, "协议错误：标准输出不是单个 JSON 对象")
    trial = interpret("m", 0, "[1, 2]", "", start_error=None, stopped_by=None, schema=None)
    assert trial.status == FAILED and "单个 JSON 对象" in trial.detail
    crashed = interpret("m", 2, "", "line1\nline2\n", start_error=None, stopped_by=None, schema=None)
    assert crashed.status == FAILED and crashed.errors == ("line1", "line2") and "退出码 2" in crashed.detail


def test_schema_violations_list_every_path():
    stdout = json.dumps({"signals": [{"message": "ok"}, {}, "x"]})
    trial = interpret("m", 0, stdout, "", start_error=None, stopped_by=None, schema=SCHEMA)
    assert trial.status == FAILED and len(trial.errors) == 2
    assert any("/signals/1" in error for error in trial.errors) and any("/signals/2" in error for error in trial.errors)


def test_unstartable_and_stopped_scripts_fail():
    assert interpret("m", None, "", "", start_error="没有这个文件", stopped_by=None, schema=None).status == FAILED
    assert interpret("m", None, "", "", start_error=None, stopped_by="timeout", schema=None).detail == "被终止(timeout)"


# 跑脚本


def test_a_script_gets_a_request_a_scratch_directory_and_only_its_secrets(workspace, guide):
    runner = FakeRunner(outcome(json.dumps({"signals": [{"message": "x"}]})))
    module = custom(guide, ("db.password",))
    trial = check.trial_script(runtime(workspace, runner, {module.key: module},
                                       secrets={"db.password": "s3cret", "other": "no"}), module)
    assert trial.status == PASSED
    command = runner.commands[0]
    request = json.loads(command.stdin)
    assert request["trial"] is True and request["module"] == "collect.access_log"
    assert runner.scratch_existed and not Path(request["scratch"]).exists()
    assert command.argv[1] == str(workspace.root / "scripts" / "probe.py") and command.cwd == workspace.root
    assert command.env["TIGHTREIN_SECRET_DB_PASSWORD"] == "s3cret"
    assert "ANTHROPIC_API_KEY" not in command.env and not any("other" in value for value in command.env.values())


def test_a_missing_secret_fails_without_running(workspace, guide):
    runner = FakeRunner(outcome("{}"))
    module = custom(guide, ("db.password",))
    trial = check.trial_script(runtime(workspace, runner, {module.key: module}), module)
    assert trial.status == FAILED and runner.commands == []


# 试跑记录与就绪


def test_check_stores_the_report_and_ready_needs_an_unchanged_passing_report(workspace, guide, conn, clock):
    workspace.setup.write_text('{"modules": {}}', encoding="utf-8")
    module = custom(guide)
    modules = {key: ModuleSetup(key, ModuleStatus.DISABLED, None, None, None, "不用", "无") for key in MODULES}
    modules[module.key] = module
    modules["release.accept"] = ModuleSetup("release.accept", ModuleStatus.ENABLED, None, None, None, None, None)
    current = runtime(workspace, FakeRunner(outcome(json.dumps({"signals": []}))), modules, conn, clock)
    current.settings.project = SimpleNamespace(commands={"test": None})
    state.put(conn, check.READY_KEY, {"at": "x"}, clock)
    report = check.check(current)
    assert [(trial.key, trial.status) for trial in report.trials] == [
        ("collect.access_log", PASSED), ("release.accept", SKIPPED), ("baseline", SKIPPED)]
    assert state.get(conn, check.READY_KEY) is None
    stored = check.last_report(conn)
    assert stored == report and stored.passed
    assert check.ready_problems(stored, check.setup_hash(workspace.setup)) == []
    workspace.setup.write_text('{"modules": {"changed": true}}', encoding="utf-8")
    assert check.ready_problems(stored, check.setup_hash(workspace.setup)) == [
        "setup.json 在上次试跑之后改过：重新执行 tightrein project check"]


class CommandRunner:
    """基线检查用：按命令行给结果。"""

    def __init__(self, outcomes: dict[str, Outcome]) -> None:
        self.outcomes = outcomes
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        return self.outcomes[" ".join(command.argv)]


def test_the_baseline_runs_every_check_command_once_on_the_main_branch(workspace, monkeypatch):
    synced = SimpleNamespace(worktree=workspace.root / "worktrees" / "baseline", commit="c" * 40)
    created: list[Path] = []
    monkeypatch.setattr(check, "create_readonly", lambda git, path, scope: created.append(path))
    monkeypatch.setattr(check, "sync_readonly", lambda git, path, marker: synced)
    runner = CommandRunner({"pytest -q": outcome("", 0), "ruff check .": outcome("", 1, "E1 bad\nE2 worse")})
    current = runtime(workspace, runner, {})
    current.settings.project = SimpleNamespace(commands={"test": "pytest -q", "lint": "ruff check .", "build": None})
    current.git, current.scope = object(), lambda project, point: None
    trials = check.run_baseline(current)
    assert [(trial.key, trial.status) for trial in trials] == [("baseline.test", PASSED), ("baseline.lint", FAILED)]
    assert trials[1].detail == "`ruff check .` 在 cccccccccccc 上不通过(退出码 1)"
    assert trials[1].errors == ("E1 bad", "E2 worse")
    assert created == [workspace.worktree(check.BASELINE_WORKTREE)]  # 只读 worktree，接入期不改代码
    assert [command.argv for command in runner.commands] == [("pytest", "-q"), ("ruff", "check", ".")]
    assert all(command.cwd == synced.worktree and "ANTHROPIC_API_KEY" not in command.env
               for command in runner.commands)


def test_ready_lists_every_failed_trial():
    report = Report("t", "h", [Trial("a", FAILED, "连不上"), Trial("b", PASSED, ""), Trial("c", FAILED, "格式不对")])
    assert check.ready_problems(report, "h") == ["a：连不上", "c：格式不对"]
    assert check.ready_problems(None, "h") == ["还没有试跑：先执行 tightrein project check"]


def test_only_one_module_keeps_the_other_results(workspace, guide, conn, clock):
    workspace.setup.write_text("{}", encoding="utf-8")
    module = custom(guide)
    current = runtime(workspace, FakeRunner(outcome("bad")), {module.key: module}, conn, clock)
    state.put(conn, check.STATE_KEY, Report("t", "h", [Trial("baseline.test", PASSED, "ok"),
                                                         Trial(module.key, PASSED, "ok")]).to_json(), clock)
    report = check.check(current, only=module.key)
    assert [(trial.key, trial.status) for trial in report.trials] == [("baseline.test", PASSED), (module.key, FAILED)]
    with pytest.raises(LookupError):
        check.check(current, only="collect.nothing")
