import pytest

from tightrein.collect.api_fuzz.limits import ProductionLimitViolated, Restriction, check


def test_outside_production_nothing_is_restricted():
    assert check("staging", ["get", "post"], []) == Restriction(("GET", "POST"), ())
    assert check(None, [], []) == Restriction((), ())


def test_production_needs_get_only_and_an_allow_list():
    with pytest.raises(ProductionLimitViolated) as raised:
        check("production", [], [])
    keys = [problem.split("：")[0] for problem in raised.value.problems]
    assert keys == ["controls.collect.api_fuzz.includeMethods", "sites.api_fuzz.allow"]
    with pytest.raises(ProductionLimitViolated):
        check("production", ["GET", "POST"], ["/api/items"])


def test_production_runs_only_the_allowed_routes_with_get():
    assert check("production", ["get"], ["/api/health", "/api/items"]) == Restriction(
        ("GET",), ("/api/health", "/api/items"))
