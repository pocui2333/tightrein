import json

import pytest

from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import rubric
from tightrein.evaluation.cases import Assertion
from tightrein.evaluation.errors import RubricInvalid
from tightrein.evaluation.scorers import assertions
from tightrein.evaluation.scorers.base import ScoringContext
from tightrein.evaluation.scorers.code import REGISTRY
from tightrein.guards.policy import GuardSettings

SERVICE = "class OrderService\n{\n    int Page = 1;\n}\n"
DIFF = """diff --git a/src/OrderService.cs b/src/OrderService.cs
--- a/src/OrderService.cs
+++ b/src/OrderService.cs
@@ -3 +3 @@
-    int Page = 1;
+    int Page = 0;
"""
TEST_DIFF = """diff --git a/tests/OrderTests.cs b/tests/OrderTests.cs
--- a/tests/OrderTests.cs
+++ b/tests/OrderTests.cs
@@ -1 +1,2 @@
-class OrderTests {}
+[Fact(Skip = "flaky")]
+class OrderTests {}
"""
PROTECTED_DIFF = """diff --git a/Migrations/MigrationList.cs b/Migrations/MigrationList.cs
--- a/Migrations/MigrationList.cs
+++ b/Migrations/MigrationList.cs
@@ -1 +1 @@
-a
+b
"""
ISSUE_SECTIONS = ["结论", "复现", "证据", "根因", "影响面", "引入", "修复方向", "验收标准", "关联", "历史"]


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    (root / "src").mkdir(parents=True)
    (root / "src" / "OrderService.cs").write_text(SERVICE, encoding="utf-8")
    return root


def score(name, outputs, params=None, **context):
    outcome = REGISTRY[name](outputs, params or {}, ScoringContext(**context))
    return outcome.result, outcome.reason


def test_the_three_rubrics_load_and_cover_design_12_2():
    loaded = {stage: rubric.load(stage) for stage in (Stage.TRIAGE, Stage.FIX, Stage.ISSUE)}
    assert [(item.id, item.method.value) for item in loaded[Stage.TRIAGE].items] == [
        ("triage.evidence-location", "code"), ("triage.output-schema", "code"), ("triage.refuted-source", "code"),
        ("triage.counter-check", "code"), ("triage.no-vague-wording", "code")]
    assert [item.id for item in loaded[Stage.FIX].by_method(ScoreMethod.JUDGE)] == [
        "fix.no-special-case", "fix.acceptance", "fix.deep-review"]
    assert rubric.item_ids(Stage.ISSUE) == {"issue.sections", "issue.evidence-location", "issue.absolute-dates"}
    assert rubric.item_ids(Stage.COLLECT) == set()
    assert loaded[Stage.TRIAGE].items[2].applies_when == rubric.Condition("verdict", "equals", "refuted")


def test_generators_see_acceptance_criteria_and_judges_see_review_items():
    triage = rubric.load(Stage.TRIAGE)
    generator = rubric.render(triage, "generator")
    judge = rubric.render(triage, "judge").splitlines()
    assert generator.splitlines()[2] == "- 每条证据都带「文件路径:行号」，且文件与行号真实存在"
    assert "评分" not in generator and "(代码)" not in generator
    assert judge[2] == "- [triage.evidence-location](代码) 每条证据都带「文件路径:行号」，且文件与行号真实存在"


@pytest.mark.parametrize("item, message", [
    ({"id": "triage.a", "text": "x", "method": "code", "scorer": "nope"}, "评分器 nope 没有注册"),
    ({"id": "fix.a", "text": "x", "method": "judge"}, "须以 triage. 开头"),
    ({"id": "triage.a", "text": "x", "method": "judge", "scorer": "values-in"}, "judge 项不写 scorer"),
    ({"id": "triage.a", "text": "x", "method": "user"}, "只能是 code 或 judge"),
    ({"id": "triage.a", "text": "x", "method": "judge", "appliesWhen": {"path": "a", "op": "near"}}, "appliesWhen"),
    ({"id": "triage.a", "method": "judge"}, "缺少 ['text']"),
])
def test_invalid_rubrics_are_rejected(tmp_path, item, message):
    (tmp_path / "triage.json").write_text(json.dumps({"stage": "triage", "items": [item]}), encoding="utf-8")
    with pytest.raises(RubricInvalid, match=message.replace("[", r"\[").replace("]", r"\]")):
        rubric.load(Stage.TRIAGE, tmp_path)


