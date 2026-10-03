import io
import json
import shutil

import pytest
from cli_world import make_cli_world
from packaging_world import COMMIT_A, COMMIT_B, Commands, Downloads, make_tool

from tightrein.cli import exit_codes
from tightrein.cli.main import main
from tightrein.packaging import third_party
from tightrein.store.files.layout import ToolLayout, UserLayout


@pytest.fixture
def world(tmp_path):
    world = make_cli_world(tmp_path)
    world.tool_root = make_tool(tmp_path / "tool")
    world.commands = Commands()
    world.downloads = Downloads()
    world.program = tmp_path / "venv" / "bin" / "tightrein"
    world.program.parent.mkdir(parents=True)
    world.program.write_text("", encoding="utf-8")
    return world


def call(world, *argv, stdin="", tty=False):
    out = io.StringIO()
    externals = world.externals(tool_root=world.tool_root, vcs_execute=world.commands, transport=world.downloads,
                                program=world.program, stdin_is_tty=lambda: tty)
    code = main(list(argv), externals, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO())
    return code, out.getvalue()


def lock_text(world):
    return ToolLayout(world.tool_root).third_party_lock().read_text(encoding="utf-8")


def test_skills_check_passes_and_reports_violations(world):
    assert call(world, "skills", "check")[0] == exit_codes.OK
    (world.tool_root / "skills" / "loop" / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")
    code, out = call(world, "skills", "check", "--json")
    assert code == exit_codes.FAILED
    assert json.loads(out)["result"][0]["rule"] == "frontmatter"


def test_third_party_lock_writes_the_lock_the_first_time(world):
    code, out = call(world, "third-party", "lock")
    assert code == exit_codes.OK, out
    skills = third_party.read_lock(ToolLayout(world.tool_root).third_party_lock())
    assert [(skill.name, skill.ref, skill.license) for skill in skills] == [
        ("differential-review", COMMIT_A, "CC-BY-SA-4.0"), ("variant-analysis", COMMIT_A, "CC-BY-SA-4.0")]
    assert call(world, "third-party", "verify")[0] == exit_codes.OK


def test_relocking_needs_confirmation_in_the_terminal(world):
    call(world, "third-party", "lock")
    before = lock_text(world)
    code, out = call(world, "third-party", "lock", "differential-review", "--ref", COMMIT_B, "--json")
    assert code == exit_codes.GATE
    assert json.loads(out)["result"][0]["changed"] == ["SKILL.md"]
    assert lock_text(world) == before
    code, _ = call(world, "third-party", "lock", "differential-review", "--ref", COMMIT_B, stdin="yes\n", tty=True)
    assert code == exit_codes.OK
    assert COMMIT_B in lock_text(world)


def test_lock_dry_run_does_not_download(world):
    code, out = call(world, "third-party", "lock", "--dry-run")
    assert code == exit_codes.OK
    assert f"将锁定 differential-review：未锁定 → {COMMIT_A}" in out
    assert world.downloads.urls == []
    assert "ref:" not in lock_text(world)


def test_verify_reports_a_missing_cache(world):
    call(world, "third-party", "lock")
    shutil.rmtree(ToolLayout(world.tool_root).third_party_cache("variant-analysis", COMMIT_A))
    code, out = call(world, "third-party", "verify")
    assert code == exit_codes.FAILED
    assert "variant-analysis" in out


def test_install_dry_run_and_uninstall_needs_confirmation(world):
    code, out = call(world, "install", "--tool", "codex", "--dry-run", "--json")
    assert code == exit_codes.OK
    kinds = {action["kind"] for action in json.loads(out)["result"]["actions"]}
    assert kinds == {"link"}
    links = world.home / ".agents" / "skills"
    assert not links.exists()
    assert call(world, "install", "--tool", "codex")[0] == exit_codes.OK
    assert (links / "loop").is_symlink()
    code, out = call(world, "uninstall", "--tool", "codex", "--json")
    assert code == exit_codes.GATE
    assert (links / "loop").is_symlink()
    assert call(world, "uninstall", "--tool", "codex", stdin="yes\n", tty=True)[0] == exit_codes.OK
    assert not (links / "loop").exists()


def test_install_repo_only_excludes_tool_and_only_touches_the_repository(world):
    assert call(world, "install", "--repo-only", "--tool", "codex")[0] == exit_codes.USAGE
    call(world, "third-party", "lock")
    code, out = call(world, "install", "--repo-only", "--dry-run", "--json")
    assert code == exit_codes.OK
    assert {action["kind"] for action in json.loads(out)["result"]["actions"]} == {"link"}
    assert call(world, "install", "--repo-only")[0] == exit_codes.OK
    assert (world.tool_root / "skills" / "variant-analysis").is_symlink()
    assert not (world.home / ".agents").exists()
    assert call(world, "install", "--repo-only", "--check")[0] == exit_codes.OK
    assert call(world, "uninstall", "--repo-only", stdin="yes\n", tty=True)[0] == exit_codes.OK
    assert not (world.tool_root / "skills" / "variant-analysis").exists()


def test_schedule_install_shows_the_plist_and_needs_confirmation(world):
    path = UserLayout(world.home).launch_agent("workspace")
    code, out = call(world, "schedule", "install", "--dry-run", "--workspace", str(world.root))
    assert code == exit_codes.OK
    assert "<key>StartCalendarInterval</key>" in out and "launchctl bootstrap" in out
    assert not path.exists()
    assert call(world, "schedule", "install", "--json", "--workspace", str(world.root))[0] == exit_codes.GATE
    assert not path.exists()
    code, _ = call(world, "schedule", "install", "--workspace", str(world.root), stdin="yes\n", tty=True)
    assert code == exit_codes.OK
    assert path.is_file()
    assert [argv[:2] for argv in world.commands.named("launchctl")] == [["launchctl", "bootstrap"]]
    code, out = call(world, "schedule", "show", "--json", "--workspace", str(world.root))
    assert json.loads(out)["result"]["plist"] == path.read_text(encoding="utf-8")
    assert world.commands.named("launchctl")[-1][:2] == ["launchctl", "print"]
