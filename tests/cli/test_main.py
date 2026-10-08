import argparse
import json
import os
import signal

import pytest

from tightrein.cli import exit_codes, main
from tightrein.cli.exit_codes import Refused, UsageError
from tightrein.onboard.setup import SetupInvalid
from tightrein.protocol import schedule
from tightrein.protocol.process import Interrupted
from tightrein.store.locks import Busy, FileLock
from tightrein.store.tables import runs

OTHER_RUN = "R-20261008T020000Z-collect"


def start_run(runtime, *, pid: int, host: str, run_id: str | None = None) -> None:
    runs.start(runtime.conn, runs.Run(id=run_id or runtime.run, stage="run", trigger="manual", status="running",
                                      started_at=runtime.clock.now(), holder_pid=pid, holder_host=host))


# --json 与退出码


def test_json_output_is_a_single_object(cli):
    data = cli.json("run", "--dry-run")
    assert set(data) == {"command", "status", "exitCode", "result", "next", "errors"}
    assert (data["command"], data["status"], data["exitCode"]) == ("run", "ok", 0)
    assert [item["stage"] for item in data["result"]["planned"]] == ["retro"]  # 来源全部不启用、没有对象
    usage = cli.json("run", "--no-such-option")
    assert (usage["status"], usage["exitCode"]) == ("usage", exit_codes.USAGE)
    assert usage["errors"][0]["type"] == "UsageError"


def test_errors_map_to_exit_codes(cli, tmp_path):
    assert exit_codes.for_error(UsageError("x")) == exit_codes.USAGE
    assert exit_codes.for_error(SetupInvalid(tmp_path, ["漏写"])) == exit_codes.USAGE
    assert exit_codes.for_error(LookupError("没有 0042")) == exit_codes.USAGE
    assert exit_codes.for_error(Refused("暂停中")) == exit_codes.REFUSED
    assert exit_codes.for_error(Busy(tmp_path / "run.lock", {"pid": 1})) == exit_codes.REFUSED
    assert exit_codes.for_error(RuntimeError("坏了")) == exit_codes.FAILED
    assert exit_codes.for_interrupt(KeyboardInterrupt()) == 130
    assert exit_codes.for_interrupt(Interrupted(signal.SIGTERM)) == 143
    assert exit_codes.for_interrupt(Interrupted(signal.SIGHUP)) == 129
    unknown = cli("status", "-p", "nope")
    assert unknown.code == exit_codes.USAGE and "nope" in unknown.out
    assert "Traceback" not in unknown.out + unknown.err
    missing = cli.json("show", "0042")
    assert missing["exitCode"] == exit_codes.USAGE and missing["errors"][0]["type"] in ("LookupError", "UsageError")


@pytest.mark.parametrize("argv, new", [
    (["continue"], "run"),
    (["find", "保存"], "show --search"),
    (["triage"], "run assess"),
    (["issue", "list"], "show / approve / reject"),
    (["problem", "ignore", "P-0001"], "problem mute"),
    (["admin", "third-party", "list"], "vendor/README.md"),
])
def test_old_commands_are_refused_with_the_new_form(cli, argv, new):
    result = cli(*argv)
    assert result.code == exit_codes.USAGE
    assert new in result.out
    data = cli.json(*argv)
    assert data["exitCode"] == exit_codes.USAGE and new in data["errors"][0]["message"]
    assert not cli.layout.database.exists()  # 没有执行：连数据库都没打开


def test_no_command_means_status_and_help_is_not_a_command():
    assert main.with_default_command([]) == ["status"]
    assert main.with_default_command(["--json"]) == ["status", "--json"]
    assert main.with_default_command(["--help"]) == ["--help"]
    assert main.renamed(["show", "0019"]) is None
    assert main.renamed(["--json", "continue"]) == ("continue", "run")


def test_help_texts_exist_in_both_languages():
    for language in ("zh", "en"):
        main.build_parser.cache_clear()
        main.build_parser(language)  # 缺键时 text() 报错


def _commands(parser, path=()):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _commands(child, (*path, name))
    yield path, parser


def test_positional_arguments_and_the_problem_status_filter_have_help():
    for language in ("zh", "en"):
        main.build_parser.cache_clear()
        missing = [(" ".join(path), action.dest) for path, parser in _commands(main.build_parser(language))
                   for action in parser._actions
                   if not isinstance(action, argparse._SubParsersAction) and not action.help
                   and (not action.option_strings or (path == ("problem", "list") and action.dest == "status"))]
        assert missing == [], language


def test_changes_need_a_confirmation_or_yes(cli):
    refused = cli("pause")  # 标准输入不是终端、没给 --yes：不当成同意
    assert refused.code == exit_codes.USAGE and not cli.layout.control_file.exists()
    assert cli("pause", "--yes").code == exit_codes.OK and cli.layout.control_file.exists()
    cli.externals.stdin_is_tty = lambda: True
    declined = cli("resume", stdin="n\n")
    assert declined.code == exit_codes.OK and cli.layout.control_file.exists()
    assert cli("resume", stdin="y\n").code == exit_codes.OK and not cli.layout.control_file.exists()


# 命令结束时就地收尾


@pytest.mark.parametrize("stopped, code", [
    (KeyboardInterrupt(), 130), (Interrupted(signal.SIGTERM), 143), (Interrupted(signal.SIGHUP), 129)])
def test_runs_left_running_by_the_command_are_closed_and_its_locks_released(cli, monkeypatch, stopped, code):
    def interrupted(runtime, **options):
        start_run(runtime, pid=os.getpid(), host=cli.externals.host)
        FileLock(runtime.workspace.run_lock, runtime.clock, stale_s=90).acquire()
        raise stopped

    monkeypatch.setattr(schedule, "run", interrupted)
    result = cli("run")
    assert result.code == code
    conn = cli.conn()
    left = runs.latest(conn)
    assert left.status == "interrupted"
    assert FileLock(cli.layout.run_lock, cli.externals.clock, stale_s=90).holder() is None
    conn.close()


def test_runs_ended_normally_and_runs_of_other_processes_are_left_alone(cli, monkeypatch):
    def failing(runtime, **options):
        start_run(runtime, pid=os.getpid(), host=cli.externals.host)
        start_run(runtime, pid=4242, host="other-host", run_id=OTHER_RUN)
        raise RuntimeError("模块漏了结束运行")

    monkeypatch.setattr(schedule, "run", failing)
    data = cli.json("run")
    assert data["exitCode"] == exit_codes.FAILED and data["errors"][0]["message"] == "模块漏了结束运行"
    conn = cli.conn()
    statuses = {run["id"]: run["status"] for run in conn.execute("SELECT id, status FROM runs")}
    conn.close()
    assert statuses[OTHER_RUN] == "running"
    assert sorted(statuses.values()) == ["failed", "running"]


def test_a_closing_failure_does_not_replace_the_result(cli, monkeypatch):
    def broken_close(*args, **options):
        raise OSError("磁盘满")

    monkeypatch.setattr(main.recovery, "close_own", broken_close)
    result = cli("pause", "--yes")
    assert result.code == exit_codes.OK
    assert "磁盘满" in result.err
    assert json.loads(cli("resume", "--yes", "--json").out)["exitCode"] == exit_codes.OK
