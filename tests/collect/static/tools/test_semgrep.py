import json
import shutil
from pathlib import Path

import pytest

from tightrein.collect.static.scope import Level, Scope
from tightrein.collect.static.tools import semgrep
from tightrein.protocol.process import Outcome, SubprocessRunner

FIXTURES = Path(__file__).parents[1] / "fixtures"


def scope(level=Level.INCREMENTAL, base="a" * 40, files=("src/OrderService.cs",)):
    return Scope(level, base, "b" * 40, files, files)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRunner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return self.outcome


def done(code, stdout="", stopped=None, start_error=None):
    return Outcome(code, stdout, "", 10, stopped, start_error)


def test_parse_recorded_semgrep_output():
    findings, errors = semgrep.parse(json.loads(fixture("semgrep-findings.json")))
    assert [(item.rule, item.file, item.line, item.column, item.severity) for item in findings] == [
        ("empty-catch", "src/Services/OrderService.cs", 9, 13, "medium"), ("no-eval", "web/main.js", 2, 10, "high")]
    assert findings[0].message == "空的 catch 吞掉了异常" and findings[0].tool == "semgrep"
    assert errors == ["web/garbage.js：Syntax error at line web/garbage.js:1:",
                      "src/Broken.cs：Syntax error at line src/Broken.cs:1:"]
    stripped, _ = semgrep.parse({"results": [{"check_id": "x", "path": "./a.py", "start": {"line": 1},
                                              "extra": {"severity": "WEIRD"}}]})
    assert (stripped[0].file, stripped[0].severity) == ("a.py", "medium")


def test_semgrep_run_stays_offline_and_saves_its_output(tmp_path):
    runner = FakeRunner(done(0, fixture("semgrep-findings.json")))
    result = semgrep.run(runner, ["p/csharp", "p/javascript"], scope(), tmp_path / "repo", tmp_path / "raw",
                         {"PATH": "/bin"}, 1800)
    assert result.status == "ok" and len(result.findings) == 2 and result.notes[0].startswith("Semgrep 报告了 2 个错误")
    command = runner.commands[0]
    assert command.argv == ("semgrep", "scan", "--config", "p/csharp", "--config", "p/javascript", "--json",
                            "--metrics=off", "src/OrderService.cs")
    assert command.cwd == tmp_path / "repo" and command.env["SEMGREP_SEND_METRICS"] == "off"
    assert command.env["SEMGREP_ENABLE_VERSION_CHECK"] == "0" and command.timeout_s == 1800
    assert json.loads((tmp_path / "raw" / "semgrep.json").read_text(encoding="utf-8"))["version"] == "1.178.0"


def test_the_command_and_its_targets(tmp_path):
    assert semgrep.resolve_command(None, tmp_path) == "semgrep"
    assert semgrep.resolve_command("local/semgrep/bin/semgrep", tmp_path) == str(tmp_path / "local/semgrep/bin/semgrep")
    assert semgrep.resolve_command("/usr/local/bin/semgrep", tmp_path) == "/usr/local/bin/semgrep"
    assert semgrep.targets(scope(Level.FULL)) == ["."]
    assert semgrep.targets(scope(base=None)) == ["."]
    assert semgrep.targets(scope(files=("a.cs", "b.js"))) == ["a.cs", "b.js"]


@pytest.mark.parametrize(("configs", "files", "note"), [
    ((), ("a.cs",), "没有配置规则集"),
    (("p/csharp",), (), "检查范围内没有文件"),
])
def test_semgrep_not_run(tmp_path, configs, files, note):
    runner = FakeRunner(done(0, "{}"))
    result = semgrep.run(runner, configs, scope(files=files), tmp_path, tmp_path, {}, 1800)
    assert result.status == "skipped" and note in result.notes[0] and runner.commands == []


def test_only_exit_code_zero_counts(tmp_path):
    rule_error = semgrep.run(FakeRunner(done(2, fixture("semgrep-rule-error.json"))), ["bad.yaml"], scope(),
                             tmp_path, tmp_path, {}, 1800)
    assert rule_error.status == "failed" and rule_error.findings == ()
    assert rule_error.notes == ("Semgrep 失败：退出码 2", "bad：Rule parse error in rule bad:")
    missing = semgrep.run(FakeRunner(done(None, start_error="FileNotFoundError: semgrep")), ["p/csharp"], scope(),
                          tmp_path, tmp_path, {}, 1800)
    assert missing.status == "failed" and missing.notes[0] == "Semgrep 无法启动：FileNotFoundError: semgrep"
    garbage = semgrep.run(FakeRunner(done(0, "not json")), ["p/csharp"], scope(), tmp_path, tmp_path, {}, 1800)
    assert garbage.notes == ("Semgrep 的输出不是 JSON",)


@pytest.mark.external
@pytest.mark.skipif(shutil.which("semgrep") is None, reason="本机没有 semgrep")
def test_a_real_semgrep_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def f(x):\n    return eval(x)\n", encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n  - id: no-eval\n    pattern: eval(...)\n    message: 不要 eval\n    languages: [python]\n"
                     "    severity: ERROR\n", encoding="utf-8")
    result = semgrep.run(SubprocessRunner(), [str(rules)], scope(Level.FULL), repo, tmp_path / "raw",
                         {"PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)}, 300)
    assert result.status == "ok"
    assert [(item.rule.rsplit(".", 1)[-1], item.file, item.line, item.severity) for item in result.findings] == [
        ("no-eval", "app.py", 2, "high")]
