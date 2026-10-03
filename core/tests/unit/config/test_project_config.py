import copy

import pytest

from tightrein.config import layers, project
from tightrein.config.capabilities import CapabilityError, ModelChoice
from tightrein.config.project import ConfigError, ConfigIssue, ExtensionSetting, MissingSetting
from tightrein.domain.enums import ExtensionMode, ExtensionPoint, LogLevel, Stage
from tightrein.store.files import yaml_text
from tightrein.store.retention import RetentionPolicy

PROJECT_YAML = """\
project:
  name: sample
  repo: /Users/me/Projects/sample
  mainBranch: main
target:
  baseUrl: https://staging.example.test
  healthcheck: /api/health
accounts:
  roles:
    Company:
      keychain: tightrein.sample.company
    Personal:
      keychain: tightrein.sample.personal
  login:
    endpoint: /api/Account/Login
    bodyTemplate:
      userName: "{account}"
      password: "{password}"
    tokenPath: data.token
localRun:
  ports:
    api: 5200
stacks: [webstack]
extensions:
  log-platform:
    use: core/loki
    options:
      url: https://logs.example.test
  authz-roles:
    command: ["{python}", authz_roles.py]
    options:
      matrixFile: src/Auth/PermissionMatrix.cs
  log-parse:
    command: ["{python}", log_parse_frames.py]
    mode: extend
    timeoutSeconds: 60
  page-routes:
    enabled: false
stages:
  triage:
    tool: claude
    capability: deep
    refuter:
      tool: claude
      model: claude-sonnet
  fix:
    tool: claude
    capability: deep
    review:
      light:
        capability: deep
      deep:
        tool: codex
        capability: deep
capabilities:
  deep:
    claude:
      model: claude-opus
      inputUsdPerMTok: 15
      outputUsdPerMTok: 75
    codex:
      model: gpt-5
      inputUsdPerMTok: 1.25
      outputUsdPerMTok: 10
schedule:
  tick:
    weekdays: [1, 2, 3, 4, 5]
    minutes: [0, 30]
  nonWorkingDays: [2026-10-12]
evaluation:
  judge:
    runner: codex
    capability: deep
  budgetUsd: 20
thresholds:
  suppressionDays: {value: 30, min: 7, max: 90}
  reproduceAttempts: {value: 4, min: 2, max: 8}
  retention:
    rawDays: {value: 14, min: 7, max: 60}
  triage:
    deferredReopenOccurrences: {value: 3, min: 1, max: 10}
  retrieval:
    contextLimits:
      static-review: {value: 25, min: 5, max: 40}
"""


@pytest.fixture
def data():
    return yaml_text.load(PROJECT_YAML)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(PROJECT_YAML, encoding="utf-8")
    return project.load(path)


def issues_of(data):
    return [str(issue) for issue in project.check(data)]


def test_a_valid_file_loads(config, tmp_path):
    assert config.path == tmp_path / "project.yaml"
    assert (config.name, str(config.repo), config.main_branch) == ("sample", "/Users/me/Projects/sample", "main")
    assert config.get("schedule.nonWorkingDays") == ["2026-10-12"]
    assert config.get("stages.fix.review.deep.tool") == "codex"


def test_missing_required_keys_are_reported_with_full_names(data):
    del data["project"]["repo"]
    del data["accounts"]["login"]["tokenPath"]
    del data["target"]["baseUrl"]
    assert issues_of(data) == [
        "accounts.login.tokenPath: 缺少必填项",
        "project.repo: 缺少必填项",
        "target.baseUrl: 缺少必填项",
    ]


def test_only_project_is_required_and_the_rest_falls_back_to_defaults(tmp_path):
    config = project.parse({"project": {"name": "sample", "repo": "/tmp/sample", "mainBranch": "main"}},
                           tmp_path / "project.yaml")
    assert config.whole_threshold("suppressionDays") == 30
    assert config.whole_threshold("triage.deferredReopenOccurrences") == 3
    assert config.get("evaluation.judge") == {"capability": None}  # 核心不给缺省工具
    assert config.get("evaluation.budgetUsd") == 10
    assert (config.base_url, config.roles()) == (None, ())
    with pytest.raises(MissingSetting):
        config.keychain_item("Admin")


