import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import REDACTED, Redactor
from tightrein.observability.tracing import Tracer
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.errors import (
    CommandFailed,
    GhAuthError,
    GhCommandError,
    GhNotFound,
    GitCommandError,
    GitNotFound,
    NetworkError,
    ProgramNotFound,
    VcsError,
)
from tightrein.vcs.process import Command, Completed, VcsProcess, subprocess_executor


class Script:
    """按顺序返回预置结果的假执行函数；结果为异常时抛出。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, stdout, stderr = outcome
        return Completed(command.argv, returncode, stdout, stderr)


def process(script, **options):
    sleeps = []
    vcs = VcsProcess(execute=script, environ={"PATH": "/usr/bin", "LANG": "zh_CN.UTF-8"}, sleep=sleeps.append,
                     **options)
    return vcs, sleeps


def test_git_runs_with_fixed_environment(tmp_path):
    script = Script((0, "abc\n", ""))
    vcs, _ = process(script)
    assert vcs.git(tmp_path, "rev-parse", "HEAD").stdout == "abc\n"
    command = script.commands[0]
    assert command.argv == ("git", "rev-parse", "HEAD")
    assert command.cwd == tmp_path
    assert command.env == {"PATH": "/usr/bin", "LANG": "zh_CN.UTF-8", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def test_missing_programs(tmp_path):
    vcs, _ = process(Script(FileNotFoundError(2, "No such file"), FileNotFoundError(2, "No such file"),
                            FileNotFoundError(2, "No such file")))
    with pytest.raises(GitNotFound) as caught:
        vcs.git(tmp_path, "status")
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    with pytest.raises(GhNotFound):
        vcs.gh(tmp_path, "pr", "list")
    with pytest.raises(ProgramNotFound):
        vcs.run(["ln", "-s", "a", "b"], tmp_path)


def test_missing_working_directory_is_reported(tmp_path):
    vcs, _ = process(Script())
    with pytest.raises(VcsError, match="工作目录不存在"):
        vcs.git(tmp_path / "gone", "status")


def test_gh_exit_code_4_is_an_auth_error(tmp_path):
    vcs, _ = process(Script((4, "", "To get started with GitHub CLI, please run:  gh auth login\n"),
                            (1, "", "GraphQL: Could not resolve to a PullRequest\n")))
    with pytest.raises(GhAuthError) as caught:
        vcs.gh(tmp_path, "pr", "view", "7")
    assert caught.value.returncode == 4
    with pytest.raises(GhCommandError):
        vcs.gh(tmp_path, "pr", "view", "7")


def test_remote_failures_are_network_errors_and_reads_retry(tmp_path):
    failure = (128, "", "fatal: unable to access 'https://github.com/o/r/': Could not resolve host\n")
    vcs, sleeps = process(Script(failure, failure, (0, "", "")))
    vcs.git(tmp_path, "fetch", "origin", retry=True)
    assert sleeps == [5, 20]
    vcs, sleeps = process(Script(failure, failure, failure))
    with pytest.raises(NetworkError):
        vcs.git(tmp_path, "fetch", "origin", retry=True)
    assert sleeps == [5, 20]


def test_writes_are_not_retried(tmp_path):
    failure = (128, "", "fatal: unable to access remote\n")
    script = Script(failure)
    vcs, sleeps = process(script)
    with pytest.raises(NetworkError):
        vcs.git(tmp_path, "push", "--porcelain", "-u", "origin", "cty/fix-order")
    assert (len(script.commands), sleeps) == (1, [])


def test_other_exit_codes_are_command_errors(tmp_path):
    vcs, _ = process(Script((128, "", "fatal: not a git repository\n"), (1, "", "")))
    with pytest.raises(GitCommandError) as caught:
        vcs.git(tmp_path, "status")
    assert caught.value.stderr == "fatal: not a git repository"
    assert str(caught.value) == "git status 退出码 128：fatal: not a git repository"
    assert vcs.git(tmp_path, "merge-base", "--is-ancestor", "a", "b", ok_codes=(0, 1)).returncode == 1


def test_timeouts(tmp_path):
    timeout = subprocess.TimeoutExpired(["git"], 300)
    vcs, _ = process(Script(timeout, timeout))
    with pytest.raises(NetworkError):
        vcs.git(tmp_path, "fetch", "origin")
    with pytest.raises(CommandFailed) as caught:
        vcs.git(tmp_path, "log")
    assert not isinstance(caught.value, NetworkError)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


def test_fetch_has_its_own_timeout(tmp_path):
    script = Script((0, "", ""), (0, "", ""))
    vcs, _ = process(script, timeout=300, fetch_timeout=45)
    vcs.git(tmp_path, "fetch", "origin", timeout=vcs.fetch_timeout)
    vcs.git(tmp_path, "status")
    assert [command.timeout for command in script.commands] == [45, 300]
    timeout = subprocess.TimeoutExpired(["git"], 45)
    slow, _ = process(Script(timeout), fetch_timeout=45)
    with pytest.raises(NetworkError, match="超过 45 秒未结束"):
        slow.git(tmp_path, "fetch", "origin", timeout=slow.fetch_timeout)


def test_error_text_is_redacted(tmp_path):
    url = "https://cty:ghp_abcdefghijklmnopqrstuvwx@github.com/o/r"
    vcs, _ = process(Script((128, "", f"fatal: could not read from {url}\n")))
    with pytest.raises(NetworkError) as caught:
        vcs.git(tmp_path, "fetch", url)
    assert "ghp_" not in str(caught.value)
    assert REDACTED in caught.value.stderr


def test_other_commands_do_not_raise_on_failure(tmp_path):
    vcs, _ = process(Script((1, "", "ln: b: File exists\n")))
    assert vcs.run(["ln", "-s", "a", "b"], tmp_path).returncode == 1


def test_each_command_writes_a_run_script_span(tmp_path):
    layout = WorkspaceLayout(tmp_path / "ws")
    clock = FixedClock(datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc))
    tracer = Tracer(EventLog(layout, Redactor()), clock, run_id="R-20261005-030000-release", stage="release")
    vcs, _ = process(Script((0, "", ""), (1, "", "")), tracer=tracer)
    vcs.git(tmp_path, "status")
    with pytest.raises(GitCommandError):
        vcs.git(tmp_path, "commit", "-F", "message.txt")
    written = events.read(layout.events_log(clock.now().date()))
    assert [event.operation for event in written] == ["run_script", "run_script"]
    assert [event.attributes for event in written] == [
        {"argv": "git status", "exitCode": 0}, {"argv": "git commit -F message.txt", "exitCode": 1},
    ]


def test_the_subprocess_executor_runs_real_programs(tmp_path):
    result = subprocess_executor(Command(("git", "--version"), tmp_path, {"PATH": "/usr/bin:/bin:/usr/local/bin"}, 30))
    assert result.returncode == 0
    assert result.stdout.startswith("git version")


def test_the_subprocess_executor_does_not_inherit_the_terminal(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    result = subprocess_executor(Command(("sh", "-c", "cat; tty || true"), tmp_path, env, 30))
    assert result.returncode == 0 and result.stdout.strip() == "not a tty"
    fed = subprocess_executor(Command(("cat",), tmp_path, env, 30, stdin="input"))
    assert fed.stdout == "input"


def test_a_timeout_ends_the_whole_process_group(tmp_path):
    # 孙进程继承了输出管道；只结束子进程时读取输出会一直等到孙进程退出
    command = Command(("sh", "-c", "sleep 30 & sleep 30"), tmp_path, {"PATH": "/usr/bin:/bin"}, 0.5)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess_executor(command)
    assert time.monotonic() - started < 10


PROXY_ENV = {"PATH": "/usr/bin", "https_proxy": "http://127.0.0.1:8118", "no_proxy": "localhost,github.com"}


def rerouting(script):
    noted = []
    vcs = VcsProcess(execute=script, environ=dict(PROXY_ENV), sleep=lambda seconds: None, on_reroute=noted.append)
    return vcs, noted


def test_network_failures_switch_route_once(tmp_path):
    script = Script((128, "", "fatal: unable to access 'https://github.com/o/r/': Failed to connect to github.com\n"),
                    (0, "", ""))
    vcs, noted = rerouting(script)
    assert vcs.git(tmp_path, "push", "origin", "cty/fix-x").returncode == 0
    first, second = (command.env for command in script.commands)
    assert first["no_proxy"] == "localhost,github.com" and second["no_proxy"] == "localhost"
    assert [(item.before, item.after, item.succeeded) for item in noted] == [("直连", "经代理", True)]
    assert noted[0].text().startswith("git push origin cty/fix-x：直连失败(fatal: unable to access")
    assert vcs.reroutes == noted


def test_timeouts_on_gh_switch_route_and_still_failing_raises(tmp_path):
    timeout = subprocess.TimeoutExpired(["gh"], 300)
    vcs, noted = rerouting(Script(timeout, (1, "", "error connecting to api.github.com\n")))
    with pytest.raises(GhCommandError):
        vcs.gh(tmp_path, "pr", "view", "7")
    assert [(item.before, item.after, item.succeeded) for item in noted] == [("直连", "经代理", False)]


def test_other_failures_and_local_commands_do_not_switch_route(tmp_path):
    rejected = (1, "", " ! [rejected]        cty/fix-x -> cty/fix-x (non-fast-forward)\n")
    vcs, noted = rerouting(Script(rejected, (4, "", "gh auth login\n"), (128, "", "Connection reset by peer\n")))
    with pytest.raises(GitCommandError):
        vcs.git(tmp_path, "push", "origin", "cty/fix-x")
    with pytest.raises(GhAuthError):
        vcs.gh(tmp_path, "pr", "list")
    with pytest.raises(GitCommandError):
        vcs.git(tmp_path, "status")
    assert noted == []
    without_proxy, _ = process(Script((128, "", "Could not resolve host: github.com\n")))
    with pytest.raises(NetworkError):
        without_proxy.git(tmp_path, "fetch", "origin")
    assert without_proxy.reroutes == []


def test_an_interrupt_kills_the_process_group_of_the_subprocess_executor(tmp_path, monkeypatch):
    started = []
    original = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        if not started:
            started.append(self.pid)
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        subprocess_executor(Command(("sh", "-c", "sleep 30 & wait"), tmp_path, {"PATH": "/usr/bin:/bin"}, 30))
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(started[0], 0)
