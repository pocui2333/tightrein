"""各表共用的列转换与读写。

记录是 dataclass，属性名与列名一一对应(小写下划线；写进 JSON 时才转成小驼峰)。
时间属性为带时区的 datetime，列中存 ISO 8601 UTC 文本；JSON 属性存紧凑文本。
每张表的 created_at、updated_at 由这里维护，不放进记录：created_at 只在插入时写。
表名与列名只来自代码中的常量；SQL 中一律加引号，以容纳 commit、trigger、key 等关键字。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import fields
from datetime import datetime
from typing import Any

from tightrein.protocol.naming import format_iso, parse_iso


class Table[R]:
    def __init__(
        self,
        name: str,
        record: type[R],
        *,
        key: str = "id",
        times: Iterable[str] = (),
        json_columns: Iterable[str] = (),
        order_by: str | None = None,
        generated: bool = False,
    ) -> None:
        """generated 为真时主键由数据库生成(自增)，插入时不写主键列。"""
        self.name = name
        self.record = record
        self.key = key
        self.times = frozenset(times)
        self.json_columns = frozenset(json_columns)
        self.order_by = order_by or quote(key)
        self.generated = generated
        self.columns = tuple(item.name for item in fields(record))  # type: ignore[arg-type]

    def encode(self, column: str, value: Any) -> Any:
        if value is None:
            return None
        if column in self.times:
            return format_iso(value)
        if column in self.json_columns:
            return dumps(value)
        return value

    def decode(self, column: str, value: Any) -> Any:
        if value is None:
            return None
        if column in self.times:
            return parse_iso(value)
        if column in self.json_columns:
            return json.loads(value)
        return value

    def from_row(self, row: sqlite3.Row) -> R:
        return self.record(**{column: self.decode(column, row[column]) for column in self.columns})

    def get(self, conn: sqlite3.Connection, key: Any) -> R | None:
        row = conn.execute(f"SELECT * FROM {quote(self.name)} WHERE {quote(self.key)} = ?", (key,)).fetchone()
        return None if row is None else self.from_row(row)

    def find(self, conn: sqlite3.Connection, **filters: Any) -> list[R]:
        """等值条件；值为 None 的条件不参与过滤。"""
        given = {column: value for column, value in filters.items() if value is not None}
        clause = " AND ".join(f"{quote(column)} = ?" for column in given)
        where = f" WHERE {clause}" if clause else ""
        rows = conn.execute(
            f"SELECT * FROM {quote(self.name)}{where} ORDER BY {self.order_by}",
            [self.encode(column, value) for column, value in given.items()],
        ).fetchall()
        return [self.from_row(row) for row in rows]

    def save(self, conn: sqlite3.Connection, record: R, now: datetime) -> None:
        """按主键插入或整行更新；created_at 只在插入时写。"""
        values = self._values(record)
        values["created_at"] = values["updated_at"] = format_iso(now)
        upsert(conn, self.name, values, self.key, keep=("created_at",))

    def insert(self, conn: sqlite3.Connection, record: R, now: datetime) -> int:
        """插入一行，返回生成的主键；用于自增主键的表。"""
        values = self._values(record)
        if self.generated:
            values.pop(self.key)
        values["created_at"] = values["updated_at"] = format_iso(now)
        names = list(values)
        cursor = conn.execute(
            f"INSERT INTO {quote(self.name)} ({', '.join(quote(name) for name in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [values[name] for name in names],
        )
        return int(cursor.lastrowid or 0)

    def delete(self, conn: sqlite3.Connection, key: Any) -> bool:
        cursor = conn.execute(f"DELETE FROM {quote(self.name)} WHERE {quote(self.key)} = ?", (key,))
        return cursor.rowcount > 0

    def _values(self, record: R) -> dict[str, Any]:
        return {column: self.encode(column, getattr(record, column)) for column in self.columns}


def quote(name: str) -> str:
    return f'"{name}"'


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def upsert(
    conn: sqlite3.Connection, table: str, values: Mapping[str, Any], key: str, keep: Iterable[str] = ()
) -> None:
    """按主键插入，已存在时更新其余列；keep 中的列只在插入时写入。"""
    kept = set(keep)
    names = list(values)
    updates = [name for name in names if name != key and name not in kept]
    action = (
        "DO UPDATE SET " + ", ".join(f"{quote(name)} = excluded.{quote(name)}" for name in updates)
        if updates else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO {quote(table)} ({', '.join(quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)}) ON CONFLICT ({quote(key)}) {action}",
        [values[name] for name in names],
    )
