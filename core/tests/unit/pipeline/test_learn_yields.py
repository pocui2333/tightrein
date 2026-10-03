from datetime import timedelta

import pytest
from learn_world import (
    LEARN_RUN,
    TRIAGE_RUN,
    fix_outputs,
    issue,
    make_learn_world,
    problem_event,
    problem_with,
    save_handoff,
    save_signal,
    triaged,
    verify_outputs,
)
from pipeline_world import NOW

from tightrein.domain.enums import (
    IssuePhase,
    CloseReason,
    Disposition,
    IssueStatus,
    KnowledgeType,
    Probe,
    ProblemEvent,
    RunnerStatus,
    RunStage,
    Stage,
    SuggestionKind,
    SuggestionStatus,
    TriageOutcome,
    Verdict,
    VerifyPhase,
    YieldOutcome,
)
from tightrein.pipeline.learn.steps import yields
from tightrein.pipeline.learn.steps.yields import YieldContext
from tightrein.store import idempotency
from tightrein.store.files import markdown
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import knowledge, pulls, stage_yield, suggestions
from tightrein.store.repos.pulls import PullRecord
from tightrein.store.repos.stage_yield import StageYieldRecord
from tightrein.store.repos.suggestions import SuggestionRecord

COLLECT_RUN = "R-20261005-010000-collect-static"
FIX_RUN = "R-20261005-030000-fix"


@pytest.fixture
def world(tmp_path):
    return make_learn_world(tmp_path)


def call(world, stage, role, subject, run_id, status=RunnerStatus.OK, at=NOW, tokens=(100, 20)):
    record = StageYieldRecord(run_id, stage, role, subject, 1, status, at, *tokens, 0.01)
    return StageYieldRecord(**{**record.__dict__, "id": stage_yield.append(world.conn, record)})


def outcome(world, record):
    return yields.judge(YieldContext(world.conn, world.layout, world.config, NOW), record).outcome


U, N, P = YieldOutcome.USEFUL, YieldOutcome.NO_YIELD, YieldOutcome.PENDING


def test_a_call_without_a_result_is_no_yield(world):
    assert outcome(world, call(world, Stage.TRIAGE, "claim-verifier", "P-0001", TRIAGE_RUN,
                               RunnerStatus.LIMIT_REACHED)) is N


def test_static_claims_follow_the_triage_of_their_problems(world):
    record = call(world, Stage.COLLECT, "static-review", "R", COLLECT_RUN)
    assert outcome(world, record) is N
    signal = save_signal(world, 1, COLLECT_RUN, probe=Probe.STATIC)
    assert outcome(world, record) is P
    problem_with(world, "P-0001", signal, probe=Probe.STATIC)
    assert outcome(world, record) is P
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE)
    assert outcome(world, record) is N
    triaged(world, "P-0001", attempt=2)
    assert outcome(world, record) is U
    problem_event(world, "P-0001", ProblemEvent.USER_FALSE_POSITIVE, operation="user_action")
    assert outcome(world, call(world, Stage.COLLECT, "claim-verifier-1", "R", COLLECT_RUN)) is N


@pytest.mark.parametrize("value,expected", [(None, P), (TriageOutcome.CORRECT, U), (TriageOutcome.OVERRIDDEN, N)])
def test_evidence_follows_the_outcome_of_its_triage(world, value, expected):
    problem_with(world, "P-0001")
    triaged(world, "P-0001", outcome=value)
    assert outcome(world, call(world, Stage.TRIAGE, "claim-verifier", "P-0001", TRIAGE_RUN)) is expected
    assert outcome(world, call(world, Stage.TRIAGE, "claim-verifier", "P-0002", TRIAGE_RUN)) is N


def test_refuter_counts_only_when_it_changed_the_verdict(world):
    problem_with(world, "P-0001")
    triaged(world, "P-0001", verdict=Verdict.REFUTED, refuter=Verdict.REFUTED)
    assert outcome(world, call(world, Stage.TRIAGE, "refuter", "P-0001", TRIAGE_RUN)) is N
    problem_with(world, "P-0002")
    triaged(world, "P-0002", verdict=Verdict.INSUFFICIENT, disposition=Disposition.MANUAL_QUEUE,
            refuter=Verdict.CONFIRMED)
    record = call(world, Stage.TRIAGE, "refuter", "P-0002", TRIAGE_RUN)
    assert outcome(world, record) is P
    triaged(world, "P-0002", attempt=2, run_id="R-20261003-030000-triage")
    assert outcome(world, record) is U
    triaged(world, "P-0002", attempt=3, verdict=Verdict.REFUTED, run_id="R-20261004-030000-triage")
    assert outcome(world, record) is N


