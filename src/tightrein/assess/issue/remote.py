"""GitHub 上已有的 Issue(采集 collect.github_issues 导入的)：接手编号、评估之后在 GitHub 上回应。纯程序，不调用模型。

- 接手：由它建出的本地 Issue 把镜像直接记成那个 GitHub 编号(extra.github，在 github.sync 之前写好)，同步时不再新建
  第二个；修复的 PR 照常写 `Closes #<编号>`，合并时由 GitHub 关闭；拆分成几个 Issue 时只有第一个接手；
- 回应(评估的事务提交之后，按去向)：并入别的问题或追加到已有的 Issue → 评论「和 #N 是同一个问题」并以 not planned
  关闭；不成立 → 一句理由并以 not planned 关闭；不修、命中取舍 →「这个先不改：理由」并以 not planned 关闭；主干上
  已修 →「已经在 <commit> 修好了」并以 completed 关闭；证据不足、观察、转人工 → 不动，留着打开；
- 写到 GitHub 的话读起来像开发者自己写的：一两句、按项目语言(zh、en、ja，没有的取英文)，不提工具与模型，不写本地
  编号(引用只用 GitHub 编号，对方没有 GitHub 编号就不写编号)；理由取取证输出的第一句并脱敏；
- 评论与关闭都经 gh 的幂等写操作(同一编号同一正文只发一次)；GitHub 出错由调用方记进评估记录，不让评估失败。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tightrein.assess.claims import signal_evidence
from tightrein.assess.issue import body, github
from tightrein.protocol.git.github import COMPLETED, NOT_PLANNED
from tightrein.store.tables import issues, occurrences, problems
from tightrein.store.tables.occurrences import Occurrence

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

SOURCE = "collect.github_issues"
REQUEST = "request"
POINT = "assess.issue"
REASON_CHARS = 200
INTERNAL_WORDS = ("主张", "取证", "候选", "信号", "本问题", "现象来源", "触发条件", "评估", "claim", "triage")
# 第一句：到中日文句末标点为止，或英文句号后跟空白(不在 `orders.py` 这种文件名中间断开)
_SENTENCE = re.compile(r"^(.+?(?:[。！？!?]|\.(?=\s)))")
_END = "。！？.!?"
TEXTS: dict[str, dict[str, str]] = {
    "duplicate": {"zh": "和 #{number} 是同一个问题，在那边一起处理。", "en": "Same problem as #{number}, tracking it there.",
                  "ja": "#{number} と同じ問題なので、そちらで対応します。"},
    "duplicateBare": {"zh": "和已有的一个问题是同一个，在那边一起处理。",
                      "en": "Same problem as an existing one, tracking it there.",
                      "ja": "既存の問題と同じなので、そちらで対応します。"},
    "invalid": {"zh": "看了下代码，这里没有问题：{reason}", "en": "Looked into the code; this isn't a bug: {reason}",
                "ja": "コードを確認しましたが、不具合ではありませんでした：{reason}"},
    "invalidBare": {"zh": "看了下代码，这里没有问题。", "en": "Looked into the code; this isn't a bug.",
                    "ja": "コードを確認しましたが、不具合ではありませんでした。"},
    "later": {"zh": "这个先不改：{reason}", "en": "Not changing this for now: {reason}", "ja": "今回は対応しません：{reason}"},
    "laterBare": {"zh": "这个先不改。", "en": "Not changing this for now.", "ja": "今回は対応しません。"},
    "tradeoff": {"zh": "之前权衡过，是有意这样做的。", "en": "it's a deliberate trade-off.",
                 "ja": "検討したうえで意図的にこうしています。"},
    "fixed": {"zh": "已经在 {commit} 修好了。", "en": "Already fixed in {commit}.", "ja": "{commit} で修正済みです。"},
    "fixedBare": {"zh": "主干上已经修好了。", "en": "Already fixed on the main branch.", "ja": "メインブランチで修正済みです。"},
}


@dataclass(frozen=True)
class Remote:
    number: int
    url: str
    request: bool  # 带需求标签的(collect.github_issues 的 featureLabels)
    title: str
    body: str


def of(found: Sequence[Occurrence]) -> Remote | None:
    """问题的出现记录中来自 GitHub Issue 的那条(取最近的)；不是导入的问题为 None。"""
    for item in sorted(found, key=lambda occurrence: (occurrence.seen_at, occurrence.id or 0), reverse=True):
        evidence = signal_evidence(item)
        number = evidence.get("number")
        if item.source == SOURCE and number is not None and str(number).isdigit():
            return Remote(int(number), str(evidence.get("url") or ""), item.evidence.get("checkType") == REQUEST,
                          str(item.evidence.get("message") or ""), str(evidence.get("body") or ""))
    return None


def of_problem(runtime: Runtime, problem_id: str) -> Remote | None:
    return of(occurrences.find(runtime.conn, problem_id))


def mirror(remote: Remote) -> dict[str, Any]:
    """接手时写进 Issue 的 extra.github：与 github.sync 自己新建时记的一样，同步据此不再新建。"""
    return {"number": remote.number, "url": remote.url, "state": "open", "label": None}


def number_of(record: Issue | None) -> int | None:
    """本地 Issue 镜像的 GitHub 编号；没有镜像为 None(不退回本地编号：本地编号不写到 GitHub 上)。"""
    found = record.extra.get(github.MIRROR) if record is not None else None
    number = found.get("number") if isinstance(found, dict) else None
    return int(number) if number is not None and str(number).isdigit() else None


def respond(runtime: Runtime, problem_id: str, remote: Remote, *, destination: str | None, issue_ids: Sequence[str],
            merged_into: str | None, output: Mapping[str, Any]) -> dict[str, Any] | None:
    """按评估的去向在 GitHub 上回应；返回做了什么(写进评估记录)，不用回应时为 None。GitError 原样抛给调用方。"""
    if not github.enabled(runtime):
        return None
    language = runtime.language
    if destination == "issue":
        linked = [number_of(issues.get(runtime.conn, issue_id)) for issue_id in issue_ids]
        if remote.number in linked:
            return None  # 已接手：由修复的 PR 关闭
        return _close(runtime, problem_id, remote, _duplicate(next(iter(linked), None), language), NOT_PLANNED)
    if destination == "merged":
        return _close(runtime, problem_id, remote, _duplicate(_target_number(runtime, merged_into), language),
                      NOT_PLANNED)
    if destination == "false_positive" and (output.get("fixedOnMain") or {}).get("commit"):
        destination = "awaiting_deploy"  # 主干上已经修好的不说「没有问题」，说已经修好
    if destination == "false_positive":
        reason = first_sentence(runtime, ((output.get("sourceOfPhenomenon") or {}).get("explanation")), language)
        text = _text("invalid", language, reason=reason) if reason else _text("invalidBare", language)
        return _close(runtime, problem_id, remote, text, NOT_PLANNED)
    if destination in ("wont_fix", "tradeoff"):
        reason = _text("tradeoff", language) if destination == "tradeoff" else first_sentence(
            runtime, (output.get("assessment") or {}).get("worthReason"), language)
        text = _text("later", language, reason=reason) if reason else _text("laterBare", language)
        return _close(runtime, problem_id, remote, text, NOT_PLANNED)
    if destination == "awaiting_deploy":
        commit = str((output.get("fixedOnMain") or {}).get("commit") or "")[:7]
        text = _text("fixed", language, commit=commit) if commit else _text("fixedBare", language)
        return _close(runtime, problem_id, remote, text, COMPLETED)
    return None


def first_sentence(runtime: Runtime, text: Any, language: str) -> str | None:
    """取一句话当理由：第一句、脱敏、截短；句末没有标点时按语言补上。带评估内部用语(主张、取证、候选……)的不用，
    回应只留一句通用的话：开发者不会这么说。"""
    found = " ".join(str(text or "").split())
    if not found or any(word in found for word in INTERNAL_WORDS):
        return None
    match = _SENTENCE.match(found)
    sentence = runtime.redactor.text(match.group(1) if match else found).strip()[:REASON_CHARS]
    if sentence and sentence[-1] not in _END:
        sentence += "。" if language.split("-")[0].split("_")[0].lower() in ("zh", "ja") else "."
    return sentence or None


def _close(runtime: Runtime, problem_id: str, remote: Remote, text: str, reason: str) -> dict[str, Any]:
    hub = runtime.github
    assert hub is not None
    scope = runtime.scope(problem_id, POINT)
    hub.issue_comment(remote.number, text, scope=scope)
    hub.issue_close(remote.number, reason, scope=scope)
    return {"number": remote.number, "comment": text, "closed": reason}


def _duplicate(number: int | None, language: str) -> str:
    return _text("duplicate", language, number=number) if number is not None else _text("duplicateBare", language)


def _target_number(runtime: Runtime, target: str | None) -> int | None:
    """并入目标对外的编号：目标的 Issue 有 GitHub 镜像时用它，否则目标自己是导入的 GitHub Issue 时用那个编号。"""
    found = problems.get(runtime.conn, target) if target else None
    if found is None:
        return None
    number = number_of(issues.get(runtime.conn, found.issue)) if found.issue else None
    if number is None:
        imported = of_problem(runtime, found.id)
        number = imported.number if imported is not None else None
    return number


def _text(key: str, language: str, **values: Any) -> str:
    found = body.pick(TEXTS[key], language)
    return found.format(**values) if values else found
