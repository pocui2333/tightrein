import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tightrein.agents.params import CONDITIONS, FRONTEND, HIGH_RISK
from tightrein.settings.load import MissingSetting, Settings, SettingsInvalid, call_points, merge
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

REPO_DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"

MODELS = {
    "opus": {"tool": "claude", "model": "opus", "effort": "high", "price": {"input": 4, "output": 20}},
    "opus-mid": {"tool": "claude", "model": "opus", "effort": "medium", "price": None},
    "flash": {"tool": "agy", "model": "gemini-flash", "effort": None, "price": None},
    "fable": {"tool": "claude", "model": "fable", "effort": None, "price": None},
}
GLOBAL = {
    "model": "opus", "modelWhen": {}, "fallback": None, "timeout": "10m", "idle": "5m", "turns": 15,
    "inputTokens": 50000, "outputTokens": 16000, "rounds": 1, "access": "read", "network": False,
}


def base(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "models": copy.deepcopy(MODELS),
        "controls": {
            "*": dict(GLOBAL),
            "implement": {"timeout": "2h"},
            "implement.design": {"modelWhen": {"high_risk": "fable"}},
            "implement.code": {"access": "write", "turns": 30, "timeout": "30m"},
            "implement.review.deep": {"model": "flash"},
            "assess.triage": {"model": "opus"},
            "assess.refute": {"model": "flash"},
        },
        "independence": [["assess.refute", "assess.triage"], ["implement.review.deep", "implement.code"]],
        "limits": {"timeouts": {"run": "4h"}, "lock": {"stale": "90s"}},
        "boundaries": {
            "changeCap": {"files": 10, "lines": 400},
            "autoApprove": {"files": 3, "lines": 100},
            "uncounted": ["*.lock"],
            "protected": {"forbidden": [".env"], "highRisk": ["migrations/"]},
        },
    }
    data.update(changes)
    return data


def tool_root(tmp_path: Path, defaults: dict[str, Any], controls: dict[str, Any] | None = None) -> ToolLayout:
    tool = ToolLayout(tmp_path / "tool")
    tool.settings_dir.mkdir(parents=True)
    tool.defaults.write_text(json.dumps(defaults), encoding="utf-8")
    if controls is not None:
        tool.controls.write_text(json.dumps(controls), encoding="utf-8")
    return tool


def workspace(tmp_path: Path, overrides: dict[str, Any], project: dict[str, Any] | None = None) -> WorkspaceLayout:
    layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    layout.root.mkdir(parents=True)
    facts = project if project is not None else {
        "repo": "../repo", "mainBranch": "main", "language": "ja", "commands": {"test": "pytest"},
        "testPatterns": ["tests/"], "frontendPatterns": [],
    }
    layout.settings.write_text(json.dumps({"project": facts, "overrides": overrides}), encoding="utf-8")
    return layout


# 缺省值文件


def test_the_shipped_defaults_are_valid() -> None:
    defaults = json.loads(REPO_DEFAULTS.read_text(encoding="utf-8"))
    assert Settings.from_data(defaults).issues() == []


def test_the_shipped_defaults_load_from_the_tool_root(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, json.loads(REPO_DEFAULTS.read_text(encoding="utf-8")))
    settings = Settings.load(tool)
    assert settings.duration("limits.lock.stale") == 90
    assert settings.project is None and settings.sites == {}


# 三层合并


def test_three_layers_override_in_order(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, base(), controls={"controls": {"implement.code": {"turns": 40}}})
    overrides = {"controls": {"implement.code": {"timeout": "20m"}}, "boundaries": {"changeCap": {"lines": 300}}}
    layout = workspace(tmp_path, overrides)
    settings = Settings.load(tool, layout)
    assert settings.control("implement.code", "turns") == 40  # controls.json
    assert settings.control("implement.code", "timeout") == "20m"  # 工作区
    assert settings.control("implement.code", "access") == "write"  # 缺省
    assert settings.get("boundaries.changeCap") == {"files": 10, "lines": 300}  # 映射按键递归合并
    assert settings.explain("boundaries.changeCap") == [
        ("defaults", {"files": 10, "lines": 400}), ("workspace", {"lines": 300})]


