import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.store.db import connect
from tightrein.store.migrations.runner import (
    MIGRATIONS_DIR,
    MigrationError,
    applied,
    discover,
    initialized_at,
    migrate,
    open_database,
)

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)

@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "tightrein.db")
    yield connection
    connection.close()


def columns(connection, table):
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def write_migration(directory, name, sql):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def test_migrate_from_an_empty_database_records_the_version(conn):
    assert applied(conn) == {}
    assert migrate(conn, FixedClock(NOW)) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "initial", "2026-10-05T03:00:00Z"), (2, "server_log_cursors", "2026-10-05T03:00:00Z"),
        (3, "stage_yield", "2026-10-05T03:00:00Z"), (4, "triage_urgency", "2026-10-05T03:00:00Z"),
        (5, "issue_origin", "2026-10-05T03:00:00Z"), (6, "issue_github", "2026-10-05T03:00:00Z"),
        (7, "issue_depends_on", "2026-10-05T03:00:00Z"), (8, "triage_treatment", "2026-10-05T03:00:00Z"),
        (9, "issue_status", "2026-10-05T03:00:00Z"), (10, "loop_onboarding", "2026-10-05T03:00:00Z"),
        (11, "collect_sources", "2026-10-05T03:00:00Z"), (12, "learn", "2026-10-05T03:00:00Z"),
        (13, "drop_prioritize_label", "2026-10-05T03:00:00Z")]


def test_migrate_again_does_nothing(conn):
    migrate(conn, FixedClock(NOW))
    conn.execute("INSERT INTO sequences VALUES ('problem', 3)")
    assert migrate(conn, FixedClock(NOW)) == []
    assert conn.execute("SELECT value FROM sequences").fetchone()[0] == 3
    assert applied(conn) == {1: "initial", 2: "server_log_cursors", 3: "stage_yield", 4: "triage_urgency",
                             5: "issue_origin", 6: "issue_github", 7: "issue_depends_on",
                             8: "triage_treatment", 9: "issue_status",
                             10: "loop_onboarding", 11: "collect_sources", 12: "learn", 13: "drop_prioritize_label"}


def test_open_database_migrates_once(tmp_path):
    first = open_database(tmp_path / "data" / "tightrein.db", FixedClock(NOW))
    first.close()
    second = open_database(tmp_path / "data" / "tightrein.db", FixedClock(NOW))
    assert applied(second) == {1: "initial", 2: "server_log_cursors", 3: "stage_yield", 4: "triage_urgency",
                             5: "issue_origin", 6: "issue_github", 7: "issue_depends_on",
                             8: "triage_treatment", 9: "issue_status",
                             10: "loop_onboarding", 11: "collect_sources", 12: "learn", 13: "drop_prioritize_label"}
    second.close()


