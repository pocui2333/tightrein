import json
from pathlib import Path

import pytest

from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint, ProbeLevel
from tightrein.extensions import defaults
from tightrein.extensions.result import ExtensionFailure, PointResult
from tightrein.sources.common.procs import ToolRun
from tightrein.sources.static import scope as scoping
from tightrein.sources.static.scope import Scope
from tightrein.sources.static.tools import extension, semgrep
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

FIXTURES = Path(__file__).parent / "fixtures" / "static"
BASE = "a" * 40
HEAD = "b" * 40


def reader(repos):
    return GitReader(VcsProcess(environ=repos.environ))


def test_incremental_scope_is_the_diff(repos):
    _, repo = repos.origin_and_clone()
    base = repos.head(repo)
    repos.commit(repo, "feat: 改动", {"src/OrderService.cs": "class OrderService {}\n", "src/New.cs": "class New {}\n"})
    repos.git(repo, "rm", "-q", "tests/OrderTests.cs")
    head = repos.commit(repo, "chore: 删除测试")
    found, reason = scoping.resolve(reader(repos), repo, base, ProbeLevel.INCREMENTAL)
    assert reason is None and found.head == head and found.base_commit == base
    assert found.files == ("src/New.cs", "src/OrderService.cs") == found.changed_files
    assert not found.first_run


def test_no_new_commits_skips_only_the_incremental_level(repos):
    _, repo = repos.origin_and_clone()
    head = repos.head(repo)
    assert scoping.resolve(reader(repos), repo, head[:12], ProbeLevel.INCREMENTAL) == (None, "上次巡检以来没有新提交")
    full, reason = scoping.resolve(reader(repos), repo, head, ProbeLevel.FULL)
    assert reason is None and full.files == (".gitignore", "README.md", "src/OrderService.cs", "tests/OrderTests.cs")
    assert full.changed_files == ()


def test_first_run_scans_everything(repos):
    _, repo = repos.origin_and_clone()
    found, _ = scoping.resolve(reader(repos), repo, None, ProbeLevel.INCREMENTAL)
    assert found.first_run and found.files == found.changed_files and len(found.files) == 4


def scope(level=ProbeLevel.INCREMENTAL, base=BASE, files=("src/OrderService.cs",)):
    return Scope(level, base, HEAD, files, files)


TOOLS_OUTPUT = {
    "tools": [
        {"name": "stack-build", "status": "ok", "exitCode": 0, "logFile": "stack-build.log", "reason": None},
        {"name": "deps-audit", "status": "ok", "exitCode": 0, "logFile": "deps-audit.json",
         "reason": None},
        {"name": "npm-build", "status": "failed", "exitCode": 1, "logFile": "npm-build.log", "reason": "编译失败"},
        {"name": "npm-audit", "status": "skipped", "exitCode": None, "logFile": None, "reason": "没有前端改动"},
    ],
    "findings": [
        {"tool": "stack-build", "kind": "build-warning", "rule": "CS8602", "file": "src/OrderService.cs", "line": 12,
         "column": 9, "message": "解引用可能出现空引用。", "severity": "low", "package": None},
        {"tool": "stack-build", "kind": "build-warning", "rule": "CS0168", "file": "src/Other.cs", "line": 3,
         "column": 5, "message": "声明了变量，但从未使用过", "severity": "low", "package": None},
        {"tool": "deps-audit", "kind": "vulnerability", "rule": "GHSA-5crp-9r3c-p9vr",
         "file": "src/App.csproj", "line": None, "column": None, "message": "Newtonsoft.Json 13.0.1 存在已知漏洞",
         "severity": "high", "package": {"name": "Newtonsoft.Json", "version": "13.0.1",
                                         "advisoryUrl": "https://github.com/advisories/GHSA-5crp-9r3c-p9vr"}},
        {"tool": "npm-build", "kind": "build-warning", "rule": "vue-cli", "file": "src/vue/src/main.js", "line": 1,
         "column": 1, "message": "警告", "severity": "low", "package": None},
    ],
}


class ToolsClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def static_tools(self, repo, commit, static_scope, raw_dir):
        self.calls.append((repo, commit, static_scope, raw_dir))
        return self.result


def ok_result(output=TOOLS_OUTPUT):
    return PointResult(ExtensionPoint.STATIC_TOOLS, ExtensionLayer.PROJECT, output=output)


def test_extension_findings_are_filtered_for_incremental_runs(tmp_path):
    client = ToolsClient(ok_result())
    tools = extension.run(client, tmp_path, scope(), tmp_path / "raw")
    assert [(item.rule, item.file) for item in tools.findings] == [
        ("CS8602", "src/OrderService.cs"), ("GHSA-5crp-9r3c-p9vr", "src/App.csproj")]
    assert tools.findings[1].package["name"] == "Newtonsoft.Json" and tools.findings[1].vulnerability
    assert tools.degraded and tools.notes == ("确定性工具 npm-build 失败：编译失败，输出见 npm-build.log",)
    request = client.calls[0][2]
    assert (request.level, request.base_commit, request.changed_files) == (
        ProbeLevel.INCREMENTAL, BASE, ("src/OrderService.cs",))
    assert tools.extensions == {"static-tools": {"implementation": "project", "cached": False}}


