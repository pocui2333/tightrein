from pathlib import Path

import pytest

from tightrein.config import user
from tightrein.config.project import ConfigError
from tightrein.config.user import InstallTarget, UserConfig

CONFIG_YAML = """\
branchPrefix: cty
defaultWorkspace: ~/Projects/tightrein/workspaces/sample
notify:
  method: none
tools:
  claude:
    path: ~/.local/bin/claude
  codex:
    path: /opt/homebrew/bin/codex
install:
  targets:
    claude:
      enabled: false
    codex:
      path: ~/.agents/skills
"""


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def write(home, text):
    path = user.default_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_default_path_is_under_the_injected_home(home):
    assert user.default_path(home) == home / ".config" / "tightrein" / "config.yaml"


def test_missing_or_empty_file_gives_defaults(home, tmp_path):
    config = user.load(home=home)
    assert config == UserConfig(user.default_path(home))
    assert config.notify_method is None
    assert config.tool_path("claude") is None
    assert config.install_target("claude") == InstallTarget()
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert user.load(empty, home) == UserConfig(empty)


def test_a_full_file_is_read_and_home_is_expanded(home):
    path = write(home, CONFIG_YAML)
    config = user.load(home=home)
    assert config.path == path
    assert config.branch_prefix == "cty"
    assert config.default_workspace == home / "Projects" / "tightrein" / "workspaces" / "sample"
    assert config.notify_method == user.NOTIFY_NONE
    assert config.tool_path("claude") == home / ".local" / "bin" / "claude"
    assert config.tool_path("codex") == Path("/opt/homebrew/bin/codex")
    assert config.install_target("claude") == InstallTarget(None, False)
    assert config.install_target("codex") == InstallTarget(home / ".agents" / "skills", True)
    assert config.install_target("agy") == InstallTarget()


def test_an_explicit_path_is_used(tmp_path, home):
    path = tmp_path / "custom.yaml"
    path.write_text("branchPrefix: lc\n", encoding="utf-8")
    assert user.load(path, home).branch_prefix == "lc"


def test_all_problems_are_listed_with_full_keys(home):
    path = write(home, """\
branchPrefix: ""
theme: dark
notify:
  method: email
tools:
  claude: {}
  codex:
    path: 3
install:
  targets:
    claude:
      enabled: "yes"
      scope: user
""")
    with pytest.raises(ConfigError) as caught:
        user.load(home=home)
    assert caught.value.path == path
    assert [issue.key for issue in caught.value.issues] == [
        "branchPrefix", "install.targets.claude.enabled", "install.targets.claude.scope", "notify.method", "theme",
        "tools.claude.path", "tools.codex.path",
    ]
    reasons = {issue.key: issue.reason for issue in caught.value.issues}
    assert (reasons["theme"], reasons["tools.claude.path"]) == ("不认识的键", "缺少必填项")


@pytest.mark.parametrize("text, reason", [
    ("- a\n", "顶层必须是映射"),
    ("notify: [\n", "YAML 无法解析"),
])
def test_unreadable_files(home, text, reason):
    write(home, text)
    with pytest.raises(ConfigError) as caught:
        user.load(home=home)
    assert caught.value.issues[0].key == "(顶层)"
    assert caught.value.issues[0].reason.startswith(reason)


AGENTS_YAML = """\
agents:
  defaultTool: claude
  stages:
    triage:
      refuter: {tool: codex, capability: strong}
    fix:
      review:
        deep: {tool: codex, capability: strong}
  capabilities:
    strong:
      claude: {model: opus, inputUsdPerMTok: 15, outputUsdPerMTok: 75}
      codex: {model: gpt-5.5, effort: high, inputUsdPerMTok: 1.25, outputUsdPerMTok: 10}
  roleCapabilities:
    static-review: light
network:
  proxy: http://127.0.0.1:8118
  noProxy: [github.com, api.github.com]
"""


def test_agents_and_network_are_read(home):
    write(home, AGENTS_YAML)
    config = user.load(home=home)
    assert config.agents["defaultTool"] == "claude"
    assert config.agents["stages"]["triage"]["refuter"] == {"tool": "codex", "capability": "strong"}
    assert config.agents["capabilities"]["strong"]["codex"]["effort"] == "high"
    assert (config.network_proxy, config.no_proxy) == ("http://127.0.0.1:8118", ("github.com", "api.github.com"))


def test_agents_take_only_tool_and_model_keys(home):
    write(home, """\
agents:
  stages:
    fix:
      limits: {maxTurns: 3}
      review:
        deep: {limits: {maxTurns: 3}}
  capabilities:
    strong:
      claude: {model: opus, inputUsdPerMTok: 15, outputUsdPerMTok: 75}
    deep:
      claude: {model: opus, inputUsdPerMTok: 1, outputUsdPerMTok: 75}
network:
  proxy: 127.0.0.1:8118
""")
    with pytest.raises(ConfigError) as caught:
        user.load(home=home)
    assert [issue.key for issue in caught.value.issues] == [
        "agents.stages.fix.limits", "agents.stages.fix.review.deep.limits", "network.proxy"]


def test_capability_prices_must_agree_within_the_user_layer(home):
    write(home, """\
agents:
  capabilities:
    strong:
      claude: {model: opus, inputUsdPerMTok: 15, outputUsdPerMTok: 75}
    deep:
      claude: {model: opus, inputUsdPerMTok: 1, outputUsdPerMTok: 75}
""")
    with pytest.raises(ConfigError) as caught:
        user.load(home=home)
    assert [str(issue) for issue in caught.value.issues] == [
        "agents.capabilities.deep.claude: 模型 opus 的价格与其他能力档中的不一致"]
