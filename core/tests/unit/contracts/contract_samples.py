"""各 schema 测试共用的合法样例与辅助函数。样例一律用函数返回新对象，测试可以放心修改。"""

import copy

from tightrein.contracts import validate


def paths(name, instance, definition=None):
    return {error.path for error in validate.validate(name, instance, definition)}


def changed(sample, **fields):
    result = copy.deepcopy(sample)
    result.update(fields)
    return result


def without(sample, *keys):
    result = copy.deepcopy(sample)
    for key in keys:
        del result[key]
    return result


def envelope(stage="collect", outputs=None, **fields):
    document = {
        "schemaVersion": 2,
        "runId": "R-20260929-021503-collect-api-fuzz",
        "stage": stage,
        "subject": {"type": "run", "id": "R-20260929-021503-collect-api-fuzz"},
        "status": "ok",
        "inputsRef": {"commits": ["d6f37025"]},
        "outputs": outputs if outputs is not None else {},
        "nextAction": "交给 aggregate",
        "blockedReason": None,
        "createdAt": "2026-09-29T02:30:00Z",
    }
    document.update(fields)
    return document


def signal():
    return {
        "id": "S-01J9Z3K6Q4X8M2V7T5N0R1B3CD",
        "runId": "R-20260929-021503-collect-api-fuzz",
        "source": "synthetic",
        "probe": "api-fuzz",
        "check": "not_a_server_error",
        "environment": "staging",
        "occurredAt": "2026-09-29T02:15:03Z",
        "release": "d6f37025",
        "location": "POST /api/Order/Query",
        "message": "服务端返回 500",
        "context": {"role": "Company", "response": {"status": 500, "elapsedMs": 132}},
        "actor": {"id": "test-company", "role": "Company"},
        "normalizedMessage": None,
        "fingerprint": None,
        "suppressed": False,
        "aggregateState": "pending",
    }


def problem():
    return {
        "id": "P-0042",
        "fingerprint": "0123456789abcdef",
        "fingerprintVersion": 1,
        "probe": "api-fuzz",
        "title": "POST /api/Order/Query 返回 500",
        "status": "new",
        "firstSeenAt": "2026-09-29T02:15:03Z",
        "lastSeenAt": "2026-09-29T02:15:03Z",
        "scope": {"location": "POST /api/Order/Query", "roles": ["Company"], "source": None},
        "firstSeenRelease": "d6f37025",
        "lastSeenRelease": "d6f37025",
        "resolvedRelease": None,
        "occurrences": 1,
        "issueId": None,
        "ignoreUntil": None,
        "intermittent": False,
        "cleanCoveredRuns": 0,
        "mergedInto": None,
    }


def regression():
    return {
        "issue": "0007",
        "problems": ["0123456789abcdef"],
        "checks": [
            {"id": "api-1", "kind": "api", "role": "Company", "file": "api-1.request.json",
             "location": "POST /api/Order/Query", "requires": ["backend"], "precondition": None},
            {"id": "static-1", "kind": "static", "file": "static-1.yaml",
             "location": "src/Services/OrderService.cs:OrderService.Query",
             "targets": ["src/Services/OrderService.cs"], "requires": []},
        ],
    }


def claim_verification():
    return {
        "analysis": "Query 把 pageSize 直接传给 Take，入口与服务都没有校验，负数时抛出异常。",
        "verdict": "confirmed",
        "facts": [{"location": "src/Services/OrderService.cs:88", "observation": "pageSize 为负数时直接传给 Take"}],
        "trigger": "pageSize 小于 0",
        "counterEvidence": [{"check": "Controller 入口是否校验 pageSize", "entry": "src/Controllers/OrderController.cs:31",
                             "upstreamValidation": {"status": "absent", "location": None}, "result": "没有校验"}],
        "impact": {"kind": "non-core-error", "roles": ["Company"], "data": "无", "callSites": ["src/Controllers/OrderController.cs:31"],
                   "consequence": "查询失败并返回 500"},
        "sourceOfPhenomenon": None,
        "rootCauses": [{"file": "src/Services/OrderService.cs", "line": 88, "symbol": "OrderService.Query"}],
        "fixedOnMain": None,
        "tradeoffHit": None,
        "missingInfo": [],
        "incidental": [{"file": "src/Services/OrderService.cs", "line": 120, "symbol": None, "text": "异常被吞掉"}],
        "report": issue_report(),
        "assessment": assessment(),
    }


