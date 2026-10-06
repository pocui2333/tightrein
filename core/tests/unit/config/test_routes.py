import pytest

from tightrein.config import routes as model_routes
from tightrein.config.routes import CALL_POINTS, ModelChoice, ModelPrice, RouteError, Routes, call_point

MODELS = {
    "opus": {"tool": "claude", "model": "claude-opus", "effort": "high", "inputUsdPerMTok": 15, "outputUsdPerMTok": 75},
    "fable": {"tool": "claude", "model": "fable"},
    "gpt": {"tool": "codex", "model": "gpt-5", "inputUsdPerMTok": 1.25, "outputUsdPerMTok": 10},
    "agy": {"tool": "agy"},
}
ROUTES = {"default": "opus", "fix.planner": "gpt", "fix.planner.high-risk": "fable", "fix.planner.large": "agy"}


@pytest.fixture
def table():
    return Routes(MODELS, ROUTES)


def test_call_points_follow_the_stage_and_role_names():
    assert call_point("fix", "fix-scout") == "fix.scout"
    assert call_point("triage", "dedup") == "triage.dedup"
    assert call_point("collect", "claim-verifier") == "collect.claim-verifier"
    with pytest.raises(KeyError):
        call_point("fix", "unknown")
    assert all(condition in model_routes.CONDITIONS for point in CALL_POINTS.values() for condition in point.conditions)


def test_lookup_order_is_conditions_then_point_then_default(table):
    assert table.resolve("fix.scout") == ModelChoice("claude", "claude-opus", "high", "opus")
    assert table.resolve("fix.planner") == ModelChoice("codex", "gpt-5", None, "gpt")
    assert table.resolve("fix.planner", ("high-risk", "large")).alias == "fable"
    assert table.resolve("fix.planner", ("large",)).alias == "agy"
    assert table.explain("fix.planner", ("large",)).key == "fix.planner.large"


def test_missing_route_names_the_call_point():
    with pytest.raises(RouteError) as caught:
        Routes(MODELS, {}).resolve("fix.executor")
    assert caught.value.key == "routes.fix.executor"
    assert "routes.default 或 routes.fix.executor" in str(caught.value)


def test_command_line_overrides(table):
    assert table.choose("fix.scout", model="claude-haiku") == ModelChoice("claude", "claude-haiku")
    assert table.choose("fix.scout", tool="claude") == ModelChoice("claude", "claude-opus", "high", "opus")
    assert table.choose("fix.scout", tool="codex") == ModelChoice("codex", None)
    assert table.choose("fix.scout", tool="codex", model="gpt-5-mini") == ModelChoice("codex", "gpt-5-mini")
    assert Routes(MODELS, {}).choose("fix.scout", tool="codex") == ModelChoice("codex", None)


def test_prices_and_cost_estimate(table):
    assert table.price("claude", "claude-opus") == ModelPrice(15.0, 75.0)
    assert table.estimate_cost("claude", "claude-opus", 200_000, 10_000) == pytest.approx(3.75)
    assert table.estimate_cost("claude", "fable", 1, 1) is None
    assert table.estimate_cost("claude", None, 1, 1) is None


def test_conflicting_prices_for_the_same_model_are_rejected():
    models = {**MODELS, "opus-mid": {**MODELS["opus"], "effort": "medium", "inputUsdPerMTok": 5}}
    with pytest.raises(RouteError) as caught:
        Routes(models)
    assert caught.value.key == "models.opus-mid"


def test_issues_report_unknown_keys_aliases_and_conditions():
    found = model_routes.issues([{"models": MODELS, "routes": {
        "default": "opus", "fix.unknown": "opus", "fix.scout.large": "opus", "triage.dedup": "missing"}}])
    keys = {issue.key: issue.reason for issue in found}
    assert "不认识的调用点" in keys["routes.fix.unknown"]
    assert "可用的条件：frontend" in keys["routes.fix.scout.large"]
    assert "别名 missing" in keys["routes.triage.dedup"]


def test_reviewers_must_differ_from_producers_including_condition_variants():
    same = model_routes.issues([{"models": MODELS, "routes": {"default": "opus"}}])
    assert [issue.key for issue in same] == ["routes.fix.review.deep", "routes.triage.refuter"]
    assert "routes.fix.review.deep" in same[0].reason
    variant = model_routes.issues([{"models": MODELS, "routes": {
        "default": "opus", "triage.refuter": "gpt", "fix.review.deep": "gpt", "fix.executor.high-risk": "gpt"}}])
    assert [issue.key for issue in variant] == ["routes.fix.review.deep"]
    assert "fix.executor.high-risk" in variant[0].reason


def test_legacy_keys_are_named_with_the_new_form():
    found = model_routes.legacy_issues({"defaultTool": "claude", "evaluation": {"judge": {}},
                                        "stages": {"fix": {"tool": "claude", "roles": {"fix-planner": {"model": "x"}},
                                                           "review": {"deep": {"capability": "strong"}}},
                                                   "triage": {"refuter": {"tool": "agy"}}}})
    assert [issue.key for issue in found] == ["defaultTool", "evaluation.judge", "stages.fix.tool",
                                              "stages.fix.roles.fix-planner.model", "stages.fix.review.deep.capability",
                                              "stages.triage.refuter"]
    assert all("models" in issue.reason and "routes" in issue.reason for issue in found)
