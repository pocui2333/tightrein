"""知识条目文件的新增、改写、合并与状态变更(architecture/03 1.6.7)。

- 草稿先校验：标题、摘要、正文不为空，至少一个标签，简称为小写字母、数字与连字符，标签前缀格式正确，
  related 中的编号是已有的条目；
- add：由 sequences 分配编号写新文件；supersedes 中的条目改为 superseded 并填写 supersededBy；
- update：改写目标条目的标题、摘要、标签、正文与 updated，编号、文件名与其他字段不变；
- merge：分配新编号写入合并后的条目，related 取草稿与各被合并条目 related 的并集；被合并的条目改为 superseded；
- set_status：续期(改 reviewBy)、归档与取代；条目从不删除文件。
每个文件先写同目录临时文件再改名(markdown.write)，不会留下半截内容；调用方在持有对象锁 knowledge 期间调用。
正文写为「# 标题」一行、空行与正文；updated 为本机时区(或注入的时区)的今天。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, tzinfo
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.errors import DraftInvalid, EntryNotFound
from tightrein.retrieval.frontmatter import tag_issues
from tightrein.store import sequences
from tightrein.store.files import markdown
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import ENTRY_TYPES, KnowledgeRecord

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Content:
    title: str
    summary: str
    tags: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class KnowledgeDraft:
    type: KnowledgeType
    slug: str
    title: str
    summary: str
    tags: tuple[str, ...]
    body: str
    review_by: date
    related: tuple[str, ...] = ()
    source_run_id: str | None = None

    @property
    def content(self) -> Content:
        return Content(self.title, self.summary, self.tags, self.body)

    def render(self) -> str:
        """交给去重判断的草稿全文。"""
        return (f"类型：{self.type.value}\n标题：{self.title}\n摘要：{self.summary}\n标签：{'、'.join(self.tags)}\n"
                f"related：{'、'.join(self.related) or '无'}\n\n{self.body.strip()}\n")


def check_draft(conn: sqlite3.Connection, draft: KnowledgeDraft) -> None:
    reasons = []
    for name in ("title", "summary", "body"):
        if not getattr(draft, name).strip():
            reasons.append(f"{name} 不能为空")
    if not draft.tags:
        reasons.append("至少要有一个标签")
    if not SLUG.match(draft.slug):
        reasons.append(f"简称只能由小写字母、数字与连字符组成：{draft.slug!r}")
    reasons += [f"tags{pointer[len('$.tags'):]}：{reason}" for pointer, reason in tag_issues(draft.tags)]
    for target in draft.related:
        record = knowledge.get(conn, target)
        if record is None or record.type not in ENTRY_TYPES:
            reasons.append(f"related 中的条目 {target} 不存在")
    if reasons:
        raise DraftInvalid("草稿不合格：" + "；".join(reasons))


def body_text(title: str, body: str) -> str:
    return f"# {title}\n\n{body.strip()}\n"


class KnowledgeWriter:
    def __init__(self, layout: WorkspaceLayout, conn: sqlite3.Connection, clock: Clock,
                 zone: tzinfo | None = None) -> None:
        self.layout = layout
        self.conn = conn
        self.clock = clock
        self.zone = zone

    def today(self) -> date:
        return local_date(self.clock.now(), self.zone)

    def _record(self, entry_id: str) -> KnowledgeRecord:
        record = knowledge.get(self.conn, entry_id)
        if record is None or record.type not in ENTRY_TYPES:
            raise EntryNotFound(entry_id)
        return record

    def _document(self, record: KnowledgeRecord) -> tuple[Path, MarkdownDocument]:
        path = self.layout.root / record.path
        return path, markdown.read(path)

    def _create(self, draft: KnowledgeDraft, content: Content, related: tuple[str, ...]) -> tuple[str, Path]:
        entry_id = sequences.next_knowledge_id(self.conn, draft.type)
        frontmatter: dict[str, Any] = {
            "id": entry_id, "type": draft.type.value, "summary": content.summary, "tags": list(content.tags),
            "status": KnowledgeStatus.ACTIVE.value, "supersededBy": None, "updated": self.today().isoformat(),
            "reviewBy": draft.review_by.isoformat(), "related": list(related), "sourceRunId": draft.source_run_id,
        }
        path = self.layout.knowledge_file(draft.type, entry_id, draft.slug)
        markdown.write(path, MarkdownDocument(frontmatter, body_text(content.title, content.body)))
        return entry_id, path

    def add(self, draft: KnowledgeDraft, supersedes: tuple[str, ...] = ()) -> tuple[str, list[Path]]:
        records = [self._record(entry_id) for entry_id in supersedes]
        entry_id, path = self._create(draft, draft.content, draft.related)
        return entry_id, [path, *(self.supersede(record.id, entry_id) for record in records)]

    def update(self, target_id: str, content: Content) -> list[Path]:
        path, document = self._document(self._record(target_id))
        frontmatter = {**document.frontmatter, "summary": content.summary, "tags": list(content.tags),
                       "updated": self.today().isoformat()}
        markdown.write(path, MarkdownDocument(frontmatter, body_text(content.title, content.body)))
        return [path]

    def merge(self, draft: KnowledgeDraft, target_ids: tuple[str, ...], content: Content) -> tuple[str, list[Path]]:
        records = [self._record(entry_id) for entry_id in target_ids]
        related: list[str] = list(draft.related)
        for record in records:
            related += [item for item in record.related if item not in related and item not in target_ids]
        entry_id, path = self._create(draft, content, tuple(related))
        return entry_id, [path, *(self.supersede(record.id, entry_id) for record in records)]

    def supersede(self, entry_id: str, by: str) -> Path:
        return self.set_status(entry_id, KnowledgeStatus.SUPERSEDED, superseded_by=by)

    def set_status(self, entry_id: str, status: KnowledgeStatus, superseded_by: str | None = None,
                   review_by: date | None = None) -> Path:
        if (status is KnowledgeStatus.SUPERSEDED) != (superseded_by is not None):
            raise ValueError("只有改为 superseded 时填写 supersededBy，且必须填写")
        path, document = self._document(self._record(entry_id))
        frontmatter = {**document.frontmatter, "status": status.value, "supersededBy": superseded_by,
                       "updated": self.today().isoformat()}
        if review_by is not None:
            frontmatter["reviewBy"] = review_by.isoformat()
        markdown.write(path, MarkdownDocument(frontmatter, document.body))
        return path
