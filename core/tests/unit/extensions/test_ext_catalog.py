import pytest

from tightrein.config import layers
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint
from tightrein.extensions.catalog import (
    ManifestError,
    ToolRequirement,
    core_methods,
    installed_stacks,
    load_catalog,
    option_problems,
    read_core,
)
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import ToolLayout

MANIFEST = {
    "name": "sample-reader",
    "point": "log-platform",
    "summary": "读取样例目录",
    "applicability": "测试用",
    "optionsSchema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
    "tools": [{"tool": "tail", "install": None}],
}


def write_method(root, directory, stem, manifest, module=True):
    folder = root / directory
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.yaml").write_text(yaml_text.dump(manifest), encoding="utf-8")
    if module:
        (folder / f"{stem}.py").write_text("", encoding="utf-8")
    return folder / f"{stem}.yaml"


def test_a_core_manifest_becomes_a_method_run_as_a_module(tmp_path):
    path = write_method(tmp_path, "log_platform", "sample_reader", MANIFEST)
    method = read_core(path)
    assert (method.id, method.point, method.layer) == ("core/sample-reader", ExtensionPoint.LOG_PLATFORM,
                                                       ExtensionLayer.CORE)
    assert method.command == ("{python}", "-m", "tightrein.extensions.methods.log_platform.sample_reader")
    assert (method.options, method.tools, method.directory, method.manifest) == (
        {}, (ToolRequirement("tail"),), tmp_path / "log_platform", path)
    assert core_methods(tmp_path) == (method,)
    assert read_core(path, check=False) == method


def test_manifest_problems_are_listed_together(tmp_path):
    write_method(tmp_path, "log_platform", "sample_reader", dict(MANIFEST, name="other", point="log-parse"))
    write_method(tmp_path, "log_platform", "with_command", dict(MANIFEST, name="with-command", command=["x"]))
    write_method(tmp_path, "log_platform", "with_options", dict(MANIFEST, name="with-options", options={"pattern": "*"}))
    write_method(tmp_path, "log_platform", "no_module", dict(MANIFEST, name="no-module"), module=False)
    write_method(tmp_path, "log_platform", "bad_schema", dict(MANIFEST, name="bad-schema",
                                                            optionsSchema={"type": "no-such-type"}))
    write_method(tmp_path, "log_platform", "no_summary", {key: value for key, value in MANIFEST.items()
                                                        if key != "summary"} | {"name": "no-summary"})
    write_method(tmp_path, "log_reader", "sample", dict(MANIFEST, name="sample"))
    with pytest.raises(ManifestError) as caught:
        core_methods(tmp_path)
    folder = tmp_path / "log_platform"
    assert caught.value.problems == (
        f"{folder / 'bad_schema.yaml'}：optionsSchema 不是合格的 JSON Schema："
        "'no-such-type' is not valid under any of the given schemas",
        f"{folder / 'no_module.yaml'}：找不到模块文件 no_module.py",
        f"{folder / 'no_summary.yaml'}：$: 'summary' is a required property",
        f"{folder / 'sample_reader.yaml'}：name 为 other，应为 sample-reader",
        f"{folder / 'sample_reader.yaml'}：point 为 log-parse，应为 log-platform",
        f"{folder / 'with_command.yaml'}：核心方法以模块运行，清单中不写 command",
        f"{folder / 'with_options.yaml'}：核心方法 options 的默认值写在 config/defaults.yaml，清单中不写 options",
        f"{tmp_path / 'log_reader' / 'sample.yaml'}：目录 log_reader 不是扩展点",
    )


def test_core_option_defaults_name_core_methods_and_fit_their_schemas():
    methods = {method.id: method for method in core_methods()}
    for method_id, defaults in layers.core_defaults()["methods"].items():
        schema = {key: value for key, value in methods[method_id].options_schema.items() if key != "required"}
        assert option_problems(schema, defaults) == [], method_id


def write_stack_method(tool, stack, point, name, **fields):
    manifest = {"name": name, "point": point.value, "summary": "测试方法", "applicability": "测试用",
                "command": ["{python}", "run.py"], "optionsSchema": {"type": "object"}, **fields}
    path = tool.method_manifest(stack, point, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text.dump(manifest), encoding="utf-8")


def test_the_catalog_joins_core_methods_and_installed_stacks(tmp_path):
    tool = ToolLayout(tmp_path / "tool")
    write_stack_method(tool, "webstack", ExtensionPoint.LOG_PARSE, "console")
    write_stack_method(tool, "webstack", ExtensionPoint.SPEC_EXPORT, "cli", tools=[{"tool": "cli", "minVersion": "2"}])
    tool.stack_manifest("webstack").write_text(yaml_text.dump(
        {"name": "webstack", "version": "0.1.0", "defaults": {"log-parse": "console"}}), encoding="utf-8")
    (tool.stacks_dir() / "draft").mkdir()
    assert installed_stacks(tool) == ("webstack",)
    catalog, issues = load_catalog(tool)
    assert issues == []
    assert [method.id for method in catalog.for_point(ExtensionPoint.LOG_PARSE)] == [
        "core/json-lines", "core/regex", "webstack/console"]
    console = catalog.find(ExtensionPoint.LOG_PARSE, "webstack/console")
    assert (console.layer, console.command, console.directory) == (
        ExtensionLayer.STACK, ("{python}", "run.py"), tool.method_dir("webstack", ExtensionPoint.LOG_PARSE, "console"))
    assert catalog.find(ExtensionPoint.SPEC_EXPORT, "webstack/cli").tools == (ToolRequirement("cli", "2"),)
    assert catalog.find(ExtensionPoint.SPEC_EXPORT, "webstack/console") is None
    assert catalog.points_of("core/manual-list") == (ExtensionPoint.AUTHZ_ENDPOINTS, ExtensionPoint.PAGE_ROUTES)
    assert catalog.stacks["webstack"].defaults == {ExtensionPoint.LOG_PARSE: "console"}
    core_only, _ = load_catalog(tool, [])
    assert core_only.stacks == {} and all(method.source == "core" for method in core_only.methods)
    assert installed_stacks(ToolLayout(tmp_path / "empty")) == ()