def test_issue_tracker_defaults_to_local_and_github_settings_are_checked(data, tmp_path):
    config = project.parse({"project": {"name": "sample", "repo": "/tmp/sample", "mainBranch": "main"}},
                           tmp_path / "project.yaml")
    assert config.issue_tracker == "local"
    assert (config.get("issues.github.privateOnly"), config.get("gates.mirror-writes")) == (True, "user")
    data["issues"] = {"tracker": "github", "github": {"repo": "owner/name"}}
    config = project.parse(data, tmp_path / "project.yaml")
    assert (config.issue_tracker, config.get("issues.github.repo"), config.get("issues.github.labelPrefix")) == (
        "github", "owner/name", "tightrein:")
    data["issues"] = {"tracker": "gitlab", "github": {"repo": "not a repo"}}
    assert [issue.split(":")[0] for issue in issues_of(data)] == ["issues.github.repo", "issues.tracker"]


def test_unknown_keys_and_wrong_values_are_reported(data):
    data["thresholds"]["unknownLimit"] = {"value": 1, "min": 0, "max": 2}
    data["thresholds"]["reproduceAttempts"] = {"value": 4}
    data["schedule"]["tick"]["minutes"] = [0, 75]
    assert issues_of(data) == [
        "schedule.tick.minutes[1]: 75 is greater than the maximum of 59",
        "thresholds.reproduceAttempts.max: 缺少必填项",
        "thresholds.reproduceAttempts.min: 缺少必填项",
        "thresholds.unknownLimit: 不认识的键",
    ]


def test_thresholds_outside_min_and_max_are_rejected(data):
    data["thresholds"]["reproduceAttempts"] = {"value": 9, "min": 2, "max": 8}
    data["thresholds"]["retrieval"]["contextLimits"]["static-review"] = {"value": 2, "min": 5, "max": 40}
    data["thresholds"]["suppressionDays"] = {"value": 30, "min": 90, "max": 7}
    assert issues_of(data) == [
        "thresholds.reproduceAttempts: value 9 越出 [2, 8]",
        "thresholds.retrieval.contextLimits.static-review: value 2 越出 [5, 40]",
        "thresholds.suppressionDays: min 90 大于 max 7",
    ]


def test_threshold_bounds_are_inclusive(data):
    data["thresholds"]["reproduceAttempts"] = {"value": 8, "min": 2, "max": 8}
    data["thresholds"]["suppressionDays"] = {"value": 7, "min": 7, "max": 7}
    assert issues_of(data) == []


def test_autonomy_size_gates_must_not_exceed_the_change_cap(data):
    data["thresholds"]["change"] = {"maxFiles": {"value": 2, "min": 1, "max": 20}}
    data["thresholds"]["autonomy"] = {"planMaxLines": {"value": 550, "min": 10, "max": 1000}}
    assert issues_of(data) == [
        "thresholds.autonomy.planMaxFiles: value 3 大于 thresholds.change.maxFiles 的 2",
        "thresholds.autonomy.planMaxLines: value 550 大于 thresholds.change.maxLines 的 200",
    ]


def test_nested_learn_thresholds_are_checked(data):
    data["thresholds"]["learn"] = {"controls": {"firstPassFloor": {"value": 0.05, "min": 0.1, "max": 1}}}
    assert issues_of(data) == ["thresholds.learn.controls.firstPassFloor: value 0.05 越出 [0.1, 1]"]


def test_capabilities_referenced_by_stages_must_be_mapped(data):
    data["stages"]["fix"]["session"] = {"tool": "agy", "capability": "deep"}
    data["evaluation"]["judge"] = {"runner": "agy", "capability": "deep"}
    assert issues_of(data) == [
        "evaluation.judge: capabilities.deep.agy: 能力档 deep 没有为工具 agy 配置模型",
        "stages.fix.session: capabilities.deep.agy: 能力档 deep 没有为工具 agy 配置模型",
    ]


