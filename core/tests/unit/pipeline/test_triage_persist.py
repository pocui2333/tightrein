import pytest
from pipeline_world import NOW, make_signal
from triage_world import make_triage_world, store_problem, verification

from tightrein.contracts import validate
from tightrein.domain.enums import (
    Complexity,
    Disposition,
    IssueLabel,
    ProblemEvent,
    ProblemStatus,
    RunnerStatus,
    ScoreMethod,
    ScoreResult,
    Severity,
    SizeTier,
    Stage,
    Treatment,
    Verdict,
    WorthRecommendation,
)
from tightrein.domain.problem import ProblemContext
from tightrein.domain.triage import TriageResult
from tightrein.pipeline.aggregate.steps import apply
from tightrein.pipeline.triage.steps import claims, disposition, evidence, persist
from tightrein.pipeline.triage.steps.case import TriageCase
from tightrein.store.files import suppressions
from tightrein.store.repos import problem_events, problems, scores, triage
from tightrein.store.repos.problem_events import OPERATION_AUTO, ProblemEventRecord
from tightrein.store.repos.scores import ScoreRecord
from tightrein.store.repos.triage import TriageRecord

RUN = "R-20261005-030000-triage"
LOCATION = "src/Services/OrderService.src:12"


def case(world, problem_id, verdict=Verdict.CONFIRMED):
    problem = problems.get(world.conn, problem_id)
    claim = claims.build(problem, [make_signal()], [])
    found = TriageCase(problem, None, [], False, claim, Complexity.LOW, "-", 1)
    found.evidence = evidence.Evidence("claim-verifier", verification(verdict.value), {
        "verdict": verdict.value, "evidence": {"facts": [{"location": LOCATION, "observation": "没有过滤"}],
                                               "impact": {"consequence": "返回 500"}},
        "rootCauses": [{"file": "src/Services/OrderService.src", "line": 12}]}, passed=True)
    found.verdict = verdict
    return found


def decide(world, problem, verdict=Verdict.CONFIRMED, **changes):
    values = dict(verdict=verdict, severity=Severity.P2, tier=SizeTier.MICRO,
                  worth=WorthRecommendation.FIX, fixed_on_main=False, tradeoff_hit=False, needs_manual=False,
                  refuter_verdict=None, estimated_files=["src/Services/OrderService.src"])
    values.update(changes)
    return disposition.decide(world.config, problem, **values)


def test_disposition_table_treatment_and_labels(tmp_path):
    world = make_triage_world(tmp_path, protectedPaths=["Migrations/"])
    problem = store_problem(world)
    fix = decide(world, problem)
    assert (fix.disposition, fix.treatment, fix.labels) == (Disposition.CREATE_ISSUE, Treatment.SCHEDULED, ())
    assert decide(world, problem, severity=Severity.P1).treatment is Treatment.IMMEDIATE
    observed = decide(world, problem, severity=Severity.P3, tier=SizeTier.MEDIUM)
    assert (observed.disposition, observed.treatment) == (Disposition.DEFERRED, Treatment.OBSERVE)
    again = decide(world, problem, severity=Severity.P3, tier=SizeTier.MEDIUM, observed_before=True)
    assert (again.disposition, again.treatment) == (Disposition.CREATE_ISSUE, Treatment.SCHEDULED)
    wont = decide(world, problem, worth=WorthRecommendation.WONT)
    assert (wont.disposition, wont.treatment) == (Disposition.ACCEPTED_TRADEOFF, Treatment.WONT_FIX)
    assert decide(world, problem, Verdict.REFUTED, severity=None).treatment is None
    protected = decide(world, problem, estimated_files=["Migrations/ChangeSet.src"])
    assert protected.labels == (IssueLabel.DISCUSS_WITH_AUTHOR,)
    assert decide(world, problem, Verdict.REFUTED, severity=None).disposition is Disposition.FALSE_POSITIVE
    with pytest.raises(ValueError):
        decide(world, problem, Verdict.REFUTED, severity=Severity.P0)
    assert decide(world, problem, Verdict.REFUTED, severity=Severity.P0,
                  refuter_verdict=Verdict.REFUTED).disposition is Disposition.FALSE_POSITIVE
    assert decide(world, problem, fixed_on_main=True).disposition is Disposition.AWAITING_DEPLOY
    assert decide(world, problem, tradeoff_hit=True, worth=None).disposition is Disposition.ACCEPTED_TRADEOFF
    assert decide(world, problem, Verdict.INSUFFICIENT).disposition is Disposition.MANUAL_QUEUE
    assert decide(world, problem, needs_manual=True).disposition is Disposition.MANUAL_QUEUE
    assert decide(world, problem, severity=Severity.P0,
                  worth=WorthRecommendation.DEFER).disposition is Disposition.CREATE_ISSUE
    deferred = decide(world, problem, worth=WorthRecommendation.DEFER)
    assert deferred.disposition is Disposition.DEFERRED
    assert (deferred.context.ignore_until.occurrences, deferred.context.ignore_until.severity_escalated) == (3, True)