def test_full_runs_keep_every_file_and_first_runs_ask_for_full(tmp_path):
    client = ToolsClient(ok_result())
    full = extension.run(client, tmp_path, scope(ProbeLevel.FULL), tmp_path)
    assert len(full.findings) == 3
    extension.run(client, tmp_path, scope(base=None), tmp_path)
    assert (client.calls[-1][2].level, client.calls[-1][2].base_commit) == (ProbeLevel.FULL, None)


def test_extension_failures_and_absence(tmp_path):
    broken = PointResult(ExtensionPoint.STATIC_TOOLS, ExtensionLayer.STACK,
                         failure=ExtensionFailure(ExtensionErrorCode.TIMEOUT, "超过 1800 秒未结束，进程组已终止"))
    failed = extension.run(ToolsClient(broken), tmp_path, scope(), tmp_path)
    assert failed.findings == () and failed.degraded
    assert failed.notes == ("static-tools 失败：timeout：超过 1800 秒未结束，进程组已终止",)
    missing = extension.run(ToolsClient(defaults.result(ExtensionPoint.STATIC_TOOLS)), tmp_path, scope(), tmp_path)
    assert (missing.findings, missing.degraded, missing.notes) == ((), False, ("未配置确定性工具",))


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_recorded_semgrep_output():
    findings, errors = semgrep.parse(json.loads(fixture("semgrep-findings.json")))
    assert [(item.rule, item.file, item.line, item.column, item.severity) for item in findings] == [
        ("empty-catch", "src/Services/OrderService.cs", 9, 13, "medium"), ("no-eval", "web/main.js", 2, 10, "high")]
    assert findings[0].message == "空的 catch 吞掉了异常" and findings[0].tool == "semgrep"
    assert errors == ["web/garbage.js：Syntax error at line web/garbage.js:1:",
                      "src/Broken.cs：Syntax error at line src/Broken.cs:1:"]


class FakeSemgrep:
    def __init__(self, run):
        self.run = run
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self.run


def test_semgrep_run(tmp_path):
    launcher = FakeSemgrep(ToolRun(0, fixture("semgrep-findings.json")))
    result = semgrep.run(launcher, ["p/csharp", "p/javascript"], scope(), tmp_path / "repo", tmp_path / "raw",
                         {"PATH": "/bin"}, 1800)
    assert result.status == "ok" and len(result.findings) == 2 and result.notes[0].startswith("Semgrep 报告了 2 个错误")
    command = launcher.commands[0]
    assert command.argv == ("semgrep", "scan", "--config", "p/csharp", "--config", "p/javascript", "--json",
                            "--metrics=off", "src/OrderService.cs")
    assert command.cwd == tmp_path / "repo" and command.env["SEMGREP_SEND_METRICS"] == "off"
    assert json.loads((tmp_path / "raw" / "semgrep.json").read_text(encoding="utf-8"))["version"] == "1.178.0"


def test_semgrep_command_is_configurable(tmp_path):
    launcher = FakeSemgrep(ToolRun(0, json.dumps({"results": [], "errors": []})))
    semgrep.run(launcher, ["p/csharp"], scope(), tmp_path, tmp_path, {}, 1800, "/opt/semgrep/bin/semgrep")
    assert launcher.commands[0].argv[:2] == ("/opt/semgrep/bin/semgrep", "scan")


def test_resolve_command_reads_relative_paths_from_the_tool_root(tmp_path):
    assert semgrep.resolve_command("semgrep", tmp_path) == "semgrep"
    assert semgrep.resolve_command("local/semgrep/bin/semgrep", tmp_path) == str(tmp_path / "local/semgrep/bin/semgrep")
    assert semgrep.resolve_command("/usr/local/bin/semgrep", tmp_path) == "/usr/local/bin/semgrep"


def test_semgrep_targets():
    assert semgrep.targets(scope(ProbeLevel.FULL)) == ["."]
    assert semgrep.targets(scope(base=None)) == ["."]
    assert semgrep.targets(scope(files=("a.cs", "b.js"))) == ["a.cs", "b.js"]


@pytest.mark.parametrize(("configs", "files", "note"), [
    ((), ("a.cs",), "没有配置 sources.static.semgrep.configs，不运行 Semgrep"),
    (("p/csharp",), (), "扫描范围内没有文件，不运行 Semgrep"),
])
def test_semgrep_not_run(tmp_path, configs, files, note):
    launcher = FakeSemgrep(ToolRun(0, "{}"))
    result = semgrep.run(launcher, configs, scope(files=files), tmp_path, tmp_path, {}, 1800)
    assert (result.status, result.notes) == ("skipped", (note,)) and launcher.commands == []


def test_semgrep_failures(tmp_path):
    rule_error = semgrep.run(FakeSemgrep(ToolRun(2, fixture("semgrep-rule-error.json"))), ["bad.yaml"], scope(),
                             tmp_path, tmp_path, {}, 1800)
    assert rule_error.status == "failed" and rule_error.findings == ()
    assert rule_error.notes == ("Semgrep 失败：退出码 2", "bad：Rule parse error in rule bad:")
    missing = semgrep.run(FakeSemgrep(ToolRun(None, start_error="FileNotFoundError: semgrep")), ["p/csharp"], scope(),
                          tmp_path, tmp_path, {}, 1800)
    assert missing.status == "failed" and missing.notes[0] == "Semgrep 失败：无法启动：FileNotFoundError: semgrep"
    garbage = semgrep.run(FakeSemgrep(ToolRun(0, "not json")), ["p/csharp"], scope(), tmp_path, tmp_path, {}, 1800)
    assert garbage.notes == ("Semgrep 的输出不是 JSON",)
