"""写成 Issue：新建、追加到同一根因的 Issue、按根因或模块拆分、用户自己提的需求、实施退回的拆分。纯程序，不调用模型。

- 同一根因追加：键是根因的「文件#方法」集合(没有方法名时用行号，比只用行号稳)，与任一未关闭 Issue 的键有交集就
  追加；追加时加入问题、写历史，新问题严重度更高时提升 Issue 的严重度并写明；
- 新建前核对正文要引用的代码位置(只补全证据、根因、报告、评估，不改主张与信号原文)：补不全或不存在就不建，交接
  为 failed 并提示重新评估；建好后按检查项(必需小节、位置真实、日期为绝对日期)再查一遍，不过就保留文件、交接为
  failed；
- 一个 Issue 只做一件事：粗规模档为「大」且根因分布在几个模块的，按模块拆成几个 Issue；
- 放行：缺省待决定；关卡 boundaries.gates.issue 配为 auto 时，低风险的(没有安全、数据、权限类标记且规模为小)直接
  放行为待修；
- 用户自己提的需求(`tightrein new`)直接是待修，没有关联问题；验收标准超过设定条数时提示拆分。
编号用 sequences 的 issue 序列与 naming.issue_id；正文写 00-issue-body.md，记录写 00-issue-record.json 与 issues 表。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from tightrein.assess import notes as code_notes
from tightrein.assess import persist
from tightrein.assess.checks import IN_TEXT, Snapshot, complete
from tightrein.assess.claims import Claim, signal_evidence
from tightrein.assess.issue import body, files, github
from tightrein.assess.issue.transitions import (
    APPROVED_AT,
    CLOSED,
    HISTORY,
    PROBLEMS,
    USER,
    IssueEvent,
    IssueStatus,
    apply_event,
)
from tightrein.assess.rating import higher
from tightrein.protocol.boundaries import gate_is_auto
from tightrein.protocol.handoff import Handoff, Metrics, Status, write
from tightrein.protocol.naming import FileName, format_iso, issue_id
from tightrein.store.db import transaction
from tightrein.store.files.atomic import write_text
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import issues, problems, sequences
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = "assess.issue"
HANDOFF = FileName(POINT, "handoff", "json")
GATE = "issue"
LOCATED_KEYS = ("facts", "counterEvidence", "rootCauses", "report", "assessment", "impact", "trigger",
                "sourceOfPhenomenon")
SENSITIVE_TASKS = frozenset({"security", "data"})
SENSITIVE_IMPACTS = frozenset({"authorization", "data-ownership", "data-correctness", "credential-leak"})
REQUIRED = {"problem": ("problem", "cause", "acceptance"), USER: ("problem",)}
RELATIVE_DATES = ("今天", "昨天", "明天", "前天", "today", "yesterday", "tomorrow", "今日", "昨日", "明日")
NOT_WORD = re.compile(r"[^a-z0-9]+")
PARAMETER = re.compile(r"^[{:<].*")


class IssueNotCreated(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("正文要引用的代码位置不合格，不建 Issue，请重新评估：" + "；".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Finding:
    """评估交给写成 Issue 的内容。"""

    problem: Problem
    latest: Occurrence
    claim: Claim
    output: Mapping[str, Any]  # 通过证据检查的取证输出
    severity: str | None
    size: str | None
    task_type: str | None
    impact_kind: str | None
    treatment: str
    labels: tuple[str, ...]
    introduced: tuple[Mapping[str, Any], ...]
    commit: str


@dataclass
class Created:
    issues: list[str] = field(default_factory=list)
    appended: str | None = None
    auto_approved: bool = False
    failures: list[str] = field(default_factory=list)  # 建好后的检查没过：文件保留，交接为 failed
    saved: dict[Path, str | None] = field(default_factory=dict)  # 改动前的文件内容，外层事务回滚时用来恢复

    def remember(self, layout: WorkspaceLayout, issue: str) -> None:
        for path in (files.record_path(layout, issue), files.body_path(layout, issue),
                     code_notes.path_of(layout, issue)):
            if path not in self.saved:
                self.saved[path] = path.read_text(encoding="utf-8") if path.is_file() else None


def undo(made: Created) -> None:
    """外层事务回滚时：恢复被追加的 Issue 的文件，删掉新建的。"""
    for path, text in made.saved.items():
        if text is None:
            path.unlink(missing_ok=True)
        else:
            write_text(path, text)


def root_key(root_causes: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({f"{cause['file']}#{cause.get('symbol') or cause['line']}" for cause in root_causes})


def same_root(runtime: Runtime, key: Sequence[str]) -> Issue | None:
    wanted = set(key)
    if not wanted:
        return None
    for issue in issues.find(runtime.conn):
        if issue.status not in CLOSED and wanted & set(issue.extra.get("rootKey") or []):
            return issue
    return None


def from_problem(runtime: Runtime, finding: Finding, snapshot: Snapshot, made: Created) -> Created:
    """评估判为要修的问题写成 Issue(或追加)，结果填进 made；调用方负责事务，回滚时调用 undo(made)。"""
    located = complete(finding.output, snapshot, keys=LOCATED_KEYS)
    reasons = located.problems() + [f"根因位置 {cause['file']}:{cause['line']}：{problem}"
                                    for cause in located.value.get("rootCauses") or []
                                    if (problem := snapshot.problem(cause)) is not None]
    if reasons:
        raise IssueNotCreated(reasons)
    finding = replace(finding, output=located.value)
    causes = list(finding.output.get("rootCauses") or [])
    existing = same_root(runtime, root_key(causes))
    if existing is not None:
        made.remember(runtime.workspace, existing.id)
        append(runtime, existing, finding)
        made.issues, made.appended = [existing.id], existing.id
        return made
    created = made
    groups = split_groups(causes) if finding.size == "large" else [causes]
    for index, group in enumerate(groups):
        part = finding if len(groups) == 1 else replace(finding, output=_subset(finding.output, group))
        record, text = _new_record(runtime, part, len(groups) > 1)
        created.remember(runtime.workspace, record.id)
        created.auto_approved = record.status == IssueStatus.TODO
        files.write(runtime, record, body=text)
        created.issues.append(record.id)
        created.failures += [f"{record.id}：{reason}" for reason in check_body(text, record.origin, snapshot)]
        if index == 0:
            notes = code_notes.load(runtime.workspace, finding.problem.id)
            if notes is not None:
                code_notes.copy_to(runtime.workspace, notes, record.id)
    return created


def append(runtime: Runtime, issue: Issue, finding: Finding) -> Issue:
    severity = higher(finding.severity, issue.severity)
    note = f"追加问题 {finding.problem.id}"
    if severity != issue.severity:
        note += f"，严重度由 {issue.severity} 提升为 {severity}"
    extra = dict(issue.extra)
    extra[PROBLEMS] = [*extra.get(PROBLEMS, []), finding.problem.id]
    extra[HISTORY] = [*extra.get(HISTORY, []), _entry(runtime, "append", note)]
    updated = replace(issue, severity=severity, extra=extra)
    files.write(runtime, updated)
    return updated


def split_groups(causes: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """按根因文件所在的模块(顶层目录)分组，保持出现顺序；只有一个模块时不拆。"""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for cause in causes:
        groups.setdefault(module_of(str(cause["file"])), []).append(cause)
    return list(groups.values()) if len(groups) > 1 else [list(causes)]


def module_of(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "."


def low_risk(finding: Finding) -> bool:
    """没有安全、数据、权限类标记且规模为小。"""
    flags = (finding.output.get("assessment") or {}).get("flags") or {}
    return (finding.size == "small" and finding.task_type not in SENSITIVE_TASKS
            and finding.impact_kind not in SENSITIVE_IMPACTS and not finding.labels
            and not any((flag or {}).get("flagged") for flag in flags.values()))


def check_body(text: str, origin: str, snapshot: Snapshot) -> list[str]:
    """建好后的检查：必需小节、正文中的代码位置真实、日期为绝对日期。"""
    sections = body.split(text)
    reasons = [f"缺少小节 {body.HEADINGS[key]['zh']}" for key in REQUIRED[origin] if not sections.get(key)]
    for match in IN_TEXT.finditer(text):
        problem = snapshot.problem(match.group(0))
        if problem is not None:
            reasons.append(f"位置 {match.group(0)}：{problem}")
    reasons += [f"用了相对日期「{word}」" for word in RELATIVE_DATES if word in text]
    return reasons


def slug(finding: Finding, max_length: int, number: int) -> str:
    """英文简称，也用作分支名：接口取路由末两段(去掉路径参数)加状态码，报错取异常类型加方法名，其余取检查类别加
    方法名；小写连字符，截断后不留尾部连字符；取不到时为「来源-序号」。"""
    problem = finding.problem
    evidence = signal_evidence(finding.latest)
    symbol = str(finding.latest.evidence.get("symbol") or problem.extra.get("symbol") or "").rpartition(".")[2]
    if problem.location and " " in problem.location:
        route = problem.location.partition(" ")[2]
        segments = [part for part in route.split("/") if part and not PARAMETER.match(part)]
        words = [*segments[-2:], str(evidence.get("status") or problem.check_type)]
    elif evidence.get("exceptionType"):
        words = [str(evidence["exceptionType"]).rpartition(".")[2], symbol]
    else:
        words = [problem.check_type.partition(":")[2] or problem.check_type, symbol]
    found = normalize("-".join(word for word in words if word), max_length)
    return found or f"{problem.source.rpartition('.')[2].replace('_', '-')}-{number}"


def normalize(text: str, max_length: int) -> str:
    return NOT_WORD.sub("-", text.lower()).strip("-")[:max_length].rstrip("-")


def new_issue(runtime: Runtime, description: str, *, severity: str | None, kind: str) -> str:
    """用户自己提的需求(`tightrein new`)：直接是待修，不经采集与评估。"""
    section = runtime.settings.section(POINT)
    title = _first_line(description)[: int(section["titleMaxLength"])]
    criteria = body.requirement_parts(description)[1]
    limit = int(section["newAcceptanceMax"])
    hint = f"验收标准有 {len(criteria)} 条，超过 {limit} 条，建议拆成几个 Issue" if len(criteria) > limit else None
    with transaction(runtime.conn):
        number = sequences.next_value(runtime.conn, sequences.ISSUE)
        identifier = issue_id(number)
        record = Issue(id=identifier, status=IssueStatus.TODO, title=title, kind=kind, origin=USER, severity=severity,
                       extra={"slug": normalize(title, int(section["slugMaxLength"])) or f"manual-{number}",
                              PROBLEMS: [], "splitHint": hint, APPROVED_AT: format_iso(runtime.clock.now()),
                              HISTORY: [_entry(runtime, "create", "用户需求，创建即为待修" + (f"；{hint}" if hint else ""))]})
        files.write(runtime, record, body=body.render_manual(identifier, title, description, runtime.language))
    _handoff(runtime, identifier, Status.PASSED, f"新建用户需求 Issue {identifier}",
             {"issues": [identifier], "problems": [], "appended": None, "autoApproved": False, "splitHint": hint,
              "failures": []})
    github.sync(runtime, record)
    return identifier


def split_back(runtime: Runtime, issue: str, parts: Sequence[str]) -> list[str]:
    """实施方案时发现一个 PR 放不下：拆成几个新 Issue(按顺序依赖前一个)，原 Issue 取消并关联新的。"""
    original = issues.get(runtime.conn, issue)
    if original is None:
        raise LookupError(f"没有 Issue {issue}")
    created: list[str] = []
    linked = list(original.extra.get(PROBLEMS) or [])
    try:
        with persist.restoring(runtime, linked), transaction(runtime.conn):
            _split(runtime, original, parts, created)
            for found in (problems.get(runtime.conn, pid) for pid in linked):
                if found is not None:  # 关联问题改挂到拆出的第一个 Issue
                    persist.update(runtime, found, issue=created[0])
            apply_event(runtime, issue, IssueEvent.SPLIT_BACK, note=f"拆成 {'、'.join(created)}",
                        updates={"gate": None})
    except BaseException:
        for identifier in created:  # 事务回滚了，新建的 Issue 文件也删掉，重建时不会凭空多出来
            files.record_path(runtime.workspace, identifier).unlink(missing_ok=True)
            files.body_path(runtime.workspace, identifier).unlink(missing_ok=True)
        raise
    return created


def _split(runtime: Runtime, original: Issue, parts: Sequence[str], created: list[str]) -> None:
    issue = original.id
    for index, description in enumerate(parts):
        number = sequences.next_value(runtime.conn, sequences.ISSUE)
        identifier = issue_id(number)
        title = _first_line(description)
        extra: dict[str, Any] = {
            "slug": normalize(title, int(runtime.settings.section(POINT)["slugMaxLength"])) or f"split-{number}",
            PROBLEMS: list(original.extra.get(PROBLEMS) or []) if index == 0 else [],
            "parent": issue, "dependsOn": created[-1] if created else None,
            APPROVED_AT: original.extra.get(APPROVED_AT) or format_iso(runtime.clock.now()),
            "rootKey": list(original.extra.get("rootKey") or []) if index == 0 else [],
            HISTORY: [_entry(runtime, "create", f"由 Issue {issue} 拆出(第 {index + 1}/{len(parts)} 个)")]}
        record = Issue(id=identifier, status=IssueStatus.TODO, title=title, kind=original.kind,
                       origin=original.origin, severity=original.severity, extra=extra)
        files.write(runtime, record, body=body.render_manual(identifier, title, description, runtime.language))
        created.append(identifier)


def write_handoff(runtime: Runtime, finding: Finding, created: Created | None, failure: str | None) -> None:
    subject = created.issues[0] if created and created.issues else finding.problem.id
    failed = failure is not None or bool(created and created.failures)
    summary = failure or (f"追加到 Issue {created.appended}" if created and created.appended
                          else f"新建 Issue {'、'.join(created.issues) if created else ''}")
    _handoff(runtime, subject, Status.FAILED if failed else Status.PASSED, summary, {
        "issues": created.issues if created else [], "problems": [finding.problem.id],
        "appended": created.appended if created else None, "autoApproved": bool(created and created.auto_approved),
        "splitHint": None, "failures": [failure] if failure else (created.failures if created else [])})


def _new_record(runtime: Runtime, finding: Finding, split: bool) -> tuple[Issue, str]:
    section = runtime.settings.section(POINT)
    number = sequences.next_value(runtime.conn, sequences.ISSUE)
    identifier = issue_id(number)
    title = body.title_of(finding.output, finding.claim.to_json(), int(section["titleMaxLength"]))
    if split:
        module = module_of(str(finding.output["rootCauses"][0]["file"]))
        title = f"{title}({module})"
    problem = finding.problem
    role = signal_evidence(finding.latest).get("role")
    facts = body.IssueFacts(
        issue=identifier, title=title, severity=finding.severity, output=finding.output,
        claim=finding.claim.to_json(), commit=finding.commit, introduced=finding.introduced, labels=finding.labels,
        probe_acceptance=body.probe_acceptance(problem.source, problem.location, problem.check_type,
                                               problem.fingerprint, role, runtime.language),
        problems=((problem.id, problem.title),))
    auto = gate_is_auto(GATE, runtime.settings) and low_risk(finding)
    status = IssueStatus.TODO if auto else IssueStatus.NEEDS_DECISION
    causes = list(finding.output.get("rootCauses") or [])
    note = f"由问题 {problem.id} 评估建立：{finding.treatment}" + ("；低风险，自动放行" if auto else "")
    record = Issue(
        id=identifier, status=status, title=title, kind="feature" if finding.task_type == "feature" else "bug",
        origin="problem", severity=finding.severity, gate=None if auto else GATE,
        extra={"slug": slug(finding, int(section["slugMaxLength"]), number), PROBLEMS: [problem.id],
               "rootCauses": causes, "rootKey": root_key(causes), "triageCommit": finding.commit,
               "treatment": finding.treatment, "size": finding.size, "taskType": finding.task_type,
               "impactKind": finding.impact_kind, "labels": list(finding.labels),
               "introducedBy": [dict(item) for item in finding.introduced], "source": problem.source,
               APPROVED_AT: format_iso(runtime.clock.now()) if auto else None,
               HISTORY: [_entry(runtime, "create", note)]})
    return record, body.render(facts, runtime.language)


def _subset(output: Mapping[str, Any], causes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """拆分出的一份：只留这个模块的根因与预估文件。"""
    module = module_of(str(causes[0]["file"]))
    assessment = dict(output.get("assessment") or {})
    assessment["files"] = [item for item in assessment.get("files") or [] if module_of(item["path"]) == module]
    return {**output, "rootCauses": list(causes), "assessment": assessment}


def _entry(runtime: Runtime, event: str, note: str) -> dict[str, Any]:
    return {"at": format_iso(runtime.clock.now()), "event": event, "actor": "tightrein", "reason": None, "note": note}


def _first_line(description: str) -> str:
    for line in description.strip().splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    raise ValueError("需求描述是空的")


def _handoff(runtime: Runtime, subject: str, status: Status, summary: str, facts: dict[str, Any]) -> None:
    from tightrein.protocol.records import versions

    path = runtime.workspace.step_file(subject, HANDOFF)
    write(path, Handoff(point=POINT, subject=subject, run=runtime.run, status=status, summary=summary, facts=facts,
                        metrics=Metrics(produced={"issues": len(facts["issues"])}),
                        created_at=format_iso(runtime.clock.now()),
                        versions=versions(runtime.tool.root, runtime.settings.hash, prompt_hash=None, tool=None,
                                          tool_version=None, model=None)))
