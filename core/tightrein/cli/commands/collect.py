"""采集、聚合与问题的人工操作：collect、collect deployments、aggregate、ignore、false-positive、merge、reopen。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.assemble import parse_now
from tightrein.cli.commands.common import leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import ProbeLevel
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.problem import Problem
from tightrein.pipeline.aggregate.service import LIVE, SKIP, AggregateRequest
from tightrein.pipeline.collect.service import CollectRequest
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED

DEPLOYMENTS = "deployments"


def _collect(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    if args.action == DEPLOYMENTS:
        found = app.collect().deployments()
        line = "没有新的部署" if found is None else f"部署 {found.commit[:12]}：{found.status.label}"
        if not app.deploys().configured():
            line = DEPLOY_UNCONFIGURED
        return Outcome("collect deployments", exit_codes.OK, [line],
                       result=None if found is None else asdict(found))
    if args.probe is None:
        raise UsageError("collect 需要 --probe")
    request = CollectRequest(ProbeKind(args.probe), ProbeLevel(args.level) if args.level else None,
                             tuple(args.select), args.target, args.commit, args.reparse,
                             Path(args.import_archive) if args.import_archive else None, dry_run=args.dry_run)
    result = app.collect().run(request)
    if result.plan is not None:
        return Outcome("collect", exit_codes.OK, [f"将运行 {args.probe}"], result=asdict(result.plan))
    run = result.run
    lines = [f"{run.id}：{run.status.label}"]
    values = {"runId": run.id, "status": run.status.value,
              "handoff": app.layout.relative(result.handoff) if result.handoff else None}
    return Outcome("collect", exit_codes.for_status(result.status), lines, {"type": "run", "id": run.id}, values)


def _aggregate(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    request = AggregateRequest(tuple(args.select), args.input, args.reproduce, args.rebuild, args.no_wait,
                               args.dry_run)
    result = app.aggregate().run(request)
    if result.plan is not None:
        return Outcome("aggregate", exit_codes.OK, [f"将处理 {len(result.plan.runs)} 个运行"],
                       result=asdict(result.plan))
    if result.run is None:
        return Outcome("aggregate", exit_codes.OK, [result.message or "没有新信号"], result={"message": result.message})
    values = {"runId": result.run.id, "status": result.status.value if result.status else None,
              "handoffs": [app.layout.relative(path) for path in result.handoffs]}
    return Outcome("aggregate", result.exit_code, [f"{result.run.id}：{result.run.status.label}"],
                   {"type": "run", "id": result.run.id}, values)


def _problem(name: str, problem: Problem) -> Outcome:
    return Outcome(name, exit_codes.OK, [f"{problem.id} 现在为「{problem.status.label}」"],
                   {"type": "problem", "id": problem.id}, {"status": problem.status.value})


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError as failure:
        raise UsageError(f"日期须为 YYYY-MM-DD：{text}") from failure


def _ignore(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    until = parse_now(args.until, app.zone) if args.until else None
    problem = app.problems().ignore(args.problem, args.reason, until=until, occurrences=args.occurrences,
                                    new_release=args.new_release)
    return _problem("ignore", problem)


def _false_positive(invocation: Any) -> Outcome:
    args = invocation.args
    return _problem("false-positive", invocation.app.problems().false_positive(args.problem, args.reason,
                                                                             expires=_date(args.expires)))


def _merge(invocation: Any) -> Outcome:
    args = invocation.args
    return _problem("merge", invocation.app.problems().merge(args.target, args.source))


def _reopen(invocation: Any) -> Outcome:
    args = invocation.args
    return _problem("reopen", invocation.app.problems().reopen(args.problem, args.reason))


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    collect = leaf(commands, common, "collect", _collect, "运行一种采集方法，或只做部署检测")
    collect.add_argument("action", nargs="?", choices=[DEPLOYMENTS], help="deployments：只做部署检测")
    collect.add_argument("--probe", choices=[probe.value for probe in ProbeKind])
    collect.add_argument("--level", choices=[level.value for level in ProbeLevel])
    collect.add_argument("--reparse", help="重新解析这次运行的原始输出")
    collect.add_argument("--import-archive", help="一次性导入迁移归档目录(incidental)")
    aggregate = leaf(commands, common, "aggregate", _aggregate, "把采集的信号归并成问题")
    aggregate.add_argument("--rebuild", action="store_true", help="按当前规则整体重放")
    aggregate.add_argument("--reproduce", choices=[LIVE, SKIP], help="复现确认的方式")
    aggregate.add_argument("--no-wait", action="store_true", help="全局锁被占用时立即退出")
    ignore = leaf(commands, common, "ignore", _ignore, "忽略一个问题")
    ignore.add_argument("problem")
    ignore.add_argument("--reason", required=True)
    ignore.add_argument("--until", help="到这个时间恢复(日期或带时区的时间)")
    ignore.add_argument("--occurrences", type=int, help="再出现这么多次时恢复")
    ignore.add_argument("--new-release", action="store_true", help="新版本上再出现时恢复")
    false = leaf(commands, common, "false-positive", _false_positive, "把问题判为误报并生成抑制规则")
    false.add_argument("problem")
    false.add_argument("--reason", required=True)
    false.add_argument("--expires", help="抑制规则的到期日期 YYYY-MM-DD")
    merge = leaf(commands, common, "merge", _merge, "把问题 B 并入问题 A")
    merge.add_argument("target", help="问题 A(保留)")
    merge.add_argument("source", help="问题 B(并入 A)")
    reopen = leaf(commands, common, "reopen", _reopen, "重新打开问题")
    reopen.add_argument("problem")
    reopen.add_argument("--reason")
