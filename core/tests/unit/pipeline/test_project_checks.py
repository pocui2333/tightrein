import pytest

from tightrein.pipeline.checks import project_checks
from tightrein.pipeline.checks.project_checks import Affected, CheckCommand
from tightrein.sources.common.procs import ToolRun

UNIT = CheckCommand("unit", "web", "make unit", when=("web/src/**",),
                    affected=Affected(((r"^web/src/(.*)\.js$", r"web/src/\1.test.js"),), "make unit {tests}"))
BUILD = CheckCommand("build", ".", "make build")
SORT = CheckCommand("i18n-sort", "web", "make sort-lang", when=("web/src/lang/*",), must_not_modify=True)


class Launcher:
    def __init__(self, run=ToolRun(0), effect=None):
        self.run = run
        self.effect = effect
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if self.effect is not None:
            self.effect()
        return self.run


@pytest.fixture
def worktree(tmp_path):
    root = tmp_path / "wt"
    (root / "web" / "src" / "lang").mkdir(parents=True)
    (root / "web" / "src" / "order.test.js").write_text("test\n", encoding="utf-8")
    (root / "web" / "src" / "lang" / "en.js").write_text("a\n", encoding="utf-8")
    return root


def run(worktree, tmp_path, commands, changed, launcher, **options):
    state = lambda root: project_checks.file_state(root, ["web/src/lang/en.js"])  # noqa: E731
    return project_checks.run(commands, worktree, changed, launcher, tmp_path / "logs", timeout=60, state=state,
                              environ={}, **options)


def test_when_selects_commands_and_full_runs_everything():
    assert project_checks.selected([UNIT, BUILD], ["README.md"]) == [BUILD]
    assert project_checks.selected([UNIT, BUILD], ["web/src/order.js"]) == [UNIT, BUILD]
    assert project_checks.selected([UNIT, SORT], ["README.md"], full=True) == [UNIT, SORT]


def test_affected_tests_are_mapped_and_fall_back_to_the_whole_command(worktree):
    assert project_checks.argv_for(UNIT, ["web/src/order.js", "web/src/cart.js"], worktree) == (
        "make", "unit", "src/order.test.js")
    assert project_checks.argv_for(UNIT, ["web/src/cart.js"], worktree) == ("make", "unit")
    assert project_checks.argv_for(UNIT, ["web/src/order.js"], worktree, full=True) == ("make", "unit")


def test_runs_write_logs_and_report_exit_codes(worktree, tmp_path):
    launcher = Launcher(ToolRun(1, "out", "err"))
    [result] = run(worktree, tmp_path, [UNIT], ["web/src/order.js"], launcher)
    assert (result.name, result.command, result.exit_code, result.passed) == (
        "unit", "make unit src/order.test.js", 1, False)
    command = launcher.commands[0]
    assert (command.cwd, command.log_file, command.timeout_seconds) == (
        worktree / "web", tmp_path / "logs" / "unit.log", 60)


def test_must_not_modify_and_unstartable_commands(worktree, tmp_path):
    touch = lambda: (worktree / "web" / "src" / "lang" / "en.js").write_text("b\n", encoding="utf-8")  # noqa: E731
    [sorted_run] = run(worktree, tmp_path, [SORT], ["web/src/lang/en.js"], Launcher(effect=touch))
    assert (sorted_run.exit_code, sorted_run.modified, sorted_run.passed) == (0, ("web/src/lang/en.js",), False)
    [missing] = run(worktree, tmp_path, [BUILD], [], Launcher(ToolRun(None, start_error="make 不存在")))
    assert (missing.not_run, missing.passed, missing.not_run_reason) == (True, False, "无法启动：make 不存在")
    [slow] = run(worktree, tmp_path, [BUILD], [], Launcher(ToolRun(None, timed_out=True)))
    assert (slow.exit_code, slow.not_run, slow.passed) == (None, False, False)


def test_commands_and_timeout_come_from_the_config(make_config):
    config = make_config(checks={"commands": [{"name": "unit", "cwd": "web", "command": "make unit",
                                               "affected": {"map": [{"pattern": "x", "test": "y"}],
                                                            "command": "make unit {tests}"}}]})
    [command] = project_checks.commands(config)
    assert command.affected == Affected((("x", "y"),), "make unit {tests}")
    assert project_checks.commands(make_config()) == []
    assert project_checks.timeout_seconds(config) == 1800.0
