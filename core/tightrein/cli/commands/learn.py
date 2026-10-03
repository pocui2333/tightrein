"""learn 的命令(architecture/08 第 3 节)：report、metrics、health、lessons、curate、improve、suggestions、accept、reject。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.assemble import parse_now
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import Stage, SuggestionKind, SuggestionStatus
from tightrein.pipeline.learn.service import LearnResult
from tightrein.pipeline.learn.steps.suggestions import SuggestionError


def _result(name: str, app: Any, result: LearnResult, line: str) -> Outcome:
    values = {"runId": result.run_id, "handoff": app.layout.relative(result.handoff), "outputs": result.outputs,
              "report": str(result.report) if result.report else None,
              "notification": result.notification.status if result.notification else None}
    return Outcome(name, exit_codes.OK, [line], {"type": "run", "id": result.run_id}, values)


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError as failure:
        raise UsageError(f"日期须为 YYYY-MM-DD：{text}") from failure


def _moment(app: Any, text: str | None) -> datetime | None:
    return parse_now(text, app.zone) if text else None


def _report(invocation: Any) -> Outcome:
    app = invocation.app
    result = app.learn().report(_day(invocation.args.week))
    return _result("learn report", app, result, f"周报：{result.report}")


def _metrics(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    values, errors = app.learn().metrics(_moment(app, args.since), _moment(app, args.until),
                                         Stage(args.stage) if args.stage else None)
    lines = [f"{item.metric}[{item.dimension}] = {item.value}" for item in values]
    lines += [f"计算失败：{item}" for item in errors]
    return Outcome("learn metrics", exit_codes.OK, lines or ["没有指标"],
                   result={"metrics": [item.to_dict() for item in values], "errors": errors})


def _health(invocation: Any) -> Outcome:
    app = invocation.app
    result = app.learn().health()
    failed = [item for item in result.outputs["health"] if not item["passed"]]
    line = f"{len(failed)} 项检查未通过" if failed else "链路健康检查全部通过"
    outcome = _result("learn health", app, result, line)
    outcome.lines += [f"- {item['check']}：{item['detail']}" for item in failed]
    return outcome


def _lessons(invocation: Any) -> Outcome:
    app = invocation.app
    result = app.learn().lessons()
    return _result("learn lessons", app, result, f"写入经验 {len(result.outputs['lessons'])} 条")


def _curate(invocation: Any) -> Outcome:
    app = invocation.app
    result = app.learn().curate()
    return _result("learn curate", app, result, f"生成复核建议 {len(result.outputs['suggestions'])} 条")


def _improve(invocation: Any) -> Outcome:
    app = invocation.app
    result = app.improve().suggest(invocation.args.days)
    values = {"runId": result.run_id, "handoff": app.layout.relative(result.handoff), "outputs": result.outputs}
    reason = result.outputs.get("reason")
    line = f"改进建议 {result.suggestion_id}：{reason}" if result.suggestion_id else f"没有生成改进建议：{reason}"
    return Outcome("learn improve", exit_codes.OK, [line], {"type": "run", "id": result.run_id}, values)


def _suggestions(invocation: Any) -> Outcome:
    args = invocation.args
    found = invocation.app.learn().suggestions(SuggestionStatus(args.status) if args.status else None,
                                               SuggestionKind(args.kind) if args.kind else None)
    lines = [f"{len(found)} 条建议", *(f"- {item.id} [{item.kind.value}] {item.subject}：{item.status.label}"
                                      for item in found)]
    return Outcome("learn suggestions", exit_codes.OK, lines, result=[asdict(item) for item in found])


def _accept(invocation: Any) -> Outcome:
    try:
        record = invocation.app.learn().accept(invocation.args.suggestion, invocation.args.action)
    except SuggestionError as failure:
        raise UsageError(str(failure)) from failure
    lines = [f"{record.id} 已接受(只记录决定，本工具不修改配置、提示或代码)"]
    if record.target_path:
        lines.append(f"按决定文档 {record.target_path} 的「下一步」自己修改")
    return Outcome("learn accept", exit_codes.OK, lines, {"type": "suggestion", "id": record.id}, asdict(record))


def _reject(invocation: Any) -> Outcome:
    try:
        record = invocation.app.learn().reject(invocation.args.suggestion, invocation.args.reason)
    except SuggestionError as failure:
        raise UsageError(str(failure)) from failure
    return Outcome("learn reject", exit_codes.OK, [f"{record.id} 已拒绝"], {"type": "suggestion", "id": record.id},
                   asdict(record))


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    learn = group(commands, "learn", "指标、周报、学习建议与链路健康")
    report = leaf(learn, common, "report", _report, "生成周报", "learn report")
    report.add_argument("--week", help="该周内的任一日期 YYYY-MM-DD，缺省为本周")
    metrics = leaf(learn, common, "metrics", _metrics, "计算指标，不写快照", "learn metrics")
    metrics.add_argument("--since", help="起始(日期或带时区的时间)，缺省为本周一")
    metrics.add_argument("--until", help="结束(日期或带时区的时间)，缺省为下周一")
    metrics.add_argument("--stage", choices=[stage.value for stage in Stage])
    leaf(learn, common, "health", _health, "链路健康检查", "learn health")
    leaf(learn, common, "lessons", _lessons, "出问题时写经验，修好的缺陷生成并验证 Semgrep 规则", "learn lessons")
    leaf(learn, common, "curate", _curate, "经验清理与知识库定期复核", "learn curate")
    improve = leaf(learn, common, "improve", _improve, "从近期失败归纳改进建议并评测(只建议，不修改)", "learn improve")
    improve.add_argument("--days", type=int, help="归纳最近多少天出问题的来源，缺省取 learn.improve.lookbackDays")
    suggestions = leaf(learn, common, "suggestions", _suggestions, "列出学习建议", "learn suggestions")
    suggestions.add_argument("--status", choices=[item.value for item in SuggestionStatus])
    suggestions.add_argument("--kind", choices=[item.value for item in SuggestionKind])
    accept = leaf(learn, common, "accept", _accept, "接受一条学习建议", "learn accept")
    accept.add_argument("suggestion", help="建议编号 LS-0001")
    accept.add_argument("--action", help="knowledge-review 的处理：renew、merge、archive")
    reject = leaf(learn, common, "reject", _reject, "拒绝一条学习建议", "learn reject")
    reject.add_argument("suggestion", help="建议编号 LS-0001")
    reject.add_argument("--reason", required=True)
