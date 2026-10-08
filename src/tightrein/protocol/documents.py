"""给人看的三种文档(protocol/handoff.md「给人看的文档」)：待审核、出问题、交付。

都由程序从 store 与各步的 handoff.json 渲染，写在对象目录的 `90-…md`，不额外调用模型：
- 待审核(`90-issue-pending.md`)：到人工关卡时，要决定什么、选项、推荐与理由、不决定会怎样、确认用的命令；
- 出问题(`90-issue-failure.md`)：停下时，停在哪一步、原因、已经试过什么、建议怎么处理、接着做的命令；
- 交付(`90-issue-deliver.md`)：流程走完时，修了什么、改了哪些文件、PR 与合并提交、验收结果、整个流程的量化数据。

排版按「结论 → 必填事实 → 量化数据 → 备注」；标题与字段说明按项目语言，字段名、路径、命令、代码不翻译。
模型写的自由文字嵌进来前整体下移标题级别(不打乱文档结构)，没闭合的代码块按 CommonMark 补上闭合标记。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.protocol import recovery
from tightrein.protocol.handoff import Handoff, Tokens
from tightrein.protocol.naming import format_count, format_duration, format_iso, kind_of
from tightrein.store.files.markdown import demote_headings, write_markdown
from tightrein.store.tables import issues, problems

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

# 自由文字嵌在二级标题之下：其中的 `#` 至少下移到三级
FREE_TEXT_LEVELS = 2
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "pending": "待审核", "failure": "出问题", "deliver": "交付", "sep": "：",
        "item": "项", "value": "内容", "point": "步骤", "command": "命令", "at": "时间",
        "decision": "要决定", "options": "选项", "recommendation": "推荐", "reason": "理由",
        "if_not": "不决定会怎样", "cause": "原因", "tried": "已经试过", "advice": "建议怎么处理",
        "nothing": "无", "facts": "必填事实", "metrics": "量化数据", "steps": "各步骤",
        "files": "改动的文件", "summary": "修了什么", "branch": "分支", "pr": "PR", "merge": "合并提交",
        "deploy": "部署", "accept": "验收", "duration": "总耗时", "calls": "模型调用", "tokens": "token(输入/输出/缓存读取)",
        "cost": "费用", "estimated": "(估算)", "rounds": "轮数", "status": "状态", "round": "轮", "path": "文件",
        "changed": "增删行",
    },
    "en": {
        "pending": "Pending review", "failure": "Stopped", "deliver": "Delivered", "sep": ": ",
        "item": "Item", "value": "Value", "point": "Step", "command": "Command", "at": "Time",
        "decision": "To decide", "options": "Options", "recommendation": "Recommendation", "reason": "Why",
        "if_not": "If nobody decides", "cause": "Reason", "tried": "Already tried", "advice": "What to do",
        "nothing": "None", "facts": "Facts", "metrics": "Metrics", "steps": "Steps",
        "files": "Changed files", "summary": "What was fixed", "branch": "Branch", "pr": "PR",
        "merge": "Merge commit", "deploy": "Deploy", "accept": "Acceptance", "duration": "Total time",
        "calls": "Model calls", "tokens": "Tokens (input/output/cache read)", "cost": "Cost",
        "estimated": " (estimated)", "rounds": "Rounds", "status": "Status", "round": "Round", "path": "File",
        "changed": "Lines +/-",
    },
    "ja": {
        "pending": "確認待ち", "failure": "停止", "deliver": "納品", "sep": "：",
        "item": "項目", "value": "内容", "point": "ステップ", "command": "コマンド", "at": "時刻",
        "decision": "決めること", "options": "選択肢", "recommendation": "推奨", "reason": "理由",
        "if_not": "決めない場合", "cause": "原因", "tried": "試したこと", "advice": "対処方法",
        "nothing": "なし", "facts": "必須事項", "metrics": "定量データ", "steps": "各ステップ",
        "files": "変更したファイル", "summary": "修正内容", "branch": "ブランチ", "pr": "PR",
        "merge": "マージコミット", "deploy": "デプロイ", "accept": "受け入れ確認", "duration": "合計時間",
        "calls": "モデル呼び出し", "tokens": "トークン(入力/出力/キャッシュ読取)", "cost": "費用",
        "estimated": "(推定)", "rounds": "ラウンド数", "status": "状態", "round": "ラウンド", "path": "ファイル",
        "changed": "増減行",
    },
}


# 公共函数


def pending(runtime: Runtime, subject: str, *, point: str, decision: str, options: list[str], recommendation: str,
            reason: str, if_not: str, command: str) -> Path:
    labels = _labels(runtime)
    lines = [_title(runtime, subject, labels["pending"]), "", free_text(decision), "",
             *_table(labels, [(labels["point"], _code(point)), (labels["command"], _code(command)),
                              (labels["at"], format_iso(runtime.clock.now()))]), ""]
    lines += [f"## {labels['options']}", ""]
    lines += [f"{number}. {_inline(option)}" for number, option in enumerate(options, start=1)] or [labels["nothing"]]
    lines += ["", f"## {labels['recommendation']}", "", free_text(recommendation) or labels["nothing"]]
    lines += ["", f"## {labels['reason']}", "", free_text(reason) or labels["nothing"]]
    lines += ["", f"## {labels['if_not']}", "", free_text(if_not) or labels["nothing"]]
    return _write(runtime, subject, "pending", lines)


def failure(runtime: Runtime, subject: str, *, point: str, reason: str, tried: list[str], advice: str,
            command: str) -> Path:
    labels = _labels(runtime)
    lines = [_title(runtime, subject, labels["failure"]), "", free_text(reason), "",
             *_table(labels, [(labels["point"], _code(point)), (labels["command"], _code(command)),
                              (labels["at"], format_iso(runtime.clock.now()))]), ""]
    lines += [f"## {labels['tried']}", ""]
    lines += [f"- {_inline(item)}" for item in tried] or [labels["nothing"]]
    lines += ["", f"## {labels['advice']}", "", free_text(advice) or labels["nothing"]]
    return _write(runtime, subject, "failure", lines)


def deliver(runtime: Runtime, subject: str) -> Path:
    """从 store 与各步 handoff.json 汇总：结论(修了什么) → 必填事实 → 改动的文件 → 量化数据 → 各步骤。"""
    labels = _labels(runtime)
    handoffs = [item.handoff for item in recovery.checkpoints(runtime.workspace, subject)]
    latest = {handoff.point: handoff for handoff in handoffs}
    record = issues.get(runtime.conn, subject) if kind_of(subject) == "issue" else None
    accept = latest.get("release.accept")
    lines = [_title(runtime, subject, labels["deliver"]), "", free_text(_fixed(latest)) or labels["nothing"], ""]
    lines += [f"## {labels['facts']}", ""]
    lines += _table(labels, [
        (labels["branch"], _code(record.branch) if record and record.branch else "—"),
        (labels["pr"], f"#{record.pr}" if record and record.pr else "—"),
        (labels["merge"], _code(record.merge_commit) if record and record.merge_commit else "—"),
        (labels["deploy"], _code(record.deploy) if record and record.deploy else "—"),
        (labels["accept"], f"{accept.status.value}{labels['sep']}{_inline(accept.summary)}" if accept else "—"),
    ])
    changed = _changed_files(latest)
    lines += ["", f"## {labels['files']}", ""]
    if changed:
        lines += [f"| {labels['path']} | {labels['changed']} |", "|---|---|"]
        lines += [f"| {_code(path)} | +{added} -{deleted} |" for path, added, deleted in changed]
    else:
        lines.append(labels["nothing"])
    lines += ["", f"## {labels['metrics']}", "", *_table(labels, _totals(labels, handoffs))]
    lines += ["", f"## {labels['steps']}", "", (f"| {labels['point']} | {labels['round']} | {labels['status']} | "
                                                 f"{labels['duration']} | {labels['value']} |"), "|---|---|---|---|---|"]
    lines += [f"| {_code(item.point)} | {item.round or ''} | {item.status.value} | "
              f"{format_duration((item.metrics.duration_ms or 0) / 1000)} | {_inline(item.summary)} |"
              for item in handoffs]
    return _write(runtime, subject, "deliver", lines)


def free_text(text: str | None) -> str:
    """模型写的自由文字：标题整体下移到二级之下，没闭合的代码块补上闭合标记。"""
    if not text:
        return ""
    return close_fences(demote_headings(text.strip(), FREE_TEXT_LEVELS))


def close_fences(text: str) -> str:
    """CommonMark：闭合标记与开启标记同种字符、不短于它、后面不带信息串；文末仍开着的代码块补一个闭合标记。"""
    opening: str | None = None
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if match is None:
            continue
        marker, rest = match.group(1), match.group(2)
        if opening is None:
            opening = marker
        elif marker[0] == opening[0] and len(marker) >= len(opening) and not rest.strip():
            opening = None
    return text if opening is None else f"{text}\n{opening}"


# 内部函数


def _labels(runtime: Runtime) -> dict[str, str]:
    return LABELS.get(runtime.language, LABELS["zh"])


def _title(runtime: Runtime, subject: str, kind: str) -> str:
    return f"# {kind}{_labels(runtime)['sep']}{subject} {_inline(_subject_title(runtime, subject))}".rstrip()


def _subject_title(runtime: Runtime, subject: str) -> str:
    kind = kind_of(subject)
    if kind == "issue":
        record = issues.get(runtime.conn, subject)
        return record.title if record else ""
    if kind == "problem":
        found = problems.get(runtime.conn, subject)
        return found.title if found else ""
    return ""


def _table(labels: dict[str, str], rows: Sequence[tuple[str, str]]) -> list[str]:
    return [f"| {labels['item']} | {labels['value']} |", "|---|---|", *(f"| {key} | {value} |" for key, value in rows)]


def _inline(text: str) -> str:
    """单行位置(表格单元、列表项)：换行并成空格，竖线转义。"""
    return " ".join(text.split()).replace("|", "\\|")


def _code(text: str) -> str:
    return f"`{text}`" if "`" not in text else f"`` {text} ``"


def _fixed(latest: dict[str, Handoff]) -> str:
    design = latest.get("implement.design")
    if design is not None and design.facts.get("summary"):
        return str(design.facts["summary"])
    delivered = latest.get("implement.deliver")
    return delivered.summary if delivered else ""


def _changed_files(latest: dict[str, Handoff]) -> list[tuple[str, int, int]]:
    """交付的改动文件；没有交付记录时取最后一轮编码的。"""
    for point in ("implement.deliver", "implement.code"):
        found = latest.get(point)
        files = (found.facts.get("changedFiles") or []) if found else []
        if files:
            return [_change(item) for item in files]
    return []


def _change(item: Any) -> tuple[str, int, int]:
    if isinstance(item, str):
        return item, 0, 0
    return str(item.get("path")), int(item.get("added") or 0), int(item.get("deleted") or 0)


def _totals(labels: dict[str, str], handoffs: Sequence[Handoff]) -> list[tuple[str, str]]:
    tokens = Tokens()
    duration = calls = 0
    cost: float | None = None
    estimated = False
    rounds = max((item.round or 0 for item in handoffs), default=0)
    for item in handoffs:
        metrics = item.metrics
        duration += metrics.duration_ms or 0
        calls += metrics.calls or 0
        if metrics.tokens is not None:
            tokens.add(metrics.tokens)
        if metrics.cost_usd is not None:
            cost = (cost or 0.0) + metrics.cost_usd
            estimated = estimated or bool(metrics.cost_estimated)
    return [
        (labels["duration"], format_duration(duration / 1000)),
        (labels["calls"], str(calls)),
        (labels["tokens"], (f"{format_count(tokens.input)} / {format_count(tokens.output)} / "
                            f"{format_count(tokens.cache_read)}")),
        (labels["cost"], "—" if cost is None else f"${cost:.2f}{labels['estimated'] if estimated else ''}"),
        (labels["rounds"], str(rounds)),
    ]


def _write(runtime: Runtime, subject: str, content: str, lines: list[str]) -> Path:
    path = runtime.workspace.human_document(subject, content)
    write_markdown(path, "\n".join(lines))
    return path


__all__ = ["LABELS", "close_fences", "deliver", "failure", "free_text", "pending"]
