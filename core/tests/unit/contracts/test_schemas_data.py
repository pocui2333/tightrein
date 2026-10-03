import pytest
from contract_samples import changed, paths, regression, without


REGRESSION = "data/regression.schema.json"
AUTHZ = "data/authz-model.schema.json"
EVAL_CASE = "data/eval-case.schema.json"
EVAL_REPORT = "data/eval-report.schema.json"
PROBE_INPUT = "data/project-probe-input.schema.json"
PROBE_OUTPUT = "data/project-probe-output.schema.json"
PROBE_IN = {"name": "daily-import", "lastRunAt": None, "state": None,
            "window": {"since": "2026-10-01T01:00:00Z", "until": "2026-10-02T01:00:00Z"},
            "workspace": "/w/sample", "environment": "production", "baseUrl": None}
PROBE_SIGNAL = {"location": "job:daily-import", "symptom": "导入没有按时完成", "evidence": ["03:00 时 status=done 为 0 条"],
                "severityHint": "P1", "fingerprint": "missed-run:daily-import"}
PROBE_OUT = {"signals": [PROBE_SIGNAL], "state": {"lastBatch": "2026-10-01"}, "notes": []}

ENDPOINTS = [
    {"method": "POST", "route": "/api/Order/Query", "requires": ["OrderRead"], "anonymous": False},
    {"method": "POST", "route": "/api/Account/Login", "requires": [], "anonymous": True},
]
ROLES = {"Company": {"OrderRead": True}, "Personal": {"OrderRead": False}}
AUTHZ_MODEL = {"commit": "d6f37025", "generatedAt": "2026-09-29T01:00:00Z", "endpoints": ENDPOINTS,
               "capabilities": ["OrderRead"], "roles": ROLES}

MODULE_CASE = {
    "schemaVersion": 1,
    "id": "E-0004",
    "kind": "module",
    "module": "triage",
    "title": "越权类问题被用户改判为成立",
    "category": "corrected",
    "source": {"runId": "R-20260929-031500-triage", "subjectId": "P-0042", "correction": "retriage --verdict"},
    "input": {"handoff": "aggregate-P-0042.json", "commit": "d6f37025", "args": []},
    "expected": {
        "excludeItems": [{"itemId": "triage.capacity-formula", "reason": "不是容量类问题"}],
        "assertions": [
            {"path": "verdict", "op": "equals", "value": "confirmed", "description": "判定为成立"},
            {"path": "rootCauses[*].file", "op": "exists", "description": "给出了根因文件"},
        ],
    },
}
RETRIEVAL_CASE = {"id": "E-0001", "kind": "retrieval", "query": "权限 校验", "filters": {"status": "active", "limit": 10},
                  "expected": ["DP-0012"], "source": "R-20260929-031500-triage"}

MODULE_REPORT = {
    "kind": "module",
    "evaluationId": "EV-20260929-031500",
    "plan": {
        "module": "triage", "caseIds": ["E-0004"], "repeats": 3, "purpose": "version",
        "variants": [
            {"label": "baseline", "version": {"label": "baseline", "commit": "HEAD", "patch": None, "useWorktree": False},
             "runner": "replay", "model": None},
        ],
    },
    "manifestSha256": "a" * 64,
    "evalsTree": "b" * 40,
    "caseStats": [{"caseId": "E-0004", "variant": "baseline", "scores": [1, 1, 0.8], "mean": 0.933, "variance": 0.013,
                   "passRate": 0.667, "itemPassCounts": {"triage.evidence-location": [3, 3]}, "unstable": True}],
    "variantStats": [{"variant": "baseline", "mean": 0.933, "passRate": 0.667, "costUsd": None, "durationMs": 1200}],
    "comparisons": [],
    "verdict": "pass",
}
RETRIEVAL_REPORT = {
    "kind": "retrieval",
    "evaluationId": "EV-20260929-031500",
    "casesSha256": "c" * 64,
    "metrics": {"recallAt5": 0.8, "recallAt10": 0.9, "mrr": 0.62, "caseCount": 10},
    "cases": [{"caseId": "E-0001", "rank": 2, "hits": ["TO-0003", "DP-0012"]}],
    "skippedCases": [],
    "baseline": None,
}

VALID = [
    (REGRESSION, regression()),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][0], requires=["backend", "compute"])])),
    (REGRESSION, changed(regression(), checks=[{"id": "test-1", "kind": "test", "file": "tests/test_order.py",
                                                "location": "src/order.py:12",
                                                "command": "python -m pytest tests/test_order.py"}])),
    (AUTHZ, AUTHZ_MODEL),
    (EVAL_CASE, MODULE_CASE),
    (EVAL_CASE, changed(MODULE_CASE, category="edge", source={"runId": "R-20260929-031500-triage", "subjectId": "P-0042"})),
    (EVAL_CASE, RETRIEVAL_CASE),
    (EVAL_REPORT, MODULE_REPORT),
    (EVAL_REPORT, changed(MODULE_REPORT, verdict=None,
                          plan=changed(MODULE_REPORT["plan"], purpose="tool-model"))),
    (EVAL_REPORT, RETRIEVAL_REPORT),
]

