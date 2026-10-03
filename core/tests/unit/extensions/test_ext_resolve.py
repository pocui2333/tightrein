import pytest
from extension_world import COMMAND, METHOD, PYTHON, SCRIPT, STACK

from tightrein.config.project import ConfigError
from tightrein.domain.enums import ExtensionLayer, ExtensionMode, ExtensionPoint
from tightrein.extensions import points
from tightrein.extensions.catalog import CORE_METHODS_DIR
from tightrein.extensions.resolve import project_implementations, resolve, stack_method_implementations

SPEC_SCHEMA = {
    "type": "object", "required": ["project"], "additionalProperties": False,
    "properties": {"project": {"type": "string"}, "configuration": {"type": "string"}},
}
TOOLS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"solution": {"type": "string"}}}
JSON_LINES = "tightrein.extensions.methods.log_parse.json_lines"


def issues_of(caught):
    return [str(issue) for issue in caught.value.issues]


def resolved(world, config):
    return resolve(config, world.workspace, world.tool, python=PYTHON)


def test_every_point_uses_the_default_without_stacks_and_extensions(world):
    resolution = resolved(world, world.config())
    for point in ExtensionPoint:
        implementation = resolution.get(point)
        assert implementation.layer is ExtensionLayer.DEFAULT
        assert implementation.timeout_seconds == points.SPECS[point].timeout_seconds
        assert (implementation.argv, implementation.method) == ((), None)


def test_the_five_steps_are_followed_in_order(world):
    stack_dir = world.stack({
        "spec-export": {"options": {"configuration": "Debug"}},
        "log-parse": {},
        "page-routes": {},
        "static-tools": {},
    }, env={"TOOL_NOLOGO": "1"})
    other = world.method("static-tools", "audit", command=["{python}", SCRIPT, "--audit"])
    extensions_dir = world.project_extension()
    config = world.config(stacks=[STACK], methods={"core/loki": {"pageSize": 500}}, extensions={
        "spec-export": {"options": {"project": "src/App/App.csproj"}},
        "authz-roles": {"command": COMMAND, "timeoutSeconds": 60},
        "log-platform": {"use": "core/loki", "options": {"url": "https://logs.example.test"}},
        "log-parse": {"command": COMMAND, "mode": "extend", "options": {"timezone": "Asia/Tokyo"}},
        "static-tools": {"use": "webstack/audit"},
        "page-routes": {"enabled": False},
    })
    resolution = resolved(world, config)
    spec = resolution.get(ExtensionPoint.SPEC_EXPORT)
    assert (spec.layer, spec.method, spec.cwd, spec.cache_name, spec.version) == (
        ExtensionLayer.STACK, "webstack/fake", stack_dir, STACK, "1.0.0")
    method_dir = world.tool.method_dir(STACK, ExtensionPoint.SPEC_EXPORT, METHOD)
    assert (spec.command, spec.argv, spec.command_file) == (
        tuple(COMMAND), (PYTHON, str(method_dir / SCRIPT)), method_dir / SCRIPT)
    assert (spec.options, spec.env, spec.timeout_seconds) == (
        {"configuration": "Debug", "project": "src/App/App.csproj"}, {"TOOL_NOLOGO": "1"}, 900)
    roles = resolution.get(ExtensionPoint.AUTHZ_ROLES)
    assert (roles.layer, roles.cwd, roles.cache_name, roles.timeout_seconds, roles.env, roles.base, roles.method) == (
        ExtensionLayer.PROJECT, extensions_dir, "demo", 60, {}, None, None)
    assert roles.argv == (PYTHON, str(extensions_dir / SCRIPT))
    source = resolution.get(ExtensionPoint.LOG_PLATFORM)
    assert (source.layer, source.method, source.cwd, source.cache_name, source.env) == (
        ExtensionLayer.CORE, "core/loki", world.workspace.root, "core", {})
    assert source.argv == (PYTHON, "-m", "tightrein.extensions.methods.log_platform.loki")
    assert source.command_file == CORE_METHODS_DIR / "log_platform" / "loki.py"
    assert source.options == {"url": "https://logs.example.test", "user": None, "tenant": None, "keychainItem": None,
                              "retentionDays": 30, "pageSize": 500, "timeoutSeconds": 30}
    parse = resolution.get(ExtensionPoint.LOG_PARSE)
    assert (parse.layer, parse.mode, parse.base.layer, parse.base.method, parse.base.cwd) == (
        ExtensionLayer.PROJECT, ExtensionMode.EXTEND, ExtensionLayer.STACK, "webstack/fake", stack_dir)
    assert parse.options == parse.base.options == {"timezone": "Asia/Tokyo"}
    tools = resolution.get(ExtensionPoint.STATIC_TOOLS)
    assert (tools.method, tools.argv) == ("webstack/audit", (PYTHON, str(other / SCRIPT), "--audit"))
    assert resolution.get(ExtensionPoint.PAGE_ROUTES).layer is ExtensionLayer.DEFAULT
    assert resolution.get(ExtensionPoint.LOCAL_RUN).layer is ExtensionLayer.DEFAULT


