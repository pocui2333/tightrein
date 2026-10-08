from pathlib import Path

from tightrein.knowledge import entries
from tightrein.knowledge.entries import EntryStatus
from tightrein.knowledge.stale import check
from tightrein.protocol.boundaries import Change
from tightrein.store.files.layout import WorkspaceLayout

BASE = "a" * 40
GONE = "b" * 40


class FakeGit:
    def __init__(self, changes: list[Change], files: tuple[str, ...], old: dict[str, str]) -> None:
        self.changes = changes
        self.files = files
        self.old = old
        self.numstat_calls: list[str] = []

    def has_commit(self, ref: str) -> bool:
        return ref != GONE

    def numstat(self, base: str, head: str | None = None) -> list[Change]:
        self.numstat_calls.append(base)
        return self.changes

    def show(self, rev: str, path: str) -> str | None:
        return self.old.get(path)

    def ls_files(self) -> tuple[str, ...]:
        return self.files


def add(layout: WorkspaceLayout, slug: str, locations: tuple[str, ...], commit: str | None = BASE) -> str:
    return entries.create(layout, kind="lessons", slug=slug, title="t", summary="s", body="b", locations=locations,
                          today="2026-10-01", commit=commit, sources=()).id


def test_deleted_and_heavily_changed_files_are_marked_for_review(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    deleted = add(layout, "deleted", ("path:src/old.py",))
    rewritten = add(layout, "rewritten", ("path:src/big.py", "route:GET /x"))
    touched = add(layout, "touched", ("path:src/small.py",))
    untouched = add(layout, "untouched", ("path:src/same.py",))
    emptied = add(layout, "emptied", ("path:src/legacy/",))
    no_base = add(layout, "no-base", ("path:src/old.py",), commit=None)
    lost_base = add(layout, "lost-base", ("path:src/old.py",), commit=GONE)
    git = FakeGit(
        changes=[Change("src/old.py", 0, 10, "D"), Change("src/big.py", 30, 30, "M"), Change("src/small.py", 1, 1, "M")],
        files=("src/big.py", "src/small.py", "src/same.py"),
        old={"src/big.py": "x\n" * 100, "src/small.py": "x\n" * 100},
    )
    report = check(layout, git, change_ratio=0.5, today="2026-10-08")
    assert {mark.entry: mark.reason for mark in report.marked} == {
        deleted: "src/old.py 已删除",
        rewritten: "src/big.py 大改：增 30 行、删 30 行，写入时 100 行",
        emptied: "src/legacy/ 下已没有文件",
    }
    assert git.numstat_calls == [BASE]
    assert any(lost_base in warning for warning in report.warnings)
    statuses = {entry.id: entry.status for entry in entries.load(layout).entries}
    assert statuses[deleted] is statuses[rewritten] is statuses[emptied] is EntryStatus.STALE
    assert statuses[touched] is statuses[untouched] is statuses[no_base] is EntryStatus.ACTIVE
