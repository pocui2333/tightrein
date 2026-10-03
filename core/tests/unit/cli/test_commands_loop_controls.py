"""pause、resume、tick 与 workspace init|check|answer 的命令行。"""

import io
import json
import sqlite3
import subprocess

from cli_world import make_cli_world

from tightrein.cli import exit_codes
from tightrein.cli.main import main


def no_keychain(args, timeout):
    """钥匙串的替身：条目都不存在(不调用真实的 security)。"""
    return subprocess.CompletedProcess(list(args), 44, "", "The specified item could not be found in the keychain.")


def call(world, *argv):
    out = io.StringIO()
    code = main(list(argv), world.externals(secret_run=no_keychain), stdin=io.StringIO(), stdout=out,
                stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def test_pause_and_resume_globally_or_for_one_workspace(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call(world, "pause", "--json")
    assert code == 0 and values["result"]["scope"] == "global"
    assert (world.home / ".local" / "state" / "tightrein" / "paused").is_file()
    code, values = call(world, "run", "--workspace", str(world.root), "--json")
    assert code == exit_codes.PRECONDITION and "已全局暂停" in values["result"]["paused"]
    code, values = call(world, "tick", "--workspace", str(world.root), "--json")
    assert code == 0 and values["result"]["kind"] == "paused"
    assert call(world, "resume", "--json")[1]["result"]["resumed"] is True
    assert call(world, "pause", "--workspace", str(world.root), "--json")[1]["result"]["scope"] == "workspace"
    code, values = call(world, "status", "--workspace", str(world.root), "--json")
    assert values["result"]["paused"].startswith("本工作区已暂停")
    assert call(world, "resume", "--workspace", str(world.root), "--json")[1]["result"]["resumed"] is True


def test_workspace_init_creates_a_workspace_and_lists_the_questions(tmp_path):
    world = make_cli_world(tmp_path)
    repo = tmp_path / "project-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "new-workspace"
    code, values = call(world, "workspace", "init", "--workspace", str(target), "--repo", str(repo), "--json")
    assert code == exit_codes.GATE and values["result"]["phase"] == "onboarding"
    states = {item["item"]: item["state"] for item in values["result"]["items"]}
    assert states["stack"] == "done" and states["checks"] == "blocked"
    assert (target / "project.yaml").is_file() and (target / "onboarding.md").is_file()
    assert values["next"] == f"tightrein worktree init --workspace {target}"
    code, values = call(world, "status", "--workspace", str(target), "--json")
    assert values["result"]["onboarding"].startswith("new-workspace：接入中，还差")
    code, values = call(world, "workspace", "answer", "platform:log-platform", "--skip", "--workspace", str(target),
                        "--json")
    assert code == 0 and {item["item"]: item["state"] for item in values["result"]["items"]}[
        "platform:log-platform"] == "done"
    code, values = call(world, "workspace", "check", "--workspace", str(world.root), "--json")
    accounts = next(item for item in values["result"]["items"] if item["item"] == "accounts")
    assert code == 0 and values["result"]["phase"] == "running" and accounts["state"] == "failed"


def test_run_dry_run_lists_the_steps_without_starting_a_run(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call(world, "run", "--dry-run", "--workspace", str(world.root), "--json")
    names = [step["name"] for step in values["result"]["steps"]]
    assert code == 0 and values["result"]["dryRun"] is True and "triage" in names and "health" in names
    with sqlite3.connect(world.root / "data" / "tightrein.db") as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_commands_without_a_preview_reject_dry_run(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call(world, "pause", "--dry-run", "--json")
    assert code == exit_codes.USAGE and "pause 不支持 --dry-run" in values["errors"][0]["message"]
    assert not (world.home / ".local" / "state" / "tightrein" / "paused").exists()
