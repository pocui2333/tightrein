"""GitHub Issue 镜像的对齐(redesign/04-issue.md 第 3 到 5 节，architecture/06 10.8)。

issues.tracker 为 github 时，每个未关闭的本地 Issue 在 GitHub 上有一个镜像 Issue。字段按归属划分：开关状态以 GitHub 为准，
标题、正文、标签与子 Issue 关系以本地为准。一次对齐：
1. `gh repo view` 确认仓库：Issue 未启用，或 privateOnly 而仓库公开时整次跳过并写明原因；
2. 读回开关状态：一次列出仓库 Issue 的开关状态，与 github_mirror 记下的不同说明用户在 GitHub 上改过——关闭时本地按
   「用户关闭」(不修)取消，重新打开时本地重新打开为待修；本地待合并或完成、GitHub 以 completed 关闭的，是 PR 按
   `Closes` 自动关闭，只记下状态。读回引起的本地转换不写评论；
3. 推送本地字段：没有镜像时建 Issue(带状态标签与类型标签；父 Issue 与前一个子任务已有镜像时以 `--parent`、
   `--blocked-by` 建立子 Issue 与阻塞关系)；标签与本地不符时改标签(去掉上次打的、加上应有的)；发出待发评论；
   关系缺失时以 `gh issue edit --parent`、`--add-blocked-by` 补上；开关状态与本地要求不符时关闭或重开(完成与取消要求关闭，
   待合并不要求，其余要求打开)。缺失的标签先建立(`--force`，重复执行无害)。
每个 Issue 一个状态标签 `<前缀><状态>` 加一个类型标签 `<前缀>type:<任务类型>`(没有任务类型的只有状态标签)；
github_mirror.labelled_status 记 `<状态>|<类型>`，旧值 `<状态>[+needs-decision]` 解析为当时打的标签。
镜像正文由本地正文转换得到(mirror_body)：去掉「历史」(旧版式另去掉标题行)，代码位置换成取证 commit 的永久链接，
「引用」中的完整证据(旧版式为「完整证据」一节)折叠，加说明与本地标记，整体经脱敏；固定文字按 project.language。
refresh 为已有镜像另加一步改标题与正文(issue rerender 用)。
写入按关卡 gates.mirror-writes：为 user 时每个 Issue 生成一个待确认操作(还没有镜像时只含建 Issue)，确认执行后由
follow_up 记下结果；为 auto 时直接经 VcsProcess.gh 执行。gh 失败不抛出：错误记进 github_mirror.error，下次对齐重算差异重试。
建 Issue 以幂等键 `github-issue:<仓库>:<编号>` 记下编号与链接，再写进 Issue 文件；键处于进行中时按正文中的本地标记找回。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.config.project import ProjectConfig
from tightrein.domain import issue_sections
from tightrein.domain.handoff import sections
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import CloseReason, IssueEvent, IssueStatus, OperationKind, OperationStatus, TaskType
from tightrein.domain.issue import GithubLink, Issue, IssueContext
from tightrein.observability.redact import Redactor
from tightrein.pipeline.issue.render import labels as label_text
from tightrein.pipeline.issue.steps import github_comments, transitions
from tightrein.pipeline.issue.steps.transitions import IssueCommandRejected, IssueEnv
from tightrein.store import idempotency
from tightrein.store.files import atomic, issue_files
from tightrein.store.files.issue_files import IssueFileConflict, IssueFileError
from tightrein.store.repos import github_mirror, issues, pending_operations
from tightrein.store.repos.github_mirror import CLOSED, OPEN
from tightrein.store.repos.issues import IssueRecord
from tightrein.vcs import gh_issues
from tightrein.vcs.errors import VcsError
from tightrein.vcs.executor import FollowUp
from tightrein.vcs.gh_issues import GhIssues, RemoteIssue
from tightrein.vcs.operations import OperationPlanner, PendingOperation
from tightrein.vcs.process import VcsProcess

ORIGIN_URL = "remote.origin.url"
LEGACY_DECISION = "needs-decision"  # 旧版另加的「需要用户决定」标签，记为 <状态>+needs-decision
LEGACY_SUFFIX = f"+{LEGACY_DECISION}"
KEY_SEPARATOR = "|"
TYPE_LABEL = "type:"
RELATION_SEPARATOR = ";"
PARENT, BLOCKED_BY = "parent", "blocked-by"
MARKER = "<!-- tightrein:{project}:{issue_id} -->"
CREATE_KEY = "github-issue:{slug}:{issue_id}"
AUTO_CLOSED = frozenset({IssueStatus.PENDING_MERGE, IssueStatus.DONE})
PR_CLOSE_REASON = "COMPLETED"
WAITING = frozenset({OperationStatus.PENDING, OperationStatus.CONFIRMED})
PERMALINK = "https://github.com/{slug}/blob/{commit}/{path}#L{first}-L{last}"
CODE_LOCATION = re.compile(r"(`?)(?<![\w/.:-])([\w./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?\1(?![\w])")
FENCE = "```"
FAILURES = (VcsError, IssueCommandRejected, IssueFileConflict, IssueFileError, ValueError)

STANDARD_TYPE_LABELS: Mapping[TaskType, tuple[str, str, str]] = {
    TaskType.BUG: ("bug", "d73a4a", "Something isn't working"),
    TaskType.FEATURE: ("enhancement", "a2eeef", "New feature or request"),
    TaskType.DOCS_CONFIG: ("documentation", "0075ca", "Improvements or additions to documentation"),
    TaskType.SECURITY: ("security", "e11d48", "Security issues and fixes"),
    TaskType.REFACTOR: ("refactor", "fbca04", "Refactoring and code cleanup"),
    TaskType.DEPENDENCY: ("dependencies", "0366d6", "Dependency updates and changes"),
    TaskType.DATA: ("data", "c5def5", "Data schema and migration"),
    TaskType.FRONTEND: ("frontend", "bfd4f2", "Frontend and UI changes"),
}


@dataclass(frozen=True)
class GithubSettings:
    repo: str | None
    label_prefix: str
    private_only: bool
    confirm_writes: bool
    label_color: str

    @classmethod
    def from_config(cls, config: ProjectConfig) -> GithubSettings:
        get = config.get
        return cls(get("issues.github.repo"), get("issues.github.labelPrefix"), bool(get("issues.github.privateOnly")),
                   not gates.auto(config, Gate.MIRROR_WRITES), get("issues.github.labelColor"))

    def label(self, status: IssueStatus) -> str:
        return f"{self.label_prefix}{status.value}"

    def type_label(self, task_type: TaskType) -> str:
        if task_type in STANDARD_TYPE_LABELS:
            return STANDARD_TYPE_LABELS[task_type][0]
        return task_type.value

    def labels_of(self, key: str | None) -> list[str]:
        """labelled_status 记下的值对应的标签：旧值与新值均能解析出要清理的历史标签。"""
        if not key:
            return []
        found: list[str] = []
        if KEY_SEPARATOR in key:
            status, task_type = key.split(KEY_SEPARATOR, 1)
            found.extend([f"{self.label_prefix}{status}", status])
            if task_type:
                found.extend([
                    f"{self.label_prefix}{TYPE_LABEL}{task_type}",
                    f"{self.label_prefix}{task_type}",
                    task_type,
                ])
                if task_type in TaskType._value2member_map_:
                    found.append(self.type_label(TaskType(task_type)))
            return [name for name in found if name]
        status = key.removesuffix(LEGACY_SUFFIX)
        found.extend([f"{self.label_prefix}{status}", status])
        if key.endswith(LEGACY_SUFFIX):
            found.extend([f"{self.label_prefix}{LEGACY_DECISION}", LEGACY_DECISION])
        if status in TaskType._value2member_map_:
            found.append(self.type_label(TaskType(status)))
        return [name for name in found if name]


@dataclass
class MirrorReport:
    """一次对齐的结果：skipped 为整次跳过的原因；unsynced 为对齐后仍未同步的项。"""

    repo: str | None = None
    skipped: str | None = None
    written: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    unsynced: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MirrorStep:
    """一条 gh 写命令：args 不含可执行文件名，其中的 `{raw}` 换成正文文件所在目录；effect 是执行成功后要记下的结果。"""

    args: tuple[str, ...]
    description: str
    effect: Mapping[str, Any]
    files: Mapping[str, str] = field(default_factory=dict)


def desired_state(issue: Issue) -> str | None:
    """本地要求的开关状态：完成与取消要求关闭；待合并不要求(合并 PR 时 GitHub 按 Closes 自动关闭，本地随后才由
    release track 推进)；其余要求打开。"""
    if issue.is_closed:
        return CLOSED
    return None if issue.status is IssueStatus.PENDING_MERGE else OPEN


def mirrored(issue: Issue) -> bool:
    """需要镜像的 Issue：已有镜像，或还未关闭(启用前已关闭的不补建)。"""
    return issue.github is not None or not issue.is_closed


def linkify(text: str, slug: str, commit: str | None, repo: Path) -> str:
    """把 `路径:行号` 与 `路径:起-止` 换成取证 commit 的永久链接；没有 commit、文件不在项目仓库中或在代码块内的不换。"""
    if not commit:
        return text

    def replace(match: re.Match[str]) -> str:
        path, first, last = match.group(2), match.group(3), match.group(4) or match.group(3)
        if not (repo / path).is_file():
            return match.group(0)
        shown = match.group(0).strip("`")
        url = PERMALINK.format(slug=slug, commit=commit, path=path, first=first, last=last)
        return f"[`{shown}`]({url})"

    parts = text.split(FENCE)
    return FENCE.join(part if index % 2 else CODE_LOCATION.sub(replace, part) for index, part in enumerate(parts))


def _fold(content: str, language: str, slug: str, commit: str | None, repo: Path, whole: bool) -> str:
    """完整证据折叠显示：旧版式的「完整证据」整节折叠；「引用」只折叠带代码位置的条目，其余照常列出。"""
    lines = content.splitlines()
    evidence = [line for line in lines if line.startswith("- ") and (whole or CODE_LOCATION.search(line))]
    others = [] if whole else [line for line in lines if line not in evidence]
    if whole:
        evidence = lines
    text = linkify("\n".join(others).strip(), slug, commit, repo)
    if not any(line.startswith("- ") for line in evidence):
        return text
    count = sum(1 for line in evidence if line.startswith("- "))
    folded = (f"<details>\n<summary>{label_text.text('evidenceSummary', language, count=count)}</summary>\n\n"
              f"{linkify(chr(10).join(evidence).strip(), slug, commit, repo)}\n\n</details>")
    return f"{text}\n\n{folded}".strip()


NONE_TEXTS = frozenset({"无", "None", "なし", label_text.MISSING, "-"})


def _is_meaningful(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    return bool(stripped and stripped not in NONE_TEXTS)


def _cause_block(cause_raw: str, language: str, slug: str, commit: str | None, repo: Path) -> str | None:
    if not _is_meaningful(cause_raw):
        return None
    lines = [line.strip() for line in cause_raw.splitlines()
             if line.strip() and not line.strip().endswith(f"：{label_text.MISSING}")
             and not line.strip().endswith(f": {label_text.MISSING}")]
    if not lines:
        return None
    trigger_label = label_text.text("trigger", language)
    trigger_lines = [line for line in lines if trigger_label in line]
    other_lines = [line for line in lines if line not in trigger_lines]
    cause_content = "\n".join(trigger_lines + other_lines)
    return linkify(cause_content, slug, commit, repo) if _is_meaningful(cause_content) else None


def _problem_block(problem_raw: str | None, reproduce_raw: str | None, language: str, slug: str,
                   commit: str | None, repo: Path) -> str | None:
    parts = []
    if _is_meaningful(problem_raw):
        parts.append(linkify(problem_raw.strip(), slug, commit, repo))
    if _is_meaningful(reproduce_raw):
        repro_title = label_text.text("mirror.reproduce", language)
        parts.append(f"### {repro_title}\n\n{linkify(reproduce_raw.strip(), slug, commit, repo)}")
    return "\n\n".join(parts) if parts else None


def _method_block(direction_raw: str | None, scope_raw: str | None, language: str, slug: str,
                  commit: str | None, repo: Path) -> str | None:
    parts = []
    if _is_meaningful(direction_raw):
        parts.append(linkify(direction_raw.strip(), slug, commit, repo))
    if _is_meaningful(scope_raw):
        scope_files_label = label_text.text("scopeFiles", language)
        for line in scope_raw.splitlines():
            clean = line.strip().lstrip("- ")
            if scope_files_label in clean and not clean.endswith(f"：{label_text.MISSING}") and not clean.endswith(f": {label_text.MISSING}"):
                files_str = clean.split("：", 1)[-1] if "：" in clean else clean.split(":", 1)[-1]
                parts.append(f"- {label_text.text('mirror.files', language)}：{linkify(files_str.strip(), slug, commit, repo)}")
    return "\n\n".join(parts) if parts else None


def mirror_body(issue: Issue, text: str, project: str, slug: str, repo: Path, language: str,
                redactor: Redactor) -> str:
    """镜像正文：按真人开发者的习惯组织为原因、问题、方法等小节；代码位置换成取证 commit 的永久链接，
    完整证据折叠，末尾为本地标记，整体经脱敏。去除了内部流水线指令、机械验收复选框与水印声明。"""
    if not issue_sections.is_handoff(text):
        blocks = []
        for title, content in sections.split(text).items():
            key = issue_sections.key_of(title)
            if key in (issue_sections.HISTORY, issue_sections.CONCLUSION):
                continue
            if key in (issue_sections.REFERENCES, issue_sections.EVIDENCE):
                content = _fold(content.strip(), language, slug, issue.triage_commit, repo,
                                key == issue_sections.EVIDENCE)
            else:
                content = linkify(content.strip(), slug, issue.triage_commit, repo)
            blocks.append(f"## {title}\n\n{content}")
        return redactor.text("\n\n".join(blocks)) + "\n\n" + MARKER.format(project=project, issue_id=issue.id) + "\n"

    found = issue_sections.split(text)
    blocks = []

    # 1. 原因 (为什么有这个 issue / 产生的背景与根因)
    cause_raw = issue_sections.find(found, issue_sections.CAUSE)
    cause_text = _cause_block(cause_raw, language, slug, issue.triage_commit, repo) if cause_raw else None
    if cause_text:
        blocks.append(f"## {label_text.text('mirror.cause', language)}\n\n{cause_text}")

    # 2. 问题 (异常现象、期望与实际表现、复现步骤)
    problem_raw = issue_sections.find(found, issue_sections.PROBLEM) or issue_sections.find(found, issue_sections.REQUIREMENT)
    reproduce_raw = issue_sections.find(found, issue_sections.REPRODUCE)
    problem_text = _problem_block(problem_raw, reproduce_raw, language, slug, issue.triage_commit, repo)
    if problem_text:
        blocks.append(f"## {label_text.text('mirror.problem', language)}\n\n{problem_text}")

    # 3. 方法 (解决思路 / 建议方案与涉及文件)
    dir_raw = issue_sections.find(found, issue_sections.DIRECTION)
    scope_raw = issue_sections.find(found, issue_sections.SCOPE)
    method_text = _method_block(dir_raw, scope_raw, language, slug, issue.triage_commit, repo)
    if method_text:
        blocks.append(f"## {label_text.text('mirror.method', language)}\n\n{method_text}")

    # 4. 参考与详细证据 (折叠显示)
    ref_raw = issue_sections.find(found, issue_sections.REFERENCES) or issue_sections.find(found, issue_sections.EVIDENCE)
    if _is_meaningful(ref_raw):
        folded = _fold(ref_raw.strip(), language, slug, issue.triage_commit, repo, True)
        if _is_meaningful(folded):
            blocks.append(folded)

    return redactor.text("\n\n".join(blocks)) + "\n\n" + MARKER.format(project=project, issue_id=issue.id) + "\n"


def closes_line(link: GithubLink, origin: str | None) -> str:
    """PR 描述末尾关联镜像 Issue 的一行：镜像在 origin 同一仓库时写 `Closes #<编号>`，否则写完整的仓库名。"""
    slug = gh_issues.repo_slug(link.url.split("/issues/")[0])
    return f"Closes #{link.number}" if slug == origin else f"Closes {slug}#{link.number}"


def labelled_key(issue: Issue) -> str:
    """github_mirror.labelled_status 记下的标签：任务类型(没有任务类型时为空字符串)。"""
    return issue.task_type.value if issue.task_type is not None else ""


def relation_parts(conn: sqlite3.Connection, issue: Issue) -> dict[str, int]:
    """应建立的关系：父 Issue 与前一个子任务已有镜像时为它们的编号。"""
    found = {}
    for kind, other in ((PARENT, issue.parent), (BLOCKED_BY, issue.depends_on)):
        record = issues.get(conn, other) if other is not None else None
        if record is not None and record.issue.github is not None:
            found[kind] = record.issue.github.number
    return found


def relations_key(parts: Mapping[str, int]) -> str | None:
    return RELATION_SEPARATOR.join(f"{kind}={number}" for kind, number in sorted(parts.items())) or None


def recorded_relations(key: str | None) -> dict[str, int]:
    return {kind: int(number) for kind, _, number in
            (item.partition("=") for item in (key or "").split(RELATION_SEPARATOR) if item)}


def unsynced(conn: sqlite3.Connection, config: ProjectConfig) -> list[str]:
    """未同步到 GitHub 的项：没有镜像、标签或开关状态未对齐、待发评论、等待确认的操作、最近一次失败。"""
    if not github_comments.enabled(config):
        return []
    waiting = {record.subject_id: record.id for record in pending_operations.find(conn)
               if record.kind is OperationKind.GITHUB_ISSUE and record.status in WAITING}
    lines = []
    for record in issues.find(conn):
        issue = record.issue
        if not mirrored(issue):
            continue
        state = github_mirror.get(conn, issue.id)
        parts = []
        if issue.github is None:
            parts.append("未建 GitHub Issue")
        else:
            if state.labelled_status != labelled_key(issue):
                parts.append("标签未同步")
            if relation_parts(conn, issue) != recorded_relations(state.relations):
                parts.append("子 Issue 或阻塞关系未同步")
            wanted = desired_state(issue)
            if wanted is not None and wanted != state.remote_state:
                parts.append("开关状态未同步")
        comments = len(github_mirror.unposted(conn, issue.id))
        if comments:
            parts.append(f"{comments} 条评论未发出")
        if not parts and state.error is None:
            continue
        line = f"{issue.id}：{'、'.join(parts) or '已对齐'}"
        if issue.id in waiting:
            line += f"(等待确认 {waiting[issue.id]})"
        if state.error is not None:
            line += f"；最近一次失败：{state.error}"
        lines.append(line)
    return lines


class GithubMirror:
    def __init__(self, env: IssueEnv, process: VcsProcess, reader: GhIssues,
                 remotes: Callable[[Path], Mapping[str, tuple[str, ...]]],
                 redactor: Redactor, run_id: str, planner: Callable[[], OperationPlanner] | None = None) -> None:
        """remotes 为读取仓库 git 远程配置的函数(GitReader.remotes)；planner 在镜像写入需要确认时构造待确认操作。"""
        self.env = env
        self.process = process
        self.reader = reader
        self.remotes = remotes
        self.redactor = redactor
        self.run_id = run_id
        self.planner = planner
        self.settings = GithubSettings.from_config(env.config)
        self.refreshing: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.env.conn

    def slug(self) -> str:
        if self.settings.repo:
            return self.settings.repo
        urls = self.remotes(self.env.config.repo).get(ORIGIN_URL) or ()
        found = gh_issues.repo_slug(urls[0]) if urls else None
        if found is None:
            raise ValueError(f"无法从 {self.env.config.repo} 的 origin 远程解析出 GitHub 仓库，在 project.yaml 写 "
                             "issues.github.repo(owner/name)")
        return found

    # 对齐

    def sync(self, issue_ids: Sequence[str] | None = None) -> MirrorReport:
        """issue_ids 为空时对齐全部 Issue。tracker 不是 github 时不调用 gh。"""
        report = MirrorReport()
        if not github_comments.enabled(self.env.config):
            return report
        try:
            report.repo = slug = self.slug()
            facts = self.reader.repository(slug)
            if not facts.issues_enabled:
                report.skipped = f"{slug} 没有启用 Issue"
            elif self.settings.private_only and not facts.private:
                report.skipped = (f"{slug} 是公开仓库，issues.github.privateOnly 为 true 时不同步：缺陷与安全发现不应公开")
            else:
                remote = self.reader.states(slug)
                labels = self.reader.labels(slug)
        except (VcsError, ValueError) as error:
            report.skipped = f"读取 GitHub 失败：{error}"
        if report.skipped is None:
            chosen = [record for record in issues.find(self.conn)
                      if (issue_ids is None or record.issue.id in issue_ids) and mirrored(record.issue)]
            for record in chosen:
                self._one(slug, record, remote, labels, report)
        report.unsynced = unsynced(self.conn, self.env.config)
        return report

    def _one(self, slug: str, record: IssueRecord, remote: Mapping[int, RemoteIssue], labels: set[str],
             report: MirrorReport) -> None:
        issue_id = record.issue.id
        try:
            self._read_back(record, remote, report)
            record = transitions.record_of(self.env, issue_id)
            if self.settings.confirm_writes:
                self._plan(slug, record.issue, labels, report)
            else:
                self._run(slug, record.issue, labels, report)
        except FAILURES as error:
            github_mirror.record_failure(self.conn, issue_id, str(error), self.env.clock.now())

    def _read_back(self, record: IssueRecord, remote: Mapping[int, RemoteIssue], report: MirrorReport) -> None:
        issue = record.issue
        if issue.github is None:
            return
        state = github_mirror.get(self.conn, issue.id)
        found = remote.get(issue.github.number)
        if found is None:
            raise ValueError(f"GitHub 上没有找到 #{issue.github.number}(已删除，或超出 runtime.vcs.issueListLimit 条)")
        if found.state == state.remote_state:
            return
        if state.remote_state is not None:
            note = f"在 GitHub 上{'关闭' if found.state == CLOSED else '重新打开'} #{issue.github.number}"
            if found.state == CLOSED and not issue.is_closed and not (
                    issue.status in AUTO_CLOSED and found.reason == PR_CLOSE_REASON):
                context = IssueContext(close_reason=CloseReason.WONT_FIX)
                transitions.apply_event(self.env, record, IssueEvent.USER_CLOSED, context,
                                        note=f"{note}(GitHub 关闭原因 {found.reason or '未注明'})", comment=False)
                report.closed.append(issue.id)
            elif found.state == OPEN and issue.is_closed:
                transitions.apply_event(self.env, record, IssueEvent.USER_REOPENED, note=note, comment=False)
                report.reopened.append(issue.id)
        github_mirror.save(self.conn, replace(state, remote_state=found.state))

    # 计算 gh 命令

    def refresh(self, issue_ids: Sequence[str]) -> MirrorReport:
        """按当前本地正文更新已有镜像的标题与正文(issue rerender)，并照常对齐状态；还没有镜像的照常新建。"""
        self.refreshing = set(issue_ids)
        try:
            return self.sync(list(issue_ids))
        finally:
            self.refreshing = set()

    def _label_steps(self, slug: str, labels: set[str]) -> list[MirrorStep]:
        wanted = [(name, color, desc) for _, (name, color, desc) in STANDARD_TYPE_LABELS.items()]
        return [MirrorStep(gh_issues.label_args(slug, name, color, desc) + ("--force",),
                           f"建立标签 {name}", {"kind": "label"})
                for name, color, desc in wanted if name not in labels]

    def _labels(self, issue: Issue) -> list[str]:
        """镜像应有的标签：仅常规类型标签，不再包含内部状态标签。"""
        if issue.task_type is not None:
            return [self.settings.type_label(issue.task_type)]
        return []

    def _body(self, slug: str, issue: Issue) -> str:
        text = issue_files.read(self.env.layout.root / transitions.record_of(self.env, issue.id).path).body
        config = self.env.config
        return mirror_body(issue, text, config.name, slug, config.repo, config.language, self.redactor)

    def _create_step(self, slug: str, issue: Issue) -> MirrorStep:
        relations = relation_parts(self.conn, issue)
        return MirrorStep(gh_issues.create_args(slug, issue.title, "{raw}/issue-body.md", self._labels(issue),
                                                relations.get(PARENT), relations.get(BLOCKED_BY)),
                          "建立镜像 Issue", {"kind": "create", "status": issue.status.value, "slug": slug,
                                             "labelled": labelled_key(issue),
                                             "relations": relations_key(relations)},
                          {"issue-body.md": self._body(slug, issue)})

    def _update_steps(self, slug: str, issue: Issue, number: int) -> list[MirrorStep]:
        state = github_mirror.get(self.conn, issue.id)
        steps = []
        if issue.id in self.refreshing:
            steps.append(MirrorStep(gh_issues.edit_body_args(slug, number, issue.title, "{raw}/issue-body.md"),
                                     "更新标题与正文", {"kind": "edited"}, {"issue-body.md": self._body(slug, issue)}))
        if state.labelled_status != labelled_key(issue):
            wanted = self._labels(issue)
            previous = [name for name in self.settings.labels_of(state.labelled_status) if name not in wanted]
            if wanted or previous:
                steps.append(MirrorStep(gh_issues.edit_labels_args(slug, number, wanted, previous),
                                        f"标签改为 {'、'.join(wanted) or '无'}",
                                        {"kind": "labels", "status": labelled_key(issue)}))
            else:
                github_mirror.save(self.conn, replace(state, labelled_status=labelled_key(issue)))
        wanted_relations = relation_parts(self.conn, issue)
        recorded = recorded_relations(state.relations)
        missing = {kind: value for kind, value in wanted_relations.items() if recorded.get(kind) != value}
        if missing:
            steps.append(MirrorStep(gh_issues.edit_relations_args(slug, number, missing.get(PARENT),
                                                                  missing.get(BLOCKED_BY)),
                                    "建立子 Issue 与阻塞关系", {"kind": "relations",
                                                         "relations": relations_key(wanted_relations)}))
        for comment in github_mirror.unposted(self.conn, issue.id):
            name = f"comment-{comment.id}.md"
            steps.append(MirrorStep(gh_issues.comment_args(slug, number, f"{{raw}}/{name}"), "追加评论",
                                    {"kind": "comment", "id": comment.id}, {name: self.redactor.text(comment.body)}))
        wanted = desired_state(issue)
        if wanted is not None and wanted != state.remote_state:
            if wanted == CLOSED:
                reason = gh_issues.COMPLETED if issue.close_reason is CloseReason.FIXED else gh_issues.NOT_PLANNED
                steps.append(MirrorStep(gh_issues.close_args(slug, number, reason), f"关闭({reason})",
                                        {"kind": "state", "state": CLOSED}))
            else:
                steps.append(MirrorStep(gh_issues.reopen_args(slug, number), "重新打开", {"kind": "state", "state": OPEN}))
        return steps

    def _created(self, slug: str, issue: Issue) -> GithubLink | None:
        """幂等键已记下的镜像：已完成的直接取用；进行中的(上次在 gh 返回后被中断)按本地标记找回，找不到时放弃该键。"""
        key = CREATE_KEY.format(slug=slug, issue_id=issue.id)
        record = idempotency.get(self.conn, key)
        if record is None:
            return None
        if record.status == idempotency.DONE and record.result is not None:
            return GithubLink.from_dict(dict(record.result))
        found = self.reader.find_marker(slug, MARKER.format(project=self.env.config.name, issue_id=issue.id))
        if found is None:
            idempotency.abandon(self.conn, key)
            return None
        idempotency.complete(self.conn, key, found.to_dict(), self.env.clock)
        return found

    # 直接执行

    def _run(self, slug: str, issue: Issue, labels: set[str], report: MirrorReport) -> None:
        created = issue.github is None
        if issue.github is None:
            link = self._created(slug, issue)
            if link is None:
                steps = [*self._label_steps(slug, labels), self._create_step(slug, issue)]
                key = CREATE_KEY.format(slug=slug, issue_id=issue.id)
                idempotency.begin(self.conn, key, self.env.clock)
                self._execute(issue.id, steps, labels)
            else:
                self._link(issue.id, link, labelled_key(issue), slug)
            issue = transitions.record_of(self.env, issue.id).issue
        steps = self._update_steps(slug, issue, issue.github.number) if issue.github is not None else []
        if steps:
            steps = [*self._label_steps(slug, labels), *steps]
            self._execute(issue.id, steps, labels)
        self._synced(issue.id)
        if created or steps:
            report.written.append(issue.id)

    def _execute(self, issue_id: str, steps: Sequence[MirrorStep], labels: set[str]) -> None:
        directory = self.env.layout.vcs_raw_dir(self.run_id, f"github-{issue_id}")
        for step in steps:
            for name, text in step.files.items():
                atomic.write_text(directory / name, text)
            args = tuple(part.replace("{raw}", str(directory)) for part in step.args)
            completed = self.process.gh(self.env.config.repo, *args)
            if step.effect["kind"] == "label":
                labels.add(args[2])
            self.apply(issue_id, step.effect, completed.stdout)

    # 逐次确认

    def _plan(self, slug: str, issue: Issue, labels: set[str], report: MirrorReport) -> None:
        operations = [record for record in pending_operations.find(self.conn, subject_id=issue.id)
                      if record.kind is OperationKind.GITHUB_ISSUE]
        waiting = [record.id for record in operations if record.status in WAITING]
        if waiting:
            return
        if issue.github is None:
            link = self._created(slug, issue)
            if link is not None:
                self._link(issue.id, link, labelled_key(issue), slug)
                issue = transitions.record_of(self.env, issue.id).issue
        main = ([self._create_step(slug, issue)] if issue.github is None
                else self._update_steps(slug, issue, issue.github.number))
        if not main:
            return
        steps = [*self._label_steps(slug, labels), *main]
        files = {name: text for step in steps for name, text in step.files.items()}
        digest = hashlib.sha256(json.dumps([[list(step.args) for step in steps], files, format_iso(issue.updated_at)],
                                           ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        key = f"github:{issue.id}:{digest}"
        if any(record.idempotency_key == key and record.status is OperationStatus.REJECTED for record in operations):
            return
        if self.planner is None:
            raise ValueError("gates.mirror-writes 为 user 时需要待确认操作的构造器")
        text = f"同步 Issue {issue.id} 到 GitHub {slug}：" + "；".join(step.description for step in main)
        operation = self.planner().plan_github_issue(
            issue.id, [(step.args, step.description) for step in steps], files, text, key,
            {"effects": [dict(step.effect) for step in steps]})
        report.operations.append(operation.id)

    def follow_up(self) -> FollowUp:
        return FollowUp(executed=self.on_executed)

    def on_executed(self, operation: PendingOperation) -> None:
        if operation.kind is not OperationKind.GITHUB_ISSUE:
            return
        outputs = list((operation.result or {}).get("outputs") or [])
        for effect, stdout in zip(operation.preconditions["effects"], outputs):
            self.apply(operation.subject_id, effect, stdout)
        self._synced(operation.subject_id)

    def _synced(self, issue_id: str) -> None:
        state = github_mirror.get(self.conn, issue_id)
        github_mirror.save(self.conn, replace(state, error=None, failed_at=None, synced_at=self.env.clock.now()))

    # 记下结果

    def apply(self, issue_id: str, effect: Mapping[str, Any], stdout: str) -> None:
        kind = effect["kind"]
        if kind == "create":
            link = gh_issues.issue_link(stdout)
            key = CREATE_KEY.format(slug=effect["slug"], issue_id=issue_id)
            idempotency.begin(self.conn, key, self.env.clock)
            idempotency.complete(self.conn, key, link.to_dict(), self.env.clock)
            self._link(issue_id, link, effect.get("labelled", effect["status"]), effect["slug"],
                       effect.get("relations"))
        elif kind == "relations":
            state = github_mirror.get(self.conn, issue_id)
            github_mirror.save(self.conn, replace(state, relations=effect["relations"]))
        elif kind == "labels":
            state = github_mirror.get(self.conn, issue_id)
            github_mirror.save(self.conn, replace(state, labelled_status=effect["status"]))
        elif kind == "comment":
            github_mirror.mark_posted(self.conn, int(effect["id"]), self.env.clock.now())
        elif kind == "state":
            state = github_mirror.get(self.conn, issue_id)
            github_mirror.save(self.conn, replace(state, remote_state=effect["state"]))

    def _link(self, issue_id: str, link: GithubLink, labelled: str, slug: str, relations: str | None = None) -> None:
        transitions.annotate(self.env, issue_id, f"已建 GitHub Issue {slug}#{link.number}：{link.url}",
                             updates={"github": link})
        state = github_mirror.get(self.conn, issue_id)
        github_mirror.save(self.conn, replace(state, remote_state=OPEN, labelled_status=labelled,
                                              relations=relations if relations is not None else state.relations))
