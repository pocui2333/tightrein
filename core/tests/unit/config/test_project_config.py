import copy

import pytest

from tightrein.config import layers, project
from tightrein.config.routes import LEGACY_HINT, ModelChoice, RouteError
from tightrein.config.project import ConfigError, ConfigIssue, ExtensionSetting, MissingSetting
from tightrein.domain.enums import ExtensionMode, ExtensionPoint, LogLevel
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
  fix:
    review:
      deep:
        limits: {maxTurns: 50}
models:
  opus: {tool: claude, model: claude-opus, effort: high, inputUsdPerMTok: 15, outputUsdPerMTok: 75}
  sonnet: {tool: claude, model: claude-sonnet}
  gpt: {tool: codex, model: gpt-5, inputUsdPerMTok: 1.25, outputUsdPerMTok: 10}
routes:
  default: opus
  triage.refuter: sonnet
  fix.review.deep: gpt
  eval.judge: gpt
schedule:
  tick:
    weekdays: [1, 2, 3, 4, 5]
    minutes: [0, 30]
  nonWorkingDays: [2026-10-12]
evaluation:
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
    assert config.get("stages.fix.review.deep.limits") == {"maxTurns": 50}


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
    assert config.routes.routes == {} and config.routes.aliases == {}  # 核心不给缺省路由
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


def test_routes_must_name_known_call_points_and_aliases(data):
    data["routes"]["fix.unknown"] = "opus"
    data["routes"]["fix.session"] = "missing"
    assert issues_of(data) == [
        "routes.fix.session: 别名 missing 没有在 models 中定义",
        "routes.fix.unknown: 不认识的调用点；调用点与条件见 `tightrein project config --routes`",
    ]


def test_reviewers_must_differ_from_the_producer(data):
    data["routes"] = {"default": "opus"}
    assert issues_of(data) == [
        "routes.fix.review.deep: 须与 fix.executor 使用不同的工具或模型(两者都解析为工具 claude、模型 claude-opus)："
        "把 routes.fix.review.deep 改为(没写时写上)另一个工具或模型的别名",
        "routes.triage.refuter: 须与 triage.claim-verifier 使用不同的工具或模型(两者都解析为工具 claude、模型 claude-opus)："
        "把 routes.triage.refuter 改为(没写时写上)另一个工具或模型的别名",
    ]


def test_conflicting_prices_are_reported(data):
    data["models"]["opus-mid"] = {"tool": "claude", "model": "claude-opus", "inputUsdPerMTok": 1, "outputUsdPerMTok": 2}
    assert issues_of(data) == ["models.opus-mid: 工具 claude 的模型 claude-opus 的价格与其他别名中的不一致"]


def test_old_model_keys_are_reported_with_the_new_form(data):
    data["defaultTool"] = "claude"
    data["stages"]["fix"]["review"]["deep"]["capability"] = "strong"
    data["evaluation"]["judge"] = {"runner": "codex"}
    assert project.check(data) == [ConfigIssue("defaultTool", LEGACY_HINT), ConfigIssue("evaluation.judge", LEGACY_HINT),
                                   ConfigIssue("stages.fix.review.deep.capability", LEGACY_HINT)]


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
    assert config.get("stages.triage.roles.refuter.limits.low") == {"maxTurns": 40, "maxDurationMs": 900000}
    assert config.get("triage.treatment.rules")[-1] == {"treatment": "observe"}


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


def test_model_choice_for_call_points_and_overrides(config):
    assert config.model_choice("triage.claim-verifier") == ModelChoice("claude", "claude-opus", "high", "opus")
    assert config.model_choice("fix.review.deep") == ModelChoice("codex", "gpt-5", None, "gpt")
    assert config.model_choice("triage.refuter") == ModelChoice("claude", "claude-sonnet", None, "sonnet")
    assert config.model_choice("fix.executor", tool="codex") == ModelChoice("codex", None)
    assert config.model_choice("fix.executor", model="claude-haiku") == ModelChoice("claude", "claude-haiku")
    assert config.routes.estimate_cost("codex", "gpt-5", 1_000_000, 0) == 1.25


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


USER = {
    "models": {"opus": {"tool": "claude", "model": "opus", "effort": "high", "inputUsdPerMTok": 15,
                        "outputUsdPerMTok": 75},
               "flash": {"tool": "agy", "model": "flash-high"}},
    "routes": {"default": "opus", "triage.refuter": "flash", "fix.review.deep": "flash"},
}


def test_the_user_routing_layer_gives_models_to_every_call_point(tmp_path):
    config = project.parse(MINIMAL, tmp_path / "project.yaml", routing=USER)
    assert config.model_choice("collect.static-review") == ModelChoice("claude", "opus", "high", "opus")
    assert config.model_choice("fix.review.deep") == ModelChoice("agy", "flash-high", None, "flash")
    assert config.routes.price("claude", "opus").input_usd_per_mtok == 15


def test_the_project_overrides_the_user_routing_layer_per_key(tmp_path):
    data = {**MINIMAL, "models": {"flash": {"tool": "agy", "model": "flash-low"}, "gpt": {"tool": "codex"}},
            "routes": {"fix.planner": "gpt"}}
    config = project.parse(data, tmp_path / "project.yaml", routing=USER)
    assert config.model_choice("fix.planner") == ModelChoice("codex", None, None, "gpt")
    assert config.model_choice("fix.review.deep").model == "flash-low"  # 项目的别名整体替换用户的同名别名
    assert config.model_choice("fix.executor").alias == "opus"


def test_without_any_route_the_error_names_the_call_point(tmp_path):
    config = project.parse(MINIMAL, tmp_path / "project.yaml")
    with pytest.raises(RouteError) as caught:
        config.model_choice("triage.claim-verifier")
    assert caught.value.key == "routes.triage.claim-verifier"
    assert "routes.default 或 routes.triage.claim-verifier" in caught.value.reason


def test_a_single_model_for_everything_fails_the_independence_check(tmp_path):
    same = {**USER, "routes": {"default": "opus"}}
    with pytest.raises(ConfigError) as caught:
        project.parse(MINIMAL, tmp_path / "project.yaml", routing=same)
    assert [issue.key for issue in caught.value.issues] == ["routes.fix.review.deep", "routes.triage.refuter"]


def test_independence_checks_the_condition_variants(tmp_path):
    variant = {**USER, "routes": {**USER["routes"], "fix.executor.high-risk": "flash"}}
    with pytest.raises(ConfigError) as caught:
        project.parse(MINIMAL, tmp_path / "project.yaml", routing=variant)
    assert [issue.key for issue in caught.value.issues] == ["routes.fix.review.deep"]
    assert "fix.executor.high-risk" in caught.value.issues[0].reason


def test_list_settings_may_be_appended_with_a_plus_key(tmp_path):
    key = "stages.fix.roles.frontend-designer.paths"
    config = project.parse(MINIMAL, tmp_path / "project.yaml")
    assert "*.vue" in config.get(key) and "components/" in config.get(key)
    data = {**MINIMAL, "stages": {"fix": {"roles": {"frontend-designer": {"paths+": ["web/"]}}}}}
    appended = project.parse(data, tmp_path / "project.yaml").get(key)
    assert appended[:-1] == config.get(key) and appended[-1] == "web/"
    data = {**MINIMAL, "stages": {"fix": {"roles": {"frontend-designer": {"paths": ["ui/"], "paths+": ["web/"]}}}}}
    assert project.parse(data, tmp_path / "project.yaml").get(key) == ["ui/", "web/"]
