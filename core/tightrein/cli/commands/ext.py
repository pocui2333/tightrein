"""ext 的命令(architecture/10 第 8 章)：list、methods、run、test。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions import commands as ext
from tightrein.extensions.resolve import project_implementations
from tightrein.store.files.layout import UserLayout


def _list(invocation: Any) -> Outcome:
    found = ext.list_points(invocation.app.resolution())
    lines = [f"{item.point.value}：{item.implementation.value} {item.method or ''}".rstrip() for item in found]
    return Outcome("ext list", exit_codes.OK, lines, result=[asdict(item) for item in found])


def _methods(invocation: Any) -> Outcome:
    point = ExtensionPoint(invocation.args.point) if invocation.args.point else None
    found = ext.list_methods(invocation.app.tool, point)
    lines = [f"{item.point.value} {item.method}：{item.summary}" for item in found]
    return Outcome("ext methods", exit_codes.OK, lines, result=[asdict(item) for item in found])


def _run(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    point = ExtensionPoint(args.point)
    if args.input is not None:
        request = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        request = ext.default_input(point, app.config, app.layout, app.session_id)
    result = ext.run_point(app.invoker(), app.resolution(), point, request, repo=app.config.repo, commit=args.commit)
    code = exit_codes.FAILED if result.failed else exit_codes.OK
    line = f"{point.value}：{'失败' if result.failed else '成功'}"
    return Outcome("ext run", code, [line], result=asdict(result))


def _test(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    user = UserLayout(app.home)
    runner = app.extension_runner()
    environ = dict(app.environ)
    if args.stack:
        outcomes = ext.run_stack_fixtures(app.tool, args.stack, user, runner=runner, environ=environ)
    elif args.workspace_extensions:
        implementations = project_implementations(app.config, app.layout)
        outcomes = ext.run_fixtures(implementations, app.layout.extension_fixtures_dir(), user, runner=runner,
                                    environ=environ)
    else:
        raise UsageError("ext test 需要 --stack <技术栈> 或 --workspace-extensions")
    failed = [item for item in outcomes if not item.passed]
    lines = [f"{len(outcomes) - len(failed)} / {len(outcomes)} 个夹具通过"]
    lines += [f"- {item.case.point.value}/{item.case.name}：{'；'.join(item.differences)}" for item in failed]
    return Outcome("ext test", exit_codes.FAILED if failed else exit_codes.OK, lines,
                   result=[asdict(item) for item in outcomes])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    group_ = group(commands, "ext", "扩展点")
    leaf(group_, common, "list", _list, "各扩展点的解析结果")
    methods = leaf(group_, common, "methods", _methods, "方法目录中可选的方法")
    methods.add_argument("--point", choices=[point.value for point in ExtensionPoint])
    run = leaf(group_, common, "run", _run, "单独调用一次扩展点(--input 为输入 JSON 文件)")
    run.add_argument("point", choices=[point.value for point in ExtensionPoint])
    test = leaf(group_, common, "test", _test, "以夹具测试扩展")
    test.add_argument("--stack", help="技术栈名称")
    test.add_argument("--workspace-extensions", action="store_true", help="测试当前工作区的项目扩展")
