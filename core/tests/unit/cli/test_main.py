import io
import json
from datetime import datetime, timezone

import pytest
from cli_world import make_cli_world
from pipeline_world import save_issue
from store_problem import save_problem

from tightrein.cli import exit_codes
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.main import build_parser, main
from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import (
    IssueStatus,
    OperationExecutor,
    OperationKind,
    OperationStatus,
    Stage,
    ViolationKind,
)
from tightrein.guards.report import GuardBlocked, Violation
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.locks import FileLockBusy, LockHeld, ObjectLock
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import pending_operations
from tightrein.store.repos.pending_operations import PendingOperationRecord
from tightrein.vcs.executor import OperationStateError

WHEN = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


def call(world, *argv):
    out = io.StringIO()
    code = main(["--help"] if not argv else list(argv), world.externals(), stdin=io.StringIO(), stdout=out,
                stderr=io.StringIO())
    return code, out.getvalue()


def call_json(world, *argv):
    code, text = call(world, *argv, "--workspace", str(world.root), "--json")
    return code, json.loads(text)


def database(world):
    return open_database(WorkspaceLayout(world.root).database(), FixedClock(WHEN))


@pytest.mark.parametrize("argv", [
    ["run", "--scheduled"], ["run", "--select", "triage+", "--subject", "P-0001"], ["status"], ["next", "7"],
    ["continue", "7", "P-0001", "--until", "release"], ["continue", "7", "--from", "fix"],
    ["find", "订单", "--since", "2026-10-01", "--type", "issue"], ["pending", "--stage", "fix"],
    ["confirm", "OP-0001"], ["reject", "OP-0001", "--note", "不需要"], ["config", "show", "--key", "loop"],
])
def test_commands_parse(argv):
    args = build_parser().parse_args([*argv, "--json", "--now", "2026-10-05"])
    assert args.json and callable(args.handler)


def test_version_is_printed_without_a_command(capsys):
    with pytest.raises(SystemExit) as exited:
        build_parser().parse_args(["--version"])
    assert exited.value.code == 0 and capsys.readouterr().out.strip() == "tightrein 0.1.0"


def test_ignore_state_requires_output(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "status", "--ignore-state")
    assert code == exit_codes.USAGE and "--output" in values["errors"][0]["message"]


def test_json_output_is_a_single_object(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "status")
    assert code == 0 and values["command"] == "status" and values["status"] == "ok"
    assert values["result"]["waiting"] == [] and values["result"]["lastRun"] is None
    code, values = call_json(world, "next")
    assert code == exit_codes.USAGE and values["status"] == "failed"


def test_human_output_starts_with_the_conclusion(tmp_path):
    world = make_cli_world(tmp_path)
    code, text = call(world, "status", "--workspace", str(world.root))
    assert code == 0 and text.splitlines()[0] == "待处理 0 项"


@pytest.mark.parametrize(("failure", "code"), [
    (UsageError("x"), 2), (ValueError("x"), 2), (LookupError("x"), 2), (OperationStateError("x"), 3),
    (GuardBlocked((Violation(ViolationKind.READONLY_MODIFIED, "src/a.src", "只读 worktree 被修改"),)), 6),
    (FileLockBusy("x"), 7),
    (LockHeld(ObjectLock("P-0001", 1, "h", WHEN, WHEN)), 7), (SchemaValidationError("s", []), 8),
    (RuntimeError("x"), 1),
])
def test_errors_map_to_exit_codes(failure, code):
    assert exit_codes.for_error(failure) == code


def test_next_and_find(tmp_path):
    world = make_cli_world(tmp_path)
    conn = database(world)
    save_issue(conn, "0007", IssueStatus.NEEDS_DECISION)
    save_problem(conn, "P-0001", title="订单查询返回 500")
    save_problem(conn, "P-0002", "second", title="订单导出返回 500")
    conn.close()
    code, values = call_json(world, "next", "7")
    assert code == 0 and values["next"] == "tightrein issue approve 7"
    assert values["result"][0]["gate"] == "issue-approval" and values["result"][0]["canContinue"] is False
    code, values = call_json(world, "next", "8")
    assert code == exit_codes.USAGE and "0008" in values["errors"][0]["message"]
    code, values = call_json(world, "find", "订单")
    assert code == exit_codes.GATE and values["stoppedAt"]["gate"] == "candidate-choice"
    assert {item["subject"]["id"] for item in values["result"]} == {"P-0001", "P-0002", "0007"}


