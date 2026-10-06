import pytest
from contract_samples import changed, paths, without


STATIC = "runner/roles/static-review.schema.json"
SCOUT = "runner/roles/fix-scout.schema.json"
REPRO = "runner/roles/repro-test.schema.json"
EXECUTOR = "runner/roles/fix-executor.schema.json"
CURATOR = "runner/roles/knowledge-curator.schema.json"
JUDGE = "runner/roles/judge.schema.json"
LESSON = "runner/roles/lesson-writer.schema.json"
RULE = "runner/roles/rule-writer.schema.json"
IMPROVEMENT = "runner/roles/improvement-writer.schema.json"

LOCATED = {"location": "src/Services/OrderService.cs:88", "description": "分页查询"}
STATIC_REVIEW = {
    "claims": [{"file": "src/Services/OrderService.cs", "line": 88, "ruleOrPattern": "DP-0012", "layer": "incremental",
                "statement": "分页参数未校验", "trigger": "pageSize 小于 0"}],
    "excluded": [{"file": "src/Services/ReportService.cs", "line": 12, "statement": "全表导出", "tradeoffId": "TO-0003"}],
}
SCOUTING = {
    "analysis": "Query 直接把 pageSize 传给 Take。",
    "existing": [LOCATED], "reusable": [], "dataStructure": [], "linkage": [LOCATED], "problems": [],
    "designIssue": None, "affectedEndpoints": ["POST /api/Order/Query"], "affectedPages": [],
    "incidental": [],
}
REPRO_TEST = {"analysis": "用 pageSize=-1 调用 Query。", "status": "written", "file": "tests/test_order.py",
              "command": "python -m pytest -q tests/test_order.py", "location": "src/Services/OrderService.cs:88",
              "covers": ["pageSize 为负数时返回 400"], "reason": None}
EXECUTION = {
    "analysis": "在入口校验 pageSize。",
    "status": "completed", "changedFiles": ["src/Services/OrderService.cs"],
    "verification": [{"command": "dotnet build SampleApp.sln", "output": "Build succeeded."}],
    "deviations": [], "bigIssue": None, "incidental": [], "outOfScope": [], "release": None,
}
CURATION = {"decision": "merge", "targetIds": ["TL-0003", "TL-0007"], "supersedes": [],
            "result": {"title": "分页参数", "summary": "分页参数必须在入口校验", "tags": ["path:src/Services/"], "body": "正文"},
            "reason": "两条经验说的是同一件事"}
JUDGEMENT = {"items": [{"itemId": "fix.minimal-change", "result": "pass", "reason": "只改了根因方法",
                        "evidence": ["src/Services/OrderService.cs:88"]}]}
LESSON_DRAFT = {
    "mode": "lesson",
    "draft": {"type": "triage-lesson", "slug": "check-entry-validation", "title": "先查入口校验", "summary": "判不成立前追到入口",
              "tags": ["stage:triage"], "body": "正文", "related": []},
    "contradictions": [], "duplicates": [],
}
RULE_DRAFT = {
    "slug": "unchecked-paging", "title": "分页参数未校验", "summary": "分页参数直接传给查询", "tags": ["path:src/Services/"],
    "phenomenon": "返回 500", "rootCausePattern": "Take 接收负数", "detection": "搜索 Take(pageSize)",
    "counterExamples": ["入口已用 Math.Max 校验"], "directories": ["src/Services/"],
    "rule": {"yaml": "rules:\n  - id: unchecked-paging\n", "reason": None},
}
IMPROVEMENT_DRAFT = {
    "reason": "三次评审驳回都因为没有检查入口校验",
    "suggestion": {"stage": "fix", "target": "prompt",
                   "patch": "--- a/skills/fix/references/fix-rules.md\n+++ b/skills/fix/references/fix-rules.md\n",
                   "model": None, "rationale": "角色说明没有要求追到入口", "addresses": ["0007"],
                   "expected": "参与改进的用例评审通过率提升"},
}
VALID = [
    (STATIC, STATIC_REVIEW),
    (SCOUT, SCOUTING),
    (SCOUT, changed(SCOUTING, designIssue={"rootCause": "权限模型", "reason": "局部修补无法根治", "locations": ["src/a.cs:1"]})),
    (REPRO, REPRO_TEST),
    (REPRO, changed(REPRO_TEST, status="cannot-write", file=None, command=None, location=None, reason="只在生产数据上出现")),
    (EXECUTOR, EXECUTION),
    (EXECUTOR, changed(EXECUTION, status="aborted", bigIssue={"description": "需要改接口契约", "locations": []})),
    (CURATOR, CURATION),
    (CURATOR, {"decision": "noop", "targetIds": ["TL-0003"], "supersedes": [], "reason": "已有等价条目"}),
    (JUDGE, JUDGEMENT),
    (LESSON, LESSON_DRAFT),
    (LESSON, {"mode": "compare", "draft": None, "contradictions": [{"ids": ["TL-0003", "TL-0009"], "facts": "结论相反"}],
              "duplicates": []}),
    (RULE, RULE_DRAFT),
    (RULE, changed(RULE_DRAFT, rule={"yaml": None, "reason": "跨文件的时序，模式表达不了"})),
    (IMPROVEMENT, IMPROVEMENT_DRAFT),
    (IMPROVEMENT, {"suggestion": None, "reason": "失败各不相同"}),
]