def test_extend_requires_a_lower_layer(world):
    world.project_extension()
    extend = {"local-run": {"command": COMMAND, "mode": "extend"}}
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(extensions=extend))
    assert issues_of(caught) == ["extensions.local-run.mode: extend 需要下一层的实现，但没有声明 stacks"]
    world.stack({"log-parse": {}})
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK], extensions=extend))
    assert issues_of(caught) == [
        "extensions.local-run.mode: extend 需要下一层的实现，stacks 中的技术栈都没有为 local-run 指定默认方法"]


def test_use_must_name_a_method_of_this_point_from_a_declared_stack(world):
    world.stack({"log-parse": {}})
    config = world.config(extensions={
        "spec-export": {"use": "core/openapi-yaml"},
        "log-platform": {"use": "core/json-lines"},
        "log-parse": {"use": "webstack/fake"},
        "page-routes": {"use": "otherstack/routes"},
    })
    with pytest.raises(ConfigError) as caught:
        resolved(world, config)
    assert issues_of(caught) == [
        "extensions.log-parse.use: 方法 webstack/fake 所属的技术栈 webstack 没有在 stacks 中声明",
        "extensions.log-platform.use: core/json-lines 属于 log-parse，不能用于 log-platform",
        "extensions.page-routes.use: 方法 otherstack/routes 所属的技术栈 otherstack 没有在 stacks 中声明",
        "extensions.spec-export.use: 方法目录中没有 core/openapi-yaml",
    ]
    fixed = resolved(world, world.config(stacks=[STACK], extensions={"log-parse": {"use": "webstack/fake"}}))
    assert fixed.get(ExtensionPoint.LOG_PARSE).method == "webstack/fake"


def test_two_stacks_with_defaults_for_the_same_point_need_use(world):
    world.stack({"log-parse": {}})
    world.stack({"log-parse": {}}, name="otherstack")
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK, "otherstack"]))
    assert issues_of(caught) == [
        "extensions.log-parse: 技术栈 webstack、otherstack 都为 log-parse 指定了默认方法，用 use 明确选择"]
    chosen = world.config(stacks=[STACK, "otherstack"], extensions={"log-parse": {"use": "otherstack/fake"}})
    implementation = resolved(world, chosen).get(ExtensionPoint.LOG_PARSE)
    assert (implementation.method, implementation.cache_name) == ("otherstack/fake", "otherstack")


def test_stack_manifest_problems_name_the_file(world):
    manifest = world.tool.stack_manifest(STACK)
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK]))
    assert issues_of(caught) == [f"stacks: 找不到技术栈扩展的清单 {manifest}"]
    world.stack({"log-parse": {}}, manifest_name="otherstack", env={"NUGET_API_KEY": "abc", "TOOL_NOLOGO": "1"},
                defaults={"log-parse": METHOD, "page-routes": "routes"})
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK]))
    assert issues_of(caught) == [
        f"stacks: {manifest} 的 defaults.page-routes 为 routes，webstack/page-routes/ 下没有这个方法",
        f"stacks: {manifest} 的 env 只能声明非敏感变量：NUGET_API_KEY",
        f"stacks: {manifest} 的 name 为 otherstack，与目录名 webstack 不一致",
    ]
    world.stack({"log-parse": {}}, defaults={"log-reader": METHOD})
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK]))
    assert issues_of(caught)[0].startswith(f"stacks: {manifest} 不合格：$.defaults")


