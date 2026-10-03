"""probe new|test|logs 与 spec draft 的命令。"""

import io
import json

import yaml
from cli_world import make_cli_world

from tightrein.cli import exit_codes
from tightrein.cli.main import main
from tightrein.extensions.invoke import ProcessOutcome
from tightrein.pipeline.collect.prompts import tasks
from tightrein.pipeline.collect.prompts.tasks import TaskContext
from tightrein.runner.task import Access
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import ToolLayout

GOOD_PROBE = '''from tightrein.sources.project_probes import helpers

found = helpers.read_input()
helpers.emit([helpers.signal("job:import", "导入没有按时完成", ["最近一次完成 " + found["window"]["since"]],
                             "missed:import", severity_hint="P2")], state={"checked": found["window"]["until"]},
             notes=["检查了导入任务"])
'''
BAD_PROBE = 'import json, sys\nsys.stdout.write(json.dumps({"signals": [{"location": "x"}]}))\n'


def call(world, *argv):
    out = io.StringIO()
    code = main([*argv, "--workspace", str(world.root), "--json", "--now", "2026-10-05T12:00:00+09:00"],
                world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def registrations(world):
    return yaml.safe_load((world.root / "project.yaml").read_text(encoding="utf-8"))["sources"]["project-probes"]


def test_probe_new_writes_a_template_and_registers_it(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call(world, "probe", "new", "daily-import", "--every", "1h")
    assert code == 0 and values["result"]["registered"] is True
    script = world.root / "probes" / "daily_import.py"
    assert script.is_file() and "helpers.emit" in script.read_text(encoding="utf-8")
    assert registrations(world) == [{"name": "daily-import", "command": ["{python}", "probes/daily_import.py"],
                                     "every": "1h"}]
    code, values = call(world, "probe", "new", "daily-import")
    assert code == exit_codes.USAGE
    code, _ = call(world, "probe", "new", "Bad_Name")
    assert code == exit_codes.USAGE
    code, _ = call(world, "probe", "new", "other", "--every", "2w")
    assert code == exit_codes.USAGE


def register(world, name, text):
    (world.root / "probes").mkdir(exist_ok=True)
    (world.root / "probes" / f"{name}.py").write_text(text, encoding="utf-8")
    data = yaml.safe_load((world.root / "project.yaml").read_text(encoding="utf-8"))
    data.setdefault("sources", {}).setdefault("project-probes", []).append(
        {"name": name, "command": ["{python}", f"probes/{name}.py"], "every": "1d"})
    (world.root / "project.yaml").write_text(yaml_text.dump(data), encoding="utf-8")


def test_probe_test_validates_the_output_and_shows_the_signals(tmp_path):
    world = make_cli_world(tmp_path)
    register(world, "good", GOOD_PROBE)
    register(world, "bad", BAD_PROBE)
    code, values = call(world, "probe", "test", "good")
    assert code == 0 and values["result"]["valid"] is True
    [signal] = values["result"]["signals"]
    assert (signal["probe"], signal["check"], signal["location"], signal["message"]) == (
        "project-probe", "good", "job:import", "导入没有按时完成")
    assert signal["context"]["probeFingerprint"] == "missed:import" and signal["context"]["severityHint"] == "P2"
    assert values["result"]["notes"] == ["检查了导入任务"]
    code, values = call(world, "probe", "test", "bad")
    assert code == exit_codes.FAILED and "输出不符合探针契约" in values["result"]["error"]
    code, _ = call(world, "probe", "test", "missing")
    assert code == exit_codes.USAGE
    assert not (world.root / "data" / "tightrein.db").exists() or _no_probe_state(world)


def _no_probe_state(world):
    import sqlite3
    conn = sqlite3.connect(world.root / "data" / "tightrein.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM probe_states").fetchone()[0] == 0
    finally:
        conn.close()


LINE = '{"ts": "2026-10-05T02:59:00Z", "level": "error", "msg": "查询失败"}'


def log_runner(request):
    """扩展进程的替身：log-platform 返回一行原文，log-parse 解析成一条条目。"""
    sent = json.loads(request.stdin.decode("utf-8"))
    if sent["point"] == "log-platform":
        output = {"chunks": [{"stream": '{app="api"}', "text": LINE + "\n", "startPosition": 0,
                              "endPosition": len(LINE) + 1, "modifiedAt": None}],
                  "truncated": False, "oldestAvailable": None}
    else:
        output = {"entries": [{"stream": '{app="api"}', "position": 0, "occurredAt": "2026-10-05T02:59:00Z",
                               "localTime": None, "level": "error", "rawLevel": "error", "category": None,
                               "eventId": None, "message": "查询失败", "exception": None, "frames": [],
                               "raw": LINE}], "state": {}, "unparsed": 0}
    return ProcessOutcome(0, json.dumps({"protocol": 1, "status": "ok", "output": output}).encode("utf-8"), b"")


def test_probe_logs_reads_through_the_log_platform_and_parse_methods(tmp_path):
    extensions = {"log-platform": {"command": ["fake-loki"]}, "log-parse": {"command": ["fake-parse"]}}
    world = make_cli_world(tmp_path, extensions=extensions)
    (world.root / "extensions").mkdir()
    world.extension_runner = log_runner
    argv = ("probe", "logs", "--query", '{app="api"}', "--since", "2026-10-05T02:00:00Z", "--until",
            "2026-10-05T03:00:00Z")
    code, values = call(world, *argv)
    assert code == 0 and values["result"]["parsed"] is True
    assert [entry["message"] for entry in values["result"]["entries"]] == ["查询失败"]
    code, values = call(world, *argv, "--raw")
    assert values["result"]["entries"] == [{"stream": '{app="api"}', "line": LINE}]
    plain = make_cli_world(tmp_path / "plain")
    code, _ = call(plain, *argv)
    assert code == exit_codes.USAGE


def test_spec_draft_needs_the_readonly_worktree_and_defines_a_read_only_task(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call(world, "spec", "draft")
    assert code == exit_codes.USAGE and "worktree init" in json.dumps(values, ensure_ascii=False)
    app = world.app()
    task = tasks.spec_draft_task(TaskContext(ToolLayout(), app.config, "R-20261005-030000-loop", tmp_path),
                                 "https://staging.example.test")
    assert (task.role, task.access, task.output_schema) == ("spec-drafter", Access.READ_ONLY,
                                                            "runner/roles/spec-drafter.schema.json")
    assert "起草接口描述" in task.instructions.prompt and "https://staging.example.test" in task.instructions.prompt