def test_reviewers_must_differ_from_the_producer(data):
    data["roleCapabilities"] = {"claim-verifier": "deep", "fix-executor": "deep"}
    data["stages"]["triage"]["refuter"] = {"tool": "claude", "model": "claude-opus"}
    data["stages"]["fix"]["review"] = {"light": {"capability": "deep"}, "deep": {"capability": "deep"}}
    assert issues_of(data) == [
        "stages.fix.review.deep: 须与生成者 fix-executor(工具 claude，模型 claude-opus)使用不同的工具或模型："
        "只用一种工具时给两者不同的模型档，例如 stages.fix.review.deep.capability: standard；"
        "用两种工具时可写 stages.fix.review.deep.tool 为另一种工具",
        "stages.triage.refuter: 须与生成者 claim-verifier(工具 claude，模型 claude-opus)使用不同的工具或模型："
        "只用一种工具时给两者不同的模型档，例如 stages.triage.refuter.capability: standard；"
        "用两种工具时可写 stages.triage.refuter.tool 为另一种工具",
    ]


def test_conflicting_capability_prices_are_reported(data):
    data["capabilities"]["search"] = {"claude": {"model": "claude-opus", "inputUsdPerMTok": 1, "outputUsdPerMTok": 2}}
    assert issues_of(data) == ["capabilities.search.claude: 模型 claude-opus 的价格与其他能力档中的不一致"]


