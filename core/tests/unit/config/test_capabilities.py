import pytest

from tightrein.config.capabilities import Capabilities, CapabilityError, ModelChoice, ModelPrice

DATA = {
    "deep": {
        "claude": {"model": "claude-opus", "inputUsdPerMTok": 15, "outputUsdPerMTok": 75},
        "codex": {"model": "gpt-5", "inputUsdPerMTok": 1.25, "outputUsdPerMTok": 10},
    },
    "search": {"claude": {"model": "claude-sonnet", "inputUsdPerMTok": 3, "outputUsdPerMTok": 15}},
}


@pytest.fixture
def capabilities():
    return Capabilities(DATA)


def test_capability_maps_to_the_model_of_each_tool(capabilities):
    assert capabilities.model("deep", "claude") == "claude-opus"
    assert capabilities.model("deep", "codex") == "gpt-5"


def test_unmapped_capability_reports_the_full_key(capabilities):
    with pytest.raises(CapabilityError) as caught:
        capabilities.model("search", "agy")
    assert caught.value.key == "capabilities.search.agy"
    assert str(caught.value).startswith("capabilities.search.agy: ")


def test_resolve_prefers_an_explicit_model(capabilities):
    assert capabilities.resolve("claude", "claude-haiku", "deep") == ModelChoice("claude", "claude-haiku")
    assert capabilities.resolve("claude", capability="deep") == ModelChoice("claude", "claude-opus", "deep")
    assert capabilities.resolve("agy") == ModelChoice("agy", None)


def test_choose_uses_the_stage_setting(capabilities):
    setting = {"tool": "claude", "capability": "deep"}
    assert capabilities.choose(setting, "stages.triage") == ModelChoice("claude", "claude-opus", "deep")
    pinned = {"tool": "claude", "model": "claude-haiku", "capability": "deep"}
    assert capabilities.choose(pinned, "stages.triage") == ModelChoice("claude", "claude-haiku")
    assert capabilities.choose({"tool": "agy", "model": None}, "stages.triage.refuter") == ModelChoice("agy", None)


def test_explicit_overrides_come_before_the_stage_setting(capabilities):
    setting = {"tool": "claude", "model": "claude-haiku", "capability": "search"}
    assert capabilities.choose(setting, "stages.fix", model="claude-opus") == ModelChoice("claude", "claude-opus")
    assert capabilities.choose(setting, "stages.fix", capability="deep") == ModelChoice("claude", "claude-opus", "deep")


def test_a_different_tool_does_not_inherit_the_model_of_the_configured_tool(capabilities):
    setting = {"tool": "claude", "model": "claude-haiku", "capability": "deep"}
    assert capabilities.choose(setting, "stages.triage", tool="codex") == ModelChoice("codex", "gpt-5", "deep")
    assert capabilities.choose({"tool": "claude", "model": "claude-haiku"}, "stages.triage", tool="codex") == (
        ModelChoice("codex", None)
    )


def test_choose_without_a_tool_reports_the_key(capabilities):
    with pytest.raises(CapabilityError) as caught:
        capabilities.choose({}, "stages.learn")
    assert caught.value.key == "stages.learn.tool"


def test_prices_and_cost_estimate(capabilities):
    assert capabilities.price("claude", "claude-opus") == ModelPrice(15.0, 75.0)
    assert capabilities.estimate_cost("claude", "claude-opus", 200_000, 10_000) == pytest.approx(3.75)
    assert capabilities.estimate_cost("claude", "unknown-model", 1, 1) is None
    assert capabilities.estimate_cost("claude", None, 1, 1) is None


def test_conflicting_prices_for_the_same_model_are_rejected():
    data = {
        "deep": {"claude": {"model": "claude-opus", "inputUsdPerMTok": 15, "outputUsdPerMTok": 75}},
        "review": {"claude": {"model": "claude-opus", "inputUsdPerMTok": 5, "outputUsdPerMTok": 25}},
    }
    with pytest.raises(CapabilityError) as caught:
        Capabilities(data)
    assert caught.value.key == "capabilities.review.claude"


def test_empty_capabilities():
    capabilities = Capabilities(None)
    assert capabilities.resolve("claude") == ModelChoice("claude", None)