def assessment():
    return {
        "worth": "fix",
        "worthReason": "每次分页参数错误都返回 500",
        "taskType": "bug",
        "estimate": {"files": [{"path": "src/Services/OrderService.cs", "isNew": False}], "lines": 6},
        "direction": "在 Query 入口校验 pageSize",
        "flags": {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}},
        "reevaluateWhen": None,
        "outOfScope": [],
        "mustKeep": [],
    }


def issue_report():
    return {
        "title": "[订单] 分页参数为负数时订单查询返回 500",
        "summary": "订单列表的 pageSize 为负数时，服务端没有校验，直接传给 Take，查询失败并返回 500。",
        "steps": ["以 Company 登录", "请求 GET /api/Order?pageSize=-1"],
        "expected": "返回 400 并说明 pageSize 不合法",
        "actual": "返回 500",
        "acceptance": ["pageSize 为负数时返回 400"],
        "severity": "P2",
        "severityReason": "非核心查询出错，可改用默认分页绕开",
    }


def collect_outputs():
    return {
        "probe": "api-fuzz",
        "level": "shallow",
        "target": {"environment": "staging", "baseUrl": "https://staging.example.test", "release": "d6f37025",
                   "worktreeHead": None},
        "runStatus": "ok",
        "environment": {"health": {"status": 200, "elapsedMs": 31}, "failedRoles": [], "reportComplete": True},
        "coverage": {"endpoints": 120, "endpointsTotal": 150, "files": 0, "methods": "GET", "sources": []},
        "signalsFile": "signals.ndjson",
        "signalCount": 2,
        "signalsByCheck": {"not_a_server_error": 2},
        "stats": {"seed": 42},
        "regressions": [{"issue": "0007", "checkId": "api-1", "result": "passed"}],
        "rawDir": "raw/api-fuzz",
        "skippedReason": None,
        "notes": [],
    }


def triage_outputs():
    return {
        "problemId": "P-0042",
        "claim": {"statement": "POST /api/Order/Query 在 pageSize 为负数时返回 500", "title": "订单查询在分页参数为负数时返回 500",
                  "facts": [{"label": "main 差异", "value": "期间未修改"}], "entryPoints": ["POST /api/Order/Query"],
                  "userNotes": []},
        "verdict": "confirmed",
        "severity": "P2",
        "complexity": "low",
        "rootCauses": [{"file": "src/Services/OrderService.cs", "line": 88, "symbol": "OrderService.Query"}],
        "introducedBy": [{"commit": "a1b2c3d4", "author": "zhang", "pr": 185}],
        "disposition": "create-issue",
        "reason": "入口没有校验分页参数",
        "triageCommit": "d6f37025",
        "refuterVerdict": None,
        "treatment": "scheduled",
        "taskType": "bug",
        "sizeTier": "micro",
        "estimate": {"files": [{"path": "src/Services/OrderService.cs", "isNew": False}], "lines": 6},
        "flags": {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}},
        "labels": [],
        "evidence": {
            "facts": [{"location": "src/Services/OrderService.cs:88", "observation": "pageSize 直接传给 Take"}],
            "trigger": "pageSize 小于 0", "counterEvidence": [],
            "impact": {"kind": "non-core-error", "roles": ["Company"], "data": "无", "callSites": [], "consequence": "返回 500"},
            "sourceOfPhenomenon": None,
        },
        "worth": {"recommendation": "fix", "reason": "持续报错", "direction": "入口校验", "reevaluateWhen": None},
        "fixedOnMain": None,
        "tradeoffHit": None,
        "mergedInto": None,
        "missingInfo": [],
        "incidentalFindings": [],
        "scores": [{"itemId": "triage.evidence-location", "result": "pass", "method": "code", "reason": ""}],
        "attempts": [{"role": "claim-verifier", "statuses": ["ok"]}],
    }


def fix_plan():
    return {
        "analysis": "Query 没有校验 pageSize，入口加校验即可。",
        "summary": "在 OrderService.Query 入口校验分页参数",
        "steps": [{"file": "src/Services/OrderService.cs", "change": "pageSize 小于 1 时抛出参数错误",
                   "verification": "dotnet build SampleApp.sln"}],
        "files": [{"path": "src/Services/OrderService.cs", "isNew": False, "reason": None}],
        "estimate": {"files": 1, "lines": 6},
        "split": None,
        "protectedTouches": [],
        "flags": {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}},
        "migration": None,
        "newDependencies": [],
        "deletions": [],
        "acceptanceMapping": [{"criterion": "本 Issue 的复现检查在修复后通过", "steps": [1]}],
        "userVisibleChange": "无",
        "affectedEndpoints": ["POST /api/Order/Query"],
        "affectedPages": [],
        "notDoing": ["不调整其他查询接口"],
        "userDecisions": [],
    }
