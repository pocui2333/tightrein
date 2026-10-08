"""Issue 的两个文件与 issues 表索引(文件为准，索引可从文件重建)。

- `00-issue-record.json`：程序写的记录(状态、严重度、关联问题、根因键、历史等)，issues 表是它的索引；
- `00-issue-body.md`：正文，给人读、给实施拼进提示；用户可以直接编辑(按必需小节的校验见
  `transitions.save_edit`)。程序建好之后只在末尾的「历史」追加行，不再整体重写，所以用户的修改不会被覆盖；
  记录中存正文的内容哈希，与文件不符即说明被手改过。
- 不覆盖没进索引的文件；简称(也是分支名)建好后不能改。
- 从文件重建(`admin rebuild`)：本模块导入时向 store.rebuild 登记 issues 的重建函数 `rebuild_issues`。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.store.files.atomic import write_text
from tightrein.store.files.directories import subdirectories
from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import normalize
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

RECORD = "record"
BODY = "body"
BODY_HASH = "bodyHash"
HISTORY_WRITTEN = "historyWritten"  # 已写进正文「历史」的条数
COLUMNS = ("id", "status", "title", "kind", "origin", "severity", "gate", "stage", "step", "round", "branch", "pr",
           "merge_commit", "deploy", "held_by")


class IssueFileError(ValueError):
    pass


def record_path(layout: WorkspaceLayout, issue_id: str) -> Path:
    return layout.shared_file(issue_id, RECORD, "json")


def body_path(layout: WorkspaceLayout, issue_id: str) -> Path:
    return layout.shared_file(issue_id, BODY, "md")


def read_body(layout: WorkspaceLayout, issue_id: str) -> str:
    path = body_path(layout, issue_id)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def write(runtime: Runtime, record: Issue, *, body: str | None = None) -> None:
    """写正文(新建或用户编辑时给出 body)、把还没写进正文的历史追加进去，再写记录文件与索引。"""
    from tightrein.assess.issue.body import append_history

    layout = runtime.workspace
    _guard(runtime, record, new_body=body is not None)
    text = normalize(body) if body is not None else read_body(layout, record.id)
    entries = list(record.extra.get("history") or [])
    written = int(record.extra.get(HISTORY_WRITTEN, 0))
    if text and written < len(entries):
        text = append_history(text, entries[written:], runtime.language)
    if text:
        write_text(body_path(layout, record.id), text)
        record.extra[BODY_HASH] = content_hash(text)
        record.extra[HISTORY_WRITTEN] = len(entries)
    write_json(record_path(layout, record.id), to_json(record))
    issues.save(runtime.conn, record, runtime.clock)


def edited(layout: WorkspaceLayout, record: Issue) -> bool:
    """正文在程序上次写入之后被手改过。"""
    return content_hash(read_body(layout, record.id)) != record.extra.get(BODY_HASH)


@contextmanager
def restoring(layout: WorkspaceLayout, issue_ids: Iterable[str], extra: Iterable[Path] = ()) -> Iterator[None]:
    """出错时把这些 Issue 的两个文件(与 extra 中的文件)恢复成进入时的内容(原来没有的删除)。"""
    paths = [path for issue_id in issue_ids for path in (record_path(layout, issue_id), body_path(layout, issue_id))]
    with restoring_paths([*paths, *extra]):
        yield


@contextmanager
def restoring_paths(paths: Iterable[Path]) -> Iterator[None]:
    """数据库事务回滚时文件也要回到原样：进入时记下内容，出错时写回(原来没有的删除)。"""
    saved: dict[Path, str | None] = {}
    for path in paths:
        if path not in saved:
            saved[path] = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        yield
    except BaseException:
        for path, text in saved.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                write_text(path, text)
        raise


def to_json(record: Issue) -> dict[str, Any]:
    data: dict[str, Any] = {_camel(column): getattr(record, column) for column in COLUMNS}
    data["extra"] = record.extra
    return data


def from_json(data: dict[str, Any]) -> Issue:
    missing = [_camel(column) for column in ("id", "status", "title", "kind", "origin") if not data.get(_camel(column))]
    if missing:
        raise IssueFileError(f"缺少 {'、'.join(missing)}")
    values: dict[str, Any] = {column: data.get(_camel(column)) for column in COLUMNS}
    return Issue(**values, extra=dict(data.get("extra") or {}))


def rebuild(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """从各 Issue 目录的记录文件重建 issues 表；有不合格的文件就整体不写，一次列出全部错误。"""
    from tightrein.assess.issue.transitions import check
    from tightrein.protocol.naming import SystemClock
    from tightrein.store.rebuild import RebuildError

    found: list[Issue] = []
    errors: list[str] = []
    for directory in subdirectories(layout.issues_dir):
        path = record_path(layout, directory.name)
        if not path.is_file() and not body_path(layout, directory.name).is_file():
            continue  # 不是 Issue 的目录(只有其他步骤的交接)
        try:
            record = from_json(read_json(path))
            if record.id != directory.name:
                raise IssueFileError(f"编号 {record.id} 与目录名不一致")
            check(record)
            found.append(record)
        except (OSError, ValueError, TypeError) as error:
            errors.append(f"{path}：{error}")
    if errors:
        raise RebuildError("issues", errors)
    clock = SystemClock()
    for record in found:
        issues.save(conn, record, clock)
    return len(found)


def _guard(runtime: Runtime, record: Issue, *, new_body: bool) -> None:
    """不覆盖没进索引的文件(新建时编号撞上残留的目录，先 admin rebuild)；简称建好后不能改(也是分支名)。"""
    path = record_path(runtime.workspace, record.id)
    indexed = issues.get(runtime.conn, record.id)
    if new_body and indexed is None and (path.is_file() or body_path(runtime.workspace, record.id).is_file()):
        raise IssueFileError(f"{path.parent} 已有 Issue 文件但不在索引中，不覆盖；先执行 tightrein admin rebuild")
    if indexed is not None and indexed.extra.get("slug") and indexed.extra.get("slug") != record.extra.get("slug"):
        raise IssueFileError(f"Issue {record.id} 的简称不能改：{indexed.extra['slug']} → {record.extra.get('slug')}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def rebuild_issues(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """登记给 store.rebuild 的 issues 重建：先从记录文件重建 issues 表，再把评估对问题的改动(问题目录的
    00-problem-assess.json，比采集最后一份快照新的)叠加到刚由去重日志重放出的 problems 上。"""
    from tightrein.assess.persist import replay

    count = rebuild(layout, conn)
    replay(layout, conn)
    return count