def test_duplicate_ids_are_rejected(tmp_path):
    item = {"id": "triage.a", "text": "x", "method": "judge"}
    (tmp_path / "triage.json").write_text(json.dumps({"stage": "triage", "items": [item, item]}), encoding="utf-8")
    with pytest.raises(RubricInvalid, match="编号重复：triage.a"):
        rubric.load(Stage.TRIAGE, tmp_path)


OUTPUTS = {"verdict": "confirmed", "labels": ["sample-label"], "reason": "缺少公司过滤",
           "evidence": {"facts": [{"location": "src/OrderService.cs:3", "observation": "没有过滤"},
                                  {"location": "src/OrderService.cs:1-2", "observation": "入口"}]}}


@pytest.mark.parametrize("op, path, value, ok", [
    ("equals", "verdict", "confirmed", True), ("equals", "verdict", "refuted", False),
    ("in", "verdict", ["confirmed", "conditional"], True), ("in", "verdict", ["refuted"], False),
    ("contains", "reason", "公司", True), ("contains", "labels", "discuss-with-author", False),
    ("contains", "reason", 5, False),
    ("matches", "evidence.facts[*].location", r":3$", True), ("matches", "evidence.facts[*].location", r":9$", False),
    ("exists", "evidence.facts[1].observation", None, True), ("exists", "evidence.trigger", None, False),
    ("absent", "evidence.trigger", None, True), ("absent", "labels[0]", None, False),
])
def test_assertion_operations(op, path, value, ok):
    outcome = assertions.evaluate(Assertion("assert-1", path, op, value, "期望", op not in ("exists", "absent")),
                                  OUTPUTS)
    assert (outcome.result is ScoreResult.PASS) is ok


def test_assertion_paths_expand_any_element():
    assert assertions.values_at(OUTPUTS, "evidence.facts[*].location") == [
        ("evidence.facts[0].location", "src/OrderService.cs:3"),
        ("evidence.facts[1].location", "src/OrderService.cs:1-2")]
    failed = assertions.evaluate(Assertion("assert-2", "verdict", "equals", "refuted", "判定不成立", True), OUTPUTS)
    assert failed.reason == "判定不成立：不成立，verdict 取到 ['confirmed']"
    with pytest.raises(ValueError):
        assertions.values_at(OUTPUTS, "evidence..facts")


def test_evidence_locations(snapshot):
    params = {"paths": ["evidence.facts[*].location", "evidence.counterEvidence[*].location"]}
    assert score("evidence-locations", OUTPUTS, params, project_snapshot=snapshot)[0] is ScoreResult.PASS
    beyond = {"evidence": {"facts": [{"location": "src/OrderService.cs:5"}, {"location": "src/Missing.cs:1"}]}}
    assert score("evidence-locations", beyond, params, project_snapshot=snapshot) == (
        ScoreResult.FAIL, "src/OrderService.cs 只有 4 行，引用了第 5 行；src/Missing.cs 在代码快照中不存在")
    outside = {"evidence": {"facts": [{"location": "../etc/passwd:1"}]}}
    assert score("evidence-locations", outside, params, project_snapshot=snapshot)[0] is ScoreResult.FAIL
    assert score("evidence-locations", {"evidence": {"facts": []}}, params, project_snapshot=snapshot) == (
        ScoreResult.FAIL, "没有带「文件路径:行号」的证据")
    root_causes = {"rootCauses": [{"file": "src/OrderService.cs", "line": 3, "symbol": None}]}
    assert score("evidence-locations", root_causes, {"paths": ["rootCauses[*]"]},
                 project_snapshot=snapshot)[0] is ScoreResult.PASS
    assert score("evidence-locations", OUTPUTS, params)[0] is ScoreResult.UNKNOWN


def test_vague_wording():
    params = {"fields": ["reason"], "words": ["可能", "建议进一步排查"]}
    assert score("vague-wording", {"reason": "缺少公司过滤"}, params)[0] is ScoreResult.PASS
    assert score("vague-wording", {"reason": "可能越权，建议进一步排查"}, params) == (
        ScoreResult.FAIL, "结论含含糊措辞：outputs.reason 含「可能」；outputs.reason 含「建议进一步排查」")