def test_project_facts_come_from_the_workspace(tmp_path: Path) -> None:
    layout = workspace(tmp_path, {})
    settings = Settings.load(tool_root(tmp_path, base()), layout)
    assert settings.project is not None
    assert settings.project.language == "ja" and settings.project.main_branch == "main"
    assert settings.project.repo == (layout.root / "../repo").resolve()
    assert settings.project.commands == {"test": "pytest"} and settings.project.test_patterns == ("tests/",)


def test_project_facts_must_all_be_written(tmp_path: Path) -> None:
    layout = workspace(tmp_path, {}, project={"repo": None, "language": "fr"})
    with pytest.raises(SettingsInvalid) as raised:
        Settings.load(tool_root(tmp_path, base()), layout)
    assert "project.mainBranch：缺少(探测不到时写 null)" in raised.value.issues
    assert any(item.startswith("project.language") for item in raised.value.issues)


def test_lists_and_values_are_replaced_as_a_whole() -> None:
    settings = Settings.from_data(base(), {"boundaries": {"uncounted": ["dist/"]}})
    assert settings.get("boundaries.uncounted") == ["dist/"]


def test_a_plus_key_appends_to_the_lower_list() -> None:
    settings = Settings.from_data(
        base(), {"boundaries": {"uncounted+": ["dist/", "*.lock"]}}, {"boundaries": {"uncounted+": ["gen/"]}})
    assert settings.get("boundaries.uncounted") == ["*.lock", "dist/", "gen/"]


def test_a_plus_key_needs_lists() -> None:
    with pytest.raises(SettingsInvalid, match="只能用于列表"):
        Settings.from_data(base(), {"boundaries": {"changeCap+": {"files": 1}}})


def test_merge_does_not_modify_its_inputs() -> None:
    lower, upper = base(), {"boundaries": {"uncounted+": ["x"], "changeCap": {"files": 5}}}
    before = (copy.deepcopy(lower), copy.deepcopy(upper))
    merge(lower, upper, path="")
    assert (lower, upper) == before


def test_append_only_lists_cannot_be_replaced(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, base(), controls={"boundaries": {"protected": {"forbidden": ["only-this"]}}})
    with pytest.raises(SettingsInvalid) as raised:
        Settings.load(tool)
    assert raised.value.issues == [
        "boundaries.protected.forbidden（controls）：只能用 `forbidden+` 追加，不能替换缺省的受保护路径"]


def test_append_only_lists_join_every_layer(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, base(), controls={"boundaries": {"protected": {"forbidden+": ["*.pem"]}}})
    protected = {"forbidden+": ["config/prod.yml"], "highRisk+": ["infra/"]}
    layout = workspace(tmp_path, {"boundaries": {"protected": protected}})
    settings = Settings.load(tool, layout)
    assert settings.get("boundaries.protected.forbidden") == [".env", "*.pem", "config/prod.yml"]
    assert settings.get("boundaries.protected.highRisk") == ["migrations/", "infra/"]


# 控制键继承与模型


def test_control_fields_inherit_from_step_to_module_to_stage_to_default() -> None:
    settings = Settings.from_data(base())
    assert settings.control("implement.code", "turns") == 30
    assert settings.control("implement.design.frontend", "timeout") == "2h"  # 取阶段
    assert settings.control("assess.triage", "timeout") == "10m"  # 取全局缺省
    limits = settings.limits_for("implement.code")
    assert (limits.timeout_s, limits.turns, limits.idle_s, limits.output_tokens) == (1800, 30, 300, 16000)


def test_missing_settings_name_the_full_key() -> None:
    settings = Settings.from_data(base())
    with pytest.raises(MissingSetting) as raised:
        settings.get("limits.retry.attempts")
    assert raised.value.key == "limits.retry.attempts" and str(raised.value) == "配置中没有 limits.retry.attempts"
    with pytest.raises(MissingSetting, match="models.nothing"):
        settings.model("nothing")


def test_section_gives_only_the_module_own_values() -> None:
    settings = Settings.from_data(base(), {"controls": {"collect.static.review": {"maxClaims": 10}}})
    assert settings.section("collect.static.review") == {"maxClaims": 10}
    assert settings.section("collect.static") == {}


