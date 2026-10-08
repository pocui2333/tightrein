"""数据库连接、事务与迁移。

每个工作区一个 SQLite 文件，每个进程一个连接。第 1 版表结构是 schema.sql，之后的改动写成
migrations/<三位序号>_<名称>.sql(从 002 起)；已执行的记录在 schema_migrations 表。
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tightrein.protocol.naming import Clock, SystemClock, format_iso

BUSY_TIMEOUT_MS = 5000
SCHEMA = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_FILE_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_RECORDS_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT;"
)
_savepoints = itertools.count(1)
_SYSTEM_CLOCK = SystemClock()


class DatabaseError(Exception):
    """数据库无法按要求打开，例如文件系统不支持 WAL。"""


class MigrationError(DatabaseError):
    """迁移文件不合格、执行失败，或数据库与程序的迁移记录不一致。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def open_database(
    path: Path, *, clock: Clock = _SYSTEM_CLOCK, migrations: Path = MIGRATIONS_DIR
) -> sqlite3.Connection:
    """打开数据库并执行尚未执行的迁移；clock 只用于记下迁移的执行时间。"""
    conn = connect(path)
    try:
        migrate(conn, clock, migrations)
    except BaseException:
        conn.close()
        raise
    return conn


def connect(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """打开数据库文件，不存在时创建；不执行迁移。

    自动提交模式：多条语句要原子执行时由 transaction 显式开启事务。WAL 让读者不被写事务阻塞；
    切换失败(例如网络文件系统)即报错，不悄悄退回回滚日志模式。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode != "wal":
        conn.close()
        raise DatabaseError(f"{path} 无法切换到 WAL 模式，当前为 {mode}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """最外层用 BEGIN IMMEDIATE：开始即取写锁，避免两个事务先读后写、升级写锁时互相等待而死锁。

    嵌套时用 SAVEPOINT，内层出错只回滚内层。代码块抛出异常时回滚本层并重新抛出。
    """
    if conn.in_transaction:
        name = f"sp_{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """全部迁移：schema.sql 为第 1 版，其后是 directory 中的编号迁移(目录不存在即没有)。"""
    migrations: dict[int, Migration] = {1: Migration(1, "schema", SCHEMA)}
    for path in sorted(directory.glob("*.sql")) if directory.is_dir() else []:
        match = _FILE_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"迁移文件名不合格：{path.name}，应为 <三位序号>_<小写名称>.sql")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"迁移序号重复：{path.name} 与 {migrations[version].path.name}")
        migrations[version] = Migration(version, match.group(2), path)
    return [migrations[version] for version in sorted(migrations)]


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    """已执行的迁移：序号到名称；还没有执行过任何迁移时为空。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return {}
    return {row["version"]: row["name"] for row in conn.execute("SELECT version, name FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, clock: Clock, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """执行尚未执行的迁移，返回本次执行的序号。

    每个迁移连同它的执行记录在一个事务中完成，失败整体回滚，数据库停在上一个迁移之后。
    数据库中有程序不认识的序号(被更新的程序写过)或同号不同名时拒绝，不在不明状态上继续。
    """
    if conn.in_transaction:
        raise MigrationError("迁移须在事务之外执行：executescript 会先提交未完成的事务")
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
        _apply(conn, migration, clock)
        executed.append(migration.version)
    return executed


def _apply(conn: sqlite3.Connection, migration: Migration, clock: Clock) -> None:
    # 名称只含小写字母、数字与下划线(文件名已校验)，可以直接写进语句
    record = (
        "INSERT INTO schema_migrations (version, name, applied_at) "
        f"VALUES ({migration.version}, '{migration.name}', '{format_iso(clock.now())}');"
    )
    body = migration.path.read_text(encoding="utf-8")
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{_RECORDS_TABLE}\n{body}\n{record}\nCOMMIT;")
    except sqlite3.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"迁移 {migration.path.name} 执行失败：{error}") from error
