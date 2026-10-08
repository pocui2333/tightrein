"""项目与技术栈自带的确定性工具(构建告警、依赖漏洞、项目自己的检查)：按配置运行项目写的只读脚本，读回统一格式的结果。

- 登记在 controls."collect.static".extension：每项 name、command(参数数组，`{python}` 为 tightrein 的解释器)、
  timeout、secrets(要注入的凭据条目名)；脚本经 collect/common/scripts 运行(工作区为当前目录，环境变量只给白名单)；
- 标准输入给 {level, baseCommit, head, changedFiles, worktree}；第一次运行(没有 base)按 full 请求：incremental 要求有
  baseCommit；
- 标准输出须符合 extension.schema.json：{tools: [{name, status, reason, logFile}], findings: [ToolFinding]}；
- 增量档只保留改动文件里的结果，但依赖漏洞不受这个限制(依赖清单常常不在改动里)；
- 某个工具失败(脚本失败、输出不合格、或它报告自己 failed)时丢弃它的结果、巡检记为 partial，其余工具照常。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tightrein.collect.common.source import SourceError, SourceInvalid
from tightrein.collect.static.claims import ToolFinding
from tightrein.collect.static.scope import Level, Scope
from tightrein.protocol import scripts
from tightrein.protocol.handoff import load_schema, schema_errors
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.process import ProcessRunner
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

SCHEMA_FILE = "extension.schema.json"
FAILED = "failed"


@dataclass(frozen=True)
class ExtensionTools:
    findings: tuple[ToolFinding, ...] = ()
    notes: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class Runner:
    """运行项目脚本需要的依赖。"""

    runner: ProcessRunner
    workspace: Path
    environ: Mapping[str, str]
    secrets: Mapping[str, str]
    redactor: Redactor
    raw: RawDir


def request(scope: Scope, worktree: Path) -> dict[str, Any]:
    incremental = scope.level is Level.INCREMENTAL and scope.base is not None
    return {"level": Level.INCREMENTAL.value if incremental else Level.FULL.value,
            "baseCommit": scope.base if incremental else None, "head": scope.head,
            "changedFiles": list(scope.changed), "worktree": str(worktree)}


def keep(finding: ToolFinding, scope: Scope, changed: frozenset[str]) -> bool:
    return scope.whole_repo or finding.vulnerability or finding.file in changed


def run(tools: Sequence[Mapping[str, Any]], scope: Scope, worktree: Path, given: Runner) -> ExtensionTools:
    if not tools:
        return ExtensionTools(notes=("未配置项目或技术栈的确定性工具",))
    document = request(scope, worktree)
    changed = frozenset(scope.changed)
    findings: list[ToolFinding] = []
    notes: list[str] = []
    degraded = False
    for tool in tools:
        name = str(tool["name"])
        try:
            stdout = scripts.run(name=name, command=tool["command"], document=document, workspace=given.workspace,
                                 runner=given.runner, environ=given.environ, secrets=given.secrets,
                                 secret_names=tool.get("secrets") or (), redactor=given.redactor, raw=given.raw,
                                 timeout_s=parse_duration(tool["timeout"]))
            output = _output(name, stdout)
        except SourceError as error:
            degraded = True
            notes.append(f"确定性工具 {name} 失败：{error.message}")
            continue
        failed_tools = set()
        for item in output["tools"]:
            if item["status"] == FAILED:
                degraded = True
                failed_tools.add(item["name"])
                log = f"，输出见 {item['logFile']}" if item.get("logFile") else ""
                notes.append(f"确定性工具 {item['name']} 失败：{item.get('reason') or '没有说明'}{log}")
        findings += [finding for finding in map(ToolFinding.from_json, output["findings"])
                     if finding.tool not in failed_tools and keep(finding, scope, changed)]
    return ExtensionTools(tuple(findings), tuple(notes), degraded)


def _output(name: str, stdout: str) -> dict[str, Any]:
    try:
        output = json.loads(stdout)
    except ValueError as error:
        raise SourceInvalid(f"{name} 的输出不是 JSON") from error
    errors = schema_errors(output, _schema())
    if errors:
        raise SourceInvalid(f"{name} 的输出不合格式：" + "；".join(errors))
    return output


@cache
def _schema() -> dict[str, Any]:
    return load_schema(Path(__file__).with_name(SCHEMA_FILE))
