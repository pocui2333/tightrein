"""Issue：create、sync、list、show、edit、approve(组合 fix prepare)、close、reopen、reindex、rerender。

不带子命令且给出 --input 时等同 issue create(评测的沙箱以这种形式启动模块)。
issue create --manual 直接新建用户需求的 Issue(不关联问题，状态为待修)，之后照常 issue approve 申请建修复分支。
issues.tracker 为 github 时 issue sync 另输出 GitHub 镜像的对齐结果与未同步项。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes, selectors
from tightrein.cli.commands.common import issue_id, leaf, module_outcome, number
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import CloseReason, IssueStatus, Severity, TaskType
from tightrein.pipeline.issue.steps.github import MirrorReport
from tightrein.store.repos.issues import IssueRecord


def _record(record: IssueRecord) -> dict[str, Any]:
    issue = record.issue
    return {"issueId": issue.id, "title": issue.title, "status": issue.status.value, "origin": issue.origin.value,
            "severity": issue.severity.value if issue.severity else None, "problems": list(issue.problems),
            "branch": issue.branch, "path": record.path}


MANUAL_SEVERITY = Severity.P2
MANUAL_CONFLICTS = (("select", "--select"), ("input", "--input"), ("dry_run", "--dry-run"), ("output", "--output"))


def _manual_text(args: argparse.Namespace) -> str:
    if (args.body is None) == (args.body_file is None):
        raise UsageError("issue create --manual 需要 --body 或 --body-file 之一")
    if args.body is not None:
        return args.body
    path = Path(args.body_file)
    if not path.is_file():
        raise UsageError(f"--body-file 指向的文件不存在：{path}")
    return path.read_text(encoding="utf-8")


def _create_manual(invocation: Any) -> Outcome:
    args = invocation.args
    used = [flag for name, flag in MANUAL_CONFLICTS if getattr(args, name, None)]
    if used:
        raise UsageError(f"--manual 不能与 {'、'.join(used)} 同时使用")
    if not args.title:
        raise UsageError("issue create --manual 需要 --title")
    record = invocation.app.issue().create_manual(args.title, _manual_text(args), Severity(args.severity),
                                                  TaskType(args.type) if args.type else None)
    subject = record.issue.id
    lines = [f"已创建用户需求 Issue {number(subject)}(状态「{record.issue.status.label}」)：{record.path}",
             f"下一步：tightrein issue approve {number(subject)}(申请建修复分支)"]
    return Outcome("issue create", exit_codes.OK, lines, {"type": "issue", "id": subject}, _record(record))


def _create(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    if getattr(args, "manual", False):
        return _create_manual(invocation)
    for name, flag in (("title", "--title"), ("body", "--body"), ("body_file", "--body-file")):
        if getattr(args, name, None) is not None:
            raise UsageError(f"{flag} 只用于 issue create --manual")
    chosen = selectors.problem_ids(app.conn, args.select) if args.select else ()
    run = app.issue().create(chosen, args.input, args.dry_run)
    if run.plan is not None:
        return Outcome("issue create", exit_codes.OK, [f"- {pid}：{action}" for pid, action in run.plan],
                       result=[{"problemId": pid, "action": action} for pid, action in run.plan])
    lines = [run.summary or ("没有需要创建 Issue 的问题" if not run.items else "")]
    lines += [f"- {item.problem_id}：{item.status.label} {item.action or ''} {item.issue_id or ''}".rstrip()
              + (f"({item.reason})" if item.reason else "") for item in run.items]
    lines += [f"- {pid} 跳过：{reason}" for pid, reason in run.skipped]
    items = [{"problemId": item.problem_id, "status": item.status.value, "issueId": item.issue_id,
              "action": item.action, "path": app.layout.relative(item.path) if item.path else None,
              "reason": item.reason} for item in run.items]
    code = run.exit_code
    if code == exit_codes.OK and not run.items and run.skipped:
        code = exit_codes.PRECONDITION
    return Outcome("issue create", code, lines, result={"items": items, "skipped": [
        {"problemId": pid, "reason": reason} for pid, reason in run.skipped]})


def _bare(invocation: Any) -> Outcome:
    if invocation.args.input is None:
        raise UsageError("issue 需要子命令；不带子命令时须给出 --input(等同 issue create)")
    return _create(invocation)


def _mirror_lines(report: MirrorReport | None) -> list[str]:
    if report is None or report.repo is None and report.skipped is None:
        return []
    if report.skipped is not None:
        return [f"GitHub 镜像未同步：{report.skipped}", *(f"- {line}" for line in report.unsynced)]
    lines = [f"GitHub 镜像 {report.repo}：写入 {len(report.written)} 个 Issue"]
    lines += [f"- 在 GitHub 上关闭，本地已按用户关闭处理：{'、'.join(report.closed)}"] if report.closed else []
    lines += [f"- 在 GitHub 上重新打开，本地已重新打开：{'、'.join(report.reopened)}"] if report.reopened else []
    lines += [f"- 待确认：tightrein confirm {operation}" for operation in report.operations]
    lines += [f"- 未同步：{line}" for line in report.unsynced]
    return lines


def _sync(invocation: Any) -> Outcome:
    report = invocation.app.issue().sync()
    lines = [report.invalid or "已同步 Issue 文件与数据库", *_mirror_lines(report.github)]
    return Outcome("issue sync", exit_codes.FAILED if report.invalid else exit_codes.OK, lines, result=asdict(report))


def _list(invocation: Any) -> Outcome:
    args = invocation.args
    found = invocation.app.issue().list(IssueStatus(args.status) if args.status else None,
                                        Severity(args.severity) if args.severity else None)
    lines = [f"{len(found)} 个 Issue", *(f"- {number(record.issue.id)} [{record.issue.status.label}] "
                                        f"{record.issue.title}" for record in found)]
    return Outcome("issue list", exit_codes.OK, lines, result=[_record(record) for record in found])


def _show(invocation: Any) -> Outcome:
    view = invocation.app.issue().show(issue_id(invocation.args.issue))
    values = {**_record(view.record), "body": view.body,
              "problemTitles": {problem.id: problem.title for problem in view.problems}}
    return Outcome("issue show", exit_codes.OK, [view.record.issue.title, view.body],
                   {"type": "issue", "id": view.record.issue.id}, values)


def _edit(invocation: Any) -> Outcome:
    if not invocation.interactive:
        raise UsageError("issue edit 只能在交互终端中由用户执行")
    stdin, stdout = invocation.stdin, invocation.stdout

    def retry(problems: list[str]) -> bool:
        stdout.write("\n".join(problems) + "\n输入 y 重新编辑，其他任意输入放弃这次修改：")
        stdout.flush()
        return stdin.readline().strip().lower() == "y"

    subject = issue_id(invocation.args.issue)
    result = invocation.app.issue().edit(subject, invocation.externals.edit_file, retry)
    line = "已保存" if result.saved else "已放弃这次修改"
    return Outcome("issue edit", exit_codes.OK, [line], {"type": "issue", "id": subject},
                   {"saved": result.saved, "sections": list(result.sections)})


def _approve(invocation: Any) -> Outcome:
    """放行后调用 fix prepare 申请建分支与 worktree(architecture/06 9.2)。"""
    app = invocation.app
    subject = issue_id(invocation.args.issue)
    record = app.issue().approve(subject, invocation.args.note)
    prepared = app.fix().prepare(subject)
    outcome = module_outcome("issue approve", app, subject, prepared,
                             f"tightrein fix start {number(subject)}", {"issueStatus": record.issue.status.value})
    outcome.lines.insert(0, f"Issue {number(subject)} 已放行，状态为「{record.issue.status.label}」")
    return outcome


def _close(invocation: Any) -> Outcome:
    args = invocation.args
    duplicate = issue_id(args.duplicate_of) if args.duplicate_of else None
    record = invocation.app.issue().close(issue_id(args.issue), CloseReason(args.reason), note=args.note,
                                          duplicate_of=duplicate)
    return Outcome("issue close", exit_codes.OK, [f"Issue {number(record.issue.id)} 已关闭"],
                   {"type": "issue", "id": record.issue.id}, _record(record))


def _reopen(invocation: Any) -> Outcome:
    record = invocation.app.issue().reopen(issue_id(invocation.args.issue), invocation.args.note)
    return Outcome("issue reopen", exit_codes.OK, [f"Issue {number(record.issue.id)} 已重新打开"],
                   {"type": "issue", "id": record.issue.id}, _record(record))


def _reindex(invocation: Any) -> Outcome:
    report = invocation.app.issue().reindex()
    return Outcome("issue reindex", exit_codes.OK, [f"更新 {len(report.updated)} 个，移除 {len(report.removed)} 个"],
                   result=asdict(report))


def _rerender(invocation: Any) -> Outcome:
    """用现有数据按当前模板重写 Issue 正文与标题并更新镜像；不调用模型。"""
    result = invocation.app.issue().rerender([issue_id(item) for item in invocation.args.issues])
    lines = []
    for item in result.items:
        if not item.rewritten:
            lines.append(f"- {number(item.issue_id)}：用户需求，本地正文不变")
        elif item.missing:
            lines.append(f"- {number(item.issue_id)}：已重写；缺少 {'、'.join(item.missing)}，显示为「—」")
        else:
            lines.append(f"- {number(item.issue_id)}：已重写")
    if any(item.missing for item in result.items):
        lines.append("要得到完整格式需重新分诊：tightrein retriage <问题编号>，再 tightrein issue rerender <编号>")
    return Outcome("issue rerender", exit_codes.OK, [f"{len(result.items)} 个 Issue", *lines,
                                                     *_mirror_lines(result.mirror)], result=asdict(result))


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    issue = commands.add_parser("issue", parents=[common], help="本地 Issue")
    issue.set_defaults(handler=_bare, command_name="issue")
    sub = issue.add_subparsers(dest="issue_command", parser_class=type(issue))
    create = leaf(sub, common, "create", _create, "为去向是提 Issue 的问题创建 Issue；--manual 直接新建用户需求",
                  "issue create")
    create.add_argument("--manual", action="store_true", help="新建用户需求的 Issue：不关联问题，免审阅")
    create.add_argument("--title", help="用户需求的标题(与 --manual 一起使用)")
    create.add_argument("--body", help="用户需求的正文")
    create.add_argument("--body-file", help="从文件读取用户需求的正文(UTF-8)")
    create.add_argument("--severity", choices=[item.value for item in Severity], default=MANUAL_SEVERITY.value,
                        help="用户需求的严重度，缺省 P2")
    create.add_argument("--type", choices=[item.value for item in TaskType],
                        help="用户需求的任务类型，决定修复通道；缺省取 fix.manualTaskType")
    leaf(sub, common, "sync", _sync, "同步 Issue 文件与数据库", "issue sync")
    listing = leaf(sub, common, "list", _list, "列出 Issue", "issue list")
    listing.add_argument("--status", choices=[item.value for item in IssueStatus])
    listing.add_argument("--severity", choices=[item.value for item in Severity])
    for name, handler, text in (("show", _show, "查看 Issue"), ("edit", _edit, "在编辑器中修改 Issue"),
                                ("reindex", _reindex, "重建 Issue 索引")):
        parser = leaf(sub, common, name, handler, text, f"issue {name}")
        if name != "reindex":
            parser.add_argument("issue", help="Issue 编号")
    approve = leaf(sub, common, "approve", _approve, "放行 Issue 并申请建修复分支", "issue approve")
    approve.add_argument("issue")
    approve.add_argument("--note")
    close = leaf(sub, common, "close", _close, "关闭 Issue", "issue close")
    close.add_argument("issue")
    close.add_argument("--reason", required=True, choices=[item.value for item in CloseReason])
    close.add_argument("--note")
    close.add_argument("--duplicate-of")
    rerender = leaf(sub, common, "rerender", _rerender,
                    "按当前模板重写 Issue 正文与标题并更新 GitHub 镜像(覆盖除关联、历史外的本地编辑)", "issue rerender")
    rerender.add_argument("issues", nargs="*", help="Issue 编号；省略时为全部未关闭的 Issue")
    reopen = leaf(sub, common, "reopen", _reopen, "重新打开 Issue", "issue reopen")
    reopen.add_argument("issue")
    reopen.add_argument("--note")