def test_values_in():
    params = {"path": "checks[*].exitCode", "allowed": [0]}
    assert score("values-in", {"checks": [{"exitCode": 0}, {"exitCode": 0}]}, params)[0] is ScoreResult.PASS
    assert score("values-in", {"checks": [{"exitCode": 0}, {"exitCode": 1}]}, params) == (
        ScoreResult.FAIL, "outputs.checks[1].exitCode 为 1")
    assert score("values-in", {"checks": []}, params) == (ScoreResult.FAIL, "checks[*].exitCode 没有任何结果")


def test_diff_rules_are_recomputed_from_the_sandbox_diff():
    settings = GuardSettings(protected_paths=("Migrations/MigrationList.cs",), test_paths=("tests/",), max_files=1,
                             max_lines=1)
    assert score("diff-size", {}, diff_text=DIFF, guard_settings=settings) == (
        ScoreResult.FAIL, "整体：增删 2 行(不含测试)，超过上限 1")
    assert score("diff-size", {}, diff_text=DIFF, guard_settings=GuardSettings())[0] is ScoreResult.PASS
    assert score("diff-protected", {}, diff_text=PROTECTED_DIFF, guard_settings=settings)[0] is ScoreResult.FAIL
    assert score("diff-protected", {}, diff_text=DIFF, guard_settings=settings)[0] is ScoreResult.PASS
    result, reason = score("diff-tests", {}, diff_text=TEST_DIFF, guard_settings=settings)
    assert result is ScoreResult.FAIL and "匹配测试路径 tests/" in reason and "新增了 [Fact(Skip" in reason
    assert score("diff-tests", {}, diff_text=DIFF, guard_settings=settings)[0] is ScoreResult.PASS
    assert score("diff-tests", {}, diff_text="", guard_settings=settings)[0] is ScoreResult.PASS
    assert score("diff-size", {})[0] is ScoreResult.UNKNOWN


def issue_document(output_dir, sections, evidence="- src/OrderService.cs:3 没有按公司过滤", extra=""):
    lines = ["# 订单越权"]
    for name in sections:
        lines += [f"## {name}", evidence if name == "证据" else "内容"]
    (output_dir / "issues").mkdir(parents=True, exist_ok=True)
    (output_dir / "issues" / "0007-order.md").write_text("\n".join(lines) + extra + "\n", encoding="utf-8")
    return {"path": "issues/0007-order.md"}


def test_plan_files_and_residue_are_recomputed_from_the_sandbox_diff():
    diff = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n@@ -1 +1,2 @@\n+console.log(x);\n"
            "diff --git a/tmp.txt b/tmp.txt\n--- /dev/null\n+++ b/tmp.txt\n@@ -0,0 +1 @@\n+x\n")
    outputs = {"planFiles": ["src/a.js"]}
    result, reason = score("diff-in-plan", outputs, {"path": "planFiles[*]"}, diff_text=diff)
    assert (result, reason) == (ScoreResult.FAIL, "tmp.txt：不在计划的文件清单中")
    assert score("diff-in-plan", {"planFiles": ["src/a.js", "tmp.txt"]}, {"path": "planFiles[*]"},
                 diff_text=diff)[0] is ScoreResult.PASS
    settings = GuardSettings(residue_patterns=(r"console\.log",))
    assert score("diff-residue", {}, diff_text=diff, guard_settings=settings)[0] is ScoreResult.FAIL
    assert score("diff-residue", {}, diff_text=diff, guard_settings=GuardSettings())[0] is ScoreResult.PASS
    assert score("diff-in-plan", outputs, {"path": "planFiles[*]"})[0] is ScoreResult.UNKNOWN


