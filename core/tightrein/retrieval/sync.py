"""知识文件到 knowledge_meta 与索引器的同步(architecture/03 1.6.3)。

1. 持有对象锁 knowledge，与其他进程(MCP 服务、定时运行、终端命令)的同步与写入互斥；等待超时抛出 LockTimeout；
2. 列出来源文件，与 knowledge_meta 的 path、file_mtime、file_size 比较：未变化的不读文件；可能变化的计算
   sha256，与记录一致的只更新 mtime 与大小；full 为真时全部重新解析并重建索引(文本预处理或索引器改动之后使用)；
3. 解析并校验变化的文件，再在「未变化的行 + 本次解析结果」上做全库一致性检查：编号不重复、related 与
   supersededBy 引用的条目存在、取代关系不构成循环；
4. 有任何问题时不写入，返回全部问题，索引保持上一次成功同步后的状态；
5. 没有问题时在一个事务中写入：新增与变化的行 upsert(保留 hits 与 last_hit_at)、同一路径编号变化的按旧编号删除
   新编号新增、已删除的文件删除行，每一行都调用全部索引器；条目编号所在的序列提升到不小于已有的最大序号。
paths 给出时只处理这些文件(写入后与 get 发现文件变化时使用)，不在其中的行视为未变化。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from tightrein.config import layers
from tightrein.domain import ids
from tightrein.domain.clock import Clock
from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.errors import FrontmatterIssue, LockTimeout
from tightrein.retrieval.frontmatter import IndexedEntry, RoutesOf, parse_source
from tightrein.retrieval.indexers import Indexer
from tightrein.retrieval.sources import SourceFile, list_sources, source_for
from tightrein.store import locks, sequences
from tightrein.store.db import transaction
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import ENTRY_TYPES, KnowledgeRecord



@dataclass(frozen=True)
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[FrontmatterIssue] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def counts(self) -> dict[str, int]:
        return {"added": len(self.added), "updated": len(self.updated), "removed": len(self.removed),
                "errors": len(self.errors)}


@dataclass(frozen=True)
class _Node:
    """一致性检查用的一行：来自未变化的记录或本次解析结果。"""

    id: str
    type: str
    status: KnowledgeStatus
    path: str
    related: tuple[str, ...]
    superseded_by: str | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _consistency(nodes: Sequence[_Node]) -> list[FrontmatterIssue]:
    problems: list[FrontmatterIssue] = []
    by_id: dict[str, list[_Node]] = {}
    for node in nodes:
        by_id.setdefault(node.id, []).append(node)
    for entry_id, same in sorted(by_id.items()):
        if len(same) > 1:
            paths = "、".join(sorted(node.path for node in same))
            problems += [FrontmatterIssue(node.path, "$.id", f"编号 {entry_id} 重复：{paths}") for node in same]
    entries = {node.id: node for node in nodes if node.type in ENTRY_TYPES}
    for node in nodes:
        for index, target in enumerate(node.related):
            if target not in entries:
                problems.append(FrontmatterIssue(node.path, f"$.related[{index}]", f"引用的条目 {target} 不存在"))
        if node.superseded_by is not None and node.superseded_by not in entries:
            problems.append(FrontmatterIssue(node.path, "$.supersededBy", f"引用的条目 {node.superseded_by} 不存在"))
    for node in entries.values():
        seen = [node.id]
        current = node
        while current.superseded_by is not None and current.superseded_by in entries:
            if current.superseded_by in seen:
                chain = " -> ".join([*seen, current.superseded_by])
                problems.append(FrontmatterIssue(node.path, "$.supersededBy", f"取代关系构成循环：{chain}"))
                break
            seen.append(current.superseded_by)
            current = entries[current.superseded_by]
    return sorted(set(problems))


class Synchronizer:
    def __init__(self, layout: WorkspaceLayout, conn: sqlite3.Connection, clock: Clock,
                 indexers: Sequence[Indexer], routes_of: RoutesOf, *, lock_wait: timedelta | None = None,
                 lock_ttl: timedelta | None = None) -> None:
        """锁的等待上限与时限取 runtime.retrieval.lockWaitSeconds、lockMinutes，没有给出时取核心缺省值。"""
        self.layout = layout
        self.conn = conn
        self.clock = clock
        self.indexers = tuple(indexers)
        self.routes_of = routes_of
        self.lock_wait = lock_wait if lock_wait is not None else timedelta(
            seconds=float(layers.core_value("runtime.retrieval.lockWaitSeconds")))
        self.lock_ttl = lock_ttl if lock_ttl is not None else timedelta(
            minutes=float(layers.core_value("runtime.retrieval.lockMinutes")))

    def sync(self, full: bool = False, paths: Sequence[Path] | None = None) -> SyncReport:
        try:
            with locks.held(self.conn, locks.KNOWLEDGE, self.clock, self.lock_ttl, wait=self.lock_wait):
                return self._sync(full, paths)
        except locks.LockHeld as error:
            raise LockTimeout(f"等待对象锁 knowledge 超时：{error}") from error

    def _sources(self, paths: Sequence[Path] | None) -> tuple[list[SourceFile], set[str] | None]:
        if paths is None:
            return list_sources(self.layout), None
        chosen = [source for source in (source_for(self.layout, path) for path in paths) if source is not None]
        return [source for source in chosen if source.path.is_file()], {source.relative for source in chosen}

    def _sync(self, full: bool, paths: Sequence[Path] | None) -> SyncReport:
        rows = {record.path: record for record in knowledge.find(self.conn)}
        sources, scope = self._sources(paths)
        parsed: list[tuple[IndexedEntry, str, float, int]] = []
        touched: list[KnowledgeRecord] = []
        problems: list[FrontmatterIssue] = []
        present = set()
        for source in sources:
            present.add(source.relative)
            stat = source.path.stat()
            row = rows.get(source.relative)
            if row is not None and not full and (row.file_mtime, row.file_size) == (stat.st_mtime, stat.st_size):
                continue
            data = source.path.read_bytes()
            digest = _sha256(data)
            if row is not None and not full and row.content_sha256 == digest:
                touched.append(replace(row, file_mtime=stat.st_mtime, file_size=stat.st_size))
                continue
            entry, found = parse_source(source, data.decode("utf-8", errors="replace"), self.routes_of)
            problems += found
            if entry is not None:
                parsed.append((entry, digest, stat.st_mtime, stat.st_size))
        in_scope = rows if scope is None else {path: row for path, row in rows.items() if path in scope}
        gone = [row for path, row in sorted(in_scope.items()) if path not in present]
        replaced_paths = {entry.path for entry, *_ in parsed}
        nodes = [_Node(row.id, row.type, row.status, row.path, row.related, row.superseded_by)
                 for path, row in rows.items() if path not in replaced_paths and row not in gone]
        nodes += [_Node(entry.id, entry.type, entry.status, entry.path, entry.related, entry.superseded_by)
                  for entry, *_ in parsed]
        problems += _consistency(nodes)
        if problems:
            return SyncReport(errors=sorted(set(problems)))
        return self._write(rows, parsed, touched, gone)

    def _write(self, rows: dict[str, KnowledgeRecord], parsed: list[tuple[IndexedEntry, str, float, int]],
               touched: list[KnowledgeRecord], gone: list[KnowledgeRecord]) -> SyncReport:
        report = SyncReport()
        now = self.clock.now()
        with transaction(self.conn):
            for record in touched:
                knowledge.save(self.conn, record)
            for row in gone:
                self._remove(row.id)
                report.removed.append(row.id)
            for entry, digest, mtime, size in parsed:
                previous = rows.get(entry.path)
                if previous is not None and previous.id != entry.id:
                    self._remove(previous.id)
                    report.removed.append(previous.id)
                    previous = None
                knowledge.save(self.conn, KnowledgeRecord(
                    id=entry.id, type=entry.type, status=entry.status, title=entry.title, summary=entry.summary,
                    updated=entry.updated, path=entry.path, content_sha256=digest, file_mtime=mtime, file_size=size,
                    indexed_at=now, tags=entry.tags, related=entry.related, superseded_by=entry.superseded_by,
                    review_by=entry.review_by,
                ))
                for indexer in self.indexers:
                    indexer.upsert(entry, self.conn)
                (report.added if previous is None else report.updated).append(entry.id)
                if entry.type in ENTRY_TYPES:
                    kind = KnowledgeType(entry.type)
                    sequences.ensure_at_least(self.conn, ids.knowledge_sequence(kind), ids.parse_sequence(entry.id))
        return SyncReport(sorted(report.added), sorted(report.updated), sorted(report.removed))

    def _remove(self, entry_id: str) -> None:
        for indexer in self.indexers:
            indexer.remove(entry_id, self.conn)
        knowledge.remove(self.conn, entry_id)
