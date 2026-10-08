import sqlite3

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue


def test_save_get_and_find_by_status(conn: sqlite3.Connection, clock: FixedClock) -> None:
    issue = Issue("0018", "todo", "订单查询缺少归属校验", "bug", "problem", severity="P1")
    issues.save(conn, issue, clock)
    issues.save(conn, Issue("0019", "needs_decision", "导出", "feature", "user"), clock)
    assert issues.get(conn, "0018") == issue
    issue.status, issue.stage, issue.step, issue.round, issue.pr = "implementing", "implement", "code", 2, 57
    issues.save(conn, issue, clock)
    assert issues.find(conn, status="implementing") == [issue]
    assert [item.id for item in issues.find(conn)] == ["0018", "0019"]
    assert issues.get(conn, "0404") is None