def test_dedup_counts_merges_of_its_problem_in_its_run(world):
    problem_with(world, "P-0001")
    problem_with(world, "P-0002")
    dedup = call(world, Stage.TRIAGE, "triage-dedup", "P-0001", TRIAGE_RUN)
    assert outcome(world, dedup) is N
    problem_event(world, "P-0002", ProblemEvent.MERGED, run_id=TRIAGE_RUN)
    assert outcome(world, dedup) is N
    problem_event(world, "P-0001", ProblemEvent.MERGED, run_id=TRIAGE_RUN)
    assert outcome(world, dedup) is U


def _pull(world, issue_id="0007", merged=True):
    pulls.save(world.conn, PullRecord(issue_id, 12, "u", "b", "t", "MERGED" if merged else "CLOSED", NOW,
                                      merged_at=NOW if merged else None, closed_at=None if merged else NOW))


def test_fix_work_counts_merged_pull_requests(world):
    record = call(world, Stage.FIX, "fix-planner", "0007", FIX_RUN)
    issue(world, "0007", status=IssueStatus.IN_PROGRESS, phase=IssuePhase.FIX)
    assert outcome(world, record) is P
    _pull(world)
    assert outcome(world, record) is U
    issue(world, "0008", close_reason=CloseReason.FIX_REJECTED)
    assert outcome(world, call(world, Stage.FIX, "fix-session", "0008", FIX_RUN)) is N
    _pull(world, "0009", merged=False)
    assert outcome(world, call(world, Stage.FIX, "fix-executor", "0009", FIX_RUN)) is N


def _rounds(*reviews):
    found = [{"mode": mode, "passed": passed} for mode, passed in reviews]
    return [{"round": 1, "checksPassed": True, "reviews": found,
             "blockerCategories": [], "risk": None, "failures": [], "discardedFindings": []}]


def test_fix_review_needs_a_fixed_blocker_and_no_accepted_findings(world):
    light = call(world, Stage.FIX, "fix-reviewer-light", "0007", FIX_RUN)
    assert outcome(world, light) is P
    save_handoff(world, RunStage.FIX, "0007", fix_outputs(rounds=_rounds(("light", True))), FIX_RUN)
    assert outcome(world, light) is N
    save_handoff(world, RunStage.FIX, "0007", fix_outputs(rounds=_rounds(("light", False), ("light", True))),
                 "R-20261006-030000-fix", attempt=2)
    assert outcome(world, light) is P
    _pull(world)
    assert outcome(world, light) is U
    release = {"issueId": "0007", "branch": "b", "commits": [], "syncs": [], "deployments": [], "pendingOperations": [],
               "acceptedFindings": [{"check": "deep-review", "location": None, "problem": "未处理"}]}
    save_handoff(world, RunStage.RELEASE, "0007", release, "R-20261006-040000-release")
    assert outcome(world, light) is N


def test_screenshot_review_counts_fixed_layout_problems(world):
    record = call(world, Stage.VERIFY, "fix-reviewer-screenshot", "0007", "R-20261005-030000-verify")
    assert outcome(world, record) is P
    shot = {"id": "shot-1", "category": "screenshot", "command": None, "result": "fail", "evidence": [], "reason": "遮挡"}
    save_handoff(world, RunStage.VERIFY, "0007", verify_outputs(conclusion="failed", items=[shot]),
                 "R-20261005-030000-verify", phase=VerifyPhase.LOCAL)
    assert outcome(world, record) is P
    save_handoff(world, RunStage.VERIFY, "0007", verify_outputs(items=[{**shot, "result": "pass"}]),
                 "R-20261006-030000-verify", phase=VerifyPhase.LOCAL, attempt=2)
    assert outcome(world, record) is U


