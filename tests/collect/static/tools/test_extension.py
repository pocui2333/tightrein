import json

from tightrein.collect.static.scope import Level, Scope
from tightrein.collect.static.tools import extension
from tightrein.protocol.process import Outcome
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

BASE = "a" * 40
HEAD = "b" * 40
OUTPUT = {
    "tools": [
        {"name": "stack-build", "status": "ok", "reason": None, "logFile": "stack-build.log"},
        {"name": "npm-build", "status": "failed", "reason": "编译失败", "logFile": "npm-build.log"},
        {"name": "npm-audit", "status": "skipped", "reason": "没有前端改动", "logFile": None},
    ],
    "findings": [
        {"tool": "stack-build", "kind": "build-warning", "rule": "CS8602", "file": "src/OrderService.cs", "line": 12,
         "column": 9, "message": "解引用可能出现空引用。", "severity": "low", "package": None},
        {"tool": "stack-build", "kind": "build-warning", "rule": "CS0168", "file": "src/Other.cs", "line": 3,
         "column": 5, "message": "声明了变量，但从未使用过", "severity": "low", "package": None},
        {"tool": "deps-audit", "kind": "vulnerability", "rule": "GHSA-5crp-9r3c-p9vr", "file": "src/App.csproj",
         "line": None, "column": None, "message": "Newtonsoft.Json 13.0.1 存在已知漏洞", "severity": "high",
         "package": {"name": "Newtonsoft.Json", "version": "13.0.1", "advisoryUrl": None}},
        {"tool": "npm-build", "kind": "build-warning", "rule": "vue-cli", "file": "src/vue/src/main.js", "line": 1,
         "column": 1, "message": "警告", "severity": "low", "package": None},
    ],
}
TOOLS = [{"name": "stack", "command": ["{python}", "scripts/stack_tools.py"], "timeout": "10m"}]


class ScriptRunner:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return self.outcomes.pop(0)


def scope(level=Level.INCREMENTAL, base=BASE):
    files = ("src/OrderService.cs",)
    return Scope(level, base, HEAD, files, files)


def given(tmp_path, runner):
    return extension.Runner(runner, tmp_path, {"PATH": "/bin"}, {}, Redactor(), RawDir(tmp_path / "raw"))


def ok(output=OUTPUT):
    return Outcome(0, json.dumps(output, ensure_ascii=False), "", 10, None, None)


def test_incremental_runs_keep_changed_files_and_vulnerabilities(tmp_path):
    runner = ScriptRunner(ok())
    tools = extension.run(TOOLS, scope(), tmp_path / "wt", given(tmp_path, runner))
    assert [(item.rule, item.file) for item in tools.findings] == [
        ("CS8602", "src/OrderService.cs"), ("GHSA-5crp-9r3c-p9vr", "src/App.csproj")]
    assert tools.degraded and tools.notes == ("确定性工具 npm-build 失败：编译失败，输出见 npm-build.log",)
    sent = json.loads(runner.commands[0].stdin)
    assert (sent["level"], sent["baseCommit"], sent["changedFiles"]) == ("incremental", BASE, ["src/OrderService.cs"])


def test_full_runs_keep_every_file_and_first_runs_ask_for_full(tmp_path):
    runner = ScriptRunner(ok(), ok())
    full = extension.run(TOOLS, scope(Level.FULL), tmp_path, given(tmp_path, runner))
    assert len(full.findings) == 3
    extension.run(TOOLS, scope(Level.BASELINE, base=None), tmp_path, given(tmp_path, runner))
    sent = json.loads(runner.commands[-1].stdin)
    assert (sent["level"], sent["baseCommit"]) == ("full", None)


def test_failures_and_absence(tmp_path):
    broken = extension.run(TOOLS, scope(), tmp_path, given(tmp_path, ScriptRunner(Outcome(3, "", "boom", 1, None,
                                                                                          None))))
    assert broken.findings == () and broken.degraded and broken.notes[0].startswith("确定性工具 stack 失败")
    invalid = extension.run(TOOLS, scope(), tmp_path, given(tmp_path, ScriptRunner(ok({"tools": []}))))
    assert invalid.degraded and "不合格式" in invalid.notes[0]
    missing = extension.run([], scope(), tmp_path, given(tmp_path, ScriptRunner()))
    assert (missing.findings, missing.degraded, missing.notes) == ((), False, ("未配置项目或技术栈的确定性工具",))
