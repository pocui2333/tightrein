from datetime import date, datetime, timezone

import pytest
from contract_samples import changed, paths, without

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType

ISSUE = "handoff/frontmatter/issue.schema.json"
KNOWLEDGE = "data/knowledge.schema.json"
REPORT = "handoff/frontmatter/report.schema.json"

ISSUE_FRONTMATTER = {
    "kind": "issue", "id": "0007", "status": "needs-decision", "from": "triage", "to": "fix", "subject": "0007",
    "parent": "P-0042", "created": "2026-09-29T03:00:00Z", "updated": "2026-09-29T03:00:00Z",
    "next": "issue approve 0007", "title": "订单查询在分页参数为负数时返回 500", "severity": "P2", "origin": "triage",
    "treatment": "scheduled", "taskType": "bug", "sizeTier": "micro", "source": "api-fuzz(shallow)",
    "problems": ["P-0042"], "rootCause": ["src/Services/OrderService.cs:88"],
    "introducedBy": {"commit": "a1b2c3d4", "author": "zhang", "pr": 185}, "triageCommit": "d6f37025",
    "findings": "data/findings/P-0042.md", "branch": None, "pr": None, "runId": "R-20260929-021503-issue",
    "closeReason": None, "hold": None, "phase": None,
}
HOLD = {"reason": "连续 2 次合并前验证失败", "stage": "verify", "since": "2026-09-30T01:00:00Z", "details": ""}
KNOWLEDGE_FRONTMATTER = {
    "id": "TL-0003", "type": "triage-lesson", "summary": "判不成立前追到入口", "tags": ["stage:triage"], "status": "active",
    "supersededBy": None, "updated": "2026-09-29", "reviewBy": "2026-12-28", "related": ["DP-0012"],
    "sourceRunId": "R-20260929-021503-learn",
}
BASE_REPORT = {"summary": "分页参数为负数时返回 500，已提 Issue", "tags": ["route:POST /api/Order/Query"],
               "runId": "R-20260929-021503-triage", "createdAt": "2026-09-29T03:00:00Z",
               "updatedAt": "2026-09-29T03:00:00Z"}
FINDING = {**BASE_REPORT, "type": "finding", "id": "triage-P-0042", "problemId": "P-0042", "status": "create-issue",
           "verdict": "confirmed", "severity": "P2", "disposition": "create-issue", "treatment": "scheduled",
           "triageCommit": "d6f37025"}
FIX_REPORT = {**BASE_REPORT, "type": "fix-report", "id": "fix-0007", "issueId": "0007", "status": "ok",
              "branch": "cty/fix-order-query-500", "baseCommit": "d6f37025"}
VERIFY_REPORT = {**without(BASE_REPORT, "updatedAt"), "type": "verify-report", "id": "verify-local-0007",
                 "issueId": "0007", "phase": "local", "status": "ok", "commit": "e5f6a7b8"}

VALID = [
    (ISSUE, ISSUE_FRONTMATTER),
    (ISSUE, changed(ISSUE_FRONTMATTER, status="done", closeReason="fixed", phase="deploy-check")),
    (ISSUE, changed(ISSUE_FRONTMATTER, status="cancelled", closeReason="wont-fix")),
    (ISSUE, changed(ISSUE_FRONTMATTER, hold=HOLD)),
    (ISSUE, changed(ISSUE_FRONTMATTER, status="in-progress", phase="verify")),
    (ISSUE, without(ISSUE_FRONTMATTER, "closeReason", "hold", "treatment")),
    (KNOWLEDGE, KNOWLEDGE_FRONTMATTER),
    (KNOWLEDGE, changed(KNOWLEDGE_FRONTMATTER, status="superseded", supersededBy="TL-0009")),
    (REPORT, FINDING),
    (REPORT, FIX_REPORT),
    (REPORT, VERIFY_REPORT),
]

INVALID = [
    (ISSUE, changed(ISSUE_FRONTMATTER, status="done"), "$.closeReason"),
    (ISSUE, without(changed(ISSUE_FRONTMATTER, status="cancelled"), "closeReason"), "$"),
    (ISSUE, changed(ISSUE_FRONTMATTER, status="merged"), "$.status"),
    (ISSUE, changed(ISSUE_FRONTMATTER, phase="merged"), "$.phase"),
    (ISSUE, changed(ISSUE_FRONTMATTER, closeReason="fixed"), "$.closeReason"),
    (ISSUE, changed(ISSUE_FRONTMATTER, hold=without(HOLD, "since")), "$.hold"),
    (ISSUE, changed(ISSUE_FRONTMATTER, rootCause=["OrderService.Query"]), "$.rootCause[0]"),
    (ISSUE, changed(ISSUE_FRONTMATTER, created="2026-09-29"), "$.created"),
    (KNOWLEDGE, changed(KNOWLEDGE_FRONTMATTER, status="superseded"), "$.supersededBy"),
    (KNOWLEDGE, changed(KNOWLEDGE_FRONTMATTER, supersededBy="TL-0009"), "$.supersededBy"),
    (KNOWLEDGE, changed(KNOWLEDGE_FRONTMATTER, tags=[]), "$.tags"),
    (KNOWLEDGE, changed(KNOWLEDGE_FRONTMATTER, hits=3), "$"),
    (REPORT, without(FINDING, "summary"), "$"),
    (REPORT, changed(FINDING, id="triage-0007"), "$.id"),
    (REPORT, changed(FINDING, status="ok"), "$.status"),
    (REPORT, without(FIX_REPORT, "baseCommit"), "$"),
    (REPORT, changed(VERIFY_REPORT, phase="prod"), "$.phase"),
    (REPORT, changed(FINDING, type="report"), "$.type"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_dates_must_be_strings_as_written_in_frontmatter():
    parsed = changed(KNOWLEDGE_FRONTMATTER, updated=date(2026, 9, 29))
    assert paths(KNOWLEDGE, parsed) == {"$.updated"}
    parsed = changed(ISSUE_FRONTMATTER, created=datetime(2026, 9, 29, 3, tzinfo=timezone.utc))
    assert paths(ISSUE, parsed) == {"$.created"}


@pytest.mark.parametrize("kind", list(KnowledgeType))
def test_every_knowledge_type_is_accepted(kind):
    sample = changed(KNOWLEDGE_FRONTMATTER, type=kind.value, id=f"{kind.prefix}-0001")
    assert paths(KNOWLEDGE, sample) == set()


def test_knowledge_status_values_follow_domain():
    for status in KnowledgeStatus:
        superseded_by = "TL-0009" if status is KnowledgeStatus.SUPERSEDED else None
        sample = changed(KNOWLEDGE_FRONTMATTER, status=status.value, supersededBy=superseded_by)
        assert paths(KNOWLEDGE, sample) == set()
