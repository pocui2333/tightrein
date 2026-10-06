import io
import json
import sqlite3

from cli_world import make_cli_world

from tightrein.cli import exit_codes
from tightrein.cli.main import main
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, InvalidQuery, SyncFailed


def call_json(world, *argv):
    out = io.StringIO()
    code = main([*argv, "--workspace", str(world.root), "--json", "--now", "2026-10-05T12:00:00+09:00"],
                world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def test_learn_commands(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "learn", "health")
    assert code == 0 and values["result"]["outputs"]["health"]
    code, values = call_json(world, "learn", "suggestions", "--status", "pending")
    assert code == 0 and values["result"] == []
    code, values = call_json(world, "learn", "metrics", "--since", "2026-09-28")
    assert code == 0 and values["result"]["metrics"]
    code, values = call_json(world, "learn", "reject", "LS-0001", "--reason", "证据不足")
    assert code == exit_codes.USAGE


def test_kb_errors_use_the_common_exit_codes():
    assert exit_codes.for_error(InvalidQuery("x")) == exit_codes.USAGE
    assert exit_codes.for_error(EntryNotFound("DP-0001")) == exit_codes.USAGE
    assert exit_codes.for_error(SyncFailed(())) == exit_codes.FAILED
    assert exit_codes.for_error(IndexUnavailable("x")) == exit_codes.PRECONDITION
    assert exit_codes.for_error(sqlite3.OperationalError("locked")) == exit_codes.PRECONDITION


def test_kb_commands_use_the_common_exit_codes(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "admin", "kb", "search", "订单", "--type", "defect-pattern")
    assert code == 0 and values["result"]["hits"] == []
    code, values = call_json(world, "admin", "kb", "get", "DP-0001")
    assert code == exit_codes.USAGE and values["errors"][0]["type"] == "EntryNotFound"
    code, values = call_json(world, "admin", "kb", "queries", "--since", "2026-10-01")
    assert code == 0 and [item["query"] for item in values["result"]] == ["订单"]


def test_worktree_init_waits_for_confirmation(tmp_path):
    world = make_cli_world(tmp_path)
    world.vcs.add(("rev-parse",), "1" * 40 + "\n")
    code, values = call_json(world, "project", "worktree", "init")
    assert code == exit_codes.GATE and values["pendingOperations"][0]["kind"] == "init-readonly-worktree"
    code, values = call_json(world, "project", "worktree", "list")
    assert code == 0 and values["result"] == []


def test_ext_and_eval_commands(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "admin", "ext", "list")
    assert code == 0 and {item["point"] for item in values["result"]} >= {"spec-export", "local-run"}
    code, values = call_json(world, "admin", "ext", "test")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "admin", "eval", "report", "EV-20261005-030000")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "admin", "eval", "add", "--module", "triage", "--from", str(tmp_path / "x.json"))
    assert code == exit_codes.USAGE
