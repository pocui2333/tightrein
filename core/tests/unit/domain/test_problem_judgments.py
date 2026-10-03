from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tightrein.domain.commit_facts import CommitFacts
from tightrein.domain.enums import Probe, ProblemStatus, RunStage, RunStatus
from tightrein.domain.problem import (
    IgnoreCondition,
    Problem,
    ProblemScope,
    count_clean_run,
    ignore_expired,
    is_covered,
    is_regression,
    resolution_ready,
)
from tightrein.domain.run import Coverage, Endpoint, Run

T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
# c1 早于 c2，c2 早于 c3
FACTS = CommitFacts.of([("c1", "c2", True), ("c2", "c3", True), ("c1", "c3", True), ("c2", "c1", False)])


def problem(probe=Probe.API_FUZZ, location="POST /api/Order/Query", roles=("Company",), status=ProblemStatus.NEW,
            **kwargs):
    fields = dict(
        id="P-0001", fingerprint="f" * 16, fingerprint_version=1, probe=probe, title="t", status=status,
        first_seen_at=T, last_seen_at=T, scope=ProblemScope(location, frozenset(roles)),
        first_seen_release="c1", last_seen_release="c1",
    )
    fields.update(kwargs)
    return Problem(**fields)


def run(probe=Probe.API_FUZZ, coverage=None, commit="c2"):
    return Run(id="R-20260930-021500-collect-x", stage=RunStage.COLLECT, started_at=T, status=RunStatus.OK,
               probe=probe, target_commit=commit, coverage=coverage or Coverage())


API_COVERAGE = Coverage(endpoints=(Endpoint("POST", "/api/Order/Query", "Company"),), methods="all")


def test_api_fuzz_covered_needs_endpoint_and_role():
    assert is_covered(problem(), run(coverage=API_COVERAGE))
    assert not is_covered(problem(roles=("Company", "Personal")), run(coverage=API_COVERAGE))
    assert not is_covered(problem(), run(coverage=replace(API_COVERAGE, methods="GET")))


def test_api_fuzz_problem_without_role_covered_by_any_role():
    assert is_covered(problem(roles=()), run(coverage=API_COVERAGE))


def test_other_probe_run_does_not_cover():
    assert not is_covered(problem(), run(probe=Probe.STATIC, coverage=API_COVERAGE))


@pytest.mark.parametrize("probe", [Probe.PLATFORM_ERRORS, Probe.ALERTS, Probe.ACCESS_LOG, Probe.PROJECT_PROBE])
def test_platform_and_probe_problems_covered_when_their_source_was_read(probe):
    found = replace(problem(probe=probe, location="OrderService.Query", roles=()),
                    scope=ProblemScope("OrderService.Query", source="error-tracking"))
    assert is_covered(found, run(probe=probe, coverage=Coverage(sources=("error-tracking",))))
    assert not is_covered(found, run(probe=probe, coverage=Coverage(sources=("log-platform",))))
    assert not is_covered(replace(found, scope=ProblemScope("OrderService.Query")),
                          run(probe=probe, coverage=Coverage(sources=("error-tracking",))))


def test_static_covered_by_file():
    static_problem = problem(probe=Probe.STATIC, location="Services/OrderService.cs:OrderService.Query", roles=())
    assert is_covered(static_problem, run(probe=Probe.STATIC, coverage=Coverage(files=("Services/OrderService.cs",))))
    assert not is_covered(static_problem, run(probe=Probe.STATIC, coverage=Coverage(files=("Services/A.cs",))))


def test_incidental_is_never_covered():
    incidental = problem(probe=Probe.INCIDENTAL, location="A.cs:A.B", roles=())
    assert not is_covered(incidental, run(probe=Probe.INCIDENTAL, coverage=Coverage(files=("A.cs",))))


def test_count_clean_run_only_on_newer_commit():
    assert count_clean_run(problem(), run(commit="c2"), FACTS).clean_covered_runs == 1
    assert count_clean_run(problem(), run(commit="c1"), FACTS).clean_covered_runs == 0
    assert count_clean_run(problem(), run(commit="zz"), FACTS).clean_covered_runs == 0


@pytest.mark.parametrize("clean,expected", [(2, False), (3, True)])
def test_resolution_needs_three_covered_runs(clean, expected):
    assert resolution_ready(problem(clean_covered_runs=clean), run(commit="c2"), FACTS) is expected


def test_resolution_threshold_is_a_parameter():
    assert resolution_ready(problem(clean_covered_runs=2), run(commit="c2"), FACTS, covered_runs=2) is True


def test_resolution_requires_newer_commit_and_resolvable_status():
    assert resolution_ready(problem(clean_covered_runs=5), run(commit="c1"), FACTS) is False
    assert resolution_ready(problem(clean_covered_runs=5, status=ProblemStatus.PENDING), run(), FACTS) is False
    assert resolution_ready(problem(clean_covered_runs=5, status=ProblemStatus.ONGOING), run(), FACTS) is True


def test_resolution_unknown_commit_relation():
    assert resolution_ready(problem(clean_covered_runs=5), run(commit="zz"), FACTS) is None


def test_static_resolves_after_one_covered_run():
    static_problem = problem(probe=Probe.STATIC, location="A.cs:A.B", roles=(), clean_covered_runs=1)
    assert resolution_ready(static_problem, run(probe=Probe.STATIC, commit="c2"), FACTS) is True


def test_regression_only_on_newer_release():
    resolved = problem(status=ProblemStatus.RESOLVED, resolved_release="c2")
    assert is_regression(resolved, "c3", FACTS) is True
    assert is_regression(resolved, "c1", FACTS) is False
    assert is_regression(resolved, "c2", FACTS) is False
    assert is_regression(resolved, "zz", FACTS) is None
    assert is_regression(problem(), "c3", FACTS) is False


def ignored(condition, **kwargs):
    return problem(status=ProblemStatus.IGNORED, ignore_until=condition, **kwargs)


def test_ignore_expires_on_date():
    condition = IgnoreCondition(until=T + timedelta(days=7))
    assert ignore_expired(ignored(condition), T + timedelta(days=7), FACTS) is True
    assert ignore_expired(ignored(condition), T + timedelta(days=6), FACTS) is False


def test_ignore_expires_after_n_more_occurrences():
    condition = IgnoreCondition(occurrences=3, baseline_occurrences=4)
    assert ignore_expired(ignored(condition, occurrences=7), T, FACTS) is True
    assert ignore_expired(ignored(condition, occurrences=6), T, FACTS) is False


def test_ignore_expires_on_new_release():
    condition = IgnoreCondition(new_release=True, baseline_release="c1")
    assert ignore_expired(ignored(condition, last_seen_release="c2"), T, FACTS) is True
    assert ignore_expired(ignored(condition, last_seen_release="c1"), T, FACTS) is False
    assert ignore_expired(ignored(condition, last_seen_release="zz"), T, FACTS) is None


def test_ignore_expires_on_severity_escalation_given_by_caller():
    condition = IgnoreCondition(severity_escalated=True)
    assert ignore_expired(ignored(condition), T, FACTS) is False
    assert ignore_expired(ignored(condition), T, FACTS, escalated=True) is True


def test_permanent_ignore_never_expires():
    assert ignore_expired(ignored(IgnoreCondition()), T + timedelta(days=999), FACTS) is False
    assert ignore_expired(problem(), T, FACTS) is False
