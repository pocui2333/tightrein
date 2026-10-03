import json
from datetime import date, datetime, timedelta, timezone

import pytest
from learn_world import (
    WEEK,
    WEEK_START,
    ZONE,
    closed_fixed,
    issue,
    issue_event,
    make_learn_world,
    problem_event,
    problem_with,
    save_handoff,
    save_run,
    save_signal,
    triaged,
    fix_outputs,
    verify_outputs,
)
from pipeline_world import NOW

from tightrein.domain.enums import (
    Disposition,
    IssueEvent,
    IssueStatus,
    Probe,
    ProblemEvent,
    ProblemStatus,
    RunStage,
    RunStatus,
    SignalAggregateState,
    Stage,
    TriageOutcome,
    Verdict,
    VerifyPhase,
)
from tightrein.domain.run import Coverage, Endpoint
from tightrein.pipeline.learn.steps import metrics, weeks
from tightrein.pipeline.learn.steps.metrics import MetricContext
from tightrein.pipeline.learn.steps.weeks import DueTime
from tightrein.runner.result import Usage
from tightrein.store.repos import pulls
from tightrein.store.repos.pulls import PullRecord
from tightrein.runner.limits import DailyBudget
from tightrein.domain.clock import FixedClock


def context(world, now=NOW):
    return MetricContext(world.conn, world.layout, world.config, weeks.week_of(WEEK, ZONE), now, ZONE)


def values(found, metric):
    return {value.dimension: (value.value, value.numerator, value.denominator, value.sample_size)
            for value in found if value.metric == metric}


def test_week_is_monday_to_monday_in_the_local_zone():
    window = weeks.week_of(date(2026, 10, 8), ZONE)
    assert (window.start, window.end) == (WEEK_START, WEEK_START + timedelta(days=7))
    assert weeks.week_start(window, ZONE) == WEEK
    assert not window.contains(WEEK_START - timedelta(seconds=1))
    assert window.contains(WEEK_START)


def test_workdays_skip_weekends_and_configured_holidays(tmp_path):
    world = make_learn_world(tmp_path, schedule={"tick": {"weekdays": [1], "minutes": [0]},
                                                 "nonWorkingDays": ["2026-10-06"]})
    friday = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)
    assert weeks.workdays(friday, datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc), world.config, ZONE) == 2


def test_due_times_follow_days_and_the_first_workday():
    tasks = [{"name": "static", "days": "workdays", "at": ["08:30"]},
             {"name": "weekly", "days": "firstWorkdayOfWeek", "at": ["08:00"]},
             {"name": "logs", "days": "daily", "at": ["23:00"]}]
    since, until = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    found = weeks.due_times(tasks, frozenset({date(2026, 10, 5)}), since, until, ZONE)
    assert found == [
        DueTime("logs", datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)),
        DueTime("logs", datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)),
        DueTime("logs", datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)),
        DueTime("weekly", datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)),
        DueTime("static", datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)),
    ]


