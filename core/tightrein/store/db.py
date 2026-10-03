"""数据库连接与事务(architecture/01 4.1)：WAL、busy timeout、外键开启；事务可以嵌套，内层用 SAVEPOINT。"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tightrein.config import layers

_savepoints = itertools.count(1)


class DatabaseError(Exception):
    """数据库无法按要求打开，例如文件系统不支持 WAL。"""


def connect(path: Path, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """打开数据库文件，不存在时创建。busy_timeout_ms 缺省取 runtime.store.busyTimeoutMs 的核心缺省值。

    连接处于自动提交模式，多条语句需要原子执行时由 transaction 显式开启事务；行可以按列名访问。
    WAL 让探针写信号与聚合读信号互不阻塞(design 2.2)，写入冲突时等待 busy_timeout_ms 毫秒。
    """
    if busy_timeout_ms is None:
        busy_timeout_ms = int(layers.core_value("runtime.store.busyTimeoutMs"))
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
    """最外层以 BEGIN IMMEDIATE 开启，开始时即取得写锁，避免读后升级写锁时死锁；嵌套时用 SAVEPOINT。

    代码块抛出异常时回滚本层并重新抛出，正常结束时提交本层。
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
