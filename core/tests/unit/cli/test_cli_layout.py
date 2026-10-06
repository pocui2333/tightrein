"""命令行的结构：根帮助分节、老写法的提示、不带命令时执行 status、帮助只列人用的参数、工作区可以写项目名。"""

import io
import json
from pathlib import Path

import pytest
from cli_world import make_cli_world

from tightrein.cli import exit_codes
from tightrein.cli.assemble import resolve_workspace
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.main import build_parser, main
from tightrein.store.files.layout import UserLayout

OLD = ("next", "pending", "confirm", "retriage", "workspace", "worktree", "probe", "config", "schedule", "install",
       "third-party", "skills", "doc", "eval", "kb", "ext", "tick")


def run(world, *argv):
    out = io.StringIO()
    code = main(list(argv), world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, out.getvalue()


def test_the_root_help_has_three_sections_on_one_screen():
    text = build_parser().format_help()
    lines = text.splitlines()
    assert len(lines) <= 40
    assert [line for line in lines if line in ("日常", "分组", "单步执行(高级)")] == ["日常", "分组", "单步执行(高级)"]
    listed = {line.split()[0] for line in lines if line.startswith("  ")}
    assert {"status", "show", "new", "approve", "issue", "problem", "project", "admin", "fix"} <= listed
    assert not listed & set(OLD)


@pytest.mark.parametrize(("argv", "hint"), [
    (["confirm", "OP-0001"], "`confirm` 已改为 `approve`"),
    (["next", "7"], "`next` 已改为 `show`"),
    (["issue", "approve", "7"], "`issue approve` 已改为 `approve`"),
    (["issue", "create", "--manual", "--title", "t"], "`issue create --manual` 已改为 `new`"),
    (["workspace", "init"], "`workspace` 已改为 `project init / check / answer`"),
    (["kb", "search", "x"], "`kb` 已改为 `admin kb`"),
])
def test_old_commands_are_refused_with_the_new_form(tmp_path, argv, hint):
    code, out = run(make_cli_world(tmp_path), *argv, "--json")
    assert code == exit_codes.USAGE and hint in json.loads(out)["errors"][0]["message"]


def test_no_command_runs_status(tmp_path):
    world = make_cli_world(tmp_path)
    config = UserLayout(world.home).config()
    config.parent.mkdir(parents=True)
    config.write_text(f"defaultWorkspace: {world.root}\n", encoding="utf-8")
    code, out = run(world)
    assert code == exit_codes.OK and "待处理" in out


def test_leaf_help_lists_only_the_options_people_use(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fix", "plan", "--help"])
    text = capsys.readouterr().out
    assert "--workspace" in text and "--json" in text and "--accept-design" in text
    assert not any(flag in text for flag in ("--select", "--replay-from", "--gate-decisions", "--dry-run"))
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--help"])
    assert "--dry-run" in capsys.readouterr().out


def test_the_workspace_can_be_a_project_name(tmp_path):
    workspaces = tmp_path / "workspaces"
    (workspaces / "shop").mkdir(parents=True)
    assert resolve_workspace(Path("shop"), workspaces) == workspaces / "shop"
    assert resolve_workspace(workspaces / "shop", workspaces) == workspaces / "shop"
    with pytest.raises(UsageError, match="已有的工作区：shop"):
        resolve_workspace(Path("nope"), workspaces)
