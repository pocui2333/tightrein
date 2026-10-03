import os
from datetime import timedelta

import pytest

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.errors import LockTimeout
from tightrein.retrieval.indexers import FtsIndexer
from tightrein.retrieval.locations import routes_of
from tightrein.retrieval.sync import Synchronizer
from tightrein.store import locks, sequences
from tightrein.store.locks import Holder
from tightrein.store.repos import knowledge


def synchronizer(world, **options):
    return Synchronizer(world.layout, world.conn, world.clock, [FtsIndexer()], routes_of(world.conn), **options)


def fts_row(world, entry_id):
    row = world.conn.execute("SELECT title, summary, tags, body FROM knowledge_fts WHERE id = ?",
                             (entry_id,)).fetchone()
    return None if row is None else tuple(row)


def seed(world):
    first = world.entry("DP-0001", "owner-check", "按公司过滤", "查询缺少公司过滤", tags=("path:src/", "权限"),
                        related=("TO-0002",))
    second = world.entry("TO-0002", "soft-delete", "软删除", "软删除是已接受的取舍")
    world.issue("0007", "order-owner", "订单查询缺少公司过滤")
    return first, second


def test_a_full_sync_indexes_entries_and_documents(world):
    seed(world)
    report = synchronizer(world).sync()
    assert (report.added, report.updated, report.removed, report.errors) == (["0007", "DP-0001", "TO-0002"], [], [], [])
    record = knowledge.get(world.conn, "DP-0001")
    assert (record.title, record.tags, record.related, record.hits) == ("按公司过滤", ("path:src/", "权限"), ("TO-0002",), 0)
    assert fts_row(world, "DP-0001") == ("按 公 司 过 滤", "查 询 缺 少 公 司 过 滤", "path:src/ 权 限",
                                         "# 按 公 司 过 滤 正 文 。")
    assert knowledge.get(world.conn, "0007").tags == ("path:src/OrderService.cs",)
    assert sequences.current(world.conn, "knowledge-DP") == 1


def test_unchanged_files_are_not_read_and_touched_files_only_update_mtime(world):
    first, _ = seed(world)
    synchronizer(world).sync()
    world.clock.advance(timedelta(hours=1))
    assert synchronizer(world).sync().changed is False
    stat = first.stat()
    os.utime(first, (stat.st_atime, stat.st_mtime + 100))
    report = synchronizer(world).sync()
    record = knowledge.get(world.conn, "DP-0001")
    assert report.changed is False
    assert (record.file_mtime, record.indexed_at) == (stat.st_mtime + 100, world.clock.now() - timedelta(hours=1))


def test_changes_keep_hits_and_deleted_files_drop_rows(world):
    first, second = seed(world)
    synchronizer(world).sync()
    knowledge.record_hit(world.conn, "DP-0001", world.clock.now())
    world.entry("DP-0001", "owner-check", "按公司与部门过滤", "查询缺少公司过滤", tags=("path:src/",))
    report = synchronizer(world).sync()
    assert report.updated == ["DP-0001"]
    record = knowledge.get(world.conn, "DP-0001")
    assert (record.title, record.hits) == ("按公司与部门过滤", 1)
    world.layout.issue_file("0007", "order-owner").unlink()
    assert synchronizer(world).sync().removed == ["0007"]
    assert knowledge.get(world.conn, "0007") is None and fts_row(world, "0007") is None


def test_an_id_change_on_the_same_path_replaces_the_row(world):
    world.issue("0007", "order-owner", "订单查询缺少公司过滤")
    synchronizer(world).sync()
    path = world.layout.issue_file("0007", "order-owner")
    path.write_text(path.read_text(encoding="utf-8").replace("id: '0007'", "id: '0008'"), encoding="utf-8")
    path.rename(world.layout.issue_file("0008", "order-owner"))
    report = synchronizer(world).sync()
    assert (report.added, report.removed) == (["0008"], ["0007"])


def test_errors_are_all_listed_and_nothing_is_written(world):
    seed(world)
    synchronizer(world).sync()
    world.entry("DP-0003", "dangling", "悬空", "引用不存在", related=("FL-0009",))
    world.entry("DP-0004", "cycle-a", "循环甲", "甲", status="superseded", superseded_by="DP-0005")
    world.entry("DP-0005", "cycle-b", "循环乙", "乙", status="superseded", superseded_by="DP-0004")
    world.entry("TO-0002", "copy", "重复", "与 TO-0002 同编号")
    missing = world.layout.knowledge_file(KnowledgeType.FIX_LESSON, "FL-0001", "missing")
    world.write(missing, {"id": "FL-0001", "type": "fix-lesson", "tags": ["x"], "status": "active",
                          "updated": "2026-09-01", "reviewBy": "2027-01-01"}, "# 缺摘要\n")
    report = synchronizer(world).sync()
    assert [(issue.path.split("/")[-1], issue.pointer) for issue in report.errors] == [
        ("DP-0003-dangling.md", "$.related[0]"),
        ("DP-0004-cycle-a.md", "$.supersededBy"),
        ("DP-0005-cycle-b.md", "$.supersededBy"),
        ("FL-0001-missing.md", "$"),
        ("TO-0002-copy.md", "$.id"),
        ("TO-0002-soft-delete.md", "$.id"),
    ]
    assert "取代关系构成循环：DP-0004 -> DP-0005 -> DP-0004" in report.errors[1].reason
    assert report.changed is False
    assert [record.id for record in knowledge.find(world.conn)] == ["0007", "DP-0001", "TO-0002"]


def test_only_given_paths_are_synced(world):
    first, second = seed(world)
    synchronizer(world).sync()
    world.entry("DP-0001", "owner-check", "新标题", "查询缺少公司过滤", related=("TO-0002",))
    world.entry("TO-0002", "soft-delete", "也改了", "软删除是已接受的取舍")
    assert synchronizer(world).sync(paths=[first]).updated == ["DP-0001"]
    assert knowledge.get(world.conn, "TO-0002").title == "软删除"
    second.unlink()
    assert synchronizer(world).sync(paths=[second]).errors[0].reason == "引用的条目 TO-0002 不存在"
    assert synchronizer(world).sync(full=True).errors != []


def test_full_sync_reindexes_every_file(world):
    seed(world)
    synchronizer(world).sync()
    world.conn.execute("DELETE FROM knowledge_fts")
    assert synchronizer(world).sync().changed is False
    report = synchronizer(world).sync(full=True)
    assert report.updated == ["0007", "DP-0001", "TO-0002"]
    assert fts_row(world, "TO-0002") is not None


def test_a_lock_held_by_another_process_times_out(world):
    seed(world)
    locks.acquire(world.conn, locks.KNOWLEDGE, world.clock, timedelta(minutes=5), holder=Holder(1, "other-host"))
    with pytest.raises(LockTimeout):
        synchronizer(world, lock_wait=timedelta(0)).sync()
    assert knowledge.find(world.conn) == []


def test_entry_status_is_kept(world):
    world.entry("DP-0001", "old", "旧", "旧条目", status="superseded", superseded_by="DP-0002")
    world.entry("DP-0002", "new", "新", "新条目")
    synchronizer(world).sync()
    old = knowledge.get(world.conn, "DP-0001")
    assert (old.status, old.superseded_by) == (KnowledgeStatus.SUPERSEDED, "DP-0002")
    assert sequences.current(world.conn, "knowledge-DP") == 2
