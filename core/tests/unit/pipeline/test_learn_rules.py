import json
from datetime import timedelta

from learn_world import closed_fixed, fix_outputs, issue, learn_env, make_learn_world, save_handoff
from pipeline_world import NOW

from tightrein.domain.enums import IssueOrigin, KnowledgeType, RunnerStatus, RunStage
from tightrein.pipeline.learn.steps import rule_check, rules
from tightrein.pipeline.learn.steps.rule_check import RuleCheckSettings
from tightrein.pipeline.learn.steps.rules import RuleEnv
from tightrein.sources.common.procs import ToolRun
from tightrein.store import idempotency
from tightrein.store.repos import knowledge, pulls
from tightrein.store.repos.pulls import PullRecord
from tightrein.vcs.git_read import Diff, FileDiff

FIX_RUN = "R-20261001-030000-fix"
MARKER = "10 / size"
RULE = """rules:
  - id: divide-by-size
    message: 除数来自参数，没有校验为 0
    languages: [generic]
    pattern: 10 / size
"""
BEFORE = "int Page(int size) => 10 / size;\n"
AFTER = "int Page(int size) => size == 0 ? 0 : 10 / Math.Max(size, 1);\n"


class FakeGit:
    """修复前后的文件内容；diff 只给出改动的行。"""

    def __init__(self, before, after):
        self.versions = {"d6f37025": before, "m1": after}

    def show(self, repo, rev, path):
        return self.versions[rev].get(path)

    def diff(self, repo, base, head, paths):
        return Diff(tuple(FileDiff(path, 1, 1, added_lines=(self.versions[head][path].rstrip(),),
                                   removed_lines=(self.versions[base][path].rstrip(),)) for path in paths))


class FakeSemgrep:
    """数一数 cwd 下含有 MARKER 的文件，作为命中。"""

    def __init__(self, fail=False):
        self.commands = []
        self.fail = fail

    def __call__(self, command):
        self.commands.append(command)
        if self.fail:
            return ToolRun(2, "", "规则无法解析")
        results = [{"check_id": "divide-by-size", "path": str(path.relative_to(command.cwd)), "start": {"line": 1},
                    "extra": {"message": "除数", "severity": "WARNING"}}
                   for path in sorted(command.cwd.rglob("*")) if path.is_file() and path.suffix != ".yaml"
                   and MARKER in path.read_text(encoding="utf-8") and "Math.Max" not in path.read_text(encoding="utf-8")]
        return ToolRun(0, json.dumps({"results": results, "errors": []}))


def rule_output(yaml=RULE, reason=None):
    return {"slug": "divide-by-size", "title": "除数来自参数", "summary": "参数为 0 时除零", "tags": ["path:src/"],
            "phenomenon": "pageSize 为 0 时返回 500", "rootCausePattern": "直接用参数做除数", "detection": "找以参数为除数的表达式",
            "counterExamples": ["入口已校验大于 0"], "directories": ["src/"], "rule": {"yaml": yaml, "reason": reason}}


def setup(tmp_path, repo_files=None, max_hits=5):
    world = make_learn_world(tmp_path, testPaths=["tests/"])
    worktree = tmp_path / "readonly"
    for path, text in (repo_files or {"src/Other.cs": "int A() => 1;\n"}).items():
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_text(text, encoding="utf-8")
    semgrep = FakeSemgrep()
    env = RuleEnv(FakeGit({"src/Order.cs": BEFORE}, {"src/Order.cs": AFTER}), semgrep,
                  RuleCheckSettings("semgrep", 60, max_hits), tmp_path / "repo", worktree)
    return world, env, semgrep


def fixed(world, issue_id="0007", origin=None, merge="m1"):
    issue(world, issue_id, **({"origin": origin, "problems_": ()} if origin else {}))
    save_handoff(world, RunStage.FIX, issue_id, fix_outputs(issue_id, changed=(("src/Order.cs", 1, 1),
                                                                               ("tests/test_order.py", 9, 0))), FIX_RUN)
    pulls.save(world.conn, PullRecord(issue_id, 12, "u", "b", "修复除零", "MERGED", NOW, merge_commit=merge))
    closed_fixed(world, issue_id, NOW - timedelta(hours=1))