def test_tables_are_strict(conn):
    migrate(conn, FixedClock(NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sequences VALUES ('problem', 'many')")


def test_foreign_keys_cascade_from_problems(conn):
    migrate(conn, FixedClock(NOW))
    conn.execute(
        "INSERT INTO problems (id, fingerprint, fingerprint_version, probe, title, status, first_seen_at, last_seen_at, "
        "occurrences, intermittent, clean_covered_runs, scope) "
        "VALUES ('P-0001', 'f1', 1, 'static', 't', 'new', 'x', 'x', 1, 0, 0, '{}')"
    )
    conn.execute(
        "INSERT INTO problem_events (problem_id, at, event, operation, detail) "
        "VALUES ('P-0001', 'x', 'reproduced', 'auto', '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO problem_events (problem_id, at, event, operation, detail) "
            "VALUES ('P-0404', 'x', 'reproduced', 'auto', '{}')"
        )
    conn.execute("DELETE FROM problems")
    assert conn.execute("SELECT COUNT(*) FROM problem_events").fetchone()[0] == 0


def test_knowledge_fts_matches_text_but_not_the_id(conn):
    migrate(conn, FixedClock(NOW))
    conn.execute(
        "INSERT INTO knowledge_fts (id, title, summary, tags, body) VALUES ('DP-0012', '缺少归属校验', 's', 't', 'café 0099')"
    )
    assert conn.execute("SELECT id FROM knowledge_fts WHERE knowledge_fts MATCH 'cafe'").fetchall()[0][0] == "DP-0012"
    assert conn.execute("SELECT id FROM knowledge_fts WHERE knowledge_fts MATCH '0012'").fetchall() == []


def test_a_failed_migration_is_rolled_back(tmp_path, conn):
    directory = tmp_path / "migrations"
    write_migration(directory, "001_first.sql", "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                    "name TEXT NOT NULL, applied_at TEXT NOT NULL);\nCREATE TABLE a (x INTEGER);")
    write_migration(directory, "002_broken.sql", "CREATE TABLE b (x INTEGER);\nINSERT INTO missing VALUES (1);")
    with pytest.raises(MigrationError, match="002_broken.sql"):
        migrate(conn, FixedClock(NOW), directory)
    assert applied(conn) == {1: "first"}
    assert columns(conn, "a") == ["x"]
    assert columns(conn, "b") == []
    assert not conn.in_transaction


def test_unknown_or_renamed_versions_in_the_database_are_rejected(tmp_path, conn):
    migrate(conn, FixedClock(NOW))
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'later', 'x')")
    with pytest.raises(MigrationError, match=r"\[99\]"):
        migrate(conn, FixedClock(NOW))
    conn.execute("DELETE FROM schema_migrations WHERE version = 99")
    conn.execute("UPDATE schema_migrations SET name = 'renamed'")
    with pytest.raises(MigrationError, match="renamed"):
        migrate(conn, FixedClock(NOW))


def test_discover_rejects_bad_names_and_duplicates(tmp_path):
    write_migration(tmp_path / "bad", "1_initial.sql", "")
    with pytest.raises(MigrationError, match="1_initial.sql"):
        discover(tmp_path / "bad")
    write_migration(tmp_path / "dup", "001_a.sql", "")
    write_migration(tmp_path / "dup", "001_b.sql", "")
    with pytest.raises(MigrationError, match="重复"):
        discover(tmp_path / "dup")
    assert [migration.name for migration in discover()] == [
        "initial", "server_log_cursors", "stage_yield", "triage_urgency", "issue_origin", "issue_github", "issue_depends_on",
        "triage_treatment", "issue_status", "loop_onboarding", "collect_sources", "learn",
        "drop_prioritize_label"]


def test_migrate_refuses_to_run_inside_a_transaction(conn):
    conn.execute("BEGIN")
    with pytest.raises(MigrationError, match="事务"):
        migrate(conn, FixedClock(NOW))
    conn.execute("ROLLBACK")


def test_cursor_table_of_the_first_version_is_rebuilt(tmp_path, conn):
    first = tmp_path / "first"
    first.mkdir()
    shutil.copy(MIGRATIONS_DIR / "001_initial.sql", first / "001_initial.sql")
    assert migrate(conn, FixedClock(NOW), first) == [1]
    conn.execute("INSERT INTO server_log_cursors VALUES ('app.log', 'h', 10, NULL, 'x')")
    assert migrate(conn, FixedClock(NOW)) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    assert columns(conn, "source_cursors") == ["source", "cursor", "parse_state", "updated_at"]
    assert conn.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0] == 0


def test_initialized_at_is_the_first_migration_time(conn):
    assert initialized_at(conn) is None
    migrate(conn, FixedClock(NOW))
    assert initialized_at(conn) == NOW


def test_issue_statuses_are_converged_to_six(tmp_path, conn):
    """迁移 009：八种状态与 hold 按 domain/issue.legacy_status 的映射改写，issue_events 同步。"""
    upto = tmp_path / "upto8"
    upto.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("00[1-8]_*.sql")):
        shutil.copy(path, upto / path.name)
    migrate(conn, FixedClock(NOW), upto)
    rows = [("0001", "merged", None, None), ("0002", "needs-decision", None, None), ("0003", "fixing", None, None),
            ("0004", "todo", None, '{"reason": "r"}'), ("0005", "closed", "wont-fix", None),
            ("0006", "closed", "fixed", None), ("0007", "pr-review", None, None), ("0008", "to-submit", None, None)]
    for issue_id, status, reason, hold in rows:
        conn.execute("INSERT INTO issues (id, slug, path, title, status, close_reason, severity, problems, "
                     "root_cause, hold, file_sha256, created_at, updated_at, origin) "
                     "VALUES (?, 's', ?, 't', ?, ?, 'P1', '[]', '[]', ?, 'h', 'x', 'x', 'triage')",
                     (issue_id, f"issues/{issue_id}-s.md", status, reason, hold))
    conn.execute("INSERT INTO issue_events (issue_id, at, event, from_status, to_status, actor) "
                 "VALUES ('0002', 'x', 'approve', 'in-review', 'todo', 'user')")
    assert migrate(conn, FixedClock(NOW)) == [9, 10, 11, 12, 13]
    found = {row[0]: tuple(row[1:]) for row in conn.execute(
        "SELECT id, status, phase, close_reason, hold IS NOT NULL FROM issues")}
    assert found == {"0001": ("done", "deploy-check", "fixed", 0), "0002": ("needs-decision", None, None, 0),
                     "0003": ("in-progress", "fix", None, 0), "0004": ("needs-decision", None, None, 1),
                     "0005": ("cancelled", None, "wont-fix", 0), "0006": ("done", None, "fixed", 0),
                     "0007": ("pending-merge", None, None, 0), "0008": ("in-progress", "submit", None, 0)}
    assert tuple(conn.execute("SELECT from_status, to_status FROM issue_events").fetchone()) == (
        "needs-decision", "todo")


