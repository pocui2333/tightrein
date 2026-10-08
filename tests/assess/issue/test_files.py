import json

import pytest

from tightrein.assess.issue import files
from tightrein.store import rebuild
from tightrein.store.files.json import read_json, write_json
from tightrein.store.tables import issues


def test_user_edits_are_not_overwritten(runtime, make_issue, layout):
    record = make_issue("0007", status="todo")
    assert not files.edited(layout, record)
    path = files.body_path(layout, "0007")
    path.write_text(path.read_text(encoding="utf-8").replace("返回 404", "返回 404 并提示"), encoding="utf-8")
    stored = issues.get(runtime.conn, "0007")
    assert files.edited(layout, stored)
    stored.extra["history"].append({"at": "2026-10-08T03:00:00Z", "event": "start", "actor": "tightrein",
                                    "reason": None, "note": None})
    files.write(runtime, stored)
    text = files.read_body(layout, "0007")
    assert "返回 404 并提示" in text and text.rstrip().endswith("start(操作者 tightrein)")  # 程序只在末尾追加历史
    assert not files.edited(layout, issues.get(runtime.conn, "0007"))
    assert read_json(files.record_path(layout, "0007"))["extra"]["historyWritten"] == 1


def test_the_record_round_trips_through_the_file(make_issue, layout):
    record = make_issue("0007", status="implementing", stage="implement", step="implement.code")
    loaded = files.from_json(read_json(files.record_path(layout, "0007")))
    assert (loaded.id, loaded.status, loaded.stage, loaded.step) == ("0007", "implementing", "implement",
                                                                     "implement.code")
    assert loaded.extra["slug"] == record.extra["slug"]
    with pytest.raises(files.IssueFileError, match="title"):
        files.from_json({"id": "0007", "status": "todo", "kind": "bug", "origin": "user"})


def test_reindex_lists_every_invalid_file_and_changes_nothing(runtime, make_issue, layout, conn):
    make_issue("0007", status="todo")
    make_issue("0008", status="todo")
    make_issue("0009", status="todo")
    write_json(layout.issue_dir("0010") / "21-assess.triage-handoff.json",  # 只有交接的目录：不是 Issue，跳过
               {"run": "R-20261008T030000Z-assess", "status": "passed"})
    broken = read_json(files.record_path(layout, "0008"))
    broken["status"] = "done"  # 完成却没有关闭原因
    write_json(files.record_path(layout, "0008"), broken)
    files.record_path(layout, "0009").write_text("{", encoding="utf-8")
    with pytest.raises(rebuild.RebuildError) as error:
        rebuild.rebuild(layout, conn)
    assert len(error.value.errors) == 2 and "0008" in error.value.errors[0] and "0009" in error.value.errors[1]
    assert len(issues.find(conn)) == 3  # 整体回滚，数据库保持原样


def test_rebuild_restores_the_issues_table_from_the_records(runtime, make_issue, layout, conn):
    make_issue("0007", status="needs_decision", extra={"hold": {"reason": "x"}})
    make_issue("0012", status="todo")
    conn.execute("DELETE FROM issues")
    assert rebuild.REBUILDERS["issues"] is rebuild.rebuild_issues  # 显式登记，转调 files.rebuild_issues
    assert rebuild.rebuild(layout, conn)["issues"] == 2
    assert issues.get(conn, "0007").extra["hold"] == {"reason": "x"}
    assert conn.execute("SELECT value FROM sequences WHERE name = 'issue'").fetchone()[0] >= 12


def test_restoring_puts_back_the_files_on_failure(make_issue, layout):
    make_issue("0007", status="todo")
    before = files.read_body(layout, "0007")
    with pytest.raises(RuntimeError), files.restoring(layout, ["0007", "0008"],
                                                    [layout.problem_dir("P-0001") / "x.json"]):
        files.body_path(layout, "0007").write_text("改坏了", encoding="utf-8")
        files.body_path(layout, "0008").parent.mkdir(parents=True)
        files.body_path(layout, "0008").write_text("新的", encoding="utf-8")
        (layout.problem_dir("P-0001")).mkdir(parents=True)
        (layout.problem_dir("P-0001") / "x.json").write_text(json.dumps({}), encoding="utf-8")
        raise RuntimeError("中途出错")
    assert files.read_body(layout, "0007") == before
    assert not files.body_path(layout, "0008").exists()
    assert not (layout.problem_dir("P-0001") / "x.json").exists()


def test_an_unindexed_file_is_not_overwritten_and_the_slug_cannot_change(runtime, make_issue, conn):
    record = make_issue("0007", status="todo")
    conn.execute("DELETE FROM issues WHERE id = '0007'")
    with pytest.raises(files.IssueFileError, match="不在索引中"):
        make_issue("0007", status="todo")
    files.rebuild_issues(runtime.workspace, conn)
    record.extra["slug"] = "renamed"
    with pytest.raises(files.IssueFileError, match="简称不能改"):
        files.write(runtime, record)
