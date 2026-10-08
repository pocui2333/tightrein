"""发布各步共用的记录：实施交付的事实(Delivery)、发布进度(ReleaseState，存在 issues.extra)、交接落盘、状态转换与事件。

发布不调用模型，各步之间只靠这几样东西衔接：
- Delivery 读自实施·交付的 handoff(44c 规定的必填事实)，发布据此提交、提 PR、判断合并；
- ReleaseState 记本工具做过什么(最近一次推送的 commit、合并的 main、未合并的原因、部署与验收)，跟着 Issue 存在
  issues.extra["release"]：合并队列一次查出全部待发布的 Issue 就能排队，不必逐个读文件；
- Issue 状态的转换一律经 assess/issue/transitions.apply_event(44c)：它同时写 Issue 记录文件与历史，关闭、重开时由它
  同步 GitHub 镜像 Issue(assess/issue/github)；发布进度也经 assess/issue/files.write 写，记录文件与 issues 表一致。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tightrein.assess.issue import files, transitions
from tightrein.assess.issue.transitions import IssueEvent
from tightrein.protocol import handoff
from tightrein.protocol.handoff import Handoff, Metrics, Status
from tightrein.protocol.naming import FileName, format_iso
from tightrein.protocol.recovery import checkpoints
from tightrein.protocol.runtime import Runtime
from tightrein.store.tables.issues import Issue

DELIVER_POINT = "implement.deliver"
EXTRA_KEY = "release"
ACTOR = "release"
# 发布各步的控制键(交接文件名的序号见 protocol/naming.STEP_SEQUENCE)：提交、同步、推送都记在 release.pr 之下
POINT_PR = "release.pr"
POINT_CI = "release.ci"
POINT_MERGE = "release.merge"
POINT_DEPLOY = "release.deploy"
POINT_ACCEPT = "release.accept"
POINT_CLEANUP = "release.cleanup"


class ReleaseBlocked(Exception):
    """发布停在这一步，需要人处理(原因写进待决定文档)；不是程序错误。"""

    def __init__(self, point: str, reason: str, *, options: tuple[str, ...] = (), command: str | None = None) -> None:
        super().__init__(reason)
        self.point = point
        self.reason = reason
        self.options = options
        self.command = command


class DeliveryMissing(Exception):
    """实施还没有交付(没有 implement.deliver 的交接)，或交接缺了发布要用的事实。"""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str | None = None


@dataclass(frozen=True)
class ReleaseText:
    """编码时 agent 给出的文字(agent 不做 git 写操作，只给文字，由程序按项目约定拼进提交信息与 PR)。"""

    title: str | None = None
    scope: str | None = None
    summary: str | None = None
    why: str | None = None
    problem: str | None = None
    approach: str | None = None
    limitations: str | None = None


@dataclass(frozen=True)
class Delivery:
    """实施·交付的必填事实(handoff.facts 中的键为小驼峰)。"""

    branch: str
    worktree: Path
    commit: str
    diff_hash: str
    changed_files: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    accepted: tuple[str, ...]  # 用户接受的未通过项：写进 PR 描述，并使自动合并不成立
    review_round: int | None
    review: tuple[str, ...]  # 最后一轮各审查的结论
    high_risk_paths: tuple[str, ...]
    text: ReleaseText

    @property
    def checks_passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass
class ReleaseState:
    """本工具在这个 Issue 上做过的发布动作；读写都经 load_state、save_state。"""

    pushed: str | None = None  # 最近一次推送的 commit：PR 头部不等于它说明有别人的提交
    synced_main: str | None = None  # 最近一次合并进来的 origin/main
    conflicts: list[str] = field(default_factory=list)  # 进行中的合并的冲突文件(用户 git add 后不再显示为冲突)
    pr_url: str | None = None
    review_posted: str | None = None  # 已发审查摘要评论的「PR:轮次」
    ci: dict[str, Any] | None = None
    merge_reasons: list[str] = field(default_factory=list)
    native_head: str | None = None  # 已为这个 head 开启 GitHub 原生自动合并
    decision: list[str] = field(default_factory=list)  # 已为这组高风险路径(或人工合并)写过待决定文档
    entered_at: str | None = None  # 第一次进入发布的时间(没有放行时间时用于排队)
    merged_at: str | None = None
    deploy: dict[str, Any] | None = None  # {commit, id, url, status, at, source}
    manual_noted: bool = False
    accept: dict[str, Any] | None = None  # 最近一次确认的结论与观察期
    revert: dict[str, Any] | None = None  # {branch, pr, url}
    cleanup: str | None = None  # done 或保留的原因

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ReleaseState:
        known = {name: value for name, value in (data or {}).items() if name in cls.__dataclass_fields__}
        return cls(**known)


# Delivery


def delivery(runtime: Runtime, issue: str) -> Delivery:
    """最近一次实施·交付的事实。"""
    found = [item.handoff for item in checkpoints(runtime.workspace, issue) if item.handoff.point == DELIVER_POINT]
    if not found:
        raise DeliveryMissing(f"Issue {issue} 还没有实施·交付的交接")
    return parse_delivery(found[-1].facts, issue)


def parse_delivery(facts: dict[str, Any], issue: str) -> Delivery:
    missing = [key for key in ("branch", "worktree", "commit", "diffHash", "changedFiles") if not facts.get(key)]
    if missing:
        raise DeliveryMissing(f"Issue {issue} 的交付缺少 {'、'.join(missing)}")
    review = facts.get("review") or {}
    text = facts.get("release") or {}
    return Delivery(
        branch=str(facts["branch"]),
        worktree=Path(facts["worktree"]),
        commit=str(facts["commit"]),
        diff_hash=str(facts["diffHash"]),
        changed_files=tuple(sorted(_path(item) for item in facts["changedFiles"])),
        checks=tuple(CheckResult(str(item["name"]), bool(item["passed"]), item.get("detail"))
                     for item in facts.get("checks") or []),
        accepted=tuple(str(item) for item in facts.get("acceptedFindings") or []),
        review_round=review.get("round"),
        review=tuple(str(item) for item in review.get("conclusions") or []),
        high_risk_paths=tuple(sorted(facts.get("highRiskPaths") or [])),
        text=ReleaseText(**{name: text.get(name) for name in ReleaseText.__dataclass_fields__}),
    )


# ReleaseState


def load_state(issue: Issue) -> ReleaseState:
    return ReleaseState.from_json(issue.extra.get(EXTRA_KEY))


def attach(issue: Issue, state: ReleaseState) -> None:
    """把发布进度放回 issue(不保存)；随后由 move 或 save_state 一起写。"""
    issue.extra = {**issue.extra, EXTRA_KEY: state.to_json()}


def save_state(runtime: Runtime, issue: Issue, state: ReleaseState) -> None:
    attach(issue, state)
    files.write(runtime, issue)


def now_iso(runtime: Runtime) -> str:
    return format_iso(runtime.clock.now())


# 交接、转换与事件


def write_handoff(runtime: Runtime, issue: str, point: str, status: Status, summary: str, facts: dict[str, Any],
                  *, started: float, produced: dict[str, int] | None = None) -> Path:
    """每一步落盘一份 handoff.json；started 为 time.monotonic() 的起点。"""
    path = runtime.workspace.step_file(issue, FileName(point, "handoff", "json"))
    record = Handoff(point=point, subject=issue, run=runtime.run, status=status, summary=summary, facts=facts,
                     metrics=Metrics(duration_ms=int((time.monotonic() - started) * 1000), produced=produced),
                     created_at=now_iso(runtime))
    handoff.write(path, record)
    return path


def move(runtime: Runtime, issue: Issue, event: IssueEvent, *, point: str, reason: str | None = None,
         note: str | None = None, updates: dict[str, Any] | None = None) -> Issue:
    """先保存 issue(含发布进度)，再经状态机转换；不在转换表中的组合由状态机抛 InvalidTransition。

    reason 按状态机的约定：FAIL 为停下原因(fix_rejected 为修复未采纳)，其余事件不填；说明写在 note。"""
    files.write(runtime, issue)
    moved = transitions.apply_event(runtime, issue.id, event, reason=reason, actor=ACTOR, note=note, updates=updates)
    emit(runtime, issue.id, point, "decision", f"{event.value}：{note or reason or ''}")
    return moved


def emit(runtime: Runtime, issue: str, point: str, kind: str, summary: str) -> None:
    runtime.events.emit(run=runtime.run, subject=issue, point=point, kind=kind, summary=summary)


def _path(item: Any) -> str:
    """交付的改动文件可以是路径，也可以是 {path, added, deleted}。"""
    return str(item["path"]) if isinstance(item, dict) else str(item)
