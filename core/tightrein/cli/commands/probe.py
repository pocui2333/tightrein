"""项目探针与接口描述起草的命令(redesign/01-collect.md 第 4、5 节)。

- probe new <名称>：在工作区 probes/ 下生成探针模板，并把登记按行写回 project.yaml 的 sources.project-probes；
- probe test <名称>：单独运行一次、校验输出、显示将产出的信号；不写数据库、不保存状态、不进入归并；
- probe logs：经工作区配置的日志平台方法取一段时间内的日志(配置了 log-parse 时返回解析后的条目)，供探针调用；
- spec draft：接口描述没有自动导出时由 AI 读代码起草 openapi.draft.yaml，交用户审阅确认(接入清单「接口描述」)。
"""

from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.config.edit import EditRejected, set_value
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, RunnerStatus
from tightrein.orchestrator.onboarding.service import SPEC_DRAFT
from tightrein.pipeline.collect.prompts import tasks
from tightrein.pipeline.collect.prompts.tasks import TaskContext
from tightrein.runner.roles import run as run_role
from tightrein.sources.base import ProbeTarget
from tightrein.sources.project_probes import registry
from tightrein.store.files import atomic, yaml_text

PROBES_DIR = "probes"
REGISTRATION = "sources.project-probes"
NAME = re.compile(r"^[a-z][a-z0-9-]*$")
DEFAULT_EVERY = "1d"
TEMPLATE = '''"""项目探针 {name}：<一句话说明它检查什么业务异常>。

契约见 docs/reference/project-probe.md；写法见 docs/how-to/write-project-probe.md。只读：不修改被测系统，不登录服务器。
"""

from tightrein.sources.project_probes import helpers


def main() -> None:
    found = helpers.read_input()
    since, until = found["window"]["since"], found["window"]["until"]
    state = found["state"] or {{}}
    signals = []
    # 在这里查询只读接口或日志平台(helpers.query_logs)，按项目规则判断，发现异常时：
    # signals.append(helpers.signal("<位置>", "<现象>", ["<证据>"], "<指纹>", severity_hint="P2"))
    helpers.emit(signals, state={{**state, "checkedUntil": until}}, notes=[f"检查了 {{since}} 到 {{until}}"])


if __name__ == "__main__":
    main()
'''