def test_models_are_looked_up_by_condition_then_by_point() -> None:
    settings = Settings.from_data(base())
    assert settings.model_for("implement.design").alias == "opus"
    assert settings.model_for("implement.design", ("high_risk",)).alias == "fable"
    assert settings.model_for("implement.design", ("frontend", "high_risk")).alias == "fable"
    assert settings.model_for("implement.design", ("unknown",)).alias == "opus"
    model = settings.model_for("implement.review.deep")
    assert (model.tool, model.model, model.effort, model.price_input) == ("agy", "gemini-flash", None, None)
    assert settings.model("opus").price_output == 20
    assert settings.fallback_for("implement.code") is None


# 校验：一次列出全部问题，每条带完整键名


def test_every_problem_is_listed_with_its_full_key() -> None:
    controls = {
        "Implement": {"turns": 3},
        "implement.code": {"turns": "many", "model": "gpt", "access": "admin"},
        "implement.design": {"modelWhen": {"high_risk": "nobody"}},
        "assess.triage": {"timeout": "30"},
    }
    issues = Settings.from_data(base(), {"controls": controls}).issues()
    assert "controls.Implement：控制键不合规：'Implement'" in issues
    assert "controls.implement.code.turns：类型不对：'many'" in issues
    assert any(item.startswith("controls.assess.triage.timeout：时长要写成数字加单位") for item in issues)
    assert "controls.implement.code.model：没有这个模型别名：gpt" in issues
    assert "controls.implement.code.access：只能是 read 或 write" in issues
    assert "controls.implement.design.modelWhen.high_risk：没有这个模型别名：nobody" in issues


def test_routes_only_name_registered_call_points_and_their_declared_conditions() -> None:
    controls = {
        "implement.cod": {"model": "opus"},  # 拼错的调用点
        "assess.triage.deep": {"fallback": "opus-mid"},  # 没有这个下级调用点
        "implement.design": {"modelWhen": {"frontend": "flash"}},  # 条件属于 implement.locate
        "retro.idea": {"modelWhen": {"high_risk": "fable"}},  # 不按条件选模型的调用点
        "implement": {"modelWhen": {"frontend": "fable", "high_risk": "fable"}},  # 上级覆盖到下面各点声明的条件
        "collect.static": {"model": "opus-mid"},  # 上级控制键可以写
        "collect.dynamic": {"timeout": "5m"},  # 不写路由的控制键不核对调用点
    }
    issues = Settings.from_data(base(), {"controls": controls}).issues()
    assert "controls.implement.cod：不是登记的调用点(src/tightrein/prompts/<调用点>.md)，也不是其上级，不能写 model" in issues
    assert ("controls.assess.triage.deep：不是登记的调用点(src/tightrein/prompts/<调用点>.md)，也不是其上级，"
            "不能写 fallback") in issues
    assert "controls.implement.design.modelWhen.frontend：没有声明这个条件，implement.design 可用的条件：high_risk" in issues
    assert "controls.retro.idea.modelWhen.high_risk：没有声明这个条件，retro.idea 可用的条件：无(该调用点不按条件选模型)" in issues
    assert not any(item.startswith(("controls.implement.modelWhen", "controls.collect.")) for item in issues)
    assert len(issues) == 4


def test_every_declared_condition_belongs_to_a_registered_call_point() -> None:
    assert set(CONDITIONS) <= call_points()
    assert {condition for conditions in CONDITIONS.values() for condition in conditions} == {HIGH_RISK, FRONTEND}


def test_the_same_model_must_have_the_same_price_in_every_alias() -> None:
    same = Settings.from_data(base(), {"models": {"opus-mid": {"price": {"input": 4, "output": 20}}}})
    assert same.issues() == []
    other = Settings.from_data(base(), {"models": {"opus-mid": {"price": {"input": 5, "output": 25}}}})
    assert other.issues() == ["models.opus-mid.price：与 opus 同为 claude/opus，价格不一致"]


