import pytest

from tightrein.config import layers, project, show
from tightrein.config.project import ConfigError
from tightrein.config.user import UserConfig
from tightrein.store.files import yaml_text

PROJECT = {
    "project": {"name": "demo", "repo": "/work/demo", "mainBranch": "main"},
    "target": {"baseUrl": "https://staging.example.test", "healthcheck": "/health"},
    "accounts": {"roles": {"Admin": {"keychain": "demo.admin"}},
                 "login": {"endpoint": "/login", "bodyTemplate": {}, "tokenPath": "token"}},
    "stages": {},
    "evaluation": {"judge": {"runner": "claude"}, "budgetUsd": 5},
    "thresholds": {"suppressionDays": {"value": 30, "min": 7, "max": 90},
                   "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}}},
}


def write_stack(root, name, text):
    directory = root / "extensions" / "stacks" / name
    directory.mkdir(parents=True)
    (directory / "defaults.yaml").write_text(text, encoding="utf-8")


def parse(tmp_path, **changes):
    return project.parse({**PROJECT, **changes}, tmp_path / "project.yaml", tmp_path)


def test_stack_layers_sit_between_core_and_project(tmp_path):
    write_stack(tmp_path, "alpha", "loop: {findLimit: 30}\nchecks: {residuePatterns: ['debug\\(']}\n")
    write_stack(tmp_path, "beta", "loop: {objectLockMinutes: 60}\n")
    config = parse(tmp_path, stacks=["alpha", "beta", "gamma"])
    assert [layer.name for layer in config.stack_layers] == ["stack:alpha", "stack:beta"]
    assert config.get("loop.findLimit") == 30
    assert config.get("loop.objectLockMinutes") == 60
    assert config.get("loop.lookbackDays") == 7
    assert parse(tmp_path, stacks=["alpha"], loop={"findLimit": 5}).get("loop.findLimit") == 5


def test_a_stack_may_only_use_core_keys(tmp_path):
    write_stack(tmp_path, "alpha", "loop: {findLimit: 30}\ntarget: {baseUrl: https://x.test}\n")
    with pytest.raises(ConfigError) as caught:
        parse(tmp_path, stacks=["alpha"])
    assert [str(issue) for issue in caught.value.issues] == ["target.baseUrl: 核心缺省值中没有这个键"]


def test_conflicting_stacks_need_a_project_value(tmp_path):
    write_stack(tmp_path, "alpha", "loop: {findLimit: 30}\n")
    write_stack(tmp_path, "beta", "loop: {findLimit: 40}\n")
    with pytest.raises(ConfigError) as caught:
        parse(tmp_path, stacks=["alpha", "beta"])
    assert [issue.key for issue in caught.value.issues] == ["loop.findLimit"]
    assert parse(tmp_path, stacks=["alpha", "beta"], loop={"findLimit": 5}).get("loop.findLimit") == 5


def test_stacks_may_append_to_lists_without_conflicting(tmp_path):
    paths = "stages: {fix: {roles: {frontend-designer: {paths+: [%s]}}}}\n"
    write_stack(tmp_path, "alpha", paths % "'*.razor'")
    write_stack(tmp_path, "beta", paths % "'web/'")
    config = parse(tmp_path, stacks=["alpha", "beta"])
    found = config.get("stages.fix.roles.frontend-designer.paths")
    assert found[-2:] == ["*.razor", "web/"] and "*.vue" in found


def test_append_only_lists_join_every_layer(tmp_path):
    write_stack(tmp_path, "alpha", "credentialFiles: ['*.jks']\n")
    config = parse(tmp_path, stacks=["alpha"], credentialFiles=["*.pem", "vault.txt"])
    core = tuple(layers.core_value("credentialFiles"))
    assert config.appended("credentialFiles") == tuple(dict.fromkeys([*core, "*.jks", "*.pem", "vault.txt"]))


def test_show_lists_the_stack_layers(tmp_path):
    write_stack(tmp_path, "alpha", "loop: {findLimit: 30}\n")
    config = parse(tmp_path, stacks=["alpha"])
    shown = show.show(config, UserConfig(tmp_path / "config.yaml"), "loop.findLimit")
    assert [(item.source, dict(item.layers)) for item in shown] == [("stack:alpha", {"core": 20, "stack:alpha": 30})]


def test_the_core_defaults_file_is_valid():
    assert yaml_text.load(layers.DEFAULTS_FILE.read_text(encoding="utf-8")) == layers.core_defaults()
