"""提 PR：精简描述(问题、改了什么、检查结果)，审查结论只发一条摘要评论。

- 分支名先复核：不符合项目的分支格式、或个人前缀是 AI 或工具名时停下，给出建议名；
- 标题与描述不调用模型：标题取编码时 agent 给的一句话(没有时取 Issue 标题)，按项目约定的格式拼出；描述取交付的文字，
  之后是关联(有 GitHub 镜像 Issue 时写 `Closes #<编号>`，合并后 GitHub 自动关闭)，最后是程序生成的检查结果与接受的
  未通过项；项目有 PR 模板时按模板标题放进同样的内容(protocol/git/format.fill_template)；
- 先查后做：该分支已有打开的 PR 时只在描述有变化时更新(protocol/git 的 GitHub.create_pr)；做决定时记下该分支上打开的
  PR(GitHub.pull_state)作为 expected：新建前再观察一次，不同即抛 Stale、不新建(由 release.py 停到下次重新观察)；
- 审查结论只作说明发一条评论，同一 PR 同一轮只发一次；不是批准，是否合并按 merge.py 的条件。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from tightrein.protocol.git import Conventions, GitHub, PullText
from tightrein.protocol.git.format import (
    HOTFIX,
    BranchRejected,
    branch_name,
    branch_problem,
    fill_template,
    kebab,
    pull_body,
    title,
)
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_PR, Delivery, ReleaseBlocked, ReleaseState
from tightrein.store.tables.issues import Issue

URGENT = "P0"
PREFIX_PLACEHOLDER = "<个人前缀>"
# PR 上程序生成的几行字按项目语言写；字段名、路径、命令不翻译
LABELS: dict[str, dict[str, str]] = {
    "zh": {"checks": "检查结果(程序生成)：", "passed": "通过", "failed": "未通过", "accepted": "接受的未通过项",
           "review": "以下是 tightrein 的审查结论，只作说明，不是批准。", "round": "第 {round} 轮审查：",
           "none": "无"},
    "en": {"checks": "Checks (generated):", "passed": "passed", "failed": "failed", "accepted": "Accepted failure",
           "review": "tightrein review summary, for information only; this is not an approval.",
           "round": "Review round {round}:", "none": "none"},
    "ja": {"checks": "チェック結果(自動生成)：", "passed": "合格", "failed": "不合格", "accepted": "受け入れた不合格項目",
           "review": "以下は tightrein のレビュー結果です。説明のみで、承認ではありません。",
           "round": "第 {round} ラウンドのレビュー：", "none": "なし"},
}


@dataclass(frozen=True)
class OpenedPull:
    number: int
    url: str
    title: str
    body: str


def open_pull(runtime: Runtime, issue: Issue, delivery: Delivery, conventions: Conventions,
              github: GitHub) -> OpenedPull:
    problem = branch_problem(delivery.branch, conventions)
    if problem is not None:
        raise ReleaseBlocked(POINT_PR, f"{problem}；建议改名为 {suggest(issue, conventions)}，改名后再发布")
    labels = LABELS.get(runtime.language, LABELS["zh"])
    heading = title(conventions.title, kind=conventions.types[issue.kind],
                    summary=delivery.text.title or delivery.text.summary or issue.title)
    body = pull_text(issue, delivery, conventions, labels)
    main = runtime.git.main_branch
    decided = github.pull_state(delivery.branch)
    number = github.create_pr(delivery.branch, main, heading, body, scope=runtime.scope(issue.id, POINT_PR),
                              expected=decided)
    return OpenedPull(number, github.pr_view(number).url, heading, body)


def pull_text(issue: Issue, delivery: Delivery, conventions: Conventions, labels: dict[str, str]) -> str:
    text = delivery.text
    mirror = mirror_number(issue)
    pull = PullText(problem=text.problem or issue.title, approach=text.approach or text.why or "",
                    limitations=text.limitations or "", relations=(f"Closes #{mirror}",) if mirror else (),
                    verification=verification(delivery, labels))
    if conventions.template:
        return fill_template(conventions.template, pull, labels["checks"])
    return pull_body(pull, labels["checks"])


def verification(delivery: Delivery, labels: dict[str, str]) -> tuple[str, ...]:
    lines = [f"{check.name}：{labels['passed'] if check.passed else labels['failed']}"
             + (f"({check.detail})" if check.detail and not check.passed else "") for check in delivery.checks]
    lines += [f"{labels['accepted']}：{item}" for item in delivery.accepted]
    return tuple(lines)


def post_review(runtime: Runtime, issue: str, number: int, delivery: Delivery, state: ReleaseState,
                github: GitHub) -> bool:
    """最后一轮审查的结论以一条评论发到 PR；同一 PR 同一轮只发一次。返回这次是否发了。"""
    if not bool(runtime.settings.control("release", "reviewComment")) or delivery.review_round is None:
        return False
    posted = f"{number}:{delivery.review_round}"
    if state.review_posted == posted:
        return False
    labels = LABELS.get(runtime.language, LABELS["zh"])
    github.comment(number, review_body(delivery, labels), scope=runtime.scope(issue, POINT_PR))
    state.review_posted = posted
    return True


def review_body(delivery: Delivery, labels: dict[str, str]) -> str:
    lines = [labels["review"], "", labels["round"].format(round=delivery.review_round)]
    lines += [f"- {item}" for item in delivery.review] or [f"- {labels['none']}"]
    return "\n".join(lines) + "\n"


def suggest(issue: Issue, conventions: Conventions) -> str:
    """建议的分支名；项目要求个人前缀而没有配置或用了 AI 名称时，前缀留给用户填写。"""
    kind = HOTFIX if issue.severity == URGENT else conventions.types[issue.kind]
    try:
        return branch_name(conventions, kind=kind, issue=issue.id, slug=kebab(issue.title))
    except BranchRejected:
        return branch_name(_without_prefix(conventions), kind=kind, issue=issue.id, slug=kebab(issue.title))


def mirror_number(issue: Issue) -> int | None:
    """GitHub 镜像 Issue 的编号(assess/issue/github 写在 extra.github)。"""
    found: Any = issue.extra.get("github")
    if isinstance(found, dict):
        found = found.get("number")
    return int(found) if isinstance(found, int | str) and str(found).isdigit() else None


def _without_prefix(conventions: Conventions) -> Conventions:
    return replace(conventions, prefix=PREFIX_PLACEHOLDER, personal_prefix=True)