def test_load_raises_with_all_issues(tmp_path, data):
    path = tmp_path / "project.yaml"
    path.write_text(PROJECT_YAML.replace("  mainBranch: main\n", ""), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        project.load(path)
    assert caught.value.path == path
    assert caught.value.issues == (ConfigIssue("project.mainBranch", "缺少必填项"),)
    assert str(caught.value) == f"{path} 校验失败：\nproject.mainBranch: 缺少必填项"


@pytest.mark.parametrize("text, reason", [
    (None, "文件不存在"),
    ("project: [\n", "YAML 无法解析"),
    ("- a\n- b\n", "顶层必须是映射"),
])
def test_unreadable_files(tmp_path, text, reason):
    path = tmp_path / "project.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        project.load(path)
    assert [issue.key for issue in caught.value.issues] == ["(顶层)"]
    assert caught.value.issues[0].reason.startswith(reason)


def test_get_falls_back_to_documented_defaults(config):
    assert config.get("localRun.ports.api") == 5200
    assert config.get("evaluation.repeats") == 3
    assert config.get("sources.platform-errors.logLimit") == 5000
    with pytest.raises(MissingSetting) as caught:
        config.get("localRun.ports.frontend")
    assert caught.value.key == "localRun.ports.frontend"


def test_get_falls_back_to_core_defaults_after_the_project(config, data, tmp_path, monkeypatch):
    monkeypatch.setattr(layers, "core_defaults", lambda: {"target": {"healthTimeoutSeconds": 10},
                                                           "evaluation": {"repeats": 7}})
    assert config.get("target.healthTimeoutSeconds") == 10
    assert config.get("evaluation.repeats") == 7
    data["target"]["healthTimeoutSeconds"] = 3
    assert project.parse(data, tmp_path / "project.yaml").get("target.healthTimeoutSeconds") == 3


def test_thresholds_use_configured_values_then_defaults(config):
    assert config.threshold("reproduceAttempts") == 4
    assert config.tunable("reproduceAttempts") == 4
    assert config.threshold("retrieval.contextLimits.static-review") == 25
    assert config.threshold("retrieval.contextLimits.triage") == 15
    assert config.whole_threshold("tiers.micro.maxLines") == 30
    assert config.whole_threshold("resolveCoveredRuns") == 3
    with pytest.raises(MissingSetting) as caught:
        config.threshold("improve.shareLimit")
    assert caught.value.key == "thresholds.improve.shareLimit"


def test_thresholds_fall_back_to_core_defaults(config, data, tmp_path, monkeypatch):
    monkeypatch.setattr(layers, "core_defaults", lambda: {
        "thresholds": {"triage": {"evidenceRetries": {"value": 2, "min": 0, "max": 5}}}})
    assert config.tunable("triage.evidenceRetries") == 2
    assert config.whole_threshold("triage.evidenceRetries") == 2
    data["thresholds"]["triage"]["evidenceRetries"] = {"value": 1, "min": 0, "max": 5}
    assert project.parse(data, tmp_path / "project.yaml").whole_threshold("triage.evidenceRetries") == 1


def test_core_defaults_file_has_the_triage_keys(config):
    assert config.whole_threshold("triage.dedupCandidates") == 10
    assert config.role_capability(Stage.TRIAGE, "refuter") == "strong"
    assert config.role_capability(Stage.TRIAGE, "dedup") == "light"
    assert config.get("stages.triage.roles.refuter.limits.low") == {"maxTurns": 40, "maxDurationMs": 900000}
    assert config.get("triage.treatment.rules")[-1] == {"treatment": "observe"}
    assert config.role_capability(Stage.COLLECT, "static-review") == "strong"
    assert config.role_capability(Stage.TRIAGE, "no-such-role") is None


def test_whole_thresholds_reject_fractions(data, tmp_path):
    data["thresholds"]["retention"]["rawDays"] = {"value": 14.5, "min": 7, "max": 60}
    config = project.parse(data, tmp_path / "project.yaml")
    with pytest.raises(ConfigError) as caught:
        config.retention_policy()
    assert [str(issue) for issue in caught.value.issues] == ["thresholds.retention.rawDays: 须为整数：14.5"]


def test_retention_policy_reads_the_thresholds(config):
    assert config.retention_policy() == RetentionPolicy(signals_days=90, raw_days=14, logs_days=90, fixes_days=90)
    assert project.core_config().retention_policy() == RetentionPolicy()


def test_roles_and_keychain_items(config):
    assert config.roles() == ("Company", "Personal")
    assert config.keychain_item("Company") == "tightrein.sample.company"
    with pytest.raises(MissingSetting):
        config.keychain_item("Admin")


def test_model_choice_for_stages_and_overrides(config):
    assert config.model_choice(Stage.TRIAGE) == ModelChoice("claude", "claude-opus", "deep")
    assert config.model_choice(Stage.FIX, "review.deep") == ModelChoice("codex", "gpt-5", "deep")
    assert config.model_choice(Stage.FIX, "review.light") == ModelChoice("claude", "claude-opus", "deep")
    assert config.model_choice(Stage.TRIAGE, "refuter") == ModelChoice("claude", "claude-sonnet")
    assert config.model_choice(Stage.FIX, tool="codex") == ModelChoice("codex", "gpt-5", "deep")
    assert config.model_choice(Stage.FIX, model="claude-haiku") == ModelChoice("claude", "claude-haiku")
    assert config.capabilities.estimate_cost("codex", "gpt-5", 1_000_000, 0) == 1.25


def test_parse_does_not_modify_the_input(data, tmp_path):
    before = copy.deepcopy(data)
    project.parse(data, tmp_path / "project.yaml")
    assert data == before


def test_stacks_and_extension_settings(config):
    assert config.stacks == ("webstack",)
    assert config.extension(ExtensionPoint.LOG_PLATFORM) == ExtensionSetting(
        use="core/loki", options={"url": "https://logs.example.test"})
    assert config.extension(ExtensionPoint.AUTHZ_ROLES) == ExtensionSetting(
        command=("{python}", "authz_roles.py"), options={"matrixFile": "src/Auth/PermissionMatrix.cs"})
    assert config.extension(ExtensionPoint.LOG_PARSE) == ExtensionSetting(
        command=("{python}", "log_parse_frames.py"), mode=ExtensionMode.EXTEND, timeout_seconds=60)
    assert config.extension(ExtensionPoint.PAGE_ROUTES) == ExtensionSetting(enabled=False)
    assert config.extension(ExtensionPoint.SPEC_EXPORT) == ExtensionSetting()


def test_stacks_and_extensions_may_be_omitted(data, tmp_path):
    del data["stacks"]
    del data["extensions"]
    config = project.parse(data, tmp_path / "project.yaml")
    assert config.stacks == ()
    assert config.extension(ExtensionPoint.LOG_PLATFORM) == ExtensionSetting()


def test_extension_entries_are_checked(data):
    data["stacks"] = ["webstack", "../stacks", "webstack"]
    data["extensions"]["api-docs"] = {"command": ["x"]}
    data["extensions"]["spec-export"] = {"command": ["{python}", "spec.py"], "mode": "extend"}
    data["extensions"]["static-tools"] = {"mode": "extend"}
    data["extensions"]["local-run"] = {"timeoutSeconds": 0}
    data["extensions"]["page-routes"] = {"use": "manual-list"}
    assert issues_of(data) == [
        "extensions.api-docs: 不认识的键",
        "extensions.local-run.timeoutSeconds: 0 is less than the minimum of 1",
        "extensions.page-routes.use: 'manual-list' does not match '^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$'",
        "extensions.spec-export.mode: 'replace' was expected",
        "extensions.static-tools.command: 缺少必填项",
        "stacks: ['webstack', '../stacks', 'webstack'] has non-unique elements",
        "stacks[1]: '../stacks' does not match '^[a-z0-9][a-z0-9-]*$'",
    ]


def test_use_and_command_are_mutually_exclusive(data):
    data["extensions"]["authz-roles"]["use"] = "core/manual-matrix"
    assert issues_of(data) == ["extensions.authz-roles.use: use 与 command 只能二选一"]


def test_error_levels_default_to_error_and_critical(config, data, tmp_path):
    assert config.error_levels() == (LogLevel.ERROR, LogLevel.CRITICAL)
    data["sources"] = {"platform-errors": {"levels": ["warning", "error"], "initialLookbackHours": 6}}
    configured = project.parse(data, tmp_path / "project.yaml")
    assert configured.error_levels() == (LogLevel.WARNING, LogLevel.ERROR)
    assert configured.get("sources.platform-errors.initialLookbackHours") == 6


def test_project_probes_and_api_fuzz_checks_are_checked(data):
    data["sources"] = {"project-probes": [{"name": "Daily", "command": [], "every": "2w"}],
                      "api-fuzz": {"checks": {"responseSchema": "sometimes"}}}
    assert [issue.split(":")[0] for issue in issues_of(data)] == [
        "sources.api-fuzz.checks.responseSchema", "sources.project-probes[0].command",
        "sources.project-probes[0].every", "sources.project-probes[0].name"]


def test_core_defaults_are_checked_without_the_required_keys(tmp_path):
    assert set(layers.core_defaults()) <= set(layers.validator().schema["properties"])
    path = tmp_path / "defaults.yaml"
    path.write_text("methods:\n  core/loki: {pageSize: 500}\ntarget: {healthTimeoutSeconds: 5}\n",
                    encoding="utf-8")
    assert layers.load_defaults(path) == {"methods": {"core/loki": {"pageSize": 500}},
                                           "target": {"healthTimeoutSeconds": 5}}
    path.write_text("methods:\n  loki: {}\nstages: {deploy: {}}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        layers.load_defaults(path)
    assert [issue.key for issue in caught.value.issues] == ["methods", "stages"]


def test_method_options_take_project_values_over_core_defaults(data, tmp_path, monkeypatch):
    monkeypatch.setattr(layers, "core_defaults", lambda: {"methods": {"core/loki": {
        "pageSize": 1000, "timeoutSeconds": 30}}})
    data["methods"] = {"core/loki": {"pageSize": 500}}
    config = project.parse(data, tmp_path / "project.yaml")
    assert config.method_options("core/loki") == {"pageSize": 500, "timeoutSeconds": 30}
    assert config.method_options("core/regex") == {}



def test_core_config_reads_only_the_core_defaults():
    core = project.core_config()
    assert core.get("packaging.targets.codex") == "~/.agents/skills"
    assert core.get("schedule.tick") == {"weekdays": [1, 2, 3, 4, 5], "minutes": [0, 15, 30, 45]}
    assert core.whole_threshold("learn.thirdPartyMinStars") == 5000


def test_the_project_tick_replaces_the_default_tick_as_a_whole(config):
    assert "hours" not in project.core_config().get("schedule.tick")
    assert config.get("schedule.tick") == config.data["schedule"]["tick"]


MINIMAL = {"project": {"name": "sample", "repo": "/tmp/sample", "mainBranch": "main"}}


def tier(model, price=1):
    return {"model": model, "inputUsdPerMTok": price, "outputUsdPerMTok": price}


SINGLE_TOOL = {
    "defaultTool": "claude",
    "capabilities": {"light": {"claude": tier("haiku")}, "standard": {"claude": tier("sonnet", 3)},
                     "strong": {"claude": tier("opus", 15)}},
    "roleCapabilities": {"refuter": "standard"},
    "stages": {"fix": {"review": {"deep": {"capability": "standard"}}}},
}


def test_the_user_agent_layer_gives_tools_and_models_to_every_stage(tmp_path):
    config = project.parse(MINIMAL, tmp_path / "project.yaml", agents=SINGLE_TOOL)
    assert config.default_tool == "claude"
    assert config.model_choice(Stage.COLLECT) == ModelChoice("claude", None)
    assert config.model_choice(Stage.FIX, "review.light") == ModelChoice("claude", "sonnet", "standard")
    assert config.model_choice(Stage.FIX, "review.deep") == ModelChoice("claude", "sonnet", "standard")
    assert config.role_capability(Stage.TRIAGE, "refuter") == "standard"
    assert config.capabilities.price("claude", "opus").input_usd_per_mtok == 15


def test_the_project_overrides_the_user_agent_layer(tmp_path):
    data = {**MINIMAL, "stages": {"fix": {"tool": "codex", "review": {"deep": {"capability": "strong"}}}},
            "capabilities": {"strong": {"codex": tier("gpt-5.5", 2)}, "standard": {"codex": tier("gpt-5.5", 2)}}}
    agents = {**SINGLE_TOOL, "stages": {"fix": {"model": "opus", "review": {"deep": {"tool": "claude"}}}}}
    config = project.parse(data, tmp_path / "project.yaml", agents=agents)
    assert config.stage_setting(Stage.FIX)["tool"] == "codex"
    assert "model" not in config.stage_setting(Stage.FIX)  # 用户层的模型属于原工具，不再沿用
    assert config.model_choice(Stage.FIX, "review.deep") == ModelChoice("claude", "opus", "strong")
    assert config.model_choice(Stage.TRIAGE) == ModelChoice("claude", None)
    assert config.capabilities.model("strong", "claude") == "opus"


def test_without_any_tool_the_error_names_the_keys(tmp_path):
    config = project.parse(MINIMAL, tmp_path / "project.yaml")
    with pytest.raises(CapabilityError) as caught:
        config.model_choice(Stage.TRIAGE)
    assert caught.value.key == "stages.triage.tool"
    assert "agents.defaultTool" in caught.value.reason and "agents: {defaultTool: claude}" in caught.value.reason


def test_a_single_tool_needs_different_tiers_for_reviewers(tmp_path):
    project.parse(MINIMAL, tmp_path / "project.yaml", agents=SINGLE_TOOL)
    same = {**SINGLE_TOOL, "roleCapabilities": {}, "stages": {}}
    with pytest.raises(ConfigError) as caught:
        project.parse(MINIMAL, tmp_path / "project.yaml", agents=same)
    assert [issue.key for issue in caught.value.issues] == ["stages.fix.review.deep", "stages.triage.refuter"]
    assert "工具 claude，模型 opus" in caught.value.issues[0].reason


def test_a_second_tool_may_review_and_the_refuter_may_give_only_a_tier(tmp_path):
    agents = {**SINGLE_TOOL, "roleCapabilities": {},
              "capabilities": {**SINGLE_TOOL["capabilities"], "strong": {"claude": tier("opus", 15),
                                                                          "codex": tier("gpt-5.5", 2)}},
              "stages": {"triage": {"refuter": {"tool": "codex"}}, "fix": {"review": {"deep": {"tool": "codex"}}}}}
    config = project.parse(MINIMAL, tmp_path / "project.yaml", agents=agents)
    assert config.model_choice(Stage.FIX, "review.deep") == ModelChoice("codex", "gpt-5.5", "strong")
    only_tier = {**SINGLE_TOOL, "roleCapabilities": {}, "stages": {"triage": {"refuter": {"capability": "light"}},
                                                                   "fix": {"review": {"deep": {"capability": "light"}}}}}
    config = project.parse(MINIMAL, tmp_path / "project.yaml", agents=only_tier)
    assert config.model_choice(Stage.TRIAGE, "refuter") == ModelChoice("claude", "haiku", "light")


TWO_TOOLS = {
    **SINGLE_TOOL,
    "capabilities": {"light": {"claude": tier("haiku"), "agy": tier("flash-low", 0.5)},
                     "standard": {"claude": tier("sonnet", 3), "agy": tier("flash-medium", 0.5)},
                     "strong": {"claude": tier("opus", 15), "agy": tier("flash-high", 0.5)}},
}


def test_roles_and_tasks_may_choose_their_own_tool_and_model(tmp_path):
    agents = {**TWO_TOOLS, "stages": {**SINGLE_TOOL["stages"],
                                      "fix": {**SINGLE_TOOL["stages"]["fix"],
                                              "roles": {"fix-scout": {"tool": "agy"}, "fix-planner": {"model": "opus"}}},
                                      "triage": {"tasks": {"dedup": {"tool": "agy", "model": "flash-low"}}}}}
    data = {**MINIMAL, "stages": {"fix": {"roles": {"fix-scout": {"tool": "claude", "limits": {"low": {"maxTurns": 5}}}}}}}
    config = project.parse(MINIMAL, tmp_path / "project.yaml", agents=agents)
    assert config.role_agent(Stage.FIX, "fix-scout") == {"tool": "agy"}
    assert config.role_agent(Stage.FIX, "fix-planner") == {"model": "opus", "tool": "claude"}  # 模型绑定环节工具
    assert config.role_agent(Stage.TRIAGE, "dedup") == {"tool": "agy", "model": "flash-low"}
    agents["stages"]["fix"]["roles"]["fix-scout"]["model"] = "flash-low"
    overridden = project.parse(data, tmp_path / "project.yaml", agents=agents)
    assert overridden.role_agent(Stage.FIX, "fix-scout") == {"tool": "claude"}  # 项目改写工具，不沿用原工具的模型


def test_role_tools_need_their_tier_mapped(tmp_path):
    agents = {**SINGLE_TOOL, "stages": {**SINGLE_TOOL["stages"], "fix": {**SINGLE_TOOL["stages"]["fix"],
                                                                         "roles": {"fix-scout": {"tool": "agy"}}}}}
    with pytest.raises(ConfigError) as caught:
        project.parse(MINIMAL, tmp_path / "project.yaml", agents=agents)
    assert [str(issue) for issue in caught.value.issues] == [
        "stages.fix.roles.fix-scout: capabilities.light.agy: 能力档 light 没有为工具 agy 配置模型"]


def test_independence_follows_the_resolved_role_tools(tmp_path):
    same = {**TWO_TOOLS, "roleCapabilities": {"refuter": "standard"},
            "stages": {"triage": {"refuter": {"tool": "agy"}, "roles": {"claim-verifier": {"tool": "agy",
                                                                                          "capability": "standard"}}},
                       "fix": {"review": {"deep": {"tool": "agy"}}}}}
    with pytest.raises(ConfigError) as caught:
        project.parse(MINIMAL, tmp_path / "project.yaml", agents=same)
    assert [issue.key for issue in caught.value.issues] == ["stages.triage.refuter"]
    assert "claim-verifier(工具 agy，模型 flash-medium)" in caught.value.issues[0].reason
    by_role = {**same, "stages": {**same["stages"], "triage": {"roles": {"claim-verifier": {"tool": "agy"},
                                                                         "refuter": {"tool": "claude"}}}}}
    config = project.parse(MINIMAL, tmp_path / "project.yaml", agents=by_role)
    assert config.role_agent(Stage.TRIAGE, "refuter", overlay=config.stage_setting(Stage.TRIAGE).get("refuter")) \
        == {"tool": "claude"}


def test_list_settings_may_be_appended_with_a_plus_key(tmp_path):
    key = "stages.fix.roles.frontend-designer.paths"
    config = project.parse(MINIMAL, tmp_path / "project.yaml")
    assert "*.vue" in config.get(key) and "components/" in config.get(key)
    data = {**MINIMAL, "stages": {"fix": {"roles": {"frontend-designer": {"paths+": ["web/"]}}}}}
    appended = project.parse(data, tmp_path / "project.yaml").get(key)
    assert appended[:-1] == config.get(key) and appended[-1] == "web/"
    data = {**MINIMAL, "stages": {"fix": {"roles": {"frontend-designer": {"paths": ["ui/"], "paths+": ["web/"]}}}}}
    assert project.parse(data, tmp_path / "project.yaml").get(key) == ["ui/", "web/"]