def test_method_manifest_problems_are_reported(world):
    world.stack({"log-parse": {"summary": ""}, "page-routes": {"command": None}})
    world.method("spec-export", "exporter", name="other")
    world.method("page-routes", "typed", options={"limit": "x"},
                 optionsSchema={"type": "object", "properties": {"limit": {"type": "integer"}}})
    (world.tool.stack_dir(STACK) / "local-run" / "empty").mkdir(parents=True)
    method_file = world.tool.method_manifest(STACK, ExtensionPoint.PAGE_ROUTES, METHOD)
    method_file.write_text(method_file.read_text(encoding="utf-8").replace("command: null\n", ""), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(stacks=[STACK]))
    parse_file = world.tool.method_manifest(STACK, ExtensionPoint.LOG_PARSE, METHOD)
    spec_file = world.tool.method_manifest(STACK, ExtensionPoint.SPEC_EXPORT, "exporter")
    typed_file = world.tool.method_manifest(STACK, ExtensionPoint.PAGE_ROUTES, "typed")
    manifest = world.tool.stack_manifest(STACK)
    assert issues_of(caught) == [
        f"stacks: {world.tool.stack_dir(STACK) / 'local-run' / 'empty'} 中没有 method.yaml",
        f"stacks: {parse_file}：$.summary: '' should be non-empty",
        f"stacks: {method_file}：技术栈方法须给出 command",
        f"stacks: {typed_file}：options 的默认值不合格：$.limit: 'x' is not of type 'integer'",
        f"stacks: {spec_file}：name 为 other，应为 exporter",
        f"stacks: {manifest} 的 defaults.log-parse 为 fake，webstack/log-parse/ 下没有这个方法",
        f"stacks: {manifest} 的 defaults.page-routes 为 fake，webstack/page-routes/ 下没有这个方法",
    ]


def test_missing_command_files_are_reported(world):
    world.stack({"spec-export": {"command": ["{python}", "missing.py"]}})
    config = world.config(stacks=[STACK], extensions={"authz-roles": {"command": ["{python}", "absent.py"]}})
    with pytest.raises(ConfigError) as caught:
        resolved(world, config)
    method_dir = world.tool.method_dir(STACK, ExtensionPoint.SPEC_EXPORT, METHOD)
    assert issues_of(caught) == [
        f"extensions.authz-roles.command: 命令文件不存在：{world.workspace.extensions_dir() / 'absent.py'}",
        f"stacks: 方法 webstack/fake 的 spec-export：命令文件不存在：{method_dir / 'missing.py'}",
    ]


def test_commands_without_a_script_and_executables_with_a_path(world):
    extensions_dir = world.project_extension()
    starter = extensions_dir / "bin" / "start"
    starter.parent.mkdir()
    starter.write_text("#!/bin/sh\n", encoding="utf-8")
    config = world.config(extensions={
        "static-tools": {"command": ["dotnet", "run", "--project", "tools"]},
        "local-run": {"command": ["./bin/start", "--plan"]},
        "page-routes": {"command": ["node", "page_routes.mjs"]},
    })
    with pytest.raises(ConfigError) as caught:
        resolved(world, config)
    assert issues_of(caught) == [
        f"extensions.page-routes.command: 命令文件不存在：{extensions_dir / 'page_routes.mjs'}"]
    (extensions_dir / "page_routes.mjs").write_text("", encoding="utf-8")
    resolution = resolved(world, config)
    tools = resolution.get(ExtensionPoint.STATIC_TOOLS)
    assert (tools.argv, tools.command_file) == (("dotnet", "run", "--project", "tools"), None)
    assert resolution.get(ExtensionPoint.LOCAL_RUN).argv == (str(extensions_dir / "bin" / "start"), "--plan")
    assert resolution.get(ExtensionPoint.PAGE_ROUTES).argv == ("node", str(extensions_dir / "page_routes.mjs"))


