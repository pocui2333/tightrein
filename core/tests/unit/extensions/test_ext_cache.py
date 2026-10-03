import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from extension_world import PYTHON, SCRIPT, STACK

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint
from tightrein.extensions.cache import ExtensionCache, cache_key, file_hash, options_hash
from tightrein.extensions.resolve import Implementation, default

COMMIT = "d6f37025"
NOW = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
ROUTES = {"routes": [{"path": "/orders", "name": None, "componentFile": None, "meta": None}], "sourceFiles": []}


@pytest.fixture
def cache(world):
    return ExtensionCache(world.workspace, FixedClock(NOW))


def stack_routes(world, **changes):
    directory = world.stack({"page-routes": {"command": ["{python}", SCRIPT]}})
    implementation = Implementation(ExtensionPoint.PAGE_ROUTES, ExtensionLayer.STACK, 120,
                                    command=("{python}", SCRIPT), argv=(PYTHON, str(directory / SCRIPT)),
                                    cwd=directory, options={"routerFile": "src/router/index.js"}, cache_name=STACK,
                                    version="1.0.0", command_file=directory / SCRIPT)
    return replace(implementation, **changes)


def project_routes(world):
    directory = world.project_extension()
    return Implementation(ExtensionPoint.PAGE_ROUTES, ExtensionLayer.PROJECT, 120, command=("{python}", SCRIPT),
                          argv=(PYTHON, str(directory / SCRIPT)), cwd=directory, cache_name="demo",
                          command_file=directory / SCRIPT)


def test_written_output_is_read_back_with_its_key(world, cache):
    implementation = stack_routes(world)
    assert cache.read(COMMIT, implementation) is None
    cache.write(COMMIT, implementation, ROUTES)
    assert cache.read(COMMIT, implementation) == ROUTES
    meta = json.loads(world.workspace.extension_meta(COMMIT, ExtensionPoint.PAGE_ROUTES).read_text(encoding="utf-8"))
    assert meta == {
        "point": "page-routes", "implementation": "stack", "method": None, "command": ["{python}", SCRIPT],
        "optionsHash": options_hash({"routerFile": "src/router/index.js"}), "stackVersion": "1.0.0",
        "commandFileHash": None, "generatedAt": "2026-09-29T02:15:00Z",
    }


@pytest.mark.parametrize("changes", [
    {"options": {"routerFile": "src/router/routes.js"}},
    {"version": "1.1.0"},
    {"command": ("node", "page_routes.mjs")},
    {"method": "webstack/other"},
])
def test_a_changed_key_is_a_miss(world, cache, changes):
    implementation = stack_routes(world)
    cache.write(COMMIT, implementation, ROUTES)
    assert cache.read(COMMIT, replace(implementation, **changes)) is None


def test_project_extensions_are_keyed_by_the_command_file_content(world, cache):
    implementation = project_routes(world)
    cache.write(COMMIT, implementation, ROUTES)
    assert cache_key(implementation)["commandFileHash"] == file_hash(implementation.command_file)
    assert cache.read(COMMIT, implementation) == ROUTES
    implementation.command_file.write_text("print('changed')\n", encoding="utf-8")
    assert cache.read(COMMIT, implementation) is None
    assert cache.read(COMMIT, stack_routes(world)) is None


def test_core_methods_are_keyed_by_the_module_content(world, cache, tmp_path):
    module = tmp_path / "manual_list.py"
    module.write_text("VERSION = 1\n", encoding="utf-8")
    implementation = Implementation(ExtensionPoint.PAGE_ROUTES, ExtensionLayer.CORE, 120,
                                    command=("{python}", "-m", "tightrein.extensions.methods.page_routes.manual_list"),
                                    cache_name="core", command_file=module, method="core/manual-list")
    cache.write(COMMIT, implementation, ROUTES)
    key = cache_key(implementation)
    assert (key["method"], key["commandFileHash"], key["stackVersion"]) == ("core/manual-list", file_hash(module), None)
    assert cache.read(COMMIT, implementation) == ROUTES
    module.write_text("VERSION = 2\n", encoding="utf-8")
    assert cache.read(COMMIT, implementation) is None


def test_missing_corrupt_or_invalid_files_are_misses(world, cache):
    implementation = stack_routes(world)
    output = world.workspace.extension_output(COMMIT, ExtensionPoint.PAGE_ROUTES)
    cache.write(COMMIT, implementation, ROUTES)
    output.write_text("{", encoding="utf-8")
    assert cache.read(COMMIT, implementation) is None
    output.write_text(json.dumps({"routes": []}), encoding="utf-8")
    assert cache.read(COMMIT, implementation) is None
    cache.write(COMMIT, implementation, ROUTES)
    world.workspace.extension_meta(COMMIT, ExtensionPoint.PAGE_ROUTES).unlink()
    assert cache.read(COMMIT, implementation) is None
    assert cache.read("a1b2c3d", implementation) is None


def test_options_hash_ignores_key_order():
    assert options_hash({"a": 1, "b": [1, 2]}) == options_hash({"b": [1, 2], "a": 1})
    assert options_hash({"a": 1}) != options_hash({"a": 2})

