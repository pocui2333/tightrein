import pytest
from contract_samples import changed, collect_outputs, envelope, paths, problem, signal, triage_outputs, without

from tightrein.contracts import validate
from tightrein.contracts.validate import FieldError

COLLECT = "handoff/outputs/collect.schema.json"
AGGREGATE = "handoff/outputs/aggregate.schema.json"
TRIAGE = "handoff/outputs/triage.schema.json"
ISSUE = "handoff/outputs/issue.schema.json"
LOOP = "handoff/outputs/loop.schema.json"

RUN_LEVEL = {
    "processedRuns": [{"runId": "R-20260929-021503-collect-api-fuzz", "probe": "api-fuzz", "grouped": 2,
                       "suppressed": 0}],
    "counts": {"new": 1, "regressed": 0, "resolved": 0, "ongoing": 1, "pending": 0},
    "forTriage": [{"problemId": "P-0042", "handoff": "aggregate-P-0042.json"}],
    "statusChanges": [{"problemId": "P-0042", "from": None, "to": "new", "event": "reproduced"}],
    "reopenedIssues": [],
    "rebuild": None,
    "notes": [],
}
PROBLEM_LEVEL = {
    "problem": problem(),
    "transition": {"problemId": "P-0042", "from": "pending", "to": "new", "event": "reproduced"},
    "latestSignal": signal(),
    "samples": [signal()],
    "reproduction": {"strategy": "replay", "attempts": 2, "reproduced": True},
    "issueId": None,
}
ISSUE_OUTPUTS = {"issueId": "0007", "action": "created", "path": "issues/0007-order-query-500.md", "problems": ["P-0042"],
                 "severity": "P2", "treatment": "scheduled", "labels": [], "acceptance": ["本 Issue 的复现检查在修复后通过"],
                 "notified": False}
LOOP_OUTPUTS = {
    "conclusion": "有 1 项等待用户处理",
    "waiting": [{"kind": "issue-approval", "subjectId": "0007", "summary": "订单查询在分页参数为负数时返回 500",
                 "command": "tightrein issue approve 7", "treatment": "immediate",
                 "severity": "P1"}],
    "steps": [{"order": 5, "name": "分诊", "executed": True, "reason": None, "runId": "R-20260929-021503-triage",
               "status": "ok", "durationMs": 81000}],
    "produced": {"newProblems": ["P-0042"], "issues": ["0007"]},
    "anomalies": [],
}

VALID = [
    (COLLECT, collect_outputs()),
    (COLLECT, changed(collect_outputs(), runStatus="skipped", skippedReason="没有新提交")),
    (COLLECT, changed(collect_outputs(), probe="platform-errors", level=None,
                      disabledSources={"alerts": "没有配置 extensions.alert-source"},
                      coverage=changed(collect_outputs()["coverage"], sources=["error-tracking"]))),
    (AGGREGATE, RUN_LEVEL),
    (AGGREGATE, PROBLEM_LEVEL),
    (TRIAGE, triage_outputs()),
    (TRIAGE, changed(triage_outputs(), verdict="refuted", disposition="false-positive", severity=None, treatment=None,
                     worth=None, labels=[], evidence=changed(triage_outputs()["evidence"], impact=None,
                                                             sourceOfPhenomenon={"location": None, "factRef": 1,
                                                                                 "explanation": "上游已校验"}))),
    (ISSUE, ISSUE_OUTPUTS),
    (LOOP, LOOP_OUTPUTS),
]

INVALID = [
    (COLLECT, changed(collect_outputs(), runStatus="skipped"), "$.skippedReason"),
    (COLLECT, changed(collect_outputs(), signalCount=-1), "$.signalCount"),
    (COLLECT, without(collect_outputs(), "environment"), "$"),
    (AGGREGATE, without(RUN_LEVEL, "counts"), "$"),
    (AGGREGATE, changed(RUN_LEVEL, counts=changed(RUN_LEVEL["counts"], flaky=0)), "$.counts"),
    (COLLECT, changed(collect_outputs(), companionRunId=None), "$"),
    (AGGREGATE, changed(PROBLEM_LEVEL, samples=[signal()] * 6), "$.samples"),
    (AGGREGATE, changed(PROBLEM_LEVEL, problem=without(problem(), "scope")), "$.problem"),
    (TRIAGE, changed(triage_outputs(), labels=["urgent"]), "$.labels[0]"),
    (TRIAGE, changed(triage_outputs(), introducedBy={"commit": "a1b2c3d4"}), "$.introducedBy"),
    (TRIAGE, changed(triage_outputs(), tradeoffHit="DP-12"), "$.tradeoffHit"),
    (ISSUE, changed(ISSUE_OUTPUTS, action="updated"), "$.action"),
    (ISSUE, changed(ISSUE_OUTPUTS, problems=[]), "$.problems"),
    (LOOP, changed(LOOP_OUTPUTS, waiting=[changed(LOOP_OUTPUTS["waiting"][0], kind="approval")]), "$.waiting[0].kind"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_handoff_is_checked_with_the_outputs_of_its_stage():
    assert validate.validate_handoff(envelope(outputs=collect_outputs())) == []
    errors = validate.validate_handoff(envelope(outputs=changed(collect_outputs(), signalCount="2")))
    assert errors == [FieldError("$.outputs.signalCount", "'2' is not of type 'integer'")]


def test_handoff_envelope_errors_stop_before_outputs():
    errors = validate.validate_handoff(envelope(outputs={"probe": "x"}, status="done"))
    assert [error.path for error in errors] == ["$.status"]


def test_failed_handoff_outputs_are_not_checked():
    document = envelope(outputs={"exception": "KeyError"}, status="failed", blockedReason="程序错误：KeyError")
    assert validate.validate_handoff(document) == []


def test_collect_stats_record_extension_layers():
    extensions = {"spec-export": {"implementation": "stack", "cached": True},
                  "authz-roles": {"implementation": "project", "cached": False},
                  "log-platform": {"implementation": "core", "cached": False}}
    assert paths(COLLECT, changed(collect_outputs(), stats={"seed": 42, "extensions": extensions})) == set()
    wrong_layer = {"extensions": {"spec-export": {"implementation": "builtin", "cached": True}}}
    assert "$.stats.extensions['spec-export'].implementation" in paths(COLLECT, changed(collect_outputs(),
                                                                                         stats=wrong_layer))
    unknown_point = {"extensions": {"swagger": {"implementation": "stack", "cached": False}}}
    assert "$.stats.extensions" in paths(COLLECT, changed(collect_outputs(), stats=unknown_point))
    assert "$.stats.seed" in paths(COLLECT, changed(collect_outputs(), stats={"seed": "42"}))
