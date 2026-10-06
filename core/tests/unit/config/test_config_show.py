from pathlib import Path

from tightrein.config import project, show
from tightrein.config.routes import CALL_POINTS
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


def test_user_routing_keys_sit_below_the_project_and_network_is_shown(make_config, tmp_path):
    routing = {"models": {"haiku": {"tool": "claude", "model": "haiku", "inputUsdPerMTok": 1, "outputUsdPerMTok": 5}},
               "routes": {"triage.dedup": "haiku", "collect.static-review": "haiku"}}
    data = make_config().data
    config = project.parse(data, tmp_path / "project.yaml", routing=routing)
    user = UserConfig(tmp_path / "config.yaml", routing=routing, network_proxy="http://me:s3cret@127.0.0.1:8118",
                      no_proxy=("github.com",), tools={"semgrep": Path("/opt/semgrep")})
    shown = {item.key: item for item in show.show(config, user)}
    assert (shown["routes.collect.static-review"].value, shown["routes.collect.static-review"].source) == (
        "haiku", "user")
    assert (shown["routes.triage.dedup"].value, shown["routes.triage.dedup"].source) == ("claude-haiku", "project")
    assert shown["models.haiku.model"].source == "user"
    assert shown["network.proxy"].value == "http://me:[已脱敏]@127.0.0.1:8118"
    assert (shown["network.noProxy"].value, shown["network.noProxy"].source) == (["github.com"], "user")
    semgrep = show.show(config, user, "runtime.tools.semgrep")[0]
    assert (semgrep.value, semgrep.source) == ("/opt/semgrep", "user")
    assert semgrep.layers == {"core": "semgrep", "user": "/opt/semgrep"}
    dedup = show.show(config, user, "routes.triage.dedup")[0]
    assert dedup.layers == {"user": "haiku", "project": "claude-haiku"}


def test_routes_list_every_call_point_with_its_source(make_config, tmp_path):
    routing = {"models": {"haiku": {"tool": "claude", "model": "haiku", "effort": "low"}},
               "routes": {"default": "haiku", "fix.planner.high-risk": "haiku"}}
    config = project.parse(make_config().data, tmp_path / "project.yaml", routing=routing)
    rows = {row.name: row for row in show.routes(config)}
    assert set(rows) == {*CALL_POINTS, "fix.planner.high-risk"}
    assert (rows["fix.planner"].key, rows["fix.planner"].alias, rows["fix.planner"].source) == (
        "fix.planner", "gpt-5", "project.yaml")
    assert (rows["fix.planner.high-risk"].alias, rows["fix.planner.high-risk"].source) == ("haiku", "用户配置")
    assert (rows["learn.rule-writer"].key, rows["learn.rule-writer"].effort) == ("default", "low")
    assert rows["learn.rule-writer"].to_dict()["route"] == "routes.default"
    bare = {row.name: row for row in show.routes(project.parse(
        {"project": make_config().data["project"]}, tmp_path / "project.yaml"))}
    assert bare["fix.executor"].key is None and bare["fix.executor"].alias is None
