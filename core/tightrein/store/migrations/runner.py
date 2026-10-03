"""迁移执行器：按序号执行 migrations 目录中的 `<三位序号>_<名称>.sql`，已执行的记录在 schema_migrations 表。

schema_migrations 本身由 001_initial.sql 建立；表不存在即视为空库。每个迁移连同它的执行记录在一个事务中完成，
失败时整体回滚，数据库停在上一个迁移之后。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tightrein.domain.clock import Clock, format_iso, parse_iso
from tightrein.store.db import connect

MIGRATIONS_DIR = Path(__file__).parent
_FILE_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """迁移文件不合格、执行失败，或数据库与程序的迁移记录不一致。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"迁移文件名不合格：{path.name}，应为 <三位序号>_<小写名称>.sql")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"迁移序号重复：{path.name} 与 {migrations[version].path.name}")
        migrations[version] = Migration(version, match.group(2), path)
    return [migrations[version] for version in sorted(migrations)]


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    """已执行的迁移：序号到名称。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return {}
    return {row["version"]: row["name"] for row in conn.execute("SELECT version, name FROM schema_migrations")}


def initialized_at(conn: sqlite3.Connection) -> datetime | None:
    """工作区数据库建立的时间：最早一次迁移的执行时间；还没有执行过迁移时为空。"""
    if not applied(conn):
        return None
    row = conn.execute("SELECT MIN(applied_at) AS at FROM schema_migrations").fetchone()
    return parse_iso(row["at"])


def migrate(conn: sqlite3.Connection, clock: Clock, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """执行尚未执行的迁移，返回本次执行的序号；全部已执行时返回空列表。"""
    if conn.in_transaction:
        raise MigrationError("迁移须在事务之外执行")
    migrations = discover(directory)
    done = applied(conn)
    unknown = sorted(set(done) - {migration.version for migration in migrations})
    if unknown:
        raise MigrationError(f"数据库中有程序不认识的迁移 {unknown}，数据库由更新的程序写入过")
    for migration in migrations:
        if migration.version in done and done[migration.version] != migration.name:
            raise MigrationError(
                f"迁移 {migration.version:03d} 在数据库中名为 {done[migration.version]}，文件名为 {migration.name}"
            )
    executed: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        record = (
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({migration.version}, '{migration.name}', '{format_iso(clock.now())}');"
        )
        script = f"BEGIN IMMEDIATE;\n{migration.path.read_text(encoding='utf-8')}\n{record}\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error as error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"迁移 {migration.path.name} 执行失败：{error}") from error
        executed.append(migration.version)
    return executed


def open_database(path: Path, clock: Clock, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """打开数据库并执行尚未执行的迁移。"""
    conn = connect(path, busy_timeout_ms)
    try:
        migrate(conn, clock)
    except BaseException:
        conn.close()
        raise
    return conn
