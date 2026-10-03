import pytest
from contract_samples import changed, fix_plan, paths, without


FIX_PLAN = "handoff/outputs/fix-plan.schema.json"
FIX_REVIEW = "handoff/outputs/fix-review.schema.json"
FIX = "handoff/outputs/fix.schema.json"
VERIFY = "handoff/outputs/verify.schema.json"
RELEASE = "handoff/outputs/release.schema.json"
LEARN = "handoff/outputs/learn.schema.json"

REVIEW = {
    "mode": "light",
    "items": [{"itemId": "fix.acceptance", "result": "fail", "reason": "负数分页参数仍返回 500"}],
    "blockers": [{"kind": "root-cause-unfixed", "location": "src/services/order_service.src:90",
                  "trigger": "pageSize 为 -1", "problem": "校验只加在控制器，服务仍会收到负数",
                  "category": "local", "rootCause": "只修了一个入口"}],
    "unverified": [],
}
RISK = {"level": "high", "categories": ["authz"],
        "hits": [{"category": "authz", "basis": "content", "file": "src/controllers/order_controller.src",
                  "excerpt": "@requires_role(\"Admin\")", "rule": "@requires_role"}]}
FIX_OUTPUTS = {
    "issueId": "0007", "branch": "cty/fix-order-query-500", "worktree": "worktrees/fix-0007", "baseCommit": "d6f37025",
    "plan": {"path": "data/fixes/0007/plan.json", "sha256": "a" * 64, "operationId": "OP-0015",
             "confirmedAt": "2026-09-29T05:00:00Z"},
    "reproCheck": [{"checkId": "api-1", "kind": "api", "hash": "b" * 64, "afterFix": "not-run"}],
    "changedFiles": [{"path": "src/services/order_service.src", "added": 5, "removed": 1}],
    "diffHash": "c" * 64,
    "checks": [{"name": "backend-build", "command": "make build", "exitCode": 0,
                "log": "rounds/1/checks/backend-build.log"}],
    "planFiles": ["src/services/order_service.src"],
    "otherRegressions": [{"issueId": "0003", "checkId": "static-1", "kind": "static", "result": "passed"}],
    "risk": {"plan": None, "apply": RISK},
    "rounds": [{"round": 1, "checksPassed": True, "reviews": [{"mode": "deep", "passed": True}],
                "blockerCategories": [], "risk": RISK, "failures": [],
                "discardedFindings": [{"finding": {"problem": "可能有并发问题"}, "reason": "缺少触发条件"}]}],
    "discardedFindings": [],
    "summary": "入口校验分页参数", "userVisibleChange": "无", "affectedEndpoints": ["POST /api/Order/Query"],
    "affectedPages": [], "migration": None, "deviations": [], "unverified": [], "leftovers": [],
    "incidentalFindings": [], "protectedTouches": [], "split": None,
}
VERIFY_OUTPUTS = {
    "issueId": "0007", "phase": "local", "target": {"url": "http://localhost:5100", "mode": "api"},
    "commit": "e5f6a7b8", "baseCommit": "d6f37025",
    "items": [{"id": "repro-api-1", "category": "issue-repro", "command": None, "result": "pass",
               "evidence": ["data/verify/0007/2026-09-29-local/raw/api-1.json"], "reason": None}],
    "conclusion": "passed", "unverified": [], "deferredToStaging": [], "migration": None, "consecutiveFailures": 0,
}
RELEASE_OUTPUTS = {
    "issueId": "0007", "branch": "cty/fix-order-query-500",
    "commits": [{"commit": "e5f6a7b8", "message": "fix: 修复订单查询在分页参数为负数时返回 500",
                 "files": ["src/Services/OrderService.cs"], "operationId": "OP-0016"}],
    "syncs": [{"mainCommit": "f0e1d2c3", "conflicts": [{"file": "src/Services/OrderService.cs", "resolution": "both"}],
               "mergeCommit": "a9b8c7d6"}],
    "push": {"remoteBranch": "origin/cty/fix-order-query-500", "commits": ["e5f6a7b8"], "at": "2026-09-29T06:00:00Z"},
    "pr": {"number": 186, "url": "https://github.com/example/sample/pull/186", "title": "Fix order query 500",
           "bodySha256": "d" * 64, "state": "open", "mergeable": "MERGEABLE", "mergeCommit": None, "mergedAt": None,
           "closedAt": None, "userNote": None},
    "deployments": [], "masterAt": None, "pendingOperations": [], "cleanup": None,
}
LEARN_OUTPUTS = {
    "metrics": [{"metric": "noise-ratio", "dimension": "probe=api-fuzz", "value": None, "numerator": 0, "denominator": 0,
                 "sampleSize": 0}],
    "health": [{"check": "探针漏跑", "passed": True, "notify": False, "detail": ""}],
    "rules": [{"issueId": "0007", "accepted": True, "path": "rules/0007-null-order.yaml", "reason": None,
               "knowledgeId": None, "check": {"accepted": True, "reason": None, "ruleId": "null-order",
                                              "beforeHits": 1, "afterHits": 0, "repoHits": 2}},
              {"issueId": "0008", "accepted": False, "path": None, "reason": "规则表达不了：跨文件的时序",
               "knowledgeId": "DP-0002"}],
    "cleanup": [{"id": "FL-0001", "action": "archived"}],
    "suggestions": [{"id": "LS-0004", "kind": "control", "subject": "gate:merge", "status": "pending",
                     "document": "data/improve/LS-0004.md", "advice": {"recommendation": "改为 user", "reason": "纠正频繁"}}],
    "yields": [{"stage": "triage", "role": "claim-verifier", "calls": 3, "inputTokens": 1200, "outputTokens": 300,
                "costUsd": 0.4, "useful": 1, "noYield": 1, "pending": 1, "tokensPerUseful": 1500.0,
                "callsSinceUseful": 2}],
    "thirdParty": [{"name": "variant-analysis", "source": "https://github.com/example/skills", "passed": False,
                    "detail": "星标数 1200 低于 5000"}],
    "lessons": [{"source": "lesson:triage:P-0001:1", "knowledgeId": "TL-0003", "decision": "add"}],
    "outcomes": [{"problemId": "P-0001", "attempt": 1, "outcome": "correct"}],
    "errors": [],
}
VALID = [
    (FIX_PLAN, fix_plan()),
    (FIX_PLAN, changed(fix_plan(), migration={"entries": ["ChangeSet 42"], "reversible": True, "revertMethod": "回滚 42"})),
    (FIX_REVIEW, REVIEW),
    (FIX_REVIEW, {"mode": "screenshot", "items": [], "blockers": [], "unverified": [],
                  "screenshots": [{"path": "screenshots/list.png", "result": "unknown", "reason": "截图模糊"}]}),
    (FIX_REVIEW, changed(REVIEW, blockers=[changed(REVIEW["blockers"][0], location=None, trigger=None)])),
    (FIX, FIX_OUTPUTS),
    (FIX, {"issueId": "0007", "branch": "cty/fix-order-query-500", "worktree": "worktrees/fix-0007",
           "baseCommit": "d6f37025"}),
    (VERIFY, VERIFY_OUTPUTS),
    (VERIFY, changed(VERIFY_OUTPUTS, phase="staging", confirmations=[
        {"object": "api-1", "source": "api", "method": "replay", "result": "pass", "detail": None}],
        revert={"operationId": "OP-0009", "message": "撤销 PR 待确认"})),
    (RELEASE, RELEASE_OUTPUTS),
    (RELEASE, changed(RELEASE_OUTPUTS, acceptedFindings=[{"check": "residue", "location": "src/a.js:3",
                                                          "problem": "新增了调试输出"}])),
    (VERIFY, changed(VERIFY_OUTPUTS, items=[changed(VERIFY_OUTPUTS["items"][0], category="full-regression")])),
    (LEARN, LEARN_OUTPUTS),
    (LEARN, {}),
    (LEARN, {"improve": {"troubles": 2, "suggestionId": None, "evaluationId": None, "reason": "不足以归纳"}}),
]