def test_method_options_follow_the_options_schema(world):
    world.stack({
        "spec-export": {"options": {"configuration": "Debug"}, "optionsSchema": SPEC_SCHEMA},
        "static-tools": {"optionsSchema": TOOLS_SCHEMA},
    })
    world.project_extension()
    config = world.config(stacks=[STACK], extensions={
        "spec-export": {"options": {"configuration": 3}},
        "static-tools": {"command": COMMAND, "mode": "extend", "options": {"solution": 1, "frontendDir": "src/web"}},
        "log-parse": {"use": "core/json-lines", "options": {"timezone": 8}},
    })
    with pytest.raises(ConfigError) as caught:
        resolved(world, config)
    assert issues_of(caught) == [
        "extensions.log-parse.options: $.timezone: 8 is not of type 'string'",
        "extensions.spec-export.options: $.configuration: 3 is not of type 'string'",
        "extensions.spec-export.options: $: 'project' is a required property",
        "extensions.static-tools.options: $.solution: 1 is not of type 'string'",
    ]
    fixed = world.config(stacks=[STACK], extensions={
        "spec-export": {"options": {"project": "src/App/App.csproj"}},
        "static-tools": {"command": COMMAND, "mode": "extend",
                         "options": {"solution": "App.sln", "frontendDir": "src/web"}},
        "log-parse": {"use": "core/json-lines", "options": {"timezone": "Asia/Tokyo"}},
    })
    resolution = resolved(world, fixed)
    assert resolution.get(ExtensionPoint.STATIC_TOOLS).base.options == {"solution": "App.sln", "frontendDir": "src/web"}
    parse = resolution.get(ExtensionPoint.LOG_PARSE)
    assert (parse.argv, parse.options["timezone"], parse.options["timeField"]) == (
        (PYTHON, "-m", JSON_LINES), "Asia/Tokyo", "timestamp")


def test_a_log_query_for_platform_errors_requires_log_parse(world):
    world.project_extension()
    platform = {"log-platform": {"use": "core/loki", "options": {"url": "https://logs.example.test"}}}
    with pytest.raises(ConfigError) as caught:
        resolved(world, world.config(extensions=platform, sources={"platform-errors": {"logQuery": '{app="api"}'}}))
    assert issues_of(caught) == [
        "extensions.log-parse: sources.platform-errors.logQuery 有值时须选用 log-parse 方法(字段映射)"]
    without_query = resolved(world, world.config(extensions=platform))
    assert without_query.get(ExtensionPoint.LOG_PARSE).layer is ExtensionLayer.DEFAULT
    only_parse = resolved(world, world.config(extensions={"log-parse": {"command": COMMAND}}))
    assert only_parse.get(ExtensionPoint.LOG_PLATFORM).layer is ExtensionLayer.DEFAULT


def test_implementations_for_fixture_runs(world):
    world.stack({"spec-export": {"options": {"configuration": "Debug"}, "optionsSchema": SPEC_SCHEMA},
                 "log-parse": {}})
    world.method("log-parse", "strict")
    stack = stack_method_implementations(world.tool, STACK, python=PYTHON)
    assert [(item.point, item.method) for item in stack] == [
        (ExtensionPoint.SPEC_EXPORT, "webstack/fake"), (ExtensionPoint.LOG_PARSE, "webstack/fake"),
        (ExtensionPoint.LOG_PARSE, "webstack/strict")]
    assert stack[0].options == {"configuration": "Debug"}
    assert stack[1].cwd == world.tool.stack_dir(STACK)
    world.project_extension()
    config = world.config(stacks=[STACK], extensions={
        "log-parse": {"command": COMMAND, "mode": "extend"},
        "page-routes": {"command": COMMAND, "enabled": False},
    })
    project = project_implementations(config, world.workspace, python=PYTHON)
    assert set(project) == {ExtensionPoint.LOG_PARSE}
    assert project[ExtensionPoint.LOG_PARSE].base is None
    with pytest.raises(ConfigError):
        stack_method_implementations(world.tool, "nostack", python=PYTHON)
