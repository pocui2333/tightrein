from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tightrein.assess.select import ASSESS
from tightrein.collect.dedup.changes import ChangeSet
from tightrein.collect.dedup.status import (
    CLEAN_RUNS,
    FALSE_POSITIVE_CHECK,
    IGNORE,
    RESOLVED_COMMIT,
    ROLES,
    SUB_SOURCE,
    CommitFacts,
    Coverage,
    Event,
    IgnoreCondition,
    Params,
    ProblemStatus,
    TransitionRejected,
    count_clean_run,
    escalated,
    false_positive_outcomes,
    ignore_expired,
    is_covered,
    is_regression,
    query_facts,
    resolution_ready,
    transition,
)
from tightrein.store.tables.problems import Problem

T = datetime(2026, 9, 29, 2, 15, tzinfo=UTC)
# c1 早于 c2，c2 早于 c3；c2 与 s2 是兄弟分支
FACTS = CommitFacts({("c1", "c2"): True, ("c2", "c3"): True, ("c1", "c3"): True, ("c2", "c1"): False,
                     ("s2", "c3"): False, ("c2", "s2"): False})


def problem(source="collect.api_fuzz", location="POST /api/Order/Query", status=ProblemStatus.NEW, **extra):
    return Problem(id="P-0001", fingerprint="f" * 16, source=source, check_type="server_error", status=status.value,
                   title="t", first_seen=T, last_seen=T, location=location, count=extra.pop("count", 1),
                   last_commit=extra.pop("last_commit", "c1"), extra=dict(extra))


API = Coverage.from_items(["POST /api/Order/Query#Company"], "c2")


def test_commit_facts_require_ancestor_and_distinct():
    assert FACTS.is_newer("c2", "c1") is True
    assert FACTS.is_newer("c1", "c1") is False
    assert FACTS.is_newer("c1", "c2") is False
    assert FACTS.is_newer("s2", "c2") is False
    assert FACTS.is_newer("zz", "c1") is None
    assert FACTS.is_newer(None, "c1") is None


def test_commit_pairs_are_queried_once_and_unknown_ones_are_left_out():
    asked = []

    def is_ancestor(commit, of):
        asked.append((commit, of))
        return None if "zz" in (commit, of) else True

    facts = query_facts([("c1", "c2"), ("c1", "c2"), ("c1", "c1"), ("zz", "c2")], is_ancestor)
    assert asked == [("c1", "c2"), ("zz", "c2")]
    assert facts.is_newer("c2", "c1") is True and facts.is_newer("c2", "zz") is None


def test_api_fuzz_covered_needs_endpoint_and_every_role():
    assert is_covered(problem(**{ROLES: ["Company"]}), "collect.api_fuzz", API)
    assert not is_covered(problem(**{ROLES: ["Company", "Personal"]}), "collect.api_fuzz", API)
    assert is_covered(problem(**{ROLES: []}), "collect.api_fuzz", API)
    all_roles = Coverage.from_items(["POST /api/Order/Query"], "c2")
    assert is_covered(problem(**{ROLES: ["Company", "Personal"]}), "collect.api_fuzz", all_roles)


def test_other_source_run_does_not_cover():
    assert not is_covered(problem(), "collect.static", API)


@pytest.mark.parametrize("source", ["collect.platform_errors", "collect.alerts", "collect.access_log",
                                    "collect.project_probes"])
def test_sub_source_problems_are_covered_when_that_source_was_read(source):
    found = problem(source=source, location=None, **{SUB_SOURCE: "sentry"})
    assert is_covered(found, source, Coverage.from_items(["sentry"], "c2"))
    assert not is_covered(found, source, Coverage.from_items(["loki"], "c2"))
    assert not is_covered(problem(source=source, location=None), source, Coverage.from_items(["sentry"], "c2"))


def test_static_covered_by_file_and_incidental_never():
    static = problem(source="collect.static", location="svc/a.py")
    assert is_covered(static, "collect.static", Coverage.from_items(["svc/a.py"], "c2"))
    assert not is_covered(static, "collect.static", Coverage.from_items(["svc/b.py"], "c2"))
    incidental = problem(source="collect.incidental", location="svc/a.py")
    assert not is_covered(incidental, "collect.incidental", Coverage.from_items([], "c2"))


def test_count_clean_run_only_on_newer_commit():
    found = problem()
    assert count_clean_run(found, Coverage(commit="c2"), FACTS) and found.extra[CLEAN_RUNS] == 1
    assert not count_clean_run(found, Coverage(commit="c1"), FACTS)
    assert not count_clean_run(found, Coverage(commit="zz"), FACTS)
    assert found.extra[CLEAN_RUNS] == 1


@pytest.mark.parametrize("clean,required,expected", [(2, 3, False), (3, 3, True), (1, 1, True)])
def test_resolution_needs_the_configured_covered_runs(clean, required, expected):
    assert resolution_ready(problem(**{CLEAN_RUNS: clean}), Coverage(commit="c2"), FACTS, required) is expected


