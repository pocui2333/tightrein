"""问题的位置(architecture/03 1.2、1.6.6)：从 problems 关联的 signals 中取出路由、页面与文件。

信号的 location 有三种写法：接口为「HTTP 方法 路由模板」，页面为以 / 开头的页面路由，代码为 `文件:类名.方法名`
或 `文件:行号`。同步 Issue 时用路由生成 route: 标签；预取时在调用方没有给出路由的情况下补取三种位置。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.retrieval.frontmatter import PAGE_VALUE, ROUTE_VALUE, RoutesOf
from tightrein.store.repos import problems, signals


@dataclass(frozen=True)
class Locations:
    routes: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


def classify(values: Sequence[str]) -> Locations:
    routes, pages, paths = set(), set(), set()
    for value in values:
        if ROUTE_VALUE.match(value):
            routes.add(value)
        elif PAGE_VALUE.match(value):
            pages.add(value)
        elif ":" in value:
            paths.add(value.split(":", 1)[0])
    return Locations(tuple(sorted(routes)), tuple(sorted(pages)), tuple(sorted(paths)))


def problem_locations(conn: sqlite3.Connection, problem_ids: Sequence[str]) -> Locations:
    values = []
    for problem_id in problem_ids:
        values += [signal.location for signal in signals.get_many(conn, problems.signal_ids(conn, problem_id))]
    return classify(values)


def routes_of(conn: sqlite3.Connection) -> RoutesOf:
    """同步 Issue 时使用：关联问题的路由列表。"""

    def lookup(problem_ids: Sequence[str]) -> list[str]:
        return list(problem_locations(conn, problem_ids).routes)

    return lookup
