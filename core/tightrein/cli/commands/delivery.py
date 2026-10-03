"""修复、验证与发布：fix *、verify *、release *(architecture/07)。

- fix start 启动所配置工具的交互会话；非交互调用(--json 或标准输入不是终端)不嵌套启动，停在关口 interactive-fix，
  给出两种方式：在终端执行 fix start，或在当前会话中执行 fix start --here 后按 fix skill 工作；
- 不带子命令的 fix 只用于 --output(评测的沙箱)：读 --input 交接文档的 Issue 编号，依次执行 plan 与 apply；
- release 的第一个位置参数是 Issue 编号(提交、推送、提 PR 的整体入口)或子命令名；release revert <编号> --reason
  提撤销该合并的 PR，交用户决定。
"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, issue_id, leaf, module_outcome, number
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import HandoffStatus
from tightrein.orchestrator import resume
from tightrein.store.files import handoff_files

RELEASE_ACTIONS = ("commit", "sync", "push", "pr", "track", "summary", "comment", "revert")


def _fix_action(name: str, call: Any) -> Any:
    def handler(invocation: Any) -> Outcome:
        subject = issue_id(invocation.args.issue)
        return module_outcome(f"fix {name}", invocation.app, subject, call(invocation, invocation.app.fix(), subject))

    return handler


def _start(invocation: Any) -> Outcome:
    args = invocation.args
    subject = issue_id(args.issue)
    if not args.here and not invocation.interactive:
        message = "修复需要交互会话，由用户选择方式"
        commands = [f"tightrein fix start {number(subject)}", f"tightrein fix start {number(subject)} --here"]
        return Outcome("fix start", exit_codes.GATE, [message, *(f"- {command}" for command in commands)],
                       {"type": "issue", "id": subject}, {"choices": commands},
                       {"subject": {"type": "issue", "id": subject}, "gate": resume.INTERACTIVE_FIX}, commands[0])
    result = invocation.app.fix().start(subject, here=args.here, force=args.force)
    return module_outcome("fix start", invocation.app, subject, result)


def _bare_fix(invocation: Any) -> Outcome:
    """--output 下对输入交接文档的 Issue 依次执行 plan 与 apply(评测的沙箱入口)。"""
    args = invocation.args
    if args.output is None or args.input is None:
        raise UsageError("fix 需要子命令；不带子命令时只用于 --output 与 --input")
    subject = handoff_files.read(args.input)["subject"]["id"]
    fix = invocation.app.fix()
    planned = fix.plan(subject)
    if planned.status is not HandoffStatus.OK:
        return module_outcome("fix", invocation.app, subject, planned)
    return module_outcome("fix", invocation.app, subject, fix.apply(subject))


def _verify_action(name: str, call: Any) -> Any:
    def handler(invocation: Any) -> Outcome:
        subject = issue_id(invocation.args.issue)
        result = call(invocation, invocation.app.verify(), subject)
        report = invocation.app.layout.relative(result.report) if result.report else None
        return module_outcome(f"verify {name}", invocation.app, subject, result,
                              extra={"conclusion": result.conclusion, "report": report})

    return handler


def _screenshots(invocation: Any, verify: Any, subject: str) -> Any:
    args = invocation.args
    if args.ok == bool(args.issue_note):
        raise UsageError("verify screenshots 需要 --ok 或 --issue <说明> 之一")
    return verify.screenshots(subject, ok=args.ok, note=args.issue_note)


def _release(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    release = app.release()
    if args.action.isdigit():
        subject = issue_id(args.action)
        return module_outcome("release", app, subject, release.release(subject, accept_findings=args.accept_findings))
    if args.action not in RELEASE_ACTIONS:
        raise UsageError(f"release 的第一个参数是 Issue 编号或 {'、'.join(RELEASE_ACTIONS)}：{args.action}")
    name = f"release {args.action}"
    if args.action == "track":
        report = release.track()
        lines = [report.summary, *report.lines, *(f"- {subject} 跳过：{reason}" for subject, reason in report.skipped)]
        return Outcome(name, exit_codes.OK, lines, result={"lines": report.lines, "skipped": [
            {"issueId": subject, "reason": reason} for subject, reason in report.skipped], "summary": report.summary})
    if args.issue is None:
        raise UsageError(f"{name} 需要 Issue 编号")
    subject = issue_id(args.issue)
    if args.action == "revert" and not args.reason:
        raise UsageError("release revert 需要 --reason <回归现象>")
    calls = {
        "commit": lambda: release.commit(subject, accept_findings=args.accept_findings),
        "sync": lambda: release.sync(subject, cont=args.cont, abort=args.abort),
        "push": lambda: release.push(subject),
        "pr": lambda: release.pr(subject),
        "summary": lambda: release.summary(subject),
        "comment": lambda: release.comment(subject),
        "revert": lambda: release.revert(subject, args.reason),
    }
    result = calls[args.action]()
    return module_outcome(name, app, subject, result,
                          extra={"path": app.layout.relative(result.path) if result.path else None})


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    fix = commands.add_parser("fix", parents=[common], help="修复")
    fix.set_defaults(handler=_bare_fix, command_name="fix")
    sub = fix.add_subparsers(dest="fix_command", parser_class=type(fix))
    prepare = leaf(sub, common, "prepare", _fix_action("prepare", lambda i, f, s: f.prepare(s)),
                   "申请建修复分支与 worktree", "fix prepare")
    start = leaf(sub, common, "start", _start, "进入修复会话", "fix start")
    start.add_argument("--here", action="store_true", help="在当前会话中按 fix skill 工作，只做状态转换")
    start.add_argument("--force", action="store_true", help="带转人工标记时确认继续")
    plan = leaf(sub, common, "plan", _fix_action(
        "plan", lambda i, f, s: f.plan(s, i.args.note, accept_design=i.args.accept_design)), "出修复计划", "fix plan")
    plan.add_argument("--note")
    plan.add_argument("--accept-design", action="store_true",
                      help="用户已同意按设计层面的根因修复，设计问题不再中止出计划")
    confirm = leaf(sub, common, "confirm", _fix_action(
        "confirm", lambda i, f, s: f.confirm(s, reject=i.args.reject, note=i.args.note)), "确认或退回修复计划",
        "fix confirm")
    confirm.add_argument("--reject", action="store_true")
    confirm.add_argument("--note")
    apply = leaf(sub, common, "apply", _fix_action("apply", lambda i, f, s: f.apply(s, review_only=i.args.review_only)),
                 "实施修复与检查", "fix apply")
    apply.add_argument("--review-only", action="store_true")
    done = leaf(sub, common, "done", _fix_action("done", lambda i, f, s: f.done(s)), "修复完成，交给验证", "fix done")
    abandon = leaf(sub, common, "abandon", _fix_action("abandon", lambda i, f, s: f.abandon(s, i.args.reason)),
                   "放弃修复", "fix abandon")
    abandon.add_argument("--reason", required=True)
    cleanup = leaf(sub, common, "cleanup", _fix_action("cleanup", lambda i, f, s: i.app.release().cleanup(s)),
                   "清理修复分支与 worktree", "fix cleanup")
    for parser in (prepare, start, plan, confirm, apply, done, abandon, cleanup):
        parser.add_argument("issue", help="Issue 编号")

    verify = group(commands, "verify", "验证")
    parsers = [
        leaf(verify, common, "local", _verify_action(
            "local", lambda i, v, s: v.local(s, confirm_migration=i.args.confirm_migration)), "PR 阶段的本机检查",
            "verify local"),
        leaf(verify, common, "staging", _verify_action("staging", lambda i, v, s: v.staging(s)), "部署后确认",
             "verify staging"),
        leaf(verify, common, "screenshots", _verify_action("screenshots", _screenshots), "截图查看的结论",
             "verify screenshots"),
    ]
    for parser in parsers:
        parser.add_argument("issue", help="Issue 编号")
    parsers[0].add_argument("--confirm-migration", action="store_true")
    parsers[2].add_argument("--ok", action="store_true")
    parsers[2].add_argument("--issue", dest="issue_note", help="截图有问题时的说明")

    release = leaf(commands, common, "release", _release, "提交、推送、提 PR 与跟踪")
    release.add_argument("action", help=f"Issue 编号，或 {'、'.join(RELEASE_ACTIONS)}")
    release.add_argument("issue", nargs="?", help="子命令的 Issue 编号")
    release.add_argument("--accept-findings", action="store_true")
    release.add_argument("--continue", dest="cont", action="store_true")
    release.add_argument("--abort", action="store_true")
    release.add_argument("--reason", help="release revert：撤销的原因(回归现象)")
