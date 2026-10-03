"""workspace init|check|answer(redesign/10-onboarding.md 第 4 节)：新建或继续接入、生成一次清单检查、回答接入问题。

- init：--workspace 指向的目录不存在或没有 project.yaml 时按 --repo(交互时可询问)新建最小的 project.yaml，工作区进入
  接入中；之后检查清单，在终端中逐项提问(回车采用推荐，输入 skip 跳过，其他输入作为值)；非交互时只检查并列出问题。
  已在运行中的工作区只做一次检查，不改阶段。
- check：对任何工作区生成一次清单检查并写 onboarding.md；运行中的工作区不改阶段(已有工作区迁移后视为运行中)。
- answer：回答一项(--recommended、--skip 或 --value)，loop skill 用它把用户的回答写回。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.config.edit import EditRejected
from tightrein.orchestrator.onboarding import service as onboarding
from tightrein.orchestrator.onboarding.service import CheckReport
from tightrein.store.files import atomic

SKIP_WORD = "skip"
DEFAULT_BRANCH = "main"
DEFAULT_LANGUAGE = "zh"


def _ask(invocation: Any, text: str) -> str:
    invocation.stdout.write(text)
    invocation.stdout.flush()
    return invocation.stdin.readline().strip()


def _report_lines(report: CheckReport) -> list[str]:
    labels = {"done": "完成", "blocked": "需要回答", "failed": "失败"}
    return [report.summary, *(f"- [{labels[item.state]}] {item.item} {item.title}：{item.detail}"
                              + (f"；推荐：{item.recommendation}" if item.state == "blocked" and item.recommendation
                                 else "") for item in report.items)]


def _result(report: CheckReport, app: Any) -> dict[str, Any]:
    return {"phase": report.phase, "summary": report.summary, "remaining": report.remaining, "failed": report.failed,
            "document": app.layout.relative(report.document) if report.document else None,
            "items": [{"item": item.item, "title": item.title, "state": item.state, "detail": item.detail,
                       "recommendation": item.recommendation, "answer": item.answer} for item in report.items]}


def _create(invocation: Any, root: Path) -> None:
    """新建工作区目录与最小的 project.yaml。"""
    args = invocation.args
    repo = args.repo
    if repo is None and invocation.interactive:
        repo = _ask(invocation, "项目仓库的路径：") or None
    if repo is None:
        raise UsageError("新建工作区需要 --repo <仓库路径>")
    repo_path = Path(repo).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise UsageError(f"{repo_path} 不是 git 仓库")
    name = args.name or root.name
    branch = args.main_branch or DEFAULT_BRANCH
    language = args.language or DEFAULT_LANGUAGE
    if invocation.interactive:
        branch = _ask(invocation, f"主分支(回车采用 {branch})：") or branch
        language = _ask(invocation, f"给人读的文字的语言 zh、en、ja(回车采用 {language})：") or language
    data = {"project": {"name": name, "repo": str(repo_path), "mainBranch": branch, "language": language}}
    root.mkdir(parents=True, exist_ok=True)
    atomic.write_text(root / "project.yaml", "# 由 tightrein workspace init 生成；接入清单见 onboarding.md\n"
                      + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def _interview(invocation: Any, flow: onboarding.Onboarding) -> list[str]:
    """逐项提问：回车采用推荐，skip 跳过，其他输入作为值。"""
    notes = []
    for item in flow.questions():
        reply = _ask(invocation, f"\n{item.title}：{item.detail}\n推荐：{item.recommendation or '跳过'}\n"
                                 f"回车采用推荐，输入 {SKIP_WORD} 跳过，或输入值：")
        mode = onboarding.RECOMMENDED if not reply else (onboarding.SKIPPED if reply == SKIP_WORD else onboarding.VALUE)
        try:
            flow.answer(item.item, mode, reply or None)
        except EditRejected as error:
            notes.append(f"{item.item} 没有写入：{error}")
    return notes


def _init(invocation: Any) -> Outcome:
    root = invocation.args.workspace
    if root is None:
        raise UsageError("workspace init 需要 --workspace <工作区目录>")
    created = not (root / "project.yaml").is_file()
    if created:
        _create(invocation, root)
    app = invocation.app
    flow = app.onboarding()
    if created:
        flow.start()
    report = flow.check()
    notes: list[str] = []
    if invocation.interactive and report.remaining:
        notes = _interview(invocation, flow)
        report = flow.check()
    lines = [("已新建工作区 " if created else "工作区 ") + str(root), *_report_lines(report), *notes]
    code = exit_codes.GATE if report.remaining else exit_codes.OK
    next_step = "tightrein workspace answer <项> --recommended" if report.remaining else None
    if not app.layout.readonly_worktree().exists():
        next_step = f"tightrein worktree init --workspace {root}"
    return Outcome("workspace init", code, lines, result=_result(report, app), next=next_step)


def _check(invocation: Any) -> Outcome:
    app = invocation.app
    report = app.onboarding().check()
    return Outcome("workspace check", exit_codes.OK, _report_lines(report), result=_result(report, app))


def _answer(invocation: Any) -> Outcome:
    args = invocation.args
    chosen = [mode for mode, given in ((onboarding.RECOMMENDED, args.recommended), (onboarding.SKIPPED, args.skip),
                                       (onboarding.VALUE, args.value is not None)) if given]
    if len(chosen) != 1:
        raise UsageError("workspace answer 需要且只能给出 --recommended、--skip、--value 之一")
    app = invocation.app
    try:
        report = app.onboarding().answer(args.item, chosen[0], args.value)
    except EditRejected as error:
        return Outcome("workspace answer", exit_codes.PRECONDITION, [str(error)])
    return Outcome("workspace answer", exit_codes.OK, _report_lines(report), result=_result(report, app))


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    workspace = group(commands, "workspace", "工作区的接入")
    init = leaf(workspace, common, "init", _init, "新建或继续接入：检查清单并逐项提问", "workspace init")
    init.add_argument("--repo", help="新建工作区时的项目仓库路径")
    init.add_argument("--name", help="新建工作区时的项目名(缺省为目录名)")
    init.add_argument("--main-branch", help="新建工作区时的主分支")
    init.add_argument("--language", help="新建工作区时给人读的文字的语言")
    leaf(workspace, common, "check", _check, "对工作区生成一次接入清单检查", "workspace check")
    answer = leaf(workspace, common, "answer", _answer, "回答一项接入问题", "workspace answer")
    answer.add_argument("item", help="清单项，如 conventions、platform:deploy-source")
    answer.add_argument("--recommended", action="store_true", help="采用推荐答案")
    answer.add_argument("--skip", action="store_true", help="跳过(不接入)")
    answer.add_argument("--value", help="给出的值(检查命令、接口描述路径、部署工作流、分支格式)")
