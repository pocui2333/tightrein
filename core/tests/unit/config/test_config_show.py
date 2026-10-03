from pathlib import Path

from tightrein.config import project, show
from tightrein.config.user import UserConfig


def test_later_layers_override_earlier_ones(make_config, tmp_path):
    config = make_config(loop={"findLimit": 5})
    user = UserConfig(tmp_path / "config.yaml", branch_prefix="cty", notify_method="none")
    shown = {item.key: item for item in show.show(config, user)}
    assert shown["loop.findLimit"].value == 5 and shown["loop.findLimit"].source == "project"
    assert shown["loop.objectLockMinutes"].value == 120 and shown["loop.objectLockMinutes"].source == "core"
    assert shown["branchPrefix"].source == "user"
    assert shown["notify.method"].value == "none"
    assert shown["thresholds.reproduceAttempts"].source == "core"


def test_key_selects_the_subtree_and_lists_every_layer(make_config, tmp_path):
    config = make_config(loop={"findLimit": 5})
    shown = show.show(config, UserConfig(tmp_path / "config.yaml"), "loop")
    assert [item.key for item in shown] == ["loop.findLimit", "loop.lookbackDays", "loop.maxActiveFixes",
                                           "loop.objectLockMinutes", "loop.watchIntervalSeconds"]
    assert shown[0].layers == {"core": 20, "project": 5}
    assert shown[1].layers == {"core": 7}


def test_tunables_and_lists_are_leaves():
    flat = show.flatten({"thresholds": {"x": {"value": 1, "min": 0, "max": 2}}, "git": {"prefixes": ["a", "b"]}})
    assert flat == {"thresholds.x": {"value": 1, "min": 0, "max": 2}, "git.prefixes": ["a", "b"]}


def test_user_paths_are_shown_as_text(make_config, tmp_path):
    user = UserConfig(tmp_path / "config.yaml", default_workspace=Path("/work/demo"),
                      tools={"claude": Path("/opt/claude")})
    shown = {item.key: item.value for item in show.show(make_config(), user, "tools")}
    assert shown == {"tools.claude.path": "/opt/claude"}


def test_user_agent_keys_sit_below_the_project_and_network_is_shown(make_config, tmp_path):
    agents = {"defaultTool": "claude", "stages": {"triage": {"tool": "codex"}, "collect": {"tool": "claude"}},
              "capabilities": {"light": {"claude": {"model": "haiku", "inputUsdPerMTok": 1, "outputUsdPerMTok": 5}},
                               "standard": {"claude": {"model": "sonnet", "inputUsdPerMTok": 3, "outputUsdPerMTok": 15}}}}
    data = make_config().data
    data = {**data, "stages": {**data["stages"], "triage": {"tool": "claude",
                                                            "refuter": data["stages"]["triage"]["refuter"]}},
            "capabilities": {tier: models for tier, models in data["capabilities"].items() if tier != "light"}}
    config = project.parse(data, tmp_path / "project.yaml", agents=agents)
    user = UserConfig(tmp_path / "config.yaml", agents=agents, network_proxy="http://me:s3cret@127.0.0.1:8118",
                      no_proxy=("github.com",), tools={"semgrep": Path("/opt/semgrep")})
    shown = {item.key: item for item in show.show(config, user)}
    assert (shown["defaultTool"].value, shown["defaultTool"].source) == ("claude", "user")
    assert (shown["stages.collect.tool"].value, shown["stages.collect.tool"].source) == ("claude", "user")
    assert (shown["stages.triage.tool"].value, shown["stages.triage.tool"].source) == ("claude", "project")
    assert shown["capabilities.light.claude.model"].source == "user"
    assert shown["network.proxy"].value == "http://me:[已脱敏]@127.0.0.1:8118"
    assert (shown["network.noProxy"].value, shown["network.noProxy"].source) == (["github.com"], "user")
    semgrep = show.show(config, user, "runtime.tools.semgrep")[0]
    assert (semgrep.value, semgrep.source) == ("/opt/semgrep", "user")
    assert semgrep.layers == {"core": "semgrep", "user": "/opt/semgrep"}
    triage = show.show(config, user, "stages.triage.tool")[0]
    assert triage.layers == {"user": "codex", "project": "claude"}
