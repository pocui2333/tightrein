import json
import os
import stat
from pathlib import Path

from tightrein.cli import exit_codes

AGY_SETTINGS = Path(".gemini") / "antigravity-cli" / "settings.json"
READ_COMMANDS = ["git grep", "git ls-files", "git log", "git show", "git diff", "git blame", "grep", "ls", "cat",
                 "head", "tail", "wc"]


def put_agy(cli) -> Path:
    """PATH 中放一个可执行的 agy，home 下建 ~/.local/bin。"""
    directory = Path(cli.externals.environ["PATH"])
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "agy"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    (cli.externals.home / ".local" / "bin").mkdir(parents=True)
    return program


def agy_settings(cli, data: dict) -> Path:
    path = cli.externals.home / AGY_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_failing_check_stops_the_install_without_any_change(cli):
    path = cli.externals.home / AGY_SETTINGS
    path.parent.mkdir(parents=True)
    path.write_text("{不是 JSON", encoding="utf-8")
    (cli.externals.home / ".local" / "bin").mkdir(parents=True)
    data = cli.json("admin", "install", "--tool", "agy", "--tool", "codex", "--yes")
    assert data["exitCode"] == exit_codes.FAILED
    problems = data["result"]["problems"]
    assert any("找不到 agy" in item for item in problems) and any("找不到 codex" in item for item in problems)
    assert any("读不出" in item for item in problems)  # 列出全部问题后才停
    assert path.read_text(encoding="utf-8") == "{不是 JSON"
    assert not (cli.externals.home / ".local" / "bin" / "tightrein").exists()
    assert not cli.tool.installed.exists()


def test_install_adds_only_missing_commands_and_uninstall_removes_only_what_it_added(cli):
    put_agy(cli)
    path = agy_settings(cli, {"theme": "dark", "permissions": {"allow": ["command(ls)", "web(*)"]}})
    data = cli.json("admin", "install", "--tool", "agy", "--yes")
    assert data["exitCode"] == exit_codes.OK
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["theme"] == "dark"
    assert after["permissions"]["allow"] == ["command(ls)", "web(*)"] + [
        f"command({command})" for command in READ_COMMANDS if command != "ls"]
    link = cli.externals.home / ".local" / "bin" / "tightrein"
    assert link.is_symlink() and Path(os.readlink(link)) == cli.externals.program
    record = json.loads(cli.tool.installed.read_text(encoding="utf-8"))
    assert "ls" not in record["tools"]["agy"]["allowedCommands"]
    assert cli.json("admin", "uninstall", "--yes")["exitCode"] == exit_codes.OK
    assert json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"] == ["command(ls)", "web(*)"]
    assert not link.exists() and not link.is_symlink()


def test_a_foreign_command_in_local_bin_is_left_alone(cli):
    put_agy(cli)
    foreign = cli.externals.home / ".local" / "bin" / "tightrein"
    foreign.write_text("别人的脚本", encoding="utf-8")
    data = cli.json("admin", "install", "--tool", "agy", "--yes")
    assert data["exitCode"] == exit_codes.OK
    assert any("不是本工具建的" in note for note in data["result"]["notes"])
    assert foreign.read_text(encoding="utf-8") == "别人的脚本"
    cli.json("admin", "uninstall", "--yes")
    assert foreign.read_text(encoding="utf-8") == "别人的脚本"


def test_dry_run_has_no_side_effects(cli):
    put_agy(cli)
    data = cli.json("admin", "install", "--tool", "agy", "--dry-run")
    assert data["exitCode"] == exit_codes.OK
    assert {item["kind"] for item in data["result"]["actions"]} == {"allow", "link"}
    assert not (cli.externals.home / AGY_SETTINGS).exists()
    assert not (cli.externals.home / ".local" / "bin" / "tightrein").exists()
    assert not cli.tool.installed.exists()


def test_check_lists_every_item_and_fails_on_problems(cli):
    secrets = cli.layout.secrets
    secrets.write_text("{}", encoding="utf-8")
    secrets.chmod(0o644)
    data = cli.json("admin", "check")
    assert data["exitCode"] == exit_codes.FAILED
    items = {item["item"]: item for item in data["result"]}
    assert items["settings"]["ok"] and items["setup"]["ok"]
    assert not items["workspace.secrets"]["ok"]  # 权限不是 600
    secrets.chmod(0o600)
    assert all(item["ok"] for item in cli.json("admin", "check")["result"]
               if not item["item"].startswith("tool."))