def test_override_uses_the_given_or_the_default_disposition(tmp_path):
    world = make_triage_world(tmp_path)
    problem = store_problem(world)
    assert disposition.override(world.config, problem, Verdict.REFUTED).disposition is Disposition.FALSE_POSITIVE
    assert disposition.override(world.config, problem, Verdict.CONDITIONAL).disposition is Disposition.CREATE_ISSUE
    assert disposition.override(world.config, problem, Verdict.CONFIRMED,
                                Disposition.DEFERRED).context.ignore_until is not None


def record(problem_id="P-0001", verdict=Verdict.REFUTED, chosen=Disposition.FALSE_POSITIVE):
    return TriageRecord(TriageResult(problem_id, 1, verdict, chosen, "入口已拒绝", "c" * 40), RUN, NOW)


def test_false_positive_writes_result_scores_event_context_and_suppression(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    problem_events.append(world.conn, ProblemEventRecord("P-0001", NOW, ProblemEvent.RETRIAGE_REQUESTED, OPERATION_AUTO,
                                                         ProblemStatus.NEW, ProblemStatus.NEW))
    event_id = problem_events.for_problem(world.conn, "P-0001")[0].id
    row = ScoreRecord(Stage.TRIAGE, RUN, "P-0001", 1, "triage.counter-check", ScoreResult.PASS, ScoreMethod.CODE, NOW)
    updated = persist.commit(world.conn, world.layout, world.clock, world.config, run_id=RUN, problem_id="P-0001",
                             event=ProblemEvent.TRIAGED, context=ProblemContext(disposition=Disposition.FALSE_POSITIVE),
                             reason="入口已拒绝", record=record(), score_rows=[row], handled=[event_id])
    assert updated.status is ProblemStatus.IGNORED
    assert triage.latest(world.conn, "P-0001").result.disposition is Disposition.FALSE_POSITIVE
    assert [item.item for item in scores.find(world.conn, run_id=RUN)] == ["triage.counter-check"]
    events = problem_events.for_problem(world.conn, "P-0001")
    assert events[0].handled_at == NOW
    assert events[-1].event is ProblemEvent.TRIAGED
    assert events[-1].detail == {"context": {"disposition": "false-positive"}}
    rules = suppressions.read(world.layout.suppressions())
    assert [(rule.fingerprint, rule.reason) for rule in rules] == [("p-0001-fingerprint", "入口已拒绝")]


def test_deferred_sets_the_reopen_condition_and_merge_uses_the_shared_repository(tmp_path):
    world = make_triage_world(tmp_path)
    problem = store_problem(world)
    store_problem(world, "P-0002", make_signal(2))
    context = disposition.context_for(world.config, problem, Disposition.DEFERRED)
    updated = persist.commit(world.conn, world.layout, world.clock, world.config, run_id=RUN, problem_id="P-0001",
                             event=ProblemEvent.TRIAGED, context=context, reason="暂不修",
                             record=record(verdict=Verdict.CONFIRMED, chosen=Disposition.DEFERRED))
    assert updated.status is ProblemStatus.IGNORED and updated.ignore_until.occurrences == 3
    merged = persist.merge(world.conn, world.layout, world.clock, world.config, run_id=RUN, problem_id="P-0002",
                           target="P-0001", reason="与 P-0001 同一根因")
    assert merged.merged_into == "P-0001"
    assert problems.by_fingerprint(world.conn, "p-0002-fingerprint").id == "P-0001"


def test_a_failed_write_rolls_everything_back(tmp_path, monkeypatch):
    world = make_triage_world(tmp_path)
    store_problem(world)

    def broken(*args, **kwargs):
        raise RuntimeError("磁盘已满")

    monkeypatch.setattr(apply, "commit", broken)
    with pytest.raises(RuntimeError):
        persist.commit(world.conn, world.layout, world.clock, world.config, run_id=RUN, problem_id="P-0001",
                       event=ProblemEvent.TRIAGED, context=ProblemContext(disposition=Disposition.FALSE_POSITIVE),
                       reason="x", record=record())
    assert triage.latest(world.conn, "P-0001") is None
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.NEW


def test_handoff_outputs_follow_the_triage_schema(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    found = case(world, "P-0001")
    found.evidence.outputs = {"verdict": "confirmed", "evidence": {key: verification()[key] for key in (
        "facts", "trigger", "counterEvidence", "impact", "sourceOfPhenomenon")},
                              "rootCauses": verification()["rootCauses"], "missingInfo": [],
                              "assessment": verification()["assessment"]}
    found.record("claim-verifier", [RunnerStatus.OK])
    decision = decide(world, found.problem)
    outputs = persist.handoff_outputs(found, decision, "判定为确认成立", "c" * 40)
    assert validate.validate("handoff/outputs/triage.schema.json", outputs) == []
    assert outputs["attempts"] == [{"role": "claim-verifier", "statuses": ["ok"]}]
    merged = persist.handoff_outputs(found, None, "已并入 P-0002", "c" * 40)
    assert (merged["verdict"], merged["disposition"]) == (None, None)
