import pytest
from pipeline_world import make_signal, make_world
from store_problem import save_problem

from tightrein.domain.enums import CheckResult, ProblemStatus, RegressionKind, RegressionResult, RunStatus
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.pipeline.verify.steps import regression, scope, verdict
from tightrein.pipeline.verify.steps.verdict import Item
from tightrein.sources.base import ProbeOutcome, ProbeTarget
from tightrein.pipeline.checks.regressions.manifest import CheckEntry
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.store.repos.regressions import RegressionCheck


def outcome(result, met=True):
    return RegressionOutcome(RegressionCheck("0007", "api-1", RegressionKind.API, "p", "h"), result, "GET /a", "",
                             met)


@pytest.mark.parametrize("result,met,expected", [
    (RegressionResult.PASSED, True, CheckResult.PASS), (RegressionResult.PASSED, False, CheckResult.WEAK),
    (RegressionResult.FAILED, True, CheckResult.FAIL), (RegressionResult.NOT_RUN, True, CheckResult.UNVERIFIED),
    (RegressionResult.INVALID, True, CheckResult.UNVERIFIED),
])
def test_single_items_after_the_fix(result, met, expected):
    assert verdict.from_outcome(outcome(result, met)) is expected


def items(*pairs):
    return [Item(f"item-{number}", category, result) for number, (category, result) in enumerate(pairs)]


@pytest.mark.parametrize("found,awaiting,expected", [
    (items(("issue-repro", CheckResult.PASS), ("other-repro", CheckResult.PASS)), False, "passed"),
    (items(("issue-repro", CheckResult.PASS), ("api-shallow", CheckResult.FAIL)), False, "failed"),
    (items(("issue-repro", CheckResult.WEAK)), False, "passed"),
    (items(("issue-repro", CheckResult.UNVERIFIED)), False, "passed"),
    (items(("issue-repro", CheckResult.PASS), ("screenshot", CheckResult.UNVERIFIED)), True, "awaiting-user"),
    (items(("issue-repro", CheckResult.PASS), ("page-patrol", CheckResult.FAIL)), True, "failed"),
    (items(("issue-repro", CheckResult.PASS), ("page-patrol", CheckResult.UNVERIFIED)), False, "passed"),
])
def test_local_conclusions(found, awaiting, expected):
    assert verdict.local_conclusion(found, awaiting) == expected


def test_scope_adds_endpoints_and_pages_from_the_changed_files():
    fix = {"affectedEndpoints": ["GET /api/orders/{id}"], "affectedPages": []}
    endpoints = {"endpoints": [{"method": "POST", "route": "/api/orders", "sourceFile": "src/orders.src"},
                               {"method": "GET", "route": "/api/users", "sourceFile": "src/users.src"}]}
    routes = {"routes": [{"path": "/orders", "componentFile": "web/orders.vue"}]}
    found = scope.compute(fix, ["src/orders.src", "web/orders.vue"], endpoints, routes)
    assert found.endpoints == ("GET /api/orders/{id}", "POST /api/orders") and found.pages == ("/orders",)
    assert scope.compute(fix, ["src/orders.src"], None, None).endpoints == ("GET /api/orders/{id}",)
    api = CheckEntry("api-1", RegressionKind.API, "api-1.request.json", "GET /a")
    page = CheckEntry("page-1", RegressionKind.PAGE, "page-1.spec.ts", "/orders")
    assert scope.mode([api], scope.Scope()) == "api"
    assert scope.mode([api, page], scope.Scope()) == "page"
    assert scope.mode([api], found) == "page"


class SignalProbe:
    def __init__(self, *found):
        self.found = found

    def run(self, target, level, options):
        return ProbeOutcome(RunStatus.OK, self.found)


def test_signals_of_problems_that_existed_before_the_fix_do_not_fail(tmp_path):
    world = make_world(tmp_path)
    old = make_signal(1)
    new = make_signal(2, location="GET /api/Invoice/7")
    save_problem(world.conn, "P-0001", fingerprint(old, CURRENT_VERSION))
    save_problem(world.conn, "P-0002", fingerprint(new, CURRENT_VERSION), status=ProblemStatus.RESOLVED)
    target = ProbeTarget("local", "R-20261005-030000-verify", tmp_path, world.clock, base_url="http://localhost:5101")
    existing = regression.existing_problems(world.conn, world.layout, ())
    passed = regression.shallow(SignalProbe(old), target, ["GET /api/Order/{id}"], existing)
    assert passed.result is CheckResult.PASS and "P-0001" in passed.reason
    failed = regression.shallow(SignalProbe(old, new), target, ["GET /api/Order/{id}"], existing)
    assert failed.result is CheckResult.FAIL and "/api/Invoice/7" in failed.reason and "P-0001" in failed.reason
    own = regression.existing_problems(world.conn, world.layout, ("P-0001",))
    assert regression.shallow(SignalProbe(old), target, ["GET /api/Order/{id}"], own).result is CheckResult.FAIL