def test_resolution_requires_newer_commit_and_resolvable_status():
    assert resolution_ready(problem(**{CLEAN_RUNS: 5}), Coverage(commit="c1"), FACTS, 3) is False
    assert resolution_ready(problem(status=ProblemStatus.PENDING, **{CLEAN_RUNS: 5}), Coverage(commit="c2"), FACTS,
                            3) is False
    assert resolution_ready(problem(status=ProblemStatus.ONGOING, **{CLEAN_RUNS: 5}), Coverage(commit="c2"), FACTS,
                            3) is True
    assert resolution_ready(problem(**{CLEAN_RUNS: 5}), Coverage(commit="zz"), FACTS, 3) is None


def test_regression_only_on_strictly_newer_release():
    resolved = problem(status=ProblemStatus.RESOLVED, **{RESOLVED_COMMIT: "c2"})
    assert is_regression(resolved, "c3", FACTS) is True
    assert is_regression(resolved, "c1", FACTS) is False
    assert is_regression(resolved, "c2", FACTS) is False
    assert is_regression(resolved, "zz", FACTS) is None
    assert is_regression(problem(), "c3", FACTS) is False


def muted(condition: IgnoreCondition, **kwargs):
    return problem(status=ProblemStatus.MUTED, **{IGNORE: condition.to_json()}, **kwargs)


def test_ignore_expires_on_date():
    condition = IgnoreCondition(until=T + timedelta(days=7))
    assert ignore_expired(muted(condition), T + timedelta(days=7), FACTS) is True
    assert ignore_expired(muted(condition), T + timedelta(days=6), FACTS) is False


def test_ignore_expires_after_n_more_occurrences():
    condition = IgnoreCondition(occurrences=3, baseline_occurrences=4)
    assert ignore_expired(muted(condition, count=7), T, FACTS) is True
    assert ignore_expired(muted(condition, count=6), T, FACTS) is False


def test_ignore_expires_on_new_release():
    condition = IgnoreCondition(new_release=True, baseline_commit="c1")
    assert ignore_expired(muted(condition, last_commit="c2"), T, FACTS) is True
    assert ignore_expired(muted(condition, last_commit="c1"), T, FACTS) is False
    assert ignore_expired(muted(condition, last_commit="zz"), T, FACTS) is None


def test_ignore_on_severity_escalation_is_given_by_the_caller():
    condition = IgnoreCondition(severity_escalated=True)
    assert ignore_expired(muted(condition), T, FACTS) is False
    assert ignore_expired(muted(condition), T, FACTS, escalated=True) is True


def test_escalation_compares_signal_hints_with_the_assessed_severity():
    hinted = [SimpleNamespace(severity_hint=None), SimpleNamespace(severity_hint="P1")]
    assert escalated(problem(severity="P2"), hinted)
    assert not escalated(problem(severity="P1"), hinted)
    assert not escalated(problem(), hinted)  # 没评估过严重度


def test_permanent_ignore_never_expires():
    assert IgnoreCondition().permanent
    assert ignore_expired(muted(IgnoreCondition()), T + timedelta(days=999), FACTS) is False
    assert ignore_expired(problem(), T, FACTS) is False


def test_transitions_outside_the_table_are_rejected():
    assert transition(problem(status=ProblemStatus.PENDING), Event.CONFIRMED) == "new"
    assert transition(problem(status=ProblemStatus.RESOLVED), Event.SEEN_AGAIN) == "resolved"
    with pytest.raises(TransitionRejected):
        transition(problem(status=ProblemStatus.NEW), Event.CONFIRMED)
    with pytest.raises(TransitionRejected):
        transition(problem(status=ProblemStatus.NEW), Event.REGRESSED)


def test_a_new_assessment_restarts_the_false_positive_count():
    found = problem(source="collect.platform_errors", location=None, status=ProblemStatus.CLOSED, verdict="refuted",
                    **{SUB_SOURCE: "sentry", ASSESS: {"attempt": 2, "verdict": "refuted"},
                       FALSE_POSITIVE_CHECK: {"attempt": 1, "cleanRuns": 5, "seen": True}})
    changeset = ChangeSet(run="r", now=T, next_number=2)
    params = Params(covered_runs={"*": 3}, watch_window=timedelta(hours=24), watch_occurrences=2,
                    false_positive_clean_runs=2)
    covered = {"collect.platform_errors": Coverage.from_items(["sentry"], "c2")}
    assert false_positive_outcomes(changeset, [found], [], covered, params) == []
    assert found.extra[FALSE_POSITIVE_CHECK] == {"attempt": 2, "cleanRuns": 1, "seen": False}
    assert false_positive_outcomes(changeset, [found], [], {}, params) == []  # 本次没覆盖到：不累计
    assert found.extra[FALSE_POSITIVE_CHECK]["cleanRuns"] == 1
    assert false_positive_outcomes(changeset, [found], [], covered, params) == ["P-0001"]
    assert found.extra[ASSESS]["outcome"] == "correct" and found.id in changeset.touched
