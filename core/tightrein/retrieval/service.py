"""KnowledgeService：检索的统一入口(architecture/03 1.4、1.6.3、1.8)，命令行与 MCP 服务都只调用它。

- search、get、related、stale、context_for 之前先做一次增量同步；同步发现格式错误或锁被占用时照常基于上一次的
  索引读取，结果的 index_warnings 写明原因；
- 每次 search、get、related、stale、context_for 写一个 execute_tool span(agent 为 kb)，sync 与写入各写一个
  run_script span；search 的 attributes 记录查询串、过滤条件与返回的编号，供挑选检索评测用例；
- get 计一次命中，record_hit 为假或沙箱模式时不计；文件已删除时同步该文件后报编号不存在，内容与索引不一致时
  先同步该文件再返回；
- 沙箱模式(--output，TIGHTREIN_SANDBOX=1)不同步、不记命中、不写入，只读已有索引；事件写到沙箱目录由
  WorkspaceLayout 的 output_dir 决定；
- write 与 set_status 只由流水线模块调用，命令行与 MCP 不提供。写文件在持有对象锁 knowledge 期间完成，
  之后只同步被写的文件并重新生成 INDEX.md。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig, core_config
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import ContextKind, KnowledgeStatus, KnowledgeWriteDecision
from tightrein.observability.tracing import Tracer
from tightrein.retrieval import context, dedup, index_md, search, stale
from tightrein.retrieval.context import ContextBundle, ContextRequest
from tightrein.retrieval.dedup import Decision, WriteOrigin, WriteOutcome
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, KnowledgeError, LockTimeout
from tightrein.retrieval.indexers import FtsIndexer, Indexer
from tightrein.retrieval.locations import routes_of
from tightrein.retrieval.models import (
    EntryDocument,
    RelatedResult,
    SearchFilters,
    SearchResult,
    StaleReport,
)
from tightrein.retrieval.ranking import CandidateSource, FtsSource
from tightrein.retrieval.sync import Synchronizer, SyncReport
from tightrein.retrieval.writer import KnowledgeDraft, KnowledgeWriter, check_draft
from tightrein.runner.service import Runner
from tightrein.store import locks
from tightrein.store.db import connect
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.migrations.runner import applied, discover
from tightrein.store.repos import knowledge

KB_AGENT = "kb"
NO_SIMILAR_REASON = "没有相似条目，直接新增"
WARNING_INVALID = "知识文件有格式错误，本次检索未包含最新改动；运行 tightrein kb sync 查看详情："
WARNING_LOCKED = "知识索引正在被其他进程更新，本次检索基于现有索引"


@dataclass(frozen=True)
class RetrievalSettings:
    """thresholds.retrieval 与 runtime.retrieval 中的取值；default() 为核心缺省值。"""

    unused_days: int
    inline_full_tokens: int
    context_limits: Mapping[ContextKind, int]
    lock_ttl: timedelta
    lock_wait: timedelta
    rrf_k: int
    column_weights: Mapping[str, float]
    candidate_factor: int
    paging: index_md.Paging
    stale_min_shared_tags: int
    stale_max_group_size: int
    dedup_similar_limit: int

    @classmethod
    def from_config(cls, config: ProjectConfig) -> RetrievalSettings:
        def runtime(name: str) -> Any:
            return config.get(f"runtime.retrieval.{name}")

        return cls(
            unused_days=config.whole_threshold("retrieval.unusedDays"),
            inline_full_tokens=config.whole_threshold("retrieval.inlineFullTokens"),
            context_limits={kind: config.whole_threshold(f"retrieval.contextLimits.{kind.value}")
                            for kind in ContextKind},
            lock_ttl=timedelta(minutes=float(runtime("lockMinutes"))),
            lock_wait=timedelta(seconds=float(runtime("lockWaitSeconds"))),
            rrf_k=int(runtime("rrfK")),
            column_weights=dict(runtime("columnWeights")),
            candidate_factor=int(runtime("candidateFactor")),
            paging=index_md.Paging(int(runtime("indexMaxLines")), int(runtime("indexPageSize")),
                                   int(runtime("rootListingLimit"))),
            stale_min_shared_tags=int(runtime("staleMinSharedTags")),
            stale_max_group_size=int(runtime("staleMaxGroupSize")),
            dedup_similar_limit=int(runtime("dedupSimilarLimit")),
        )

    @classmethod
    def default(cls) -> RetrievalSettings:
        return cls.from_config(core_config())


def open_index(layout: WorkspaceLayout) -> sqlite3.Connection:
    """打开已初始化的数据库；文件不存在或迁移未执行时抛出 IndexUnavailable，不创建也不迁移。"""
    path = layout.database()
    if not path.is_file():
        raise IndexUnavailable(f"数据库 {path} 不存在，先执行 tightrein init 初始化工作区")
    try:
        conn = connect(path)
        missing = sorted(set(migration.version for migration in discover()) - set(applied(conn)))
    except sqlite3.Error as error:
        raise IndexUnavailable(f"数据库 {path} 无法读取：{error}") from error
    if missing:
        conn.close()
        raise IndexUnavailable(f"数据库 {path} 还有迁移未执行({missing})，先执行 tightrein init")
    return conn


class KnowledgeService:
    def __init__(
        self,
        layout: WorkspaceLayout,
        conn: sqlite3.Connection,
        clock: Clock,
        tracer: Tracer,
        *,
        settings: RetrievalSettings | None = None,
        sandbox: bool = False,
        zone: tzinfo | None = None,
        sources: Sequence[CandidateSource] | None = None,
        indexers: Sequence[Indexer] | None = None,
        lock_wait: timedelta | None = None,
    ) -> None:
        settings = settings or RetrievalSettings.default()
        self.layout = layout
        self.conn = conn
        self.clock = clock
        self.tracer = tracer
        self.settings = settings
        self.sandbox = sandbox
        self.zone = zone
        self.sources = tuple(sources) if sources is not None else (FtsSource(conn, settings.column_weights),)
        self.lock_wait = lock_wait if lock_wait is not None else settings.lock_wait
        self.synchronizer = Synchronizer(layout, conn, clock, tuple(indexers or (FtsIndexer(),)), routes_of(conn),
                                         lock_wait=self.lock_wait, lock_ttl=settings.lock_ttl)

    # 同步

    def sync(self, full: bool = False) -> SyncReport:
        """显式同步(kb sync)：无论有无变化都写一个事件。"""
        if self.sandbox:
            return SyncReport()
        with self.tracer.span("run_script", agent=KB_AGENT, attributes={"operation": "sync", "full": full}) as span:
            report = self._apply_sync(full)
            span.set(status="error" if report.errors else "ok", attributes=report.counts())
        return report

    def _apply_sync(self, full: bool) -> SyncReport:
        report = self.synchronizer.sync(full)
        if report.changed:
            self.regenerate_index_files()
        return report

    def _refresh(self) -> list[str]:
        """读取前的增量同步：只在有变化或有错误时写事件，文件未变化的读取不产生同步事件。"""
        if self.sandbox:
            return []
        try:
            report = self._apply_sync(False)
        except LockTimeout:
            return [WARNING_LOCKED]
        if report.changed or report.errors:
            with self.tracer.span("run_script", agent=KB_AGENT, status="error" if report.errors else "ok",
                                  attributes={"operation": "sync", "full": False, **report.counts()}):
                pass
        if report.errors:
            return [WARNING_INVALID + "、".join(sorted({issue.path for issue in report.errors}))]
        return []

    def regenerate_index_files(self) -> list[str]:
        if self.sandbox:
            return []
        return index_md.regenerate(self.conn, self.layout, self.settings.paging)

    def _event(self, operation: str, **attributes: Any) -> None:
        with self.tracer.span("execute_tool", agent=KB_AGENT, attributes={"operation": operation, **attributes}):
            pass

    # 读取

    def search_options(self) -> dict[str, int]:
        """传给 search.search 的候选倍数与 RRF 常数。"""
        return {"candidate_factor": self.settings.candidate_factor, "rrf_k": self.settings.rrf_k}

    def search(self, query: str, filters: SearchFilters = SearchFilters()) -> SearchResult:
        warnings = self._refresh()
        hits = search.search(self.conn, self.sources, query, filters, **self.search_options())
        self._event("search", query=query, filters=filters.to_dict(), ids=[hit.id for hit in hits])
        return SearchResult(hits, warnings)

    def get(self, entry_id: str, record_hit: bool = True) -> EntryDocument:
        warnings = self._refresh()
        record = search.require(self.conn, entry_id)
        path = self.layout.root / record.path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record.content_sha256:
            if not self.sandbox:
                self.synchronizer.sync(paths=[path])
            if not path.is_file():
                raise EntryNotFound(entry_id)
            record = search.require(self.conn, entry_id)
        document = search.read_entry(self.layout, record)
        if record_hit and not self.sandbox:
            knowledge.record_hit(self.conn, entry_id, self.clock.now())
        self._event("get", id=entry_id, recordHit=record_hit and not self.sandbox)
        return EntryDocument(document.id, document.frontmatter, document.body, document.path, tuple(warnings))

    def related(self, entry_id: str) -> RelatedResult:
        warnings = self._refresh()
        entries = search.related(self.conn, entry_id)
        self._event("related", id=entry_id, ids=[entry.hit.id for entry in entries])
        return RelatedResult(entries, warnings)

    def today(self) -> date:
        return local_date(self.clock.now(), self.zone)

    def stale(self) -> StaleReport:
        warnings = self._refresh()
        report = stale.stale(self.conn, self.clock.now(), self.today(), self.settings.unused_days,
                             min_shared_tags=self.settings.stale_min_shared_tags,
                             max_group_size=self.settings.stale_max_group_size)
        self._event("stale", overdue=len(report.overdue), unused=len(report.unused),
                    groups=len(report.contradiction_groups))
        return StaleReport(report.overdue, report.unused, report.contradiction_groups, warnings)

    def context_for(self, request: ContextRequest) -> ContextBundle:
        self._refresh()
        bundle = context.build(self.conn, self.layout, self.sources, request,
                               self.settings.context_limits[request.kind], self.settings.inline_full_tokens,
                               **self.search_options())
        self._event("context", kind=request.kind.value, ids=[item.id for item in bundle.items],
                    inline=[document.id for document in bundle.inline_documents],
                    estimatedTokens=bundle.estimated_tokens)
        return bundle

    # 写入

    def _writable(self) -> None:
        if self.sandbox:
            raise KnowledgeError("沙箱模式(--output)下不写入知识")

    def _held(self) -> AbstractContextManager[locks.Acquired]:
        return locks.held(self.conn, locks.KNOWLEDGE, self.clock, self.settings.lock_ttl, wait=self.lock_wait)

    def _after_write(self, paths: Sequence[Path]) -> None:
        report = self.synchronizer.sync(paths=list(paths))
        if report.errors:
            raise KnowledgeError("写入的文件没有通过同步检查：" + "；".join(str(issue) for issue in report.errors))
        self.regenerate_index_files()

    def write(self, draft: KnowledgeDraft, runner: Runner | None, origin: WriteOrigin) -> WriteOutcome:
        """没有相似条目时直接新增，不需要执行器；有相似条目而没有给出执行器时报错，不写入。"""
        self._writable()
        check_draft(self.conn, draft)
        self._refresh()
        candidates = dedup.similar(self.conn, self.layout, self.sources, draft,
                                    limit=self.settings.dedup_similar_limit, **self.search_options())
        if candidates:
            if runner is None:
                raise KnowledgeError(f"有 {len(candidates)} 条相似条目，需要经执行器判断是否写入，但没有给出执行器")
            decision = dedup.decide(runner, self.clock, self.layout, origin, draft, candidates, self.conn)
        else:
            decision = Decision(KnowledgeWriteDecision.ADD, (), (), None, NO_SIMILAR_REASON)
        writer = KnowledgeWriter(self.layout, self.conn, self.clock, self.zone)
        with self.tracer.span("run_script", agent=KB_AGENT, decision=decision.decision.value,
                              reason=decision.reason, attributes={"operation": "write"}) as span:
            try:
                with self._held():
                    outcome = dedup.apply(writer, draft, decision)
            except locks.LockHeld as error:
                raise LockTimeout(f"等待对象锁 knowledge 超时：{error}") from error
            if outcome.paths:
                self._after_write(outcome.paths)
            span.set(attributes={"writtenId": outcome.written_id, "supersededIds": outcome.superseded_ids,
                                 "candidates": [document.id for document in candidates]})
        return outcome

    def set_status(self, entry_id: str, status: KnowledgeStatus, superseded_by: str | None = None,
                   review_by: date | None = None) -> None:
        """续期(review_by)、归档或取代；由 learn 在用户于周报中确认后调用。"""
        self._writable()
        writer = KnowledgeWriter(self.layout, self.conn, self.clock, self.zone)
        with self.tracer.span("run_script", agent=KB_AGENT, decision=status.value,
                              attributes={"operation": "set-status", "id": entry_id}):
            try:
                with self._held():
                    path = writer.set_status(entry_id, status, superseded_by, review_by)
            except locks.LockHeld as error:
                raise LockTimeout(f"等待对象锁 knowledge 超时：{error}") from error
            self._after_write([path])