INVALID = [
    (FIX_PLAN, changed(fix_plan(), steps=[]), "$.steps"),
    (FIX_PLAN, changed(fix_plan(), acceptanceMapping=[{"criterion": "x", "steps": [0]}]),
     "$.acceptanceMapping[0].steps[0]"),
    (FIX_PLAN, without(fix_plan(), "notDoing"), "$"),
    (FIX_REVIEW, changed(REVIEW, blockers=[changed(REVIEW["blockers"][0], category="minor")]), "$.blockers[0].category"),
    (FIX_REVIEW, changed(REVIEW, mode="ui"), "$.mode"),
    (FIX_REVIEW, changed(REVIEW, mode="screenshot"), "$"),
    (FIX, changed(FIX_OUTPUTS, selfCheck=[]), "$"),
    (FIX, changed(FIX_OUTPUTS, risk={"plan": None, "apply": changed(RISK, level="medium")}), "$.risk.apply"),
    (FIX, without(FIX_OUTPUTS, "baseCommit"), "$"),
    (FIX, changed(FIX_OUTPUTS, changedFiles=[{"path": "a.cs", "added": -1, "removed": 0}]), "$.changedFiles[0].added"),
    (VERIFY, changed(VERIFY_OUTPUTS, conclusion="failed-as-expected"), "$.conclusion"),
    (VERIFY, changed(VERIFY_OUTPUTS, phase="reproduce"), "$.phase"),
    (VERIFY, changed(VERIFY_OUTPUTS, items=[changed(VERIFY_OUTPUTS["items"][0], result="ok")]), "$.items[0].result"),
    (RELEASE, changed(RELEASE_OUTPUTS, pr=changed(RELEASE_OUTPUTS["pr"], state="draft")), "$.pr"),
    (RELEASE, without(RELEASE_OUTPUTS, "commits"), "$"),
    (LEARN, {"metrics": [{"metric": "x", "dimension": "all", "value": 1}]}, "$.metrics[0]"),
    (LEARN, {"summary": "x"}, "$"),
    (LEARN, {"variantScanRequests": []}, "$"),
    (LEARN, {"cleanup": [{"id": "FL-0001", "action": "deleted"}]}, "$.cleanup[0].action"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)
