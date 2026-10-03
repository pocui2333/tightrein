import json
import os
from datetime import datetime, timezone

import pytest
from extension_world import PYTHON

from tightrein.config.project import ConfigError
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions.cache import ExtensionCache
from tightrein.extensions.catalog import ToolRequirement
from tightrein.extensions.client import ExtensionClient
from tightrein.extensions.commands import list_methods
from tightrein.extensions.invoke import Invoker
from tightrein.extensions.resolve import resolve

RUN = "R-20261005-030000-collect-platform-errors"
COMMIT = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
ROUTES_YAML = "routes:\n  - {path: /orders, name: orders}\n"


def test_methods_are_listed_per_point_with_stack_defaults(world):
    world.stack({"log-parse": {"summary": "解析某种控制台日志", "options": {"timezone": "UTC"}}})
    world.method("log-parse", "strict")
    listings = list_methods(world.tool, ExtensionPoint.LOG_PARSE)
    assert [(item.method, item.layer, item.default_of) for item in listings] == [
        ("core/json-lines", ExtensionLayer.CORE, None), ("core/regex", ExtensionLayer.CORE, None),
        ("webstack/fake", ExtensionLayer.STACK, "webstack"), ("webstack/strict", ExtensionLayer.STACK, None)]
    regex = listings[1]
    assert regex.options["multiline"] is True and "pattern" in regex.options_schema["required"]
    assert (listings[2].summary, listings[2].options) == ("解析某种控制台日志", {"timezone": "UTC"})
    everything = list_methods(world.tool)
    assert [item.point for item in everything] == sorted((item.point for item in everything),
                                                          key=list(ExtensionPoint).index)
    semgrep = next(item for item in everything if item.method == "core/semgrep")
    install = "core/.venv/bin/python -m venv local/semgrep && local/semgrep/bin/pip install semgrep"
    assert semgrep.tools == (ToolRequirement("semgrep", None, install),)
    world.stack({}, name="draft", manifest_name="other")
    with pytest.raises(ConfigError):
        list_methods(world.tool)


def client_for(world, extensions):
    world.workspace.root.mkdir(parents=True, exist_ok=True)
    config = world.config(extensions=extensions)
    resolution = resolve(config, world.workspace, world.tool, python=PYTHON)
    invoker = Invoker(world.workspace, world.user, run_id=RUN, environ={"PATH": os.environ["PATH"]},
                      scratch_root=world.root)
    return ExtensionClient(config, resolution, invoker, ExtensionCache(world.workspace, FixedClock(NOW)),
                           head=lambda repo: COMMIT)


@pytest.mark.slow
def test_core_methods_run_as_extension_processes(world):
    line = json.dumps({"timestamp": "2026-10-05T02:59:00Z", "level": "error", "message": "查询失败"},
                      ensure_ascii=False)
    chunk = {"stream": '{app="api"}', "text": line + "\n", "startPosition": 0,
             "endPosition": len((line + "\n").encode("utf-8")), "modifiedAt": None}
    (world.workspace.root / "e2e").mkdir(parents=True)
    (world.workspace.root / "e2e" / "routes.yaml").write_text(ROUTES_YAML, encoding="utf-8")
    client = client_for(world, {
        "log-parse": {"use": "core/json-lines"},
        "page-routes": {"use": "core/manual-list", "options": {"file": "e2e/routes.yaml"}},
    })
    parsed = client.log_parse([chunk], None)
    assert (parsed.implementation, parsed.failure) == (ExtensionLayer.CORE, None)
    assert [(entry["level"], entry["message"]) for entry in parsed.output["entries"]] == [("error", "查询失败")]
    routes = client.page_routes(world.root / "repo", COMMIT)
    assert [route["path"] for route in routes.output["routes"]] == ["/orders"]
    assert client.page_routes(world.root / "repo", COMMIT).cached
    meta = json.loads(world.workspace.extension_meta(COMMIT, ExtensionPoint.PAGE_ROUTES).read_text(encoding="utf-8"))
    assert (meta["implementation"], meta["method"]) == ("core", "core/manual-list")


@pytest.mark.slow
def test_method_errors_reach_the_caller(world):
    client = client_for(world, {
        "page-routes": {"use": "core/manual-list", "options": {"file": "e2e/missing.yaml"}},
        "spec-export": {"use": "core/openapi-file", "options": {"path": "docs/openapi.json"}},
    })
    (world.root / "repo").mkdir()
    failed = client.page_routes(world.root / "repo", COMMIT)
    assert (failed.failure.code, failed.failure.message) == (ExtensionErrorCode.INVALID_INPUT,
                                                             "工作区中没有 e2e/missing.yaml")
    missing = client.spec_export(world.root / "repo", COMMIT)
    assert (missing.implementation, missing.output, missing.failure) == (ExtensionLayer.DEFAULT, None, None)
    assert missing.notes[-1] == "核心方法报告不适用：仓库中没有接口描述文件 docs/openapi.json"
