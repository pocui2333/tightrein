from pathlib import Path

import pytest

from tightrein.protocol.naming import FileName
from tightrein.store.files.layout import TOOL_ROOT_ENV, ToolLayout, WorkspaceLayout

ROOT = Path("/tw/workspaces/shop")


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "..\\x", "a\0b", "/etc"])
def test_segments_cannot_escape_their_directory(value: str) -> None:
    layout = WorkspaceLayout(ROOT)
    for build in (layout.issue_dir, layout.problem_dir, layout.run_dir, layout.object_lock, layout.worktree,
                  layout.knowledge_kind, ToolLayout(Path("/tw")).workspace, ToolLayout(Path("/tw")).vendor_skill):
        with pytest.raises(ValueError, match="路径"):
            build(value)


def test_workspace_paths() -> None:
    layout = WorkspaceLayout(ROOT)
    assert layout.project == "shop"
    assert layout.database == ROOT / "data" / "tightrein.db"
    assert layout.run_lock == ROOT / "data" / "run.lock"
    assert layout.object_lock("0018") == ROOT / "data" / "locks" / "0018.lock"
    assert layout.events("R-20261007T093000Z-collect") == (
        ROOT / "data" / "runs" / "R-20261007T093000Z-collect" / "events.jsonl")
    assert layout.worktree("0018") == ROOT / "worktrees" / "0018"


def test_object_files_go_to_the_directory_of_their_kind() -> None:
    layout = WorkspaceLayout(ROOT)
    assert layout.subject_dir("R-20261007T093000Z-collect") == ROOT / "data" / "runs" / "R-20261007T093000Z-collect"
    assert layout.subject_dir("P-0003") == ROOT / "data" / "problems" / "P-0003"
    assert layout.subject_dir("0018") == ROOT / "data" / "issues" / "0018"
    name = FileName("implement.code", "handoff", "json", round=2)
    assert layout.step_file("0018", name) == ROOT / "data" / "issues" / "0018" / "35-implement.code.r2-handoff.json"
    assert layout.shared_file("0018", "notes", "json") == ROOT / "data" / "issues" / "0018" / "00-issue-notes.json"
    assert layout.shared_file("P-0003", "evidence", "json").name == "00-problem-evidence.json"
    assert layout.human_document("0018", "pending") == ROOT / "data" / "issues" / "0018" / "90-issue-pending.md"
    with pytest.raises(ValueError):
        layout.human_document("0018", "handoff")
    with pytest.raises(ValueError):
        layout.shared_file("0018", "../x", "json")


def test_tool_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool = ToolLayout(Path("/tw"))
    assert tool.defaults == Path("/tw/settings/defaults.json")
    assert tool.vendor_lock == Path("/tw/vendor/lock.json")
    assert tool.workspace("shop") == WorkspaceLayout(Path("/tw/workspaces/shop"))
    monkeypatch.setenv(TOOL_ROOT_ENV, str(tmp_path))
    assert ToolLayout.discover().root == tmp_path.resolve()
    monkeypatch.delenv(TOOL_ROOT_ENV)
    assert (ToolLayout.discover().root / "src" / "tightrein").is_dir()
