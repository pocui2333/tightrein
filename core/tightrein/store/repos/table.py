"""仓储共用的列转换与 SQL 拼装。

列转换把 Python 值与列中的文本互转：时间为 format_iso 的 UTC 文本，日期为 YYYY-MM-DD，枚举为取值，JSON 为紧凑文本，
布尔为 0 或 1；None 写为 NULL，作为主键一部分的列可以指定写为空串。表名与列名只来自代码中的常量，SQL 中一律加引号，以容纳 check、commit 等关键字。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import date
from enum import Enum
from typing import Any, Generic, TypeVar

from tightrein.domain.clock import format_iso, parse_iso

R = TypeVar("R")


@dataclass(frozen=True)
class Codec:
    """一列的转换：encode 从 Python 值到列值，decode 反向，两者都不会收到空值。

    empty 是 Python 的 None 在列中的写法，默认也为 NULL；作为主键一部分的列不能为 NULL，用空串表示。
    """

    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any]
    empty: Any = None

    def to_column(self, value: Any) -> Any:
        return self.empty if value is None else self.encode(value)

    def from_column(self, value: Any) -> Any:
        return None if value is None or value == self.empty else self.decode(value)


TIME = Codec(format_iso, parse_iso)
DATE = Codec(lambda value: value.isoformat(), date.fromisoformat)
JSON = Codec(lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")), json.loads)
BOOL = Codec(lambda value: 1 if value else 0, bool)
STRINGS = Codec(lambda value: json.dumps(list(value), ensure_ascii=False, separators=(",", ":")),
                lambda text: tuple(json.loads(text)))


def enum_codec(cls: type[Enum], empty: Any = None) -> Codec:
    return Codec(lambda member: member.value, cls, empty)


def quote(name: str) -> str:
    return f'"{name}"'


def upsert(
    conn: sqlite3.Connection, table: str, values: Mapping[str, Any], keys: Iterable[str], keep: Iterable[str] = ()
) -> None:
    """按主键或唯一键插入，已存在时更新其余列；keep 中的列只在插入时写入，更新时保留原值。"""
    key_names = tuple(keys)
    kept = set(keep)
    names = list(values)
    updates = [name for name in names if name not in key_names and name not in kept]
    action = (
        "DO UPDATE SET " + ", ".join(f"{quote(name)} = excluded.{quote(name)}" for name in updates)
        if updates else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO {quote(table)} ({', '.join(quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)}) "
        f"ON CONFLICT ({', '.join(quote(name) for name in key_names)}) {action}",
        [values[name] for name in names],
    )


def insert(conn: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> int:
    """插入一行，返回 rowid；用于自增主键的表。"""
    names = list(values)
    cursor = conn.execute(
        f"INSERT INTO {quote(table)} ({', '.join(quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [values[name] for name in names],
    )
    return int(cursor.lastrowid or 0)


def given(filters: Mapping[str, Any]) -> dict[str, Any]:
    """去掉值为 None 的条件：仓储查询函数的参数为 None 表示不按该列过滤。"""
    return {name: value for name, value in filters.items() if value is not None}


def where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    """等值条件；值为 None 时匹配空值。没有条件时返回空串。"""
    clauses: list[str] = []
    params: list[Any] = []
    for name, value in filters.items():
        if value is None:
            clauses.append(f"{quote(name)} IS NULL")
        else:
            clauses.append(f"{quote(name)} = ?")
            params.append(value)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def select(
    conn: sqlite3.Connection, table: str, filters: Mapping[str, Any], order_by: str | None = None
) -> list[sqlite3.Row]:
    clause, params = where(filters)
    order = f" ORDER BY {order_by}" if order_by else ""
    return conn.execute(f"SELECT * FROM {quote(table)}{clause}{order}", params).fetchall()


def delete(conn: sqlite3.Connection, table: str, filters: Mapping[str, Any]) -> int:
    if not filters:
        raise ValueError("删除须带条件")
    clause, params = where(filters)
    return conn.execute(f"DELETE FROM {quote(table)}{clause}", params).rowcount


class Table(Generic[R]):
    """字段名与列名一一对应的记录表：record 是冻结的数据类，codecs 给出需要转换的字段。"""

    def __init__(
        self,
        name: str,
        record: type[R],
        keys: tuple[str, ...],
        codecs: Mapping[str, Codec] | None = None,
        order_by: str | None = None,
        generated: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.record = record
        self.keys = keys
        self.codecs = dict(codecs or {})
        self.order_by = order_by
        self.generated = generated
        self.columns = tuple(field.name for field in fields(record))

    def encode(self, name: str, value: Any) -> Any:
        codec = self.codecs.get(name)
        return value if codec is None else codec.to_column(value)

    def to_row(self, record: R) -> dict[str, Any]:
        return {
            name: self.encode(name, getattr(record, name))
            for name in self.columns
            if not (name in self.generated and getattr(record, name) is None)
        }

    def from_row(self, row: sqlite3.Row) -> R:
        values = {}
        for name in self.columns:
            codec = self.codecs.get(name)
            values[name] = row[name] if codec is None else codec.from_column(row[name])
        return self.record(**values)

    def save(self, conn: sqlite3.Connection, record: R) -> None:
        upsert(conn, self.name, self.to_row(record), self.keys)

    def insert(self, conn: sqlite3.Connection, record: R) -> int:
        return insert(conn, self.name, self.to_row(record))

    def get(self, conn: sqlite3.Connection, **keys: Any) -> R | None:
        rows = select(conn, self.name, {name: self.encode(name, value) for name, value in keys.items()})
        return self.from_row(rows[0]) if rows else None

    def find(self, conn: sqlite3.Connection, **filters: Any) -> list[R]:
        encoded = {name: self.encode(name, value) for name, value in filters.items()}
        return [self.from_row(row) for row in select(conn, self.name, encoded, self.order_by)]

    def delete(self, conn: sqlite3.Connection, **keys: Any) -> int:
        return delete(conn, self.name, {name: self.encode(name, value) for name, value in keys.items()})
