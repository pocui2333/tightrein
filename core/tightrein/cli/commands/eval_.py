"""eval 的命令(architecture/03 2.7)：run、resume、report、verify、add、seal。

改进建议的评测由 learn improve 发起，这里不提供 --proposal；评分器自检(eval verify --scorers)在 evaluation 中还没有实现，
不提供该参数。report 显示已生成的报告，不重新统计。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import Stage
from tightrein.evaluation import cases, manifest
from tightrein.evaluation import service as evaluation
from tightrein.evaluation.report import EvaluationReport
from tightrein.evaluation.sandbox import SubprocessModuleRunner
from tightrein.evaluation.service import Dependencies, EvaluationSettings
from tightrein.evaluation.variants import CANDIDATE, HEAD, MIN_REPEATS, VersionSpec, tool_model_plan, version_plan
from tightrein.observability.tracing import Tracer

MODULES = [stage.value for stage in evaluation.SUBJECT_TYPES]


def _split(text: str | None) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()] if text else []


def dependencies(app: Any) -> Dependencies:
    runner = SubprocessModuleRunner(dict(app.environ), process=app.extension_runner(),
                                    timeout_seconds=float(app.config.get("runtime.evaluation.sandboxTimeoutSeconds")))
    tracer = Tracer(app.events, app.clock, run_id=None, stage=Stage.IMPROVE.value)
    return Dependencies(app.layout, app.tool.root, EvaluationSettings.from_config(app.config), runner,
                        app.runner_for, app.process, app.clock, tracer)


def _report(name: str, app: Any, report: EvaluationReport) -> Outcome:
    path = app.layout.eval_report_md(report.evaluation_id)
    values = report.to_dict()
    return Outcome(name, exit_codes.OK, [f"评测 {report.evaluation_id}：{values.get('verdict') or '完成'}",
                                         f"报告：{app.layout.relative(path)}"],
                   {"type": "eval", "id": report.evaluation_id}, values)


def _run(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    module = Stage(args.module)
    runners: Sequence[str] = _split(args.runner) or [app.config.model_choice(module).tool]
    models: Sequence[str | None] = _split(args.model) or [None]
    repeats = args.repeats or MIN_REPEATS
    chosen = _split(args.cases)
    if len(runners) > 1 or len(models) > 1:
        plan = tool_model_plan(module, runners, models, case_ids=chosen, repeats=repeats)
    else:
        candidate = VersionSpec(CANDIDATE, args.version or HEAD, use_worktree=args.worktree)
        plan = version_plan(module, candidate, runners[0], models[0], chosen, repeats)
    return _report("eval run", app, evaluation.evaluate(plan, dependencies(app)))


def _resume(invocation: Any) -> Outcome:
    app = invocation.app
    return _report("eval resume", app, evaluation.resume(invocation.args.evaluation, dependencies(app)))


def _show(invocation: Any) -> Outcome:
    app = invocation.app
    path = app.layout.eval_report_md(invocation.args.evaluation)
    if not path.is_file():
        raise UsageError(f"评测 {invocation.args.evaluation} 还没有报告")
    return Outcome("eval report", exit_codes.OK, [path.read_text(encoding="utf-8")],
                   result={"report": app.layout.relative(path)})


def _verify(invocation: Any) -> Outcome:
    app = invocation.app
    module = Stage(invocation.args.module) if invocation.args.module else None
    verified = cases.verify_cases(app.layout, app.process, module)
    line = f"用例校验通过：模块用例 {len(verified.module_cases)} 个，检索用例 {len(verified.retrieval_cases)} 个"
    return Outcome("eval verify", exit_codes.OK, [line], result={"manifestSha256": verified.manifest_sha256,
                                                                   "moduleCases": len(verified.module_cases)})


def _add(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    if args.commit is None:
        raise UsageError("eval add 需要 --commit")
    path = cases.add_case(app.layout, Stage(args.module), args.source, args.commit,
                          interactive=invocation.interactive, environ=dict(app.environ))
    return Outcome("eval add", exit_codes.OK, [f"已建立用例 {app.layout.relative(path)}，填写 case.json 后执行 eval seal"],
                   result={"case": app.layout.relative(path)})


def _seal(invocation: Any) -> Outcome:
    app = invocation.app
    stdin, stdout = invocation.stdin, invocation.stdout

    def confirm(changes: Sequence[Any]) -> bool:
        stdout.write("\n".join(str(item) for item in changes) + "\n输入 yes 重新封存：")
        stdout.flush()
        return stdin.readline().strip() == "yes"

    tracer = Tracer(app.events, app.clock, run_id=None, stage=Stage.IMPROVE.value)
    sha = manifest.seal(app.layout, interactive=invocation.interactive, environ=dict(app.environ),
                        confirm=confirm, tracer=tracer)
    return Outcome("eval seal", exit_codes.OK, [f"manifest 已封存：{sha}"], result={"manifestSha256": sha})


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    group_ = group(commands, "eval", "模块评测")
    run = leaf(group_, common, "run", _run, "按参数组成计划并运行评测(--runner、--model 可用逗号给出多个)")
    run.add_argument("--module", required=True, choices=MODULES)
    run.add_argument("--cases", help="用例编号，逗号分隔")
    run.add_argument("--version", help="候选版本的 commit，缺省为 HEAD")
    run.add_argument("--worktree", action="store_true", help="候选版本为当前工作区(含未提交改动)")
    run.add_argument("--repeats", type=int)
    for name, handler, text in (("resume", _resume, "续跑"), ("report", _show, "显示报告")):
        parser = leaf(group_, common, name, handler, text)
        parser.add_argument("evaluation", help="评测编号")
    verify = leaf(group_, common, "verify", _verify, "校验用例")
    verify.add_argument("--module", choices=MODULES)
    add = leaf(group_, common, "add", _add, "由用户在终端中新增用例(--commit 为用例的 commit)")
    add.add_argument("--module", required=True, choices=MODULES)
    add.add_argument("--from", dest="source", required=True, type=Path, help="交接文档")
    leaf(group_, common, "seal", _seal, "用户确认后重算 manifest")
