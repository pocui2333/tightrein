import io
import json

from cli_world import make_cli_world
from fix_world import make_fix_world

from tightrein.cli import exit_codes
from tightrein.cli.main import main


def call_json(world, *argv):
    out = io.StringIO()
    code = main([*argv, "--workspace", str(world.root), "--json", "--now", "2026-10-05T12:00:00+09:00"],
                world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def delivery_world(tmp_path):
    world = make_cli_world(tmp_path)
    base = make_fix_world(tmp_path)
    base.conn.close()
    return world


def test_fix_start_without_a_terminal_offers_two_ways(tmp_path):
    world = delivery_world(tmp_path)
    code, values = call_json(world, "fix", "start", "1")
    assert code == exit_codes.GATE and values["stoppedAt"]["gate"] == "interactive-fix"
    assert values["result"]["choices"] == ["tightrein fix start 1", "tightrein fix start 1 --here"]
    code, values = call_json(world, "fix", "start", "1", "--here")
    assert code == 0 and "fix plan" in values["result"]["message"]
    code, values = call_json(world, "fix", "prepare", "1")
    assert code == exit_codes.PRECONDITION and "进行中" in values["result"]["message"]


def test_release_and_verify_report_preconditions(tmp_path):
    world = delivery_world(tmp_path)
    code, values = call_json(world, "release", "1")
    assert code == exit_codes.PRECONDITION and values["subject"] == {"type": "issue", "id": "0001"}
    code, values = call_json(world, "release", "track")
    assert code == 0 and values["result"]["lines"] == []
    code, values = call_json(world, "release", "sync")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "release", "deploy", "1")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "verify", "screenshots", "1")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "verify", "staging", "1")
    assert code == exit_codes.PRECONDITION and values["result"]["conclusion"] is None


def test_bare_fix_is_only_for_the_evaluation_sandbox(tmp_path):
    world = delivery_world(tmp_path)
    code, values = call_json(world, "fix")
    assert code == exit_codes.USAGE and "--output" in values["errors"][0]["message"]
    decisions = tmp_path / "gates.json"
    decisions.write_text("{}", encoding="utf-8")
    code, values = call_json(world, "fix", "plan", "1", "--gate-decisions", str(decisions))
    assert code == exit_codes.USAGE and "--gate-decisions" in values["errors"][0]["message"]