def test_new_problems_by_probe_and_severity_within_the_week(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    problem_with(world, "P-0002", probe=Probe.ALERTS)
    problem_with(world, "P-0003")
    problem_event(world, "P-0001", ProblemEvent.REPRODUCED, WEEK_START, ProblemStatus.NEW)
    problem_event(world, "P-0001", ProblemEvent.USER_REOPENED, NOW, ProblemStatus.NEW)
    problem_event(world, "P-0002", ProblemEvent.REPRODUCED, NOW, ProblemStatus.NEW)
    problem_event(world, "P-0003", ProblemEvent.REPRODUCED, WEEK_START - timedelta(seconds=1), ProblemStatus.NEW)
    triaged(world, "P-0001")
    found = values(metrics.new_problems(context(world)), "new-problems")
    assert found == {"all": (2, None, None, 2), "probe=api-fuzz": (1, None, None, 1), "probe=alerts": (1, None, None, 1),
                     "severity=P1": (1, None, None, 1), "severity=untriaged": (1, None, None, 1)}


def test_api_coverage_unions_runs_and_splits_methods_with_a_cached_spec(tmp_path):
    world = make_learn_world(tmp_path, sources={"api-fuzz": {"exclude": ["^/api/Internal"]}})
    commit = "a" * 40
    spec = {"paths": {"/api/Order": {"get": {}, "post": {}}, "/api/User": {"get": {}},
                      "/api/Internal/x": {"get": {}}}}
    world.layout.openapi(commit).parent.mkdir(parents=True)
    world.layout.openapi(commit).write_text(json.dumps(spec), encoding="utf-8")
    save_run(world, "R-20261005-010000-collect-api-fuzz", probe=Probe.API_FUZZ,
             coverage=Coverage((Endpoint("GET", "/api/Order", "Admin"),), endpoints_total=3))
    save_run(world, "R-20261005-020000-collect-api-fuzz", probe=Probe.API_FUZZ, commit=commit,
             coverage=Coverage((Endpoint("POST", "/api/Order", "Admin"),), endpoints_total=3))
    found = values(metrics.api_coverage(context(world)), "api-coverage")
    assert found["all"] == (2 / 3, 2, 3, 2)
    assert found["method=GET"] == (0.5, 1, 2, 2)
    assert found["method=write"] == (1.0, 1, 1, 2)


def test_coverage_without_runs_or_totals_has_no_value(tmp_path):
    world = make_learn_world(tmp_path)
    assert values(metrics.api_coverage(context(world)), "api-coverage") == {"all": (None, 0, 0, 0)}
    save_run(world, "R-20261005-010000-collect-api-fuzz", probe=Probe.API_FUZZ,
             coverage=Coverage((Endpoint("GET", "/api/Order", "Admin"),)))
    assert values(metrics.api_coverage(context(world)), "api-coverage") == {"all": (None, 1, None, 1)}


def test_noise_counts_suppressed_voided_and_intermittent_signals(tmp_path):
    world = make_learn_world(tmp_path)
    run = "R-20261005-010000-collect-api-fuzz"
    suppressed = save_signal(world, 1, run, suppressed=True)
    save_signal(world, 2, run, aggregate_state=SignalAggregateState.VOIDED)
    intermittent = save_signal(world, 3, run)
    save_signal(world, 4, run)
    save_signal(world, 5, "R-20261005-010000-collect-alerts", probe=Probe.ALERTS)
    save_signal(world, 6, run, at=WEEK_START - timedelta(days=1), suppressed=True)
    problem_with(world, "P-0001", intermittent, status=ProblemStatus.PENDING, intermittent=True)
    problem_with(world, "P-0002", suppressed)
    found = metrics.noise(context(world))
    assert values(found, "noise-ratio") == {"all": (0.6, 3, 5, 5), "probe=api-fuzz": (0.75, 3, 4, 4),
                                            "probe=alerts": (0.0, 0, 1, 1)}
    assert values(found, "noise-count") == {"kind=suppressed": (1, None, None, 1), "kind=voided": (1, None, None, 1),
                                            "kind=intermittent": (1, None, None, 1)}


def test_triage_accuracy_uses_outcomes_filled_this_week(tmp_path):
    world = make_learn_world(tmp_path)
    for number in (1, 2, 3):
        problem_with(world, f"P-000{number}")
    triaged(world, "P-0001", outcome=TriageOutcome.CORRECT, outcome_at=NOW)
    triaged(world, "P-0002", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
            outcome=TriageOutcome.FALSE_REFUTE, outcome_at=NOW)
    triaged(world, "P-0003", outcome=TriageOutcome.CORRECT, outcome_at=WEEK_START - timedelta(days=1))
    found = metrics.triage_accuracy(context(world))
    assert values(found, "triage-accuracy") == {"all": (0.5, 1, 2, 2), "probe=api-fuzz": (0.5, 1, 2, 2),
                                                "verdict=confirmed": (1.0, 1, 1, 1), "verdict=refuted": (0.0, 0, 1, 1)}
    assert values(found, "false-refutes") == {"all": (1, None, None, 1)}


def test_triage_accuracy_without_outcomes_has_no_sample(tmp_path):
    world = make_learn_world(tmp_path)
    assert values(metrics.triage_accuracy(context(world)), "triage-accuracy") == {"all": (None, 0, 0, 0)}


def test_manual_queue_counts_open_problems_and_the_longest_wait(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    problem_with(world, "P-0002", status=ProblemStatus.IGNORED)
    problem_with(world, "P-0003")
    triaged(world, "P-0001", disposition=Disposition.MANUAL_QUEUE, at=NOW - timedelta(days=7))
    triaged(world, "P-0002", disposition=Disposition.MANUAL_QUEUE)
    triaged(world, "P-0003", disposition=Disposition.MANUAL_QUEUE)
    triaged(world, "P-0003", attempt=2, disposition=Disposition.CREATE_ISSUE)
    found = metrics.manual_queue(context(world))
    assert values(found, "manual-queue") == {"all": (1, None, None, 1)}
    assert values(found, "manual-queue-max-wait") == {"all": (5, None, None, 1)}


def test_lead_times_take_the_median_of_each_segment(tmp_path):
    world = make_learn_world(tmp_path)
    start = WEEK_START - timedelta(days=5)
    problem_with(world, "P-0001", first_seen_at=start)
    issue(world, "0007", created=start + timedelta(hours=2))
    issue_event(world, "0007", IssueEvent.APPROVE, start + timedelta(hours=5), IssueStatus.TODO)
    pulls.save(world.conn, PullRecord("0007", 12, "https://example.test/pr/12", "cty/fix-x", "修复", "MERGED",
                                      start + timedelta(hours=9), merged_at=start + timedelta(hours=10)))
    closed_fixed(world, "0007", NOW)
    issue(world, "0008", problems_=("P-0009",), created=start)
    closed_fixed(world, "0008", NOW)
    found = values(metrics.lead_times(context(world)), "lead-time-hours")
    assert found["segment=seen-to-issue"] == (2.0, None, None, 1)
    assert found["segment=issue-to-approve"] == (3.0, None, None, 1)
    assert found["segment=approve-to-pr"] == (4.0, None, None, 1)
    assert found["segment=pr-to-merge"] == (1.0, None, None, 1)
    assert found["segment=merge-to-fixed"][3] == 1


def test_verify_first_pass_and_change_size_read_handoffs(tmp_path):
    world = make_learn_world(tmp_path)
    run = "R-20261005-030000-verify"
    save_handoff(world, RunStage.VERIFY, "0007", verify_outputs("0007"), run, phase=VerifyPhase.LOCAL)
    save_handoff(world, RunStage.VERIFY, "0008", verify_outputs("0008", "failed"), run, phase=VerifyPhase.LOCAL)
    save_handoff(world, RunStage.VERIFY, "0008", verify_outputs("0008"), "R-20261006-030000-verify",
                 phase=VerifyPhase.LOCAL, attempt=2)
    assert values(metrics.verify_first_pass(context(world)), "verify-first-pass") == {"all": (0.5, 1, 2, 2)}
    fix_run = "R-20261005-030000-fix"
    save_handoff(world, RunStage.FIX, "0007", fix_outputs("0007", (("a", 200, 0),)), fix_run)
    save_handoff(world, RunStage.FIX, "0007", fix_outputs("0007", (("a", 3, 1),)), "R-20261006-030000-fix", attempt=2)
    save_handoff(world, RunStage.FIX, "0008", fix_outputs("0008", tuple((f"f{n}", 1, 0) for n in range(11))), fix_run)
    found = metrics.change_size(context(world))
    assert values(found, "change-files") == {"stat=median": (6.0, None, None, 2), "stat=max": (11, None, None, 2)}
    assert values(found, "change-lines") == {"stat=median": (7.5, None, None, 2), "stat=max": (11, None, None, 2)}
    assert values(found, "change-over-cap") == {"all": (1, None, None, 2)}


def test_fixed_regression_and_pr_rejection(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007")
    closed_fixed(world, "0007", NOW - timedelta(days=30))
    issue_event(world, "0007", IssueEvent.PROBLEM_REGRESSED, NOW - timedelta(days=20), IssueStatus.TODO)
    closed_fixed(world, "0008", NOW)
    closed_fixed(world, "0009", NOW - timedelta(days=200))
    found = metrics.fixed_issues(context(world)) + metrics.regression_rate(context(world))
    assert values(found, "fixed-issues") == {"all": (1, None, None, 1)}
    assert values(found, "regression-rate") == {"all": (0.5, 1, 2, 2)}
    for number, merged, closed in ((1, NOW, None), (2, None, NOW), (3, None, WEEK_START - timedelta(days=2))):
        pulls.save(world.conn, PullRecord(f"00{number}0", number, "u", "b", "t", "CLOSED", NOW - timedelta(days=9),
                                          merged_at=merged, closed_at=closed))
    assert values(metrics.pr_rejection(context(world)), "pr-rejection-rate") == {"all": (0.5, 1, 2, 2)}


def test_cost_comes_from_budget_usage_and_durations_from_runs(tmp_path):
    world = make_learn_world(tmp_path)
    budget = DailyBudget(world.conn, FixedClock(NOW), ZONE)
    budget.add(Stage.TRIAGE, Usage(1000, 200, cost_usd=0.5))
    budget.add(Stage.FIX, Usage(300, 100, cost_usd=0.2, cost_estimated=True))
    DailyBudget(world.conn, FixedClock(WEEK_START - timedelta(hours=1)), ZONE).add(Stage.FIX, Usage(9, 9, cost_usd=9))
    save_run(world, "R-20261005-030000-triage", RunStage.TRIAGE, ended=NOW + timedelta(minutes=10))
    save_run(world, "R-20261005-040000-triage", RunStage.TRIAGE, started=NOW + timedelta(hours=1),
             ended=NOW + timedelta(hours=1, minutes=30))
    found = metrics.cost(context(world))
    assert values(found, "tokens") == {"all": (1600, None, None, 1600), "stage=fix": (400, None, None, 400),
                                       "stage=triage": (1200, None, None, 1200)}
    assert values(found, "cost-usd")["all"] == (0.7, None, None, 2)
    assert values(found, "cost-usd-estimated") == {"stage=fix": (0.2, None, None, 1)}
    assert values(found, "run-minutes") == {"stage=triage": (40.0, None, None, 2)}
    assert values(found, "run-minutes-median") == {"stage=triage": (20.0, None, None, 2)}


def test_compute_keeps_going_when_one_metric_fails_and_filters_by_stage(tmp_path, monkeypatch):
    world = make_learn_world(tmp_path)

    def broken(ctx):
        raise ValueError("来源缺失")

    monkeypatch.setattr(metrics, "METRICS", (("noise", Stage.AGGREGATE, broken),
                                             *[item for item in metrics.METRICS if item[0] != "noise"]))
    found, errors = metrics.compute(context(world))
    assert errors == [{"item": "noise", "reason": "ValueError: 来源缺失"}]
    assert "fixed-issues" in {value.metric for value in found}
    found, errors = metrics.compute(context(world), Stage.TRIAGE)
    assert {value.metric for value in found} == {"triage-accuracy", "false-refutes", "manual-queue",
                                                 "manual-queue-max-wait"}


def test_snapshots_are_overwritten_and_trends_are_read_back(tmp_path):
    world = make_learn_world(tmp_path)
    for offset, value in ((2, 3), (1, 5), (0, 7)):
        week = WEEK - timedelta(days=7 * offset)
        metrics.save_snapshots(world.conn, week, [metrics.MetricValue.count("fixed-issues", "all", value)], NOW)
    metrics.save_snapshots(world.conn, WEEK, [metrics.MetricValue.count("fixed-issues", "all", 8)], NOW)
    assert [item.value for item in metrics.trend(world.conn, "fixed-issues", "all", WEEK, 2)] == [5, 8]


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.RUNNING])
def test_coverage_ignores_runs_that_did_not_finish(tmp_path, status):
    world = make_learn_world(tmp_path)
    save_run(world, "R-20261005-010000-collect-api-fuzz", probe=Probe.API_FUZZ, status=status,
             coverage=Coverage((Endpoint("GET", "/a", None),), endpoints_total=1))
    assert values(metrics.api_coverage(context(world)), "api-coverage") == {"all": (None, 0, 0, 0)}
