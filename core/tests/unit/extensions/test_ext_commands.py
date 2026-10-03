import json
from datetime import timedelta
from pathlib import Path

import pytest
from extension_world import COMMAND, METHOD, PYTHON, STACK

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionMode, ExtensionPoint, Probe
from tightrein.extensions.commands import (
    FixtureCase,
    default_input,
    differences,
    discover_fixtures,
    list_points,
    run_fixtures,
    run_point,
    run_stack_fixtures,
    substitute,
)
from tightrein.extensions.invoke import Invoker
from tightrein.extensions.resolve import project_implementations, resolve

RUN = "R-20260929-021503-loop"
COMMIT = "d6f37025c3b1e0a9f8e7d6c5b4a39281706f5e4d"
DOCUMENT = {"openapi": "3.0.1", "paths": {}}
ROUTE = {"path": "/orders", "name": None, "componentFile": None, "meta": {"file": "{fixture}/files/a.vue"}}
ROUTES = {"routes": [ROUTE], "sourceFiles": ["src/router/index.js"]}


def resolution_for(world, extensions, **changes):
    world.project_extension()
    config = world.config(extensions=extensions, **changes)
    return config, resolve(config, world.workspace, world.tool, python=PYTHON)


def invoker(world):
    return Invoker(world.workspace, world.user, run_id=RUN, environ={"PATH": "/usr/bin:/bin"}, scratch_root=world.root)