INVALID = [
    (STATIC, changed(STATIC_REVIEW, claims=[changed(STATIC_REVIEW["claims"][0], layer="deep")]), "$.claims[0].layer"),
    (STATIC, changed(STATIC_REVIEW, excluded=[without(STATIC_REVIEW["excluded"][0], "tradeoffId")]),
     "$.excluded[0]"),
    (SCOUT, without(SCOUTING, "analysis"), "$"),
    (SCOUT, changed(SCOUTING, existing=[{"location": "OrderService", "description": "x"}]),
     "$.existing[0].location"),
    (REPRO, changed(REPRO_TEST, status="cannot-write", reason=None), "$.reason"),
    (REPRO, changed(REPRO_TEST, location=None), "$.location"),
    (EXECUTOR, changed(EXECUTION, status="aborted"), "$.bigIssue"),
    (EXECUTOR, changed(EXECUTION, status="done"), "$.status"),
    (CURATOR, without(CURATION, "result"), "$"),
    (CURATOR, changed(CURATION, decision="add"), "$"),
    (JUDGE, {"items": [changed(JUDGEMENT["items"][0], result="not-applicable")]}, "$.items[0].result"),
    (LESSON, changed(LESSON_DRAFT, draft=None), "$.draft"),
    (LESSON, changed(LESSON_DRAFT, draft=changed(LESSON_DRAFT["draft"], slug="Check Entry")), "$.draft"),
    (RULE, changed(RULE_DRAFT, counterExamples=[]), "$.counterExamples"),
    (RULE, without(RULE_DRAFT, "rule"), "$"),
    (IMPROVEMENT, changed(IMPROVEMENT_DRAFT, suggestion=changed(IMPROVEMENT_DRAFT["suggestion"], target="config")),
     "$.suggestion"),
]

SPEC_DRAFTER = "runner/roles/spec-drafter.schema.json"
SPEC_DRAFT = {"openapi": {"openapi": "3.0.3", "info": {"title": "sample", "version": "draft"},
                          "paths": {"/api/questions": {"get": {"responses": {"200": {"description": "ok"}}}}}},
              "sources": [{"path": "/api/questions", "file": "api/questions.js"}],
              "uncertain": ["分页参数是否必填"]}
VALID += [(SPEC_DRAFTER, SPEC_DRAFT), (SPEC_DRAFTER, changed(SPEC_DRAFT, sources=[], uncertain=[]))]
INVALID += [
    (SPEC_DRAFTER, changed(SPEC_DRAFT, openapi={"openapi": "2.0", "info": {}, "paths": {}}), "$.openapi.openapi"),
    (SPEC_DRAFTER, without(SPEC_DRAFT, "uncertain"), "$"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)
