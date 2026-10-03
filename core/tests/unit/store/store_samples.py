"""store 各测试共用的实体样例。时间取固定值，编号取合法格式。"""

from datetime import date, datetime, timezone

from tightrein.domain.enums import (
    CloseReason,
    Complexity,
    Disposition,
    IssueLabel,
    IssueStatus,
    KnowledgeStatus,
    KnowledgeType,
    Probe,
    ProbeLevel,
    ProblemStatus,
    RunStage,
    RunStatus,
    Severity,
    SizeTier,
    Source,
    Stage,
    TaskType,
    Treatment,
    Verdict,
)
from tightrein.domain.issue import Hold, Issue
from tightrein.domain.knowledge import KnowledgeEntry
from tightrein.domain.problem import IgnoreCondition, Problem, ProblemScope
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck, Run
from tightrein.domain.signal import Signal
from tightrein.domain.triage import IntroducedBy, RootCause, TriageFlags, TriageResult

T0 = datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)
RUN_ID = "R-20260929-021503-collect-api-fuzz"


def run(run_id=RUN_ID, **changes):
    values = dict(
        id=run_id, stage=RunStage.COLLECT, started_at=T0, status=RunStatus.OK, probe=Probe.API_FUZZ,
        level=ProbeLevel.SHALLOW, ended_at=datetime(2026, 9, 29, 2, 20, tzinfo=timezone.utc),
        target_commit="abc1234",
        coverage=Coverage(endpoints=(Endpoint("GET", "/api/Order/{id}", "Company"),), endpoints_total=12,
                          methods="GET"),
        environment_detail=EnvironmentDetail(health=HealthCheck(200, 35), failed_roles=("Personal",)),
        trace_id="0" * 32,
    )
    values.update(changes)
    return Run(**values)


def signal(number=1, run_id=RUN_ID, **changes):
    values = dict(
        id=f"S-01J9Z3{number:020d}", run_id=run_id, source=Source.ERROR, probe=Probe.API_FUZZ,
        check="not_a_server_error", environment="staging", occurred_at=T0, release="abc1234",
        location="GET /api/Order/42", message="500 Internal Server Error",
        context={"response": {"status": 500}}, actor={"role": "Company"},
    )
    values.update(changes)
    return Signal(**values)


def problem(problem_id="P-0001", fingerprint="a1b2c3d4e5f60718", **changes):
    values = dict(
        id=problem_id, fingerprint=fingerprint, fingerprint_version=1, probe=Probe.API_FUZZ,
        title="GET /api/Order/{id} not_a_server_error 500", status=ProblemStatus.NEW, first_seen_at=T0,
        last_seen_at=T0, scope=ProblemScope("GET /api/Order/{id}", frozenset({"Company"})),
        first_seen_release="abc1234", last_seen_release="abc1234",
    )
    values.update(changes)
    return Problem(**values)


def ignored_problem(problem_id="P-0002", fingerprint="b1b2c3d4e5f60718"):
    condition = IgnoreCondition(until=datetime(2026, 11, 1, tzinfo=timezone.utc), occurrences=3,
                                baseline_occurrences=2, baseline_release="abc1234")
    return problem(problem_id, fingerprint, status=ProblemStatus.IGNORED, ignore_until=condition, occurrences=2,
                   intermittent=True, clean_covered_runs=1)


def triage_result(problem_id="P-0001", attempt=1, **changes):
    values = dict(
        problem_id=problem_id, attempt=attempt, verdict=Verdict.CONFIRMED, disposition=Disposition.CREATE_ISSUE,
        reason="缺少归属校验", triage_commit="abc1234", severity=Severity.P0,
        complexity=Complexity.LOW, root_causes=(RootCause("Services/OrderService.cs", 88, "OrderService.Query"),),
        introduced_by=IntroducedBy("def5678", "someone", 12), refuter_verdict=Verdict.CONFIRMED,
        flags=TriageFlags(public_contract=True), labels=(IssueLabel.DISCUSS_WITH_AUTHOR,),
        treatment=Treatment.IMMEDIATE, task_type=TaskType.SECURITY, size_tier=SizeTier.SMALL,
    )
    values.update(changes)
    return TriageResult(**values)


def issue(issue_id="0007", **changes):
    values = dict(
        id=issue_id, slug="order-owner-check", title="订单查询缺少归属校验", status=IssueStatus.NEEDS_DECISION,
        severity=Severity.P0, created_at=T0, updated_at=datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc),
        treatment=Treatment.IMMEDIATE, task_type=TaskType.SECURITY, size_tier=SizeTier.SMALL,
        problems=("P-0001", "P-0003"),
        root_cause=("Services/OrderService.cs:88",), introduced_by=IntroducedBy("def5678", None, None),
        triage_commit="abc1234", findings="data/findings/P-0001.md", branch="fix/0007-order-owner-check",
        hold=Hold("验证连续失败", Stage.VERIFY, T0, "两次合并前验证失败"),
    )
    values.update(changes)
    return Issue(**values)


def closed_issue(issue_id="0008"):
    return issue(issue_id, slug="duplicate", status=IssueStatus.CANCELLED, close_reason=CloseReason.DUPLICATE,
                 hold=None)


def knowledge_entry(entry_id="DP-0012", **changes):
    values = dict(
        id=entry_id, type=KnowledgeType.DEFECT_PATTERN, title="查询接口缺少归属校验",
        summary="按编号查询时没有校验数据归属", status=KnowledgeStatus.ACTIVE, updated=date(2026, 9, 1),
        path="knowledge/defect-pattern/DP-0012-owner-check.md", tags=("api", "权限"), related=("TL-0001",),
        review_by=date(2027, 3, 1),
    )
    values.update(changes)
    return KnowledgeEntry(**values)
