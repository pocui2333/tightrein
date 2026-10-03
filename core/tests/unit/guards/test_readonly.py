import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.guards import file_state, readonly
from tightrein.guards.readonly import ReadonlyLockError, ReadonlyRestoreError
from tightrein.store.locks import Holder

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def worktree(tmp_path):
    root = tmp_path / "worktrees" / "readonly"
    (root / "src").mkdir(parents=True)
    (root / "src" / "A.cs").write_text("class A {}\n", encoding="utf-8")
    (root / ".git").write_text("gitdir: /repo/.git/worktrees/readonly\n", encoding="utf-8")
    (root / "locked.txt").write_text("already read-only\n", encoding="utf-8")
    os.chmod(root / "locked.txt", 0o444)
    os.symlink("src/A.cs", root / "link.cs")
    yield root
    for directory, _, files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in files:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)


def writable(path):
    return bool(os.lstat(path).st_mode & stat.S_IWUSR)


def test_snapshot_and_compare(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two", encoding="utf-8")
    before = file_state.snapshot_files(tmp_path, ["a.txt", "b.txt", "missing.txt"])
    assert sorted(before) == ["a.txt", "b.txt"]
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "b.txt").unlink()
    (tmp_path / "c.txt").write_text("new", encoding="utf-8")
    changes = file_state.compare(before, file_state.snapshot_files(tmp_path, ["a.txt", "b.txt", "c.txt"], before))
    assert (changes.added, changes.modified, changes.deleted) == (("c.txt",), ("a.txt",), ("b.txt",))
    assert changes.paths == ("a.txt", "b.txt", "c.txt")
    assert not file_state.compare(before, before)


def test_walk_does_not_enter_skipped_directories(tmp_path, monkeypatch):
    for path in ("core/a.py", "core/.venv/lib/site.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x\n", encoding="utf-8")
    entered = []
    real_walk = os.walk

    def recording_walk(*args, **kwargs):
        for directory, directories, names in real_walk(*args, **kwargs):
            entered.append(Path(directory).name)
            yield directory, directories, names

    monkeypatch.setattr(file_state.os, "walk", recording_walk)
    assert file_state.walk(tmp_path, lambda name: name == ".venv") == ["core/a.py"]
    assert ".venv" not in entered


def test_unchanged_size_and_mtime_reuse_the_previous_hash(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    before = file_state.snapshot_files(tmp_path, ["a.txt"])
    info = os.stat(tmp_path / "a.txt")
    previous = {"a.txt": file_state.FileState(info.st_size, info.st_mtime_ns, "f" * 64)}
    assert file_state.snapshot_files(tmp_path, ["a.txt"], previous)["a.txt"].sha256 == "f" * 64
    assert file_state.snapshot_files(tmp_path, ["a.txt"])["a.txt"] == before["a.txt"]


def test_walk_does_not_follow_symlinks(worktree):
    assert file_state.walk(worktree) == [".git", "link.cs", "locked.txt", "src/A.cs"]
    assert file_state.walk(worktree / "src" / "A.cs") == ["."]
    assert file_state.walk(worktree / "missing") == []
    tree = file_state.snapshot_tree(worktree)
    assert tree["link.cs"].size == len("src/A.cs")


def test_lock_removes_write_permission(worktree, tmp_path, clock):
    marker_path = tmp_path / "data" / "guards" / "readonly-readonly.json"
    marker = readonly.lock(worktree, marker_path, clock, run_id="R-20261005-030000-triage")
    saved = json.loads(marker_path.read_text(encoding="utf-8"))
    assert saved["runId"] == "R-20261005-030000-triage"
    assert saved["pid"] == os.getpid()
    assert set(marker.modes) == {".", ".git", "locked.txt", "src", "src/A.cs"}
    for relative in (".", ".git", "src", "src/A.cs"):
        assert not writable(worktree / relative)
    with pytest.raises(PermissionError):
        (worktree / "src" / "A.cs").write_text("changed", encoding="utf-8")
    with pytest.raises(PermissionError):
        (worktree / "src" / "B.cs").write_text("new", encoding="utf-8")


def test_restore_puts_back_the_original_modes(worktree, tmp_path, clock):
    marker_path = tmp_path / "readonly-readonly.json"
    readonly.lock(worktree, marker_path, clock)
    readonly.restore(marker_path)
    assert writable(worktree / "src" / "A.cs")
    assert writable(worktree / "src")
    assert not writable(worktree / "locked.txt")
    assert not marker_path.exists()


def test_a_failed_lock_is_undone(worktree, tmp_path, clock):
    calls = []

    def chmod(path, mode):
        calls.append(path)
        if len(calls) == 3:
            raise PermissionError(1, "Operation not permitted")
        os.chmod(path, mode, follow_symlinks=False)

    marker_path = tmp_path / "readonly-readonly.json"
    with pytest.raises(ReadonlyLockError):
        readonly.lock(worktree, marker_path, clock, chmod=chmod)
    assert writable(worktree) and writable(worktree / "src" / "A.cs")
    assert not marker_path.exists()


def test_a_failed_restore_keeps_the_marker(worktree, tmp_path, clock):
    marker_path = tmp_path / "readonly-readonly.json"
    readonly.lock(worktree, marker_path, clock)

    def refuse(path, mode):
        raise PermissionError(1, "Operation not permitted")

    with pytest.raises(ReadonlyRestoreError) as caught:
        readonly.restore(marker_path, chmod=refuse)
    assert marker_path.exists()
    assert "chmod -R u+w" in str(caught.value)
    readonly.restore(marker_path)


def test_recover_restores_locks_of_dead_processes(worktree, tmp_path, clock):
    guards_dir = tmp_path / "data" / "guards"
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead = int(finished.stdout)
    readonly.lock(worktree, guards_dir / "readonly-readonly.json", clock, holder=Holder(dead, "host"))
    other = tmp_path / "worktrees" / "fix-0007"
    other.mkdir()
    readonly.lock(other, guards_dir / "readonly-fix-0007.json", clock)
    recovered = readonly.recover(guards_dir)
    assert [marker.worktree for marker in recovered] == [worktree]
    assert writable(worktree / "src" / "A.cs")
    assert not (guards_dir / "readonly-readonly.json").exists()
    assert (guards_dir / "readonly-fix-0007.json").exists()
    readonly.restore(guards_dir / "readonly-fix-0007.json")
