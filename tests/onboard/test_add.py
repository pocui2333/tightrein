import json
import stat

import pytest

from tightrein.onboard import add
from tightrein.onboard.setup import MODULES
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout


def test_the_plan_lists_what_will_be_created_and_refuses_a_non_repository(tool, repo, tmp_path):
    plan = add.plan(tool, repo)
    workspace = tool.workspace("shop")
    assert plan.workspace == workspace and plan.repo == repo.resolve()
    assert plan.creates[0] == workspace.root and workspace.setup in plan.files and workspace.secrets in plan.files
    assert not workspace.root.exists()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="不是 git 仓库"):
        add.plan(tool, plain)


def test_add_builds_the_workspace_with_drafts(tool, repo, clock, fake_git):
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    added = add.add(add.plan(tool, repo, "web"), git=fake_git(remote=["main"]), clock=clock, language="zh")
    workspace: WorkspaceLayout = added.workspace
    assert workspace.root == tool.workspaces_dir / "web"
    for directory in (workspace.scripts_dir, workspace.data_dir, workspace.worktrees_dir,
                      workspace.knowledge_kind("lessons")):
        assert directory.is_dir()
    assert stat.S_IMODE(workspace.secrets.stat().st_mode) == 0o600
    assert json.loads(workspace.sites.read_text(encoding="utf-8")) == {}
    settings = json.loads(workspace.settings.read_text(encoding="utf-8"))
    assert settings["project"]["commands"]["test"] == "npm test" and settings["project"]["mainBranch"] == "main"
    setup = json.loads(workspace.setup.read_text(encoding="utf-8"))
    assert setup["project"] == "web" and list(setup["modules"]) == list(MODULES)
    assert any("status：只能是" in issue for issue in added.issues)
    page = workspace.setup_md.read_text(encoding="utf-8")
    assert "接入清单：web" in page and "### 从提交历史推断的约定" in page and "- 提交：历史中没有统一的风格" in page
    open_database(workspace.database).close()


def test_an_existing_workspace_is_never_overwritten(tool, repo, clock, fake_git):
    add.add(add.plan(tool, repo), git=fake_git(), clock=clock, language="zh")
    with pytest.raises(ValueError, match="工作区已存在"):
        add.plan(tool, repo)