def test_a_rule_that_passes_the_three_checks_joins_the_library_once(tmp_path):
    world, env, semgrep = setup(tmp_path)
    fixed(world)
    world.runner.add("rule-writer", rule_output())
    report = rules.generate(learn_env(world), env)
    (result,) = report.results
    assert (result["accepted"], result["path"], report.errors) == (True, "rules/0007-divide-by-size.yaml", [])
    assert result["check"] == {"accepted": True, "reason": None, "ruleId": "divide-by-size", "beforeHits": 1,
                               "afterHits": 0, "repoHits": 0}
    text = (world.layout.root / result["path"]).read_text(encoding="utf-8")
    assert text.startswith("# 来源：Issue 0007") and "修复前命中 1 处，修复后命中 0 处，全仓库命中 0 处" in text
    task = world.runner.tasks[0]
    assert task.role == "rule-writer" and "-int Page(int size) => 10 / size;" in task.instructions.prompt
    assert "tests/test_order.py" not in task.instructions.prompt
    assert [command.cwd.name for command in semgrep.commands] == ["before", "after", "readonly"]
    assert rules.generate(learn_env(world), env).results == []
    assert rules.recent(world.conn, NOW - timedelta(days=1), NOW + timedelta(days=1)) == [result]


def test_a_rule_that_fails_verification_becomes_a_defect_pattern(tmp_path):
    world, env, _ = setup(tmp_path, {f"src/S{number}.cs": BEFORE for number in range(3)}, max_hits=2)
    fixed(world)
    world.runner.add("rule-writer", rule_output())
    (result,) = rules.generate(learn_env(world), env).results
    assert result["accepted"] is False and result["path"] is None
    assert "全仓库命中 3 处，超过上限 2" in result["reason"]
    assert result["knowledgeId"] == "DP-0001" and not world.layout.rules_dir().exists()
    (entry,) = knowledge.find(world.conn, types=(KnowledgeType.DEFECT_PATTERN.value,))
    body = (world.layout.root / entry.path).read_text(encoding="utf-8")
    assert "## 根因写法\n\n直接用参数做除数" in body and "没有收入规则库：验证不通过" in body


def test_rules_that_cannot_be_written_are_kept_as_patterns_and_runner_failures_retry(tmp_path):
    world, env, _ = setup(tmp_path)
    fixed(world, "0007")
    fixed(world, "0008")
    world.runner.add("rule-writer", rule_output(yaml=None, reason="跨文件的时序"), RunnerStatus.FAILED)
    report = rules.generate(learn_env(world), env)
    assert [item["reason"] for item in report.results] == ["规则表达不了：跨文件的时序"]
    assert report.errors == [{"item": "rule:0008", "reason": "rule-writer 没有给出结果：failed fake-error"}]
    assert idempotency.get(world.conn, "rule:0008") is None


def test_missing_material_and_manual_issues_are_skipped(tmp_path):
    world, env, _ = setup(tmp_path)
    fixed(world, "0007", merge=None)
    fixed(world, "0008", origin=IssueOrigin.MANUAL)
    report = rules.generate(learn_env(world), env)
    assert report.results == [{"issueId": "0007", "accepted": False, "path": None,
                               "reason": "不生成规则：没有合并提交", "knowledgeId": None}]
    assert world.runner.tasks == []
    assert rules.generate(learn_env(world, tmp_path / "out"), env).results == []


def test_rule_check_rejects_malformed_rules_and_rules_that_miss_the_bug(tmp_path):
    settings = RuleCheckSettings("semgrep", 60, 5)
    semgrep = FakeSemgrep()
    before, after = {"src/Order.cs": BEFORE}, {"src/Order.cs": AFTER}
    work = tmp_path / "work"

    def check(text, before_files=before, after_files=after, launcher=semgrep):
        return rule_check.check(launcher, settings, text, before_files, after_files, tmp_path, work)

    assert check("rules: [").reason.startswith("规则不是合法的 YAML")
    assert check("rules:\n  - id: a\n  - id: b\n").reason == "规则文件必须在 rules 下恰好有一条规则"
    assert check("rules:\n  - id: a\n    pattern: x\n").reason == "规则缺少 message、languages"
    assert check(RULE.replace("    pattern: 10 / size\n", "")).reason.startswith("规则没有模式")
    assert check(RULE, before_files={"src/Order.cs": AFTER}).reason == "修复前的代码没有命中，规则没有抓住这个缺陷"
    assert check(RULE, after_files={"src/Order.cs": BEFORE}).reason == "修复后的代码仍命中 1 处"
    failed = check(RULE, launcher=FakeSemgrep(fail=True))
    assert failed.accepted is False and failed.reason.startswith("修复前版本：Semgrep 失败")