def test_document_sections_locations_and_dates(tmp_path, snapshot):
    output_dir = tmp_path / "output"
    outputs = issue_document(output_dir, ISSUE_SECTIONS)
    sections = {"document": "path", "sections": ISSUE_SECTIONS}
    locations = {"document": "path", "section": "证据"}
    words = {"document": "path", "words": ["今天", "上周"]}
    context = {"output_dir": output_dir, "project_snapshot": snapshot}
    assert score("document-sections", outputs, sections, **context)[0] is ScoreResult.PASS
    assert score("document-locations", outputs, locations, **context)[0] is ScoreResult.PASS
    assert score("absolute-dates", outputs, words, **context)[0] is ScoreResult.PASS
    outputs = issue_document(output_dir, ISSUE_SECTIONS[:-2], evidence="- src/OrderService.cs:9", extra="\n上周出现")
    assert score("document-sections", outputs, sections, **context) == (ScoreResult.FAIL, "缺少章节：关联、历史")
    assert score("document-locations", outputs, locations, **context) == (
        ScoreResult.FAIL, "src/OrderService.cs 只有 4 行，引用了第 9 行")
    assert score("absolute-dates", outputs, words, **context) == (ScoreResult.FAIL, "使用了相对日期：上周")
    assert score("document-sections", {"path": "issues/none.md"}, sections, **context) == (
        ScoreResult.FAIL, "人读文档 issues/none.md 不存在")
    assert score("document-sections", {}, sections, **context)[0] is ScoreResult.FAIL


def test_verdict_fields_require_the_conditional_fields():
    params = {"path": "verdict", "allowed": ["confirmed", "conditional", "refuted", "insufficient"],
              "required": {"insufficient": "missingInfo[*]", "conditional": "evidence.trigger"}}
    assert score("verdict-fields", {"verdict": "confirmed"}, params)[0] is ScoreResult.PASS
    assert score("verdict-fields", {"verdict": "maybe"}, params)[0] is ScoreResult.FAIL
    assert score("verdict-fields", {"verdict": "insufficient", "missingInfo": []}, params) == (
        ScoreResult.FAIL, "判定为 insufficient 时 outputs.missingInfo[*] 不能为空")
    assert score("verdict-fields", {"verdict": "conditional", "evidence": {"trigger": "pageSize 小于 0"}},
                 params)[0] is ScoreResult.PASS


def refuted(source, facts=({"label": "请求", "value": "GET /api/Order"},)):
    return {"verdict": "refuted", "claim": {"facts": list(facts), "entryPoints": []},
            "evidence": {"sourceOfPhenomenon": source}}


def test_refuted_source_points_to_code_or_a_fact(snapshot):
    by_fact = refuted({"location": None, "factRef": 1, "explanation": "请求本身带了错误的参数"})
    assert score("refuted-source", by_fact)[0] is ScoreResult.PASS
    assert score("refuted-source", refuted({"location": None, "factRef": 2, "explanation": "x"}))[0] is ScoreResult.FAIL
    by_code = refuted({"location": "src/OrderService.cs:3", "factRef": None, "explanation": "这里已校验"})
    assert score("refuted-source", by_code, project_snapshot=snapshot)[0] is ScoreResult.PASS
    missing = refuted({"location": "src/OrderService.cs:9", "factRef": None, "explanation": "x"})
    assert score("refuted-source", missing, project_snapshot=snapshot)[0] is ScoreResult.FAIL
    both = refuted({"location": "src/OrderService.cs:3", "factRef": 1, "explanation": "x"})
    assert score("refuted-source", both, project_snapshot=snapshot)[0] is ScoreResult.FAIL
    assert score("refuted-source", refuted(None))[0] is ScoreResult.FAIL


def checked(*items, entry_points=("GET /api/Order/{id}",)):
    return {"claim": {"entryPoints": list(entry_points), "facts": []}, "evidence": {"counterEvidence": list(items)}}


def counter(entry, status="absent", location=None):
    return {"check": "入口是否校验", "entry": entry, "upstreamValidation": {"status": status, "location": location},
            "result": "没有校验"}


def test_counter_check_traces_an_entry_and_the_upstream_validation(snapshot):
    assert score("counter-check", checked(counter("GET /api/Order/{id}")))[0] is ScoreResult.PASS
    assert score("counter-check", checked(counter("src/OrderService.cs:1", "present", "src/OrderService.cs:3")),
                 project_snapshot=snapshot)[0] is ScoreResult.PASS
    assert score("counter-check", checked())[0] is ScoreResult.FAIL
    assert score("counter-check", checked(counter("OrderController")), project_snapshot=snapshot) == (
        ScoreResult.FAIL, "第 1 条的入口：'OrderController' 不是「文件路径:行号」")
    assert score("counter-check", checked(counter("GET /api/Order/{id}", "present")))[0] is ScoreResult.FAIL
    assert score("counter-check", checked(counter("src/OrderService.cs:1")))[0] is ScoreResult.UNKNOWN
