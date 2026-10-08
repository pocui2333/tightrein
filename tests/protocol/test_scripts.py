import json

import pytest

from tightrein.collect.common.source import SourceMisconfigured, SourceUnavailable
from tightrein.protocol import scripts
from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor


class FakeRunner:
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        return self.outcome


def outcome(exit_code=0, stdout="{}", stderr="", stopped_by=None, start_error=None):
    return Outcome(exit_code, stdout, stderr, 5, stopped_by, start_error)


def run(tmp_path, runner, secret_names=(), secrets=None):
    redactor = Redactor()
    for value in (secrets or {}).values():
        redactor.register(value)
    return scripts.run(name="daily-import", command=("{python}", "scripts/a.py"), document={"name": "daily-import"},
                       workspace=tmp_path, runner=runner,
                       environ={"PATH": "/bin", "HOME": "/h", "GITHUB_TOKEN": "ghp_x", "AWS_SECRET": "y"},
                       secrets=secrets or {}, secret_names=secret_names, redactor=redactor,
                       raw=RawDir(tmp_path / "raw"), timeout_s=30)


def test_scripts_get_the_input_and_only_the_registered_secrets(tmp_path):
    runner = FakeRunner(outcome(stdout='{"signals": []}'))
    stdout = run(tmp_path, runner, ["demo.readonly"], {"demo.readonly": "s3cret", "other": "x"})
    assert stdout == '{"signals": []}'
    [command] = runner.commands
    assert command.argv[1] == "scripts/a.py" and command.argv[0] != "{python}" and command.cwd == tmp_path
    assert json.loads(command.stdin) == {"name": "daily-import"} and command.timeout_s == 30
    assert command.env["TIGHTREIN_SECRET_DEMO_READONLY"] == "s3cret"
    assert command.env["TIGHTREIN_SCRIPT_NAME"] == "daily-import"
    assert "GITHUB_TOKEN" not in command.env and "AWS_SECRET" not in command.env
    assert not any(name.startswith("TIGHTREIN_SECRET_OTHER") for name in command.env)


def test_failures_are_typed_and_stderr_is_redacted(tmp_path):
    runner = FakeRunner(outcome(exit_code=3, stderr="token=s3cret\nTraceback\nboom"))
    with pytest.raises(SourceUnavailable, match="退出码 3"):
        run(tmp_path, runner, ["demo.readonly"], {"demo.readonly": "s3cret"})
    stderr = (tmp_path / "raw" / "daily-import.stderr.log").read_text(encoding="utf-8")
    assert "s3cret" not in stderr and "boom" in stderr
    with pytest.raises(SourceUnavailable, match="timeout"):
        run(tmp_path, FakeRunner(outcome(exit_code=None, stopped_by="timeout")))
    with pytest.raises(SourceUnavailable, match="无法启动"):
        run(tmp_path, FakeRunner(outcome(exit_code=None, start_error="No such file")))
    with pytest.raises(SourceMisconfigured, match="demo.readonly"):
        run(tmp_path, FakeRunner(outcome()), ["demo.readonly"], {})


def test_command_for_a_registered_script():
    assert scripts.command_for("scripts/access.py") == ("{python}", "scripts/access.py")
    assert scripts.command_for("scripts/access.sh") == ("scripts/access.sh",)
    with pytest.raises(SourceMisconfigured):
        scripts.command_for("")
    assert scripts.secret_env("loki.token") == "TIGHTREIN_SECRET_LOKI_TOKEN"