INVALID = [
    (REGRESSION, changed(regression(), problems=["not-a-fingerprint"]), "$.problems[0]"),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][0], role=None)]), "$.checks[0].role"),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][1], targets=[])]), "$.checks[0].targets"),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][0], id="page-1")]), "$.checks[0].id"),
    (REGRESSION, changed(regression(), checks=[{"id": "test-1", "kind": "test", "file": "tests/test_order.py",
                                                "location": "src/order.py:12"}]), "$.checks[0]"),
    (REGRESSION, changed(regression(), checks=[without(regression()["checks"][0], "requires")]), "$.checks[0]"),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][0], requires=["Back End"])]),
     "$.checks[0].requires[0]"),
    (REGRESSION, changed(regression(), checks=[changed(regression()["checks"][0], requires=["backend", "backend"])]),
     "$.checks[0].requires"),
    (AUTHZ, changed(AUTHZ_MODEL, endpoints=[without(ENDPOINTS[0], "anonymous")]), "$.endpoints[0]"),
    (AUTHZ, changed(AUTHZ_MODEL, endpoints=[changed(ENDPOINTS[0], policies=["OrderRead"])]), "$.endpoints[0]"),
    (AUTHZ, changed(AUTHZ_MODEL, roles={"Company": {"OrderRead": "yes"}}), "$.roles.Company.OrderRead"),
    (AUTHZ, without(AUTHZ_MODEL, "roles"), "$"),
    (EVAL_CASE, changed(MODULE_CASE, module="collect"), "$.module"),
    (EVAL_CASE, changed(MODULE_CASE, source={"runId": "R-20260929-031500-triage", "subjectId": "P-0042"}),
     "$.source"),
    (EVAL_CASE, changed(MODULE_CASE, expected={"assertions": [{"path": "verdict", "op": "equals", "description": "x"}]}),
     "$.expected.assertions[0]"),
    (EVAL_CASE, changed(MODULE_CASE, expected={"assertions": [
        {"path": "verdict", "op": "absent", "value": 1, "description": "x"}]}), "$.expected.assertions[0]"),
    (EVAL_CASE, without(MODULE_CASE, "schemaVersion"), "$"),
    (EVAL_CASE, changed(RETRIEVAL_CASE, expected=[]), "$.expected"),
    (EVAL_CASE, changed(RETRIEVAL_CASE, filters={"limit": 51}), "$.filters.limit"),
    (EVAL_REPORT, changed(MODULE_REPORT, verdict=None), "$.verdict"),
    (EVAL_REPORT, changed(MODULE_REPORT, plan=changed(MODULE_REPORT["plan"], repeats=2)), "$.plan.repeats"),
    (EVAL_REPORT, changed(RETRIEVAL_REPORT, metrics=changed(RETRIEVAL_REPORT["metrics"], mrr=1.2)), "$.metrics.mrr"),
]

VALID += [
    (PROBE_INPUT, PROBE_IN),
    (PROBE_INPUT, changed(PROBE_IN, lastRunAt="2026-10-01T01:00:00Z", state={"lastBatch": "x"},
                          baseUrl="https://h.test")),
    (PROBE_OUTPUT, PROBE_OUT),
    (PROBE_OUTPUT, {"signals": []}),
    (PROBE_OUTPUT, {"signals": [changed(PROBE_SIGNAL, severityHint=None, occurredAt="2026-10-02T02:00:00Z",
                                        context={"expectedAt": "02:00"})]}),
]
INVALID += [
    (PROBE_INPUT, without(PROBE_IN, "window"), "$"),
    (PROBE_INPUT, changed(PROBE_IN, workspace="workspaces/sample"), "$.workspace"),
    (PROBE_OUTPUT, {"signals": [without(PROBE_SIGNAL, "fingerprint")]}, "$.signals[0]"),
    (PROBE_OUTPUT, {"signals": [changed(PROBE_SIGNAL, evidence=[])]}, "$.signals[0].evidence"),
    (PROBE_OUTPUT, {"signals": [changed(PROBE_SIGNAL, severityHint="high")]}, "$.signals[0].severityHint"),
    (PROBE_OUTPUT, changed(PROBE_OUT, extra=1), "$"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_authz_endpoints_use_the_extension_route_format():
    assert paths(AUTHZ, ENDPOINTS[0], definition="endpoint") == set()
    assert paths(AUTHZ, changed(ENDPOINTS[0], route="api/Order/Query"), definition="endpoint") == {"$.route"}
