"""受影响的接口与页面、启动模式与相关的其他复现检查(architecture/07 13 第 4 步)。

受影响的接口：修复交接文档的 affectedEndpoints，加上 authz-endpoints 输出中处理方法所在文件属于改动文件的端点；
受影响的页面：affectedPages，加上 page-routes 输出中组件文件属于改动文件的页面路由；两个扩展没有实现时只用交接文档的
清单。需要页面时用 page 模式；相关的其他复现检查为最近结果是 passed、且位置属于受影响的接口或页面、或针对的代码文件
(静态类的 targets、测试类 location 中的路径)与改动文件有交集的检查。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.domain.enums import RegressionKind, RegressionResult
from tightrein.extensions.client import MODE_API, MODE_PAGE
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.manifest import CheckEntry, ManifestInvalid
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import regressions
from tightrein.store.repos.regressions import RegressionCheck



@dataclass(frozen=True)
class Scope:
    endpoints: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()


def compute(fix_outputs: Mapping[str, Any], changed: Sequence[str], endpoints_output: Mapping[str, Any] | None,
            routes_output: Mapping[str, Any] | None) -> Scope:
    files = set(changed)
    endpoints = list(fix_outputs.get("affectedEndpoints") or [])
    endpoints += [f"{item['method']} {item['route']}" for item in (endpoints_output or {}).get("endpoints", [])
                  if item.get("sourceFile") in files]
    pages = list(fix_outputs.get("affectedPages") or [])
    pages += [item["path"] for item in (routes_output or {}).get("routes", []) if item.get("componentFile") in files]
    return Scope(tuple(dict.fromkeys(endpoints)), tuple(dict.fromkeys(pages)), tuple(changed))


def mode(entries: Sequence[CheckEntry], scope: Scope) -> str:
    return MODE_PAGE if scope.pages or any(entry.kind is RegressionKind.PAGE for entry in entries) else MODE_API


def entries_of(directory: Path, checks: Sequence[RegressionCheck]) -> list[CheckEntry]:
    try:
        loaded = manifest.load(directory)
    except ManifestInvalid:
        return []
    return [entry for check in checks if (entry := loaded.entry(check.check_id)) is not None]


def _related(entry: CheckEntry, scope: Scope) -> bool:
    if entry.kind in (RegressionKind.STATIC, RegressionKind.TEST):
        return bool(set(entry.code_paths()) & set(scope.changed))
    return entry.location in scope.endpoints or entry.location in scope.pages


def related_regressions(conn: sqlite3.Connection, layout: WorkspaceLayout, issue_id: str,
                        scope: Scope) -> list[RegressionCheck]:
    found = []
    for check in regressions.find(conn, last_result=RegressionResult.PASSED):
        if check.issue_id == issue_id:
            continue
        try:
            entry = manifest.load(layout.regression_dir(check.issue_id)).entry(check.check_id)
        except ManifestInvalid:
            continue
        if entry is not None and _related(entry, scope):
            found.append(check)
    return found
