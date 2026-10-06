import json
import os
import shutil

import pytest
from packaging_world import COMMIT_A, make_world, skill_text

from tightrein.packaging import claude, install


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def targets(world, tool=install.ALL):
    ctx = world.context()
    return ctx, install.targets(ctx.config, ctx.user, ctx.home, tool)


def run_install(world, tool=install.ALL, dry_run=False):
    ctx, chosen = targets(world, tool)
    return install.install(ctx, chosen, dry_run=dry_run)


def agents(world):
    return world.home / ".agents" / "skills"


def build_dir(world):
    return world.home / ".cache" / "tightrein" / "packaging" / "claude"


def installed(world):
    return json.loads(world.tool.third_party_installed().read_text(encoding="utf-8"))


def everything(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_links_point_to_the_repository_skills(world):
    plan = run_install(world, install.CODEX)
    assert os.readlink(agents(world) / "loop") == str(world.tool.skills_dir() / "loop")
    assert (agents(world) / "fix" / "references" / "rules.md").is_file()
    assert "differential-review 尚未锁定，未安装；先执行 tightrein third-party lock" in plan.notes
    record = installed(world)["tools"]["codex"]
    assert (record["path"], record["method"], sorted(record["skills"])) == (str(agents(world)), "link", ["fix", "loop"])


def test_locked_third_party_skills_are_downloaded_and_linked_in_the_repository(world, tmp_path):
    world.lock_all()
    cache = world.context().cache("differential-review", COMMIT_A)
    assert cache.parent.parent == world.tool.local_dir() / "third_party-cache"
    shutil.rmtree(world.tool.local_dir() / "third_party-cache")
    run_install(world, install.CODEX)
    assert os.readlink(world.tool.skills_dir() / "differential-review") == str(cache)
    assert os.readlink(agents(world) / "differential-review") == str(world.tool.skills_dir() / "differential-review")
    assert (agents(world) / "variant-analysis" / "SKILL.md").is_file()
    assert installed(world)["repo"]["links"]["variant-analysis"].endswith(COMMIT_A)
    assert len(world.downloads.urls) == 4


def test_the_claude_plugin_is_built_and_installed_through_the_plugin_commands(world):
    run_install(world, install.CLAUDE)
    root = build_dir(world)
    marketplace = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((root / "tightrein" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert marketplace["name"] == "tightrein-local"
    assert marketplace["plugins"][0]["source"] == "./tightrein"
    assert plugin["name"] == "tightrein"
    assert plugin["version"] == installed(world)["tools"]["claude"]["version"]
    assert (root / "tightrein" / "skills" / "loop" / "SKILL.md").is_file()
    assert not (root / "tightrein" / "skills" / "loop").is_symlink()
    assert world.commands.named("claude") == [
        ["claude", "plugin", "marketplace", "add", str(root)],
        ["claude", "plugin", "install", "tightrein@tightrein-local"],
        ["claude", "plugin", "list", "--json"],
    ]


def test_a_second_install_does_nothing_and_a_changed_skill_updates_the_plugin(world):
    run_install(world)
    before = installed(world)
    world.commands.calls.clear()
    assert run_install(world).actions == []
    assert world.commands.calls == []
    assert installed(world) == before
    (world.tool.skills_dir() / "loop" / "SKILL.md").write_text(skill_text("loop", "新的正文\n"), encoding="utf-8")
    run_install(world)
    assert world.commands.named("claude")[:2] == [["claude", "plugin", "marketplace", "update", "tightrein-local"],
                                                  ["claude", "plugin", "update", "tightrein@tightrein-local"]]
    assert installed(world)["tools"]["claude"]["version"] != before["tools"]["claude"]["version"]


def test_a_foreign_entry_stops_the_install_without_any_change(world):
    (agents(world) / "loop").mkdir(parents=True)
    with pytest.raises(install.InstallError, match="loop 已存在且不是本工具安装的"):
        run_install(world)
    assert everything(world.home) == [".agents", ".agents/skills", ".agents/skills/loop"]
    assert world.commands.calls == []
    assert not world.tool.third_party_installed().exists()


def test_a_failing_skills_check_stops_the_install(world):
    (world.tool.skills_dir() / "loop" / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")
    with pytest.raises(install.InstallError, match="skills check：loop frontmatter"):
        run_install(world)


def test_dry_run_has_no_side_effects(world):
    world.lock_all()
    world.downloads.urls.clear()
    before = everything(world.home)
    plan = run_install(world, dry_run=True)
    assert {action.kind for action in plan.actions} == {"link", "build", "command", "allow"}
    assert everything(world.home) == before
    assert world.commands.calls == [] and world.downloads.urls == []
    assert not world.tool.third_party_installed().exists()


def test_the_user_config_moves_a_target(world, tmp_path):
    world.write_user_config(f"install:\n  targets:\n    agy: {{path: {tmp_path / 'agy'}}}\n"
                            "    claude: {enabled: false}\n")
    ctx, chosen = targets(world)
    assert [(target.tool, target.path) for target in chosen] == [("codex", agents(world)),
                                                                 ("agy", tmp_path / "agy")]
    install.install(ctx, chosen)
    assert (tmp_path / "agy" / "loop").is_symlink()
    assert (agents(world) / "loop").is_symlink()


def test_codex_and_agy_share_links_and_uninstalling_one_keeps_them(world):
    run_install(world, install.CODEX)
    assert [action.kind for action in run_install(world, install.AGY).actions] == ["allow"]
    assert sorted(installed(world)["tools"]) == ["agy", "codex"]
    ctx, codex = targets(world, install.CODEX)
    assert install.uninstall(ctx, codex).actions == []
    assert (agents(world) / "loop").is_symlink()
    assert sorted(installed(world)["tools"]) == ["agy"]
    ctx, agy = targets(world, install.AGY)
    install.uninstall(ctx, agy)
    assert not (agents(world) / "loop").exists()
    assert installed(world)["tools"] == {}


def test_agy_gets_the_read_only_commands_and_uninstall_removes_only_those(world):
    settings = world.home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "x", "permissions": {"allow": ["command(git grep)", "command(npm)"]}}),
                        encoding="utf-8")
    plan = run_install(world, install.AGY)
    assert [(action.kind, action.argv[:2]) for action in plan.actions if action.kind == "allow"] == [
        ("allow", ("git ls-files", "git log"))]
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow[:2] == ["command(git grep)", "command(npm)"] and "command(wc)" in allow
    assert "git grep" not in installed(world)["tools"]["agy"]["allowedCommands"]
    ctx, agy = targets(world, install.AGY)
    assert all(item.status == "ok" for item in install.check(ctx, agy) if item.name == install.PERMISSIONS)
    install.uninstall(ctx, agy)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["command(git grep)", "command(npm)"] and data["model"] == "x"


def test_uninstall_only_removes_what_this_tool_created(world, tmp_path):
    run_install(world)
    foreign = agents(world) / "someone-else"
    foreign.symlink_to(tmp_path, target_is_directory=True)
    ctx, chosen = targets(world)
    plan = install.uninstall(ctx, chosen)
    assert sorted(action.kind for action in plan.actions) == ["command", "command", "remove", "unallow", "unlink",
                                                              "unlink"]
    assert foreign.is_symlink()
    assert not build_dir(world).exists()
    assert world.commands.named("claude")[-2:] == [["claude", "plugin", "uninstall", "tightrein@tightrein-local"],
                                                   ["claude", "plugin", "marketplace", "remove", "tightrein-local"]]


def test_check_reports_missing_stale_and_conflicting_entries(world, tmp_path):
    run_install(world)
    ctx, chosen = targets(world)
    assert {item.status for item in install.check(ctx, chosen)} == {install.OK}
    (agents(world) / "fix").unlink()
    (agents(world) / "fix").symlink_to(tmp_path, target_is_directory=True)
    (world.tool.skills_dir() / "extra").mkdir()
    (world.tool.skills_dir() / "extra" / "SKILL.md").write_text(skill_text("extra"), encoding="utf-8")
    (agents(world) / "extra").mkdir()
    (world.tool.skills_dir() / "loop" / "SKILL.md").write_text(skill_text("loop", "改过\n"), encoding="utf-8")
    statuses = {(item.tool, item.name): item.status for item in install.check(ctx, chosen)}
    assert statuses[("codex", "fix")] == install.STALE
    assert statuses[("codex", "extra")] == install.CONFLICT
    assert statuses[("codex", "loop")] == install.OK
    assert statuses[("claude", claude.PLUGIN_ID)] == install.STALE
    os.rmdir(agents(world) / "extra")
    assert {(item.tool, item.name): item.status for item in install.check(ctx, chosen)}[
        ("agy", "extra")] == install.MISSING


def test_a_failing_plugin_command_is_reported_with_its_output(world):
    world.commands.failing.add("plugin")
    with pytest.raises(install.InstallError, match="claude plugin marketplace add .* 失败\\(退出码 5\\)：失败的原因"):
        run_install(world, install.CLAUDE)


def test_repo_only_places_third_party_skills_without_touching_any_tool_directory(world):
    world.lock_all()
    shutil.rmtree(world.tool.local_dir() / "third_party-cache")
    ctx = world.context()
    plan = install.install(ctx, [], dry_run=True)
    assert {action.kind for action in plan.actions} == {"download", "link"}
    assert not world.tool.third_party_installed().exists()
    install.install(ctx, [])
    cache = ctx.cache("variant-analysis", COMMIT_A)
    assert os.readlink(world.tool.skills_dir() / "variant-analysis") == str(cache)
    assert installed(world) == {"tools": {}, "repo": {"links": {
        "differential-review": str(ctx.cache("differential-review", COMMIT_A)), "variant-analysis": str(cache)}}}
    assert everything(world.home) == []
    assert world.commands.calls == []
    assert {(item.tool, item.status) for item in install.check(ctx, [])} == {(install.REPO, install.OK)}
    (world.tool.skills_dir() / "variant-analysis").unlink()
    assert {item.name: item.status for item in install.check(ctx, [])}["variant-analysis"] == install.MISSING


def test_uninstall_repo_only_removes_the_repository_links_unless_a_tool_still_uses_them(world):
    world.lock_all()
    run_install(world, install.CODEX)
    ctx, codex = targets(world, install.CODEX)
    with pytest.raises(install.InstallError, match="codex 仍经 skills/<名称> 使用第三方 skill"):
        install.uninstall(ctx, [], repo_only=True)
    assert (world.tool.skills_dir() / "variant-analysis").is_symlink()
    install.uninstall(ctx, codex)
    plan = install.uninstall(ctx, [], repo_only=True)
    assert [action.path.name for action in plan.actions] == ["differential-review", "variant-analysis"]
    assert not (world.tool.skills_dir() / "variant-analysis").exists()
    assert ctx.cache("variant-analysis", COMMIT_A).is_dir()
    assert installed(world)["repo"]["links"] == {}
