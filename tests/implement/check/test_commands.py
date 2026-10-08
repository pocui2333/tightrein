from __future__ import annotations

import re
from pathlib import Path

from tightrein.implement.check import commands
from tightrein.implement.check.affected import Planned, ProjectCommand
from tightrein.implement.check.commands import CommandSettings
from tightrein.protocol.process import Command, Outcome

SETTINGS = CommandSettings(timeout_s=900, environment=(re.compile("Connection refused", re.IGNORECASE),),
                           environment_retries=1, test_failure_exit_codes=frozenset({1}), output_chars=8000,
                           log_tail_bytes=1 << 20)
TEST = ProjectCommand("test", "pytest -q {tests}")


class Scripted:
    """依次返回预置结果，stdout 写进 stdout_path(与真实的 ProcessRunner 一致)。"""

    def __init__(self, *outcomes: tuple[int | None, str, str | None, str | None]) -> None:
        self.outcomes = list(outcomes)
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        code, stdout, stopped, start_error = self.outcomes.pop(0)
        assert command.stdout_path is not None
        command.stdout_path.write_text(stdout, encoding="utf-8")
        return Outcome(code, "", "warning on stderr\n", 1, stopped, start_error)


def _run(tmp_path: Path, runner: Scripted, planned: Planned | None = None) -> commands.CommandResult:
    return commands.run(planned or Planned(TEST), worktree=tmp_path, log=tmp_path / "logs" / "test.log", runner=runner,
                        environ={"PATH": "/usr/bin", "SECRET_TOKEN": "x"}, settings=SETTINGS)


def test_commands_are_split_without_a_shell_and_logged(tmp_path: Path) -> None:
    runner = Scripted((0, "tests/test_a.py::test_ok PASSED\n", None, None))
    result = _run(tmp_path, runner, Planned(TEST, ("tests/test_a.py",)))
    assert result.passed and result.exit_code == 0 and result.attempts == 1
    command = runner.commands[0]
    assert command.argv == ("pytest", "-q", "tests/test_a.py") and command.cwd == tmp_path
    assert command.timeout_s == 900 and "SECRET_TOKEN" not in command.env
    assert (tmp_path / "logs" / "test.log").read_text().endswith("warning on stderr\n")
    assert result.to_json()["tests"] == ["tests/test_a.py"]


def test_assertion_failures_are_not_retried_and_the_output_is_trimmed(tmp_path: Path) -> None:
    output = "tests/test_a.py::test_ok PASSED\nFAILED tests/test_a.py::test_b - AssertionError\nE   assert 1 == 2\n"
    runner = Scripted((1, output, None, None))
    result = _run(tmp_path, runner)
    assert result.result == commands.FAILED and result.attempts == 1 and len(runner.commands) == 1
    assert result.output is not None and "E   assert 1 == 2" in result.output and "test_ok PASSED" not in result.output


def test_environment_failures_are_retried_once(tmp_path: Path) -> None:
    flaky = Scripted((1, "psycopg: Connection refused\n", None, None), (0, "ok\n", None, None))
    assert _run(tmp_path, flaky).passed and len(flaky.commands) == 2
    broken = Scripted((1, "Connection refused\n", None, None), (1, "Connection refused\n", None, None))
    result = _run(tmp_path, broken)
    # 重试后仍是环境问题：记「未运行」而不是失败
    assert result.result == commands.NOT_RUN and result.attempts == 2 and "环境问题" in (result.reason or "")


def test_unexpected_exit_codes_are_invalid_and_unstartable_commands_did_not_run(tmp_path: Path) -> None:
    invalid = _run(tmp_path, Scripted((5, "no tests ran\n", None, None)))
    assert invalid.result == commands.INVALID and "收集错误" in (invalid.reason or "")
    lint = _run(tmp_path, Scripted((5, "E501\n", None, None)), Planned(ProjectCommand("lint", "ruff check .")))
    assert lint.result == commands.FAILED
    missing = _run(tmp_path, Scripted((None, "", None, "No such file: pytest")))
    assert missing.result == commands.NOT_RUN and "无法启动" in (missing.reason or "")


def test_timeouts_fail_with_an_empty_exit_code(tmp_path: Path) -> None:
    result = _run(tmp_path, Scripted((None, "slow\n", "timeout", None)))
    assert result.result == commands.FAILED and result.exit_code is None and "timeout" in (result.reason or "")
    assert commands.summary([result]) == {"test": commands.FAILED}