def test_unknown_keys_are_reported_and_a_bad_structure_skips_the_semantic_checks() -> None:
    upper = {"bondaries": {"changeCap": {"files": 1}}, "models": {"opus": {"modle": "x"}, "bare": {"model": "m"}},
             "controls": {"assess.refute": {"model": "opus-mid"}}}  # 与 assess.triage 同模型属于语义检查，先不报
    assert Settings.from_data(base(), upper).issues() == [
        "bondaries（layer1）：不认识的键", "models.opus.modle：不认识的键", "models.bare.tool：缺少"]


def test_mandatory_gates_cannot_be_configured() -> None:
    settings = Settings.from_data(base(), {"boundaries": {"gates": {"over_cap": "auto", "merge": "manual"}}})
    assert settings.issues() == ["boundaries.gates.over_cap：必须人工的关卡，不能配置"]


def test_booleans_are_not_counts() -> None:
    issues = Settings.from_data(base(), {"controls": {"implement.code": {"turns": True}}}).issues()
    assert "controls.implement.code.turns：类型不对：True" in issues


def test_the_global_default_is_required() -> None:
    data = base()
    data["controls"].pop("*")
    assert "controls.*：缺少全局缺省" in Settings.from_data(data).issues()


def test_load_raises_with_all_issues(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, base(), controls={"controls": {"implement.code": {"turns": "x", "access": "admin"}}})
    with pytest.raises(SettingsInvalid) as raised:
        Settings.load(tool)
    assert len(raised.value.issues) == 2


def test_unreadable_files_are_reported(tmp_path: Path) -> None:
    tool = tool_root(tmp_path, base())
    tool.controls.write_text("{oops", encoding="utf-8")
    with pytest.raises(SettingsInvalid, match="不是合法的 JSON"):
        Settings.load(tool)
    tool.controls.write_text("[]", encoding="utf-8")
    with pytest.raises(SettingsInvalid, match="顶层必须是对象"):
        Settings.load(tool)
    with pytest.raises(SettingsInvalid, match="文件不存在"):
        Settings.load(ToolLayout(tmp_path / "nowhere"))


# 独立性：审查者与生成者不能用同一个模型


def test_reviewers_must_differ_from_producers() -> None:
    assert Settings.from_data(base()).issues() == []
    same = Settings.from_data(base(), {"controls": {"assess.refute": {"model": "opus-mid"}}})
    # opus 与 opus-mid 推理强度不同，模型相同，仍算同一个
    assert same.issues() == ["controls.assess.refute：与 assess.triage 用了同一个模型 claude/opus"]


def test_independence_checks_the_condition_variants() -> None:
    settings = Settings.from_data(base(), {"controls": {"implement.code": {"modelWhen": {"high_risk": "flash"}}}})
    assert settings.issues() == ["controls.implement.review.deep：与 implement.code 用了同一个模型 agy/gemini-flash"]


def test_a_single_model_for_everything_fails_the_independence_check() -> None:
    data = base()
    data["controls"] = {"*": dict(GLOBAL)}
    assert len(Settings.from_data(data).issues()) == 2


# 门槛上下级


def test_auto_approval_must_not_exceed_the_change_cap() -> None:
    settings = Settings.from_data(base(), {"boundaries": {"autoApprove": {"lines": 500}}})
    assert settings.issues() == ["boundaries.autoApprove.lines：500 超过改动量上限 400"]
    assert Settings.from_data(base(), {"boundaries": {"autoApprove": {"lines": 400}}}).issues() == []  # 含端点


def test_lower_time_limits_must_not_exceed_the_upper_ones() -> None:
    controls = {"implement.code": {"timeout": "3h"}, "assess": {"timeout": "5h"}}
    settings = Settings.from_data(base(), {"controls": controls})
    issues = settings.issues()
    # 下级时限之和不超过上级：implement 下各小步骤之和超过对象级 2h
    assert any(item.startswith("controls.implement.timeout：下级时限之和") and item.endswith("超过上级 2h")
               for item in issues)
    assert "controls.assess.timeout：超过一次完整运行的时限 4h" in issues


def test_the_settings_hash_follows_the_merged_values() -> None:
    first, second = Settings.from_data(base()), Settings.from_data(base(), {"limits": {"lock": {"stale": "60s"}}})
    assert first.hash != second.hash and first.hash == Settings.from_data(base()).hash