def _new(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    name = args.name
    if not NAME.match(name):
        raise UsageError(f"探针名只能用小写字母、数字与 -，以字母开头：{name}")
    items = list(app.config.data.get("sources", {}).get("project-probes", []))
    if any(item.get("name") == name for item in items):
        raise UsageError(f"{REGISTRATION} 中已登记 {name}")
    registry.interval(args.every)
    script = app.layout.root / PROBES_DIR / f"{name.replace('-', '_')}.py"
    if script.exists():
        raise UsageError(f"{app.layout.relative(script)} 已存在")
    script.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(script, TEMPLATE.format(name=name))
    relative = app.layout.relative(script)
    entry = {"name": name, "command": ["{python}", relative], "every": args.every}
    try:
        set_value(app.config.path, REGISTRATION, [*items, entry])
    except EditRejected as error:
        lines = [f"已生成 {relative}；{error}", f"请手动在 {REGISTRATION} 中加入：{entry}"]
        return Outcome("probe new", exit_codes.GATE, lines,
                       result={"script": relative, "registration": entry, "registered": False})
    lines = [f"已生成 {relative} 并登记到 {REGISTRATION}", f"试跑：tightrein probe test {name}"]
    return Outcome("probe new", exit_codes.OK, lines,
                   result={"script": relative, "registration": entry, "registered": True})


def _test(invocation: Any) -> Outcome:
    app = invocation.app
    name = invocation.args.name
    with tempfile.TemporaryDirectory(prefix="tightrein-probe-test-") as scratch:
        target = ProbeTarget(environment=str(app.config.get("target.environment")), run_id=app.session_id,
                             raw_dir=Path(scratch), clock=app.clock, base_url=app.base_url())
        try:
            trial = app.project_probes().trial(name, target)
        except LookupError as error:
            raise UsageError(str(error)) from error
        stderr = Path(scratch) / f"{name}.stderr.log"
        log = stderr.read_text(encoding="utf-8") if stderr.is_file() else ""
    if not trial.run.ok:
        lines = [f"{name} 的输出无效：{trial.run.error}", *([f"标准错误：{log.strip()}"] if log.strip() else [])]
        return Outcome("probe test", exit_codes.FAILED, lines, result={"valid": False, "error": trial.run.error})
    output = trial.run.output or {}
    lines = [f"{name} 的输出符合契约，将产出 {len(trial.signals)} 条信号"]
    lines += [f"- {signal.location}：{signal.message}(指纹 {signal.ctx('probeFingerprint')}，"
              f"严重度提示 {signal.ctx('severityHint') or '无'})" for signal in trial.signals]
    lines += [f"说明：{note}" for note in output.get("notes", [])]
    return Outcome("probe test", exit_codes.OK, lines,
                   result={"valid": True, "signals": [signal.to_dict() for signal in trial.signals],
                           "state": output.get("state"), "notes": output.get("notes", [])})


def _time(text: str) -> Any:
    try:
        return parse_iso(text)
    except ValueError as error:
        raise UsageError(f"时间须为带时区的 ISO 时间：{text}") from error


def _logs(invocation: Any) -> Outcome:
    args = invocation.args
    client = invocation.app.extensions()
    if not client.configured(ExtensionPoint.LOG_PLATFORM):
        raise UsageError("没有配置 extensions.log-platform")
    fetched = client.log_platform(args.query, _time(args.since), _time(args.until), args.limit)
    if fetched.output is None:
        detail = fetched.failure.describe() if fetched.failure is not None else "没有输出"
        return Outcome("probe logs", exit_codes.FAILED, [f"log-platform 失败：{detail}"], result={"entries": []})
    chunks = fetched.output["chunks"]
    parse = not args.raw and client.resolution.get(ExtensionPoint.LOG_PARSE).layer is not ExtensionLayer.DEFAULT
    if parse:
        parsed = client.log_parse(chunks, None)
        if parsed.output is None:
            detail = parsed.failure.describe() if parsed.failure is not None else "没有输出"
            return Outcome("probe logs", exit_codes.FAILED, [f"log-parse 失败：{detail}"], result={"entries": []})
        entries: list[Any] = parsed.output["entries"]
    else:
        entries = [{"stream": chunk["stream"], "line": line} for chunk in chunks for line in chunk["text"].splitlines()]
    return Outcome("probe logs", exit_codes.OK, [f"{len(entries)} 条"],
                   result={"entries": entries, "truncated": fetched.output["truncated"], "parsed": parse})


def _draft(invocation: Any) -> Outcome:
    app = invocation.app
    worktree = app.layout.readonly_worktree()
    if not worktree.is_dir():
        raise UsageError("没有只读 worktree：先执行 tightrein worktree init")
    task = tasks.spec_draft_task(TaskContext(app.tool, app.config, app.session_id, worktree), app.base_url())
    result = run_role(app.runner(), task, app.clock, app.overrides)
    if result.status is not RunnerStatus.OK or result.output is None:
        return Outcome("spec draft", exit_codes.FAILED, [f"起草未完成：{result.status.label}"],
                       result={"status": result.status.value})
    path = app.layout.root / SPEC_DRAFT
    atomic.write_text(path, yaml_text.dump(result.output["openapi"]))
    uncertain = list(result.output["uncertain"])
    lines = [f"已起草 {app.layout.relative(path)}({len(result.output['openapi'].get('paths', {}))} 个路由)",
             *(f"待确认：{item}" for item in uncertain),
             "审阅修改后改名为 openapi.yaml，执行 tightrein workspace answer spec --recommended 登记"]
    return Outcome("spec draft", exit_codes.OK, lines,
                   result={"draft": app.layout.relative(path), "uncertain": uncertain,
                           "sources": list(result.output["sources"])})


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    probes = group(commands, "probe", "项目探针")
    new = leaf(probes, common, "new", _new, "生成项目探针模板并登记", "probe new")
    new.add_argument("name")
    new.add_argument("--every", default=DEFAULT_EVERY, help="运行间隔，如 15m、1h、1d")
    test = leaf(probes, common, "test", _test, "单独试跑一个项目探针并校验输出", "probe test")
    test.add_argument("name")
    logs = leaf(probes, common, "logs", _logs, "经日志平台取一段时间内的日志", "probe logs")
    logs.add_argument("--query", required=True)
    logs.add_argument("--since", required=True)
    logs.add_argument("--until", required=True)
    logs.add_argument("--limit", type=int, default=1000)
    logs.add_argument("--raw", action="store_true", help="返回原文行，不经 log-parse")
    spec = group(commands, "spec", "接口描述")
    leaf(spec, common, "draft", _draft, "由 AI 读代码起草接口描述，交用户确认", "spec draft")