def test_existing_workspaces_run_and_old_reproductions_are_dropped(tmp_path, conn):
    """迁移 010：已有工作区视为运行中；verify 的 reproduce 交接记录删除，其余保留。"""
    upto = tmp_path / "upto9"
    upto.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("00[1-9]_*.sql")):
        shutil.copy(path, upto / path.name)
    migrate(conn, FixedClock(NOW), upto)
    for phase in ("reproduce", "local"):
        conn.execute("INSERT INTO handoffs (run_id, stage, phase, subject_id, attempt, status, path, schema_version, "
                     "created_at) VALUES ('R-1', 'verify', ?, '0001', 1, 'ok', 'p', 1, 'x')", (phase,))
    assert migrate(conn, FixedClock(NOW)) == [10, 11, 12, 13]
    assert [row[0] for row in conn.execute("SELECT phase FROM handoffs")] == ["local"]
    assert conn.execute("SELECT value FROM workspace_meta WHERE key = 'phase'").fetchone()[0] == "running"


def test_collect_sources_rewrites_old_probes_statuses_and_coverage(tmp_path, conn):
    """迁移 011：server-log 改为 platform-errors，e2e 只保留有 Issue 的问题，不稳定改为待确认并标记间歇，环境问题删除，
    覆盖与范围的 JSON 改写，关联表与运行的两列删除。"""
    upto = tmp_path / "upto10"
    upto.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("0[01][0-9]_*.sql")):
        if path.name < "011":
            shutil.copy(path, upto / path.name)
    migrate(conn, FixedClock(NOW), upto)
    conn.execute("INSERT INTO server_log_cursors VALUES ('s', '{}', NULL, 'x')")
    run = ("INSERT INTO runs (id, stage, probe, started_at, coverage, environment_verdict, environment_detail, status) "
           "VALUES (?, 'collect', ?, 'x', ?, ?, '{}', 'ok')")
    conn.execute(run, ("R-1", "server-log", '{"serverLog": true}', "ok"))
    conn.execute(run, ("R-2", "e2e", '{"pages": ["/a"], "cases": ["c"]}', "suspected-outage"))
    conn.execute(run, ("R-3", "api-fuzz", '{"endpointsTotal": 3}', None))
    signal = ("INSERT INTO signals (id, run_id, source, probe, \"check\", environment, occurred_at, location, message, "
              "context, actor, suppressed, aggregate_state) VALUES (?, ?, 'error', ?, 'c', 'staging', 'x', 'l', 'm', "
              "'{}', '{}', 0, ?)")
    conn.execute(signal, ("S-1", "R-1", "server-log", "done"))
    conn.execute(signal, ("S-2", "R-2", "e2e", "done"))
    conn.execute(signal, ("S-3", "R-3", "api-fuzz", "held"))
    problem = ("INSERT INTO problems (id, fingerprint, fingerprint_version, probe, title, status, first_seen_at, "
               "last_seen_at, occurrences, issue_id, intermittent, clean_covered_runs, scope) "
               "VALUES (?, ?, 1, ?, 't', ?, 'x', 'x', 1, ?, 0, 0, ?)")
    scope = '{"location": "L", "roles": ["A"], "case": null, "page": null}'
    for row in (("P-0001", "f1", "server-log", "new", None), ("P-0002", "f2", "e2e", "new", None),
                ("P-0003", "f3", "e2e", "ongoing", "0007"), ("P-0004", "f4", "api-fuzz", "flaky", None),
                ("P-0005", "f5", "api-fuzz", "environment", None)):
        conn.execute(problem, (*row, scope))
    conn.execute("INSERT INTO problem_events (problem_id, at, event, from_status, to_status, operation, detail) "
                 "VALUES ('P-0004', 'x', 'not-reproduced', 'pending', 'flaky', 'auto', '{}')")
    assert migrate(conn, FixedClock(NOW)) == [11, 12, 13]
    found = {row["id"]: (row["probe"], row["status"], row["intermittent"]) for row in conn.execute(
        "SELECT id, probe, status, intermittent FROM problems ORDER BY id")}
    assert found == {"P-0001": ("platform-errors", "new", 0), "P-0003": ("incidental", "ongoing", 0),
                     "P-0004": ("api-fuzz", "pending", 1)}
    assert conn.execute("SELECT scope FROM problems WHERE id = 'P-0001'").fetchone()[0] == (
        '{"location":"L","roles":["A"],"source":"log-platform"}')
    assert [tuple(row) for row in conn.execute("SELECT id, probe, coverage FROM runs ORDER BY id")] == [
        ("R-1", "platform-errors", '{"sources":["log-platform"]}'), ("R-3", "api-fuzz", '{"endpointsTotal":3}')]
    assert [tuple(row) for row in conn.execute("SELECT id, aggregate_state FROM signals ORDER BY id")] == [
        ("S-1", "done"), ("S-3", "voided")]
    assert conn.execute("SELECT to_status FROM problem_events").fetchone()[0] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0] == 0
    assert "environment_verdict" not in columns(conn, "runs") and "companion_run_id" not in columns(conn, "runs")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"pending_claims", "probe_states", "source_cursors"} <= tables and "problem_links" not in tables