def test_lessons_count_when_the_written_entry_is_hit(world):
    record = call(world, Stage.LEARN, "lesson-writer", "P-0001", LEARN_RUN)
    assert outcome(world, record) is P
    idempotency.run_once(world.conn, "lesson:triage:P-0001:1", lambda: {"knowledgeId": None, "decision": "noop"},
                         world.clock)
    assert outcome(world, record) is N
    other = call(world, Stage.LEARN, "lesson-writer", "P-0002", LEARN_RUN, at=NOW - timedelta(days=100))
    idempotency.run_once(world.conn, "lesson:triage:P-0002:1", lambda: {"knowledgeId": "TL-0001", "decision": "add"},
                         world.clock)
    assert outcome(world, other) is N
    _entry(world, "TL-0001")
    assert outcome(world, other) is N
    knowledge.record_hit(world.conn, "TL-0001", NOW)
    assert outcome(world, other) is U


def _entry(world, entry_id):
    kind = KnowledgeType.from_prefix(entry_id.split("-")[0])
    frontmatter = {"id": entry_id, "type": kind.value, "summary": "摘要", "tags": ["topic"], "status": "active",
                   "supersededBy": None, "updated": "2026-06-01", "reviewBy": "2027-03-01", "related": []}
    markdown.write(world.layout.knowledge_file(kind, entry_id, "entry"),
                   MarkdownDocument(frontmatter, "# 摘要\n\n正文\n"))
    world.knowledge_service().sync()


def test_rules_improvements_and_comparisons_follow_their_results(world):
    rule = call(world, Stage.LEARN, "rule-writer", "0007", LEARN_RUN)
    other = call(world, Stage.LEARN, "rule-writer", "0008", LEARN_RUN)
    improvement = call(world, Stage.LEARN, "improvement-writer", LEARN_RUN, LEARN_RUN)
    compare = call(world, Stage.LEARN, "lesson-writer-compare-1", "2026-10-05", LEARN_RUN)
    assert (outcome(world, rule), outcome(world, improvement), outcome(world, compare)) == (P, N, N)
    idempotency.run_once(world.conn, "rule:0007", lambda: {"issueId": "0007", "accepted": True}, world.clock)
    idempotency.run_once(world.conn, "rule:0008", lambda: {"issueId": "0008", "accepted": False}, world.clock)
    assert (outcome(world, rule), outcome(world, other)) == (U, N)
    suggestions.save(world.conn, SuggestionRecord("LS-0001", SuggestionKind.IMPROVEMENT, "prompt:fix:skills/x.md",
                                                  SuggestionStatus.PENDING, NOW, {"runId": LEARN_RUN}))
    suggestions.save(world.conn, SuggestionRecord("LS-0002", SuggestionKind.KNOWLEDGE_REVIEW, "TL-0001,TL-0002",
                                                  SuggestionStatus.REJECTED, NOW,
                                                  {"runId": LEARN_RUN, "compared": True}))
    assert (outcome(world, improvement), outcome(world, compare)) == (P, N)
    suggestions.save(world.conn, SuggestionRecord("LS-0001", SuggestionKind.IMPROVEMENT, "prompt:fix:skills/x.md",
                                                  SuggestionStatus.ACCEPTED, NOW, {"runId": LEARN_RUN}))
    assert outcome(world, improvement) is U


def test_backfill_decides_only_pending_records_and_summarize_groups_roles(world):
    problem_with(world, "P-0001")
    triaged(world, "P-0001", outcome=TriageOutcome.CORRECT)
    call(world, Stage.TRIAGE, "claim-verifier", "P-0001", TRIAGE_RUN, at=NOW - timedelta(days=1))
    call(world, Stage.TRIAGE, "claim-verifier", "P-0009", TRIAGE_RUN, tokens=(50, 10))
    call(world, Stage.TRIAGE, "claim-verifier", "P-0001", "R-20261005-040000-triage", tokens=(10, 0))
    call(world, Stage.COLLECT, "claim-verifier-2", "R", COLLECT_RUN, at=NOW - timedelta(days=30))
    ctx = YieldContext(world.conn, world.layout, world.config, NOW)
    assert yields.backfill(ctx) == 4
    assert yields.backfill(ctx) == 0
    found = {(item.stage, item.role): item for item in yields.summarize(world.conn, NOW - timedelta(days=7))}
    assert set(found) == {(Stage.TRIAGE, "claim-verifier")}
    summary = found[(Stage.TRIAGE, "claim-verifier")]
    assert (summary.calls, summary.useful, summary.no_yield, summary.pending) == (3, 1, 2, 0)
    assert summary.tokens_per_useful == 190
    assert summary.calls_since_useful == 2
    assert yields.role_group("variant-scan-dp-0003") == "variant-scan"
