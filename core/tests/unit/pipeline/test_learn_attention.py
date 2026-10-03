from datetime import date, timedelta

from learn_world import (
    WEEK,
    WEEK_START,
    ZONE,
    issue,
    issue_event,
    make_learn_world,
    problem_with,
    save_run,
    save_signal,
    triaged,
)
from pipeline_world import NOW

from tightrein.domain.enums import (
    Disposition,
    IssueEvent,
    IssueStatus,
    Probe,
    ProblemStatus,
    SuggestionKind,
    SuggestionStatus,
    TriageOutcome,
    Verdict,
)
from tightrein.domain.problem import ProblemScope
from tightrein.domain.run import Coverage, Endpoint
from tightrein.domain.suppression import SuppressionRule
from tightrein.pipeline.learn.steps import attention, outcomes, weeks
from tightrein.pipeline.learn.steps.metrics import MetricContext
from tightrein.store.files import suppressions
from tightrein.store.repos import pulls, suggestions, triage
from tightrein.store.repos.pulls import PullRecord
from tightrein.store.repos.suggestions import SuggestionRecord


def context(world):
    return MetricContext(world.conn, world.layout, world.config, weeks.week_of(WEEK, ZONE), NOW, ZONE)


def kinds(found):
    return [(item.kind, item.subject_id) for item in found]


def test_attention_lists_every_kind_with_a_command(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007", status=IssueStatus.NEEDS_DECISION, created=NOW - timedelta(days=9))
    issue(world, "0008", status=IssueStatus.NEEDS_DECISION, created=NOW - timedelta(days=1))
    pulls.save(world.conn, PullRecord("0009", 21, "https://example.test/pr/21", "b", "t", "OPEN",
                                      NOW - timedelta(days=10), mergeable="MERGEABLE"))
    problem_with(world, "P-0001")
    triaged(world, "P-0001", disposition=Disposition.MANUAL_QUEUE)
    problem_with(world, "P-0002", status=ProblemStatus.IGNORED)
    triaged(world, "P-0002", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE)
    problem_with(world, "P-0003", status=ProblemStatus.IGNORED)
    triaged(world, "P-0003", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
            at=WEEK_START - timedelta(days=1))
    today = date(2026, 10, 5)
    rules = [SuppressionRule.for_fingerprint("f1", "误报", today, 5),
             SuppressionRule.for_fingerprint("f2", "误报", today, 30)]
    world.layout.suppressions().write_text(suppressions.render(rules), encoding="utf-8")
    for at in (NOW - timedelta(days=20), NOW - timedelta(days=5)):
        issue_event(world, "0010", IssueEvent.PROBLEM_REGRESSED, at, IssueStatus.TODO)
    issue_event(world, "0011", IssueEvent.PROBLEM_REGRESSED, NOW, IssueStatus.TODO)
    suggestions.save(world.conn, SuggestionRecord("LS-0001", SuggestionKind.COVERAGE_GAP, "api-fuzz",
                                                  SuggestionStatus.PENDING, NOW))
    found = attention.collect(context(world))
    assert kinds(found) == [("issue-review", "0007"), ("pr-review", "0009"), ("pr-mergeable", "0009"),
                            ("manual-queue", "P-0001"), ("suppression-expiring", "f1"), ("false-positive", "P-0002"),
                            ("regressed-issue", "0010"), ("suggestion", "LS-0001")]
    assert all(item.command for item in found)
    assert found[5].command == "tightrein retriage P-0002 --verdict confirmed --reason <原因>"
    assert attention.regressed_issues(world.conn) == {"0010": 2}


def _covered_run(world, number, started, probe=Probe.API_FUZZ):
    save_run(world, f"R-2026100{number}-010000-collect-api-fuzz", probe=probe, started=started,
             coverage=Coverage((Endpoint("GET", "/api/Order/{id}", "Admin"),)))


def test_a_false_positive_not_seen_in_later_covered_runs_is_marked_correct(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001", status=ProblemStatus.IGNORED,
                 scope=ProblemScope("GET /api/Order/{id}", frozenset({"Admin"})))
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
            at=NOW - timedelta(days=5))
    for number in (1, 2):
        _covered_run(world, number, NOW - timedelta(days=4 - number))
    assert outcomes.backfill(world.conn, world.config, NOW) == []
    _covered_run(world, 3, NOW - timedelta(days=1))
    assert outcomes.backfill(world.conn, world.config, NOW) == [("P-0001", 1)]
    assert triage.latest(world.conn, "P-0001").result.outcome is TriageOutcome.CORRECT
    assert outcomes.backfill(world.conn, world.config, NOW) == []


def test_a_false_positive_seen_again_is_not_marked(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001", status=ProblemStatus.IGNORED, fingerprint="fp-1",
                 scope=ProblemScope("GET /api/Order/{id}", frozenset({"Admin"})))
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
            at=NOW - timedelta(days=5))
    for number in (1, 2, 3):
        _covered_run(world, number, NOW - timedelta(days=4 - number))
    save_signal(world, 9, "R-20261003-010000-collect-api-fuzz", at=NOW - timedelta(days=2), fingerprint="fp-1",
                suppressed=True)
    assert outcomes.backfill(world.conn, world.config, NOW) == []