def test_continue_stops_at_a_gate_with_exit_code_4(tmp_path):
    world = make_cli_world(tmp_path)
    conn = database(world)
    save_issue(conn, "0007", IssueStatus.NEEDS_DECISION)
    conn.close()
    code, values = call_json(world, "continue", "7")
    assert code == exit_codes.GATE and values["stoppedAt"]["gate"] == "issue-approval"
    assert values["next"] == "tightrein issue approve 7"


def test_pending_lists_operations_and_reject_records_the_note(tmp_path):
    world = make_cli_world(tmp_path)
    conn = database(world)
    pending_operations.save(conn, PendingOperationRecord(
        "OP-0001", Stage.FIX, "0001", OperationKind.LOCAL_MIGRATION, OperationExecutor.VCS, "改动测试库",
        True, "migration:0001", 1, OperationStatus.PENDING, WHEN,
        description={"repo": "/tmp/r", "branch": None, "files": [], "affectsRemote": False, "undo": "无",
                     "text": "应用迁移"}))
    conn.close()
    code, values = call_json(world, "pending")
    assert code == 0 and values["pendingOperations"][0]["id"] == "OP-0001"
    code, values = call_json(world, "reject", "OP-0001", "--note", "暂不应用")
    assert code == 0 and values["result"]["status"] == "rejected"


def test_config_show_lists_layers(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "config", "show", "--key", "loop.findLimit")
    assert code == 0 and values["result"] == [{"key": "loop.findLimit", "value": 20, "source": "core",
                                               "layers": {"core": 20}}]
    code, values = call_json(world, "config", "show", "--key", "nothing.here")
    assert code == exit_codes.USAGE


def _failing_command(monkeypatch, failure, *, end_run=False):
    """把 status 命令换成：开始一个运行、取一把对象锁，然后抛出 failure(None 为正常返回且不结束运行)。"""
    from datetime import timedelta

    from tightrein.cli import main as main_module
    from tightrein.cli.output import Outcome
    from tightrein.domain.enums import HandoffStatus, RunStage
    from tightrein.pipeline.common import stage_runs
    from tightrein.store import locks

    started = []
    real = build_parser()

    def handler(invocation):
        app = invocation.app
        run = stage_runs.begin(RunStage.TRIAGE, app.layout, app.conn, app.clock, app.events)
        locks.acquire(app.conn, "P-0001", app.clock, timedelta(hours=1), run_id=run.id)
        started.append(run.id)
        if end_run:
            run.end(HandoffStatus.OK)
        if failure is not None:
            raise failure
        return Outcome("status", exit_codes.OK, ["完成"])

    class Parser:
        def parse_args(self, argv):
            args = real.parse_args(argv)
            args.handler = handler
            return args

    monkeypatch.setattr(main_module, "build_parser", lambda: Parser())
    return started


def _ended(world, run_id):
    from tightrein.store import locks
    from tightrein.store.repos import runs

    conn = database(world)
    try:
        return runs.get(conn, run_id).status, locks.get(conn, "P-0001")
    finally:
        conn.close()


@pytest.mark.parametrize(("failure", "code", "status"), [
    (KeyboardInterrupt(), 130, "interrupted"),
    (exit_codes.Terminated(15), 143, "interrupted"),
    (exit_codes.Terminated(1), 129, "interrupted"),
    (RuntimeError("坏了"), exit_codes.FAILED, "failed"),
    (None, exit_codes.OK, "failed"),
])
def test_runs_left_running_by_the_command_are_closed_and_its_locks_released(tmp_path, monkeypatch, failure, code,
                                                                             status):
    world = make_cli_world(tmp_path)
    started = _failing_command(monkeypatch, failure)
    result, text = call(world, "status", "--workspace", str(world.root))
    assert result == code
    if isinstance(failure, (KeyboardInterrupt, exit_codes.Terminated)):
        assert "status 被中断" in text and "Traceback" not in text
    run_status, lock = _ended(world, started[0])
    assert (run_status.value, lock) == (status, None)


def test_runs_ended_normally_and_runs_of_other_processes_are_left_alone(tmp_path, monkeypatch):
    from tightrein.domain.enums import RunStage, RunStatus
    from tightrein.domain.run import Run
    from tightrein.store.repos import runs

    world = make_cli_world(tmp_path)
    conn = database(world)
    other = Run("R-20261005-010000-fix", RunStage.FIX, WHEN, RunStatus.RUNNING, holder_pid=999_999,
                holder_host="another-host")
    runs.save(conn, other)
    conn.commit()
    conn.close()
    started = _failing_command(monkeypatch, KeyboardInterrupt(), end_run=True)
    assert call(world, "status", "--workspace", str(world.root))[0] == 130
    assert _ended(world, started[0])[0] is RunStatus.OK
    conn = database(world)
    assert runs.get(conn, other.id).status is RunStatus.RUNNING
    conn.close()