def test_learn_adds_tool_and_model_and_drops_the_improve_tables(tmp_path, conn):
    """迁移 012：stage_yield 加工具与模型列，提案、失败归类与 improve 状态表删除，缺陷模式与评测用例建议、合入提案的操作删除。"""
    upto = tmp_path / "upto11"
    upto.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("0[01][0-9]_*.sql")):
        if path.name < "012":
            shutil.copy(path, upto / path.name)
    migrate(conn, FixedClock(NOW), upto)
    suggestion = "INSERT INTO suggestions (id, kind, subject, evidence, status, created_at) VALUES (?, ?, 's', '{}', 'pending', 'x')"
    for row in (("LS-0001", "defect-pattern"), ("LS-0002", "probe-config"), ("LS-0003", "eval-case")):
        conn.execute(suggestion, row)
    operation = ("INSERT INTO pending_operations (id, stage, subject_id, kind, executor, impact, reversible, "
                 "idempotency_key, confirmations_required, status, created_at, commands, description, preconditions, "
                 "confirmations_given) VALUES (?, 'improve', 's', ?, 'vcs', 'i', 1, ?, 1, 'pending', 'x', '[]', '{}', "
                 "'{}', 0)")
    conn.execute(operation, ("OP-0001", "apply-proposal", "k1"))
    conn.execute(operation, ("OP-0002", "push", "k2"))
    assert migrate(conn, FixedClock(NOW)) == [12, 13]
    assert {"tool", "model"} <= set(columns(conn, "stage_yield"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"proposals", "failures", "improve_state"} & tables
    assert [row[0] for row in conn.execute("SELECT id FROM suggestions")] == ["LS-0002"]
    assert [row[0] for row in conn.execute("SELECT id FROM pending_operations")] == ["OP-0002"]


def test_the_retired_prioritize_label_is_dropped_from_triage_results(tmp_path, conn):
    """迁移 013：分诊结果的 labels 去掉已由处理标签取代的 prioritize，其余标签保留。"""
    upto = tmp_path / "upto12"
    upto.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("0[01][0-9]_*.sql")):
        if path.name < "013":
            shutil.copy(path, upto / path.name)
    migrate(conn, FixedClock(NOW), upto)
    for problem_id, labels in (("P-1", '["prioritize"]'), ("P-2", '["prioritize", "discuss-with-author"]'),
                               ("P-3", '["discuss-with-author"]')):
        conn.execute("INSERT INTO triage_results (problem_id, attempt, run_id, verdict, root_causes, disposition, "
                     "reason, triage_commit, flags, labels, created_at) "
                     "VALUES (?, 1, 'R-1', 'confirmed', '[]', 'issue', 'r', 'c', '{}', ?, 'x')", (problem_id, labels))
    assert migrate(conn, FixedClock(NOW)) == [13]
    found = dict(conn.execute("SELECT problem_id, labels FROM triage_results"))
    assert found == {"P-1": "[]", "P-2": '["discuss-with-author"]', "P-3": '["discuss-with-author"]'}
