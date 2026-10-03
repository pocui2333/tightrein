"""Issue 文件与 issues 表索引的同步(design 10.3，architecture/01 4.2)。

Issue 文件 `issues/<编号>-<简称>.md` 是数据来源，用户会直接编辑它；issues 表只是索引，记录写入或索引时文件内容的
哈希。写入前若发现文件已被改动(哈希与索引不符，或文件存在而没有索引)，抛出 IssueFileConflict，由调用方先 reindex，
不覆盖用户的编辑。头信息为交接文档 issue 类型的版式(handoff/frontmatter/issue.schema.json)：基础头信息(kind、id、
status、from、to、subject、parent、created、updated、next)加分类信息与程序字段，next 按 domain/next_step 生成。
旧版式(`type: issue`，八种状态，可能带已删除的 priorityScore 与 fixability)读取时换算为新版式(from_legacy，状态映射与
迁移 009 相同)，下次写入即为新格式。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.contracts.validate import validate
from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain import next_step
from tightrein.domain.enums import (
    CloseReason,
    IssueOrigin,
    IssuePhase,
    IssueStatus,
    Severity,
    SizeTier,
    TaskType,
    Treatment,
)
from tightrein.domain.ids import parse_sequence
from tightrein.domain.issue import GithubLink, Hold, Issue, legacy_status
from tightrein.store import sequences
from tightrein.store.db import transaction
from tightrein.store.files import markdown
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import FrontmatterError, MarkdownDocument
from tightrein.store.repos import issues
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.triage import introduced_by_dict, introduced_by_from

SCHEMA = "handoff/frontmatter/issue.schema.json"
LEGACY_TYPE = "issue"  # 旧版式头信息的 type 字段
LEGACY_KEYS = ("type", "priorityScore", "fixability")
FROM_TRIAGE, FROM_USER, FROM_FIX = "triage", "user", "fix"
TO_FIX = "fix"
ISSUE_ID = re.compile(r"^\d{4,}$")
_FILE_NAME = re.compile(r"^(\d{4,})-(.+)\.md$")


class IssueFileError(ValueError):
    """Issue 文件的名称、frontmatter 或编号不合格。"""


class IssueFileConflict(Exception):
    """文件在上次写入或索引之后被修改过。"""


@dataclass(frozen=True)
class IssueDocument:
    issue: Issue
    body: str
    run_id: str | None = None


@dataclass(frozen=True)
class ReindexReport:
    updated: tuple[str, ...]
    removed: tuple[str, ...]


def _upstream(issue: Issue) -> str | None:
    """头信息的 parent(上游)：拆分出的子任务为父 Issue，分诊生成的为第一个关联问题，用户需求为空。"""
    if issue.parent is not None:
        return issue.parent
    return issue.problems[0] if issue.problems else None


def _writer(issue: Issue) -> str:
    if issue.parent is not None:
        return FROM_FIX
    return FROM_USER if issue.origin is IssueOrigin.MANUAL else FROM_TRIAGE


def _next(issue: Issue) -> str | None:
    step = next_step.for_issue(issue)
    return None if step.command is None else f"{step.command} {issue.id}"


def to_frontmatter(issue: Issue, run_id: str | None) -> dict[str, Any]:
    return {
        "kind": "issue",
        "id": issue.id,
        "status": issue.status.value,
        "from": _writer(issue),
        "to": TO_FIX,
        "subject": issue.id,
        "parent": _upstream(issue),
        "created": format_iso(issue.created_at),
        "updated": format_iso(issue.updated_at),
        "next": _next(issue),
        "title": issue.title,
        "severity": issue.severity.value,
        "taskType": issue.task_type.value if issue.task_type is not None else None,
        "sizeTier": issue.size_tier.value if issue.size_tier is not None else None,
        "treatment": issue.treatment.value if issue.treatment is not None else None,
        "source": issue.source,
        "origin": issue.origin.value,
        "problems": list(issue.problems),
        "rootCause": list(issue.root_cause),
        "introducedBy": introduced_by_dict(issue.introduced_by) if issue.introduced_by is not None else None,
        "triageCommit": issue.triage_commit,
        "findings": issue.findings,
        "branch": issue.branch,
        "pr": issue.pr,
        "runId": run_id,
        "closeReason": issue.close_reason.value if issue.close_reason is not None else None,
        "hold": issue.hold.to_dict() if issue.hold is not None else None,
        "github": issue.github.to_dict() if issue.github is not None else None,
        "dependsOn": issue.depends_on,
        "phase": issue.phase.value if issue.phase is not None else None,
    }


def from_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """旧版式的头信息(type: issue，八种状态)换算为新版式；带 hold 的未关闭 Issue 为待决定，已合并为完成并等待部署后确认。"""
    found = {key: value for key, value in data.items() if key not in LEGACY_KEYS}
    close_reason = CloseReason(found["closeReason"]) if found.get("closeReason") else None
    status, phase, close_reason = legacy_status(found["status"], close_reason, found.get("hold") is not None)
    problems = list(found.get("problems") or [])
    converted = {
        "kind": "issue", "id": found["id"], "status": status.value, "from": FROM_TRIAGE, "to": TO_FIX,
        "subject": found["id"], "parent": problems[0] if problems else None,
        "created": found["createdAt"], "updated": found["updatedAt"], "next": None,
        "title": found["title"], "severity": found["severity"],
        "taskType": found.get("taskType"), "sizeTier": found.get("sizeTier"), "treatment": found.get("treatment"),
        "source": None, "origin": found.get("origin", IssueOrigin.TRIAGE.value), "problems": problems,
        "rootCause": found.get("rootCause") or [], "introducedBy": found.get("introducedBy"),
        "triageCommit": found.get("triageCommit"), "findings": found.get("findings"), "branch": found.get("branch"),
        "pr": found.get("pr"), "runId": found.get("runId"),
        "closeReason": close_reason.value if close_reason is not None else None,
        "hold": found.get("hold") if status is IssueStatus.NEEDS_DECISION else None,
        "github": found.get("github"), "dependsOn": found.get("dependsOn"),
        "phase": phase.value if phase is not None else None,
    }
    if converted["origin"] == IssueOrigin.MANUAL.value:
        converted["from"] = FROM_USER
    return converted


def from_frontmatter(data: dict[str, Any], slug: str) -> IssueDocument:
    """由已通过 schema 校验的头信息还原 Issue；正文为空，由调用方补上。"""
    close_reason = data.get("closeReason")
    treatment, task_type, size_tier = data.get("treatment"), data.get("taskType"), data.get("sizeTier")
    introduced_by, hold, github = data.get("introducedBy"), data.get("hold"), data.get("github")
    upstream, phase = data.get("parent"), data.get("phase")
    issue = Issue(
        id=data["id"],
        slug=slug,
        title=data["title"],
        status=IssueStatus(data["status"]),
        severity=Severity(data["severity"]),
        created_at=parse_iso(data["created"]),
        updated_at=parse_iso(data["updated"]),
        treatment=Treatment(treatment) if treatment is not None else None,
        task_type=TaskType(task_type) if task_type is not None else None,
        size_tier=SizeTier(size_tier) if size_tier is not None else None,
        problems=tuple(data["problems"]),
        root_cause=tuple(data["rootCause"]),
        introduced_by=introduced_by_from(introduced_by) if introduced_by is not None else None,
        triage_commit=data.get("triageCommit"),
        findings=data.get("findings"),
        branch=data.get("branch"),
        pr=data.get("pr"),
        close_reason=CloseReason(close_reason) if close_reason is not None else None,
        hold=Hold.from_dict(hold) if hold is not None else None,
        origin=IssueOrigin(data["origin"]),
        github=GithubLink.from_dict(github) if github is not None else None,
        depends_on=data.get("dependsOn"),
        parent=upstream if upstream is not None and ISSUE_ID.match(upstream) else None,
        phase=IssuePhase(phase) if phase is not None else None,
        source=data.get("source"),
    )
    return IssueDocument(issue, "", data.get("runId"))


def parse_file_name(path: Path) -> tuple[str, str]:
    match = _FILE_NAME.match(path.name)
    if match is None:
        raise IssueFileError(f"{path.name} 不是 <编号>-<简称>.md 形式的 Issue 文件名")
    return match.group(1), match.group(2)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: Path) -> IssueDocument:
    issue_id, slug = parse_file_name(path)
    try:
        document = markdown.read(path)
    except FrontmatterError as error:
        raise IssueFileError(str(error)) from error
    data = from_legacy(document.frontmatter) if document.frontmatter.get("type") == LEGACY_TYPE \
        else document.frontmatter
    errors = validate(SCHEMA, data)
    if errors:
        details = "；".join(str(error) for error in errors)
        raise IssueFileError(f"{path.name} 的 frontmatter 不符合 {SCHEMA}：{details}")
    if data["id"] != issue_id:
        raise IssueFileError(f"{path.name} 的 frontmatter 编号为 {data['id']}，与文件名不符")
    parsed = from_frontmatter(data, slug)
    return IssueDocument(parsed.issue, document.body, parsed.run_id)


def write(conn: sqlite3.Connection, layout: WorkspaceLayout, document: IssueDocument) -> IssueRecord:
    """写 Issue 文件并更新索引，返回索引记录。"""
    issue = document.issue
    path = layout.issue_file(issue.id, issue.slug)
    relative = layout.relative(path)
    existing = issues.get(conn, issue.id)
    if existing is not None and existing.path != relative:
        raise IssueFileError(f"Issue {issue.id} 的文件为 {existing.path}，简称不能改为 {issue.slug}")
    if path.exists():
        current = content_hash(path.read_text(encoding="utf-8"))
        if existing is None or existing.file_sha256 != current:
            raise IssueFileConflict(f"{relative} 在上次写入或索引之后被修改过，先执行 reindex 再写入")
    frontmatter = to_frontmatter(issue, document.run_id)
    errors = validate(SCHEMA, frontmatter)
    if errors:
        details = "；".join(str(error) for error in errors)
        raise IssueFileError(f"Issue {issue.id} 的 frontmatter 不符合 {SCHEMA}：{details}")
    text = markdown.write(path, MarkdownDocument(frontmatter, document.body))
    record = IssueRecord(issue, relative, content_hash(text))
    issues.save(conn, record)
    return record


def reindex(conn: sqlite3.Connection, layout: WorkspaceLayout) -> ReindexReport:
    """按 issues/ 下的文件重建索引：新增或改动过的文件更新索引，文件已不存在的删除索引，issue 序列不小于最大编号。

    有任何文件不合格时列出全部不合格的文件并抛出 IssueFileError，索引保持不变。
    """
    directory = layout.issues_dir()
    paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
    problems: list[str] = []
    found: dict[str, tuple[IssueDocument, Path, str]] = {}
    for path in paths:
        try:
            document = read(path)
        except IssueFileError as error:
            problems.append(str(error))
            continue
        if document.issue.id in found:
            problems.append(f"编号 {document.issue.id} 同时出现在 {found[document.issue.id][1].name} 与 {path.name}")
            continue
        found[document.issue.id] = (document, path, content_hash(path.read_text(encoding="utf-8")))
    if problems:
        raise IssueFileError("Issue 文件不合格：\n" + "\n".join(problems))
    updated: list[str] = []
    with transaction(conn):
        for issue_id, (document, path, digest) in found.items():
            existing = issues.get(conn, issue_id)
            record = IssueRecord(document.issue, layout.relative(path), digest)
            if existing != record:
                issues.save(conn, record)
                updated.append(issue_id)
        removed = [record.issue.id for record in issues.find(conn) if record.issue.id not in found]
        for issue_id in removed:
            issues.remove(conn, issue_id)
        if found:
            sequences.ensure_at_least(conn, sequences.ISSUE, max(parse_sequence(issue_id) for issue_id in found))
    return ReindexReport(tuple(sorted(updated, key=parse_sequence)), tuple(removed))
