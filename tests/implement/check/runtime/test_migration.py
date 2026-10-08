from __future__ import annotations

from datetime import timedelta
from typing import Any

from tightrein.implement.check.changes import collect
from tightrein.implement.check.runtime import migration
from tightrein.implement.context import Decision
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import format_iso

DESIGN = {"migration": {"entries": ["加 orders.created_at 索引"], "reversible": True, "revertMethod": "删除索引"}}


def test_changed_migrations_are_listed_from_the_diff_only(world: Any) -> None:
    world.repo.write({"migrations/0002_index.sql": "\nCREATE INDEX idx ON orders(created_at);\n",
                      "src/orders.py": "X = 1\n"})
    changes = collect(world.git(), world.repo.base)
    files = migration.changed(changes.paths, ["migrations/"])
    assert files == ["migrations/0002_index.sql"]
    request = migration.request(world.repo.path, changes.lines, files, DESIGN)
    assert request.entries == ("CREATE INDEX idx ON orders(created_at);",)
    assert request.revert == "可以撤销：删除索引" and "CREATE INDEX" in request.text()
    assert migration.request(world.repo.path, changes.lines, files, {}).revert == "方案没有说明能否撤销"
    # 迁移文件一改哈希就变
    before = request.hash
    world.repo.write({"migrations/0002_index.sql": "CREATE INDEX idx2 ON orders(created_at);\n"})
    assert migration.files_hash(world.repo.path, files) != before


def test_migrations_need_a_confirmation_on_the_same_hash(world: Any) -> None:
    waiting = world.handoff("implement.check", {"migration": {"hash": "abc"}}, Status.PENDING)
    later = format_iso(world.clock.now() + timedelta(minutes=5))
    approved = Decision("implement.check", "approve", None, None, later)
    assert migration.confirmed(world.context(latest={"implement.check": waiting}, decisions=[approved]), "abc")
    # 哈希变了、没有决定、决定早于停下、决定是 reject，都不算确认
    assert not migration.confirmed(world.context(latest={"implement.check": waiting}, decisions=[approved]), "def")
    assert not migration.confirmed(world.context(latest={"implement.check": waiting}), "abc")
    earlier = Decision("implement.check", "approve", None, None, format_iso(world.clock.now() - timedelta(hours=1)))
    assert not migration.confirmed(world.context(latest={"implement.check": waiting}, decisions=[earlier]), "abc")
    rejected = Decision("implement.check", "reject", None, "不行", later)
    assert not migration.confirmed(world.context(latest={"implement.check": waiting}, decisions=[rejected]), "abc")
