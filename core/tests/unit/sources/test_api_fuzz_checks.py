import pytest

from tightrein.config.project import ConfigError
from tightrein.domain.enums import ProbeLevel
from tightrein.sources.api_fuzz import checks, invoke, limits
from tightrein.sources.base import ProbeOptions


def test_default_tiers_and_the_auto_response_schema(make_config):
    exported = checks.plan(make_config(), "core/openapi-url")
    assert exported.enabled == frozenset({"serverError", "authorization", "statusCode", "responseSchema"})
    assert exported.excluded == ("unsupported_method", "allow_header_conformance")
    handwritten = checks.plan(make_config(), "core/openapi-file")
    assert "response_schema_conformance" in handwritten.excluded and not handwritten.response_time
    custom = checks.plan(make_config(sources={"api-fuzz": {"checks": {"authorization": False, "responseTime": True}}}),
                         None)
    assert not custom.authorization and custom.response_time and "ignored_auth" in custom.excluded


def test_parameters_pass_excluded_checks_and_only_slow_responses_when_enabled(tmp_path, make_config):
    config = make_config(thresholds={"slowResponseSeconds": {"value": 3, "min": 1, "max": 30}})
    plan = checks.plan(config, "core/openapi-file")
    params = invoke.parameters(ProbeLevel.SHALLOW, config, ProbeOptions(), lambda: 1, plan)
    assert params.max_response_time is None
    argv = invoke.build_argv(params, tmp_path / "st", tmp_path / "spec.json", tmp_path / "c.toml", "http://h", tmp_path)
    excluded = argv[argv.index("--exclude-checks") + 1].split(",")
    assert "response_schema_conformance" in excluded and "not_a_server_error" not in excluded
    slow = checks.plan(make_config(sources={"api-fuzz": {"checks": {"responseTime": True}}},
                                   thresholds={"slowResponseSeconds": {"value": 3, "min": 1, "max": 30}}), None)
    assert invoke.parameters(ProbeLevel.SHALLOW, config, ProbeOptions(), lambda: 1, slow).max_response_time == 3


def production(make_config, **api_fuzz):
    return make_config(target={"baseUrl": "https://prod.example.test", "environment": "production"},
                       sources={"api-fuzz": api_fuzz})


def test_production_needs_get_only_and_an_allow_list(make_config):
    assert limits.check(make_config()) == ()
    with pytest.raises(ConfigError) as raised:
        limits.check(production(make_config))
    keys = [issue.key for issue in raised.value.issues]
    assert keys == ["sources.api-fuzz.levels.deep.includeMethod", "sources.api-fuzz.production.allow"]
    allowed = production(make_config, production={"allow": ["/api/health", "/api/items"]},
                         levels={"deep": {"includeMethod": "GET"}})
    assert limits.check(allowed) == ("/api/health", "/api/items")


def test_production_runs_only_the_allowed_routes_with_get(make_config):
    config = production(make_config, production={"allow": ["/api/items"]}, levels={"deep": {"includeMethod": "GET"}})
    params = invoke.parameters(ProbeLevel.DEEP, config, ProbeOptions(include_methods=("POST",)), lambda: 1,
                               allowed=limits.check(config))
    assert params.include_methods == ("GET",) and params.include_paths == ("/api/items",)
    narrowed = invoke.parameters(ProbeLevel.DEEP, config, ProbeOptions(include_paths=("/api/items", "/api/admin")),
                                 lambda: 1, allowed=("/api/items",))
    assert narrowed.include_paths == ("/api/items",)