def write_case(directory: Path, request: dict, expected: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.json").write_text(json.dumps(request), encoding="utf-8")
    (directory / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return directory


def fixture_request(point, options):
    uses_repo = point not in ("error-tracking", "log-platform", "log-parse", "alert-source")
    return {"protocol": 1, "point": point, "workspace": "{fixture}", "repo": "{fixture}/files" if uses_repo else None,
            "commit": "d6f37025" if uses_repo else None, "options": options, "scratchDir": "/tmp/ignored",
            "base": None, "input": {}}


def test_list_points_shows_layer_method_command_options_and_timeout(world):
    world.stack({"log-parse": {"options": {"timestampFormat": "HH:mm:ss"}}})
    _, resolution = resolution_for(world, {
        "log-parse": {"command": COMMAND, "mode": "extend", "timeoutSeconds": 60},
        "log-platform": {"use": "core/loki", "options": {"url": "https://logs.example.test"}},
    }, stacks=[STACK])
    listings = {listing.point: listing for listing in list_points(resolution)}
    assert list(listings) == list(ExtensionPoint)
    parse = listings[ExtensionPoint.LOG_PARSE]
    assert (parse.implementation, parse.method, parse.command, parse.options, parse.timeout_seconds, parse.mode,
            parse.base) == (ExtensionLayer.PROJECT, None, tuple(COMMAND), {}, 60, ExtensionMode.EXTEND,
                            ExtensionLayer.STACK)
    source = listings[ExtensionPoint.LOG_PLATFORM]
    assert (source.implementation, source.method, source.options["url"], source.timeout_seconds) == (
        ExtensionLayer.CORE, "core/loki", "https://logs.example.test", 120)
    routes = listings[ExtensionPoint.PAGE_ROUTES]
    assert (routes.implementation, routes.command, routes.timeout_seconds, routes.base) == (
        ExtensionLayer.DEFAULT, (), 120, None)


def test_default_inputs_come_from_the_workspace(world):
    config = world.config(sources={"platform-errors": {"logQuery": '{app="api"} |= "ERROR"'}},
                          localRun={"ports": {"api": 5100}})
    layout = world.workspace
    assert default_input(ExtensionPoint.SPEC_EXPORT, config, layout, RUN) == {
        "outputFile": str(layout.extension_trial_openapi(RUN).absolute())}
    assert default_input(ExtensionPoint.AUTHZ_ROLES, config, layout, RUN) == {"roles": ["Company"]}
    tracking = default_input(ExtensionPoint.ERROR_TRACKING, config, layout, RUN)
    assert set(tracking) == {"since", "until"}
    assert parse_iso(tracking["until"]) - parse_iso(tracking["since"]) == timedelta(hours=24)
    logs = default_input(ExtensionPoint.LOG_PLATFORM, config, layout, RUN)
    assert (logs["query"], logs["limit"], logs["since"]) == ('{app="api"} |= "ERROR"', 200, tracking["since"])
    assert default_input(ExtensionPoint.ALERT_SOURCE, config, layout, RUN) == {}
    with pytest.raises(ValueError, match="logQuery"):
        default_input(ExtensionPoint.LOG_PLATFORM, world.config(), layout, RUN)
    assert default_input(ExtensionPoint.STATIC_TOOLS, config, layout, RUN) == {
        "level": "full", "baseCommit": None, "changedFiles": [],
        "rawDir": str(layout.probe_raw_dir(RUN, Probe.STATIC).absolute())}
    assert default_input(ExtensionPoint.LOCAL_RUN, config, layout, RUN) == {
        "mode": "api", "ports": {"backend": 5100, "frontend": None}}
    assert default_input(ExtensionPoint.PAGE_ROUTES, config, layout, RUN) == {}
    with pytest.raises(ValueError):
        default_input(ExtensionPoint.LOG_PARSE, config, layout, RUN)


def test_run_point_calls_once_without_touching_the_cache(world):
    options = {"behavior": "spec", "document": DOCUMENT}
    config, resolution = resolution_for(world, {"spec-export": {"command": COMMAND, "options": options}})
    input = default_input(ExtensionPoint.SPEC_EXPORT, config, world.workspace, RUN)
    result = run_point(invoker(world), resolution, ExtensionPoint.SPEC_EXPORT, input, repo=world.root, commit=COMMIT)
    assert result.output["specFile"] == input["outputFile"]
    assert world.workspace.extension_trial_openapi(RUN).is_file()
    assert not world.workspace.openapi(COMMIT).exists()
    assert not world.workspace.extension_meta(COMMIT, ExtensionPoint.SPEC_EXPORT).exists()
    with pytest.raises(ValueError):
        run_point(invoker(world), resolution, ExtensionPoint.SPEC_EXPORT, input, repo=None, commit=None)


def test_run_point_applies_the_checks_beyond_the_schema(world):
    options = {"behavior": "spec", "document": {"openapi": "3.0.1"}}
    config, resolution = resolution_for(world, {"spec-export": {"command": COMMAND, "options": options}})
    input = default_input(ExtensionPoint.SPEC_EXPORT, config, world.workspace, RUN)
    result = run_point(invoker(world), resolution, ExtensionPoint.SPEC_EXPORT, input, repo=world.root, commit=COMMIT)
    assert result.failure.code is ExtensionErrorCode.SCHEMA_INVALID


def test_fixtures_are_compared_with_the_expected_response(world):
    world.stack({"page-routes": {}, "log-parse": {}})
    fixtures = world.tool.stack_fixtures_dir(STACK)
    passing = write_case(fixtures / "page-routes" / METHOD / "basic",
                         fixture_request("page-routes", {"output": ROUTES}),
                         {"protocol": 1, "status": "ok", "output": ROUTES})
    wrong = dict(ROUTES, sourceFiles=["src/router/routes.js"])
    write_case(fixtures / "page-routes" / METHOD / "changed", fixture_request("page-routes", {"output": ROUTES}),
               {"protocol": 1, "status": "ok", "output": wrong})
    write_case(fixtures / "log-parse" / METHOD / "broken", fixture_request("log-parse", {"behavior": "garbage"}),
               {"protocol": 1, "status": "ok", "output": {}})
    write_case(fixtures / "page-routes" / "retired" / "orphan", fixture_request("page-routes", {"output": ROUTES}),
               {"protocol": 1, "status": "ok", "output": ROUTES})
    outcomes = run_stack_fixtures(world.tool, STACK, world.user, python=PYTHON, environ={"PATH": "/usr/bin:/bin"},
                                  scratch_root=world.root)
    by_name = {outcome.case.name: outcome for outcome in outcomes}
    assert [outcome.case.name for outcome in outcomes] == ["broken", "basic", "changed", "orphan"]
    assert by_name["basic"].passed and by_name["basic"].differences == ()
    assert by_name["basic"].case == FixtureCase(ExtensionPoint.PAGE_ROUTES, "basic", passing, METHOD)
    assert by_name["orphan"].differences == ("技术栈 webstack 的 page-routes 没有方法 retired",)
    assert by_name["changed"].differences == (
        '$.output.sourceFiles[0]: 期望 "src/router/routes.js"，实际 "src/router/index.js"',)
    assert not by_name["broken"].passed
    assert by_name["broken"].differences[0] == "退出码 0，标准输出不是单个 JSON 对象"


def test_fixture_schema_errors_and_missing_implementations_fail(world):
    world.project_extension()
    fixtures = world.workspace.extension_fixtures_dir()
    invalid = {"routes": [], "sourceFiles": [], "extra": 1}
    write_case(fixtures / "page-routes" / "extra-field", fixture_request("page-routes", {"output": invalid}),
               {"protocol": 1, "status": "ok", "output": invalid})
    write_case(fixtures / "authz-roles" / "no-extension", fixture_request("authz-roles", {"output": {}}),
               {"protocol": 1, "status": "ok", "output": {}})
    config = world.config(extensions={"page-routes": {"command": COMMAND}})
    outcomes = run_fixtures(project_implementations(config, world.workspace, python=PYTHON), fixtures, world.user,
                            environ={"PATH": "/usr/bin:/bin"}, scratch_root=world.root)
    assert [(outcome.case.name, outcome.passed) for outcome in outcomes] == [
        ("no-extension", False), ("extra-field", False)]
    assert outcomes[0].differences == ("没有 authz-roles 的实现",)
    assert outcomes[1].differences == ("$.output: Additional properties are not allowed ('extra' was unexpected)",)


def test_fixture_directories_must_be_extension_points(tmp_path):
    (tmp_path / "swagger" / "basic").mkdir(parents=True)
    with pytest.raises(ValueError):
        discover_fixtures(tmp_path)
    assert discover_fixtures(tmp_path / "missing") == []


def test_substitute_and_differences():
    fixture = Path("/fixtures/page-routes/basic")
    assert substitute({"repo": "{fixture}/files", "list": ["{fixture}", "x{fixture}"], "n": 1}, fixture) == {
        "repo": "/fixtures/page-routes/basic/files", "list": ["/fixtures/page-routes/basic", "x{fixture}"], "n": 1}
    assert differences({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []
    assert differences({"a": 1, "c": 2}, {"a": True, "d": 3}) == [
        "$.a: 期望 1，实际 true", "$.c: 期望存在，实际缺少", "$.d: 期望没有，实际为 3"]
    assert differences([1], [1, 2]) == ["$: 期望 [1]，实际 [1, 2]"]
