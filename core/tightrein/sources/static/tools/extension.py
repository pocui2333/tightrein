"""经 static-tools 扩展取得技术栈与项目的确定性工具结果(architecture/04 5.3，architecture/10 3.6)。

- 扩展返回的每条 findings 转成 ToolFinding；incremental 档位只保留 file 在改动文件中的条目，依赖漏洞不受此限制
  (依赖清单文件不在改动中同样保留)；
- 某个工具 status 为 failed 时该工具的结果为空、探针为 partial，原因与日志文件写入 notes；skipped 的工具只记录；
- 扩展整体失败或超时：技术栈工具的结果为空，Semgrep 与审查照常，探针为 partial；没有扩展时注明「未配置确定性工具」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ProbeLevel
from tightrein.extensions.client import ExtensionClient, StaticScope
from tightrein.sources.static.reviewer import ToolFinding
from tightrein.sources.static.scope import Scope


@dataclass(frozen=True)
class ExtensionTools:
    findings: tuple[ToolFinding, ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    degraded: bool = False
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def to_finding(item: Mapping[str, Any]) -> ToolFinding:
    return ToolFinding(item["tool"], item["kind"], item["rule"], item["file"], item.get("line"), item.get("column"),
                       item["message"], item["severity"], item.get("package"))


def keep(finding: ToolFinding, scope: Scope) -> bool:
    return scope.whole_repo or finding.vulnerability or finding.file in set(scope.changed_files)


def run(client: ExtensionClient, repo: Path, scope: Scope, raw_dir: Path) -> ExtensionTools:
    """第一次巡检没有 base 与 baseline 档位都按 full 请求扩展。"""
    incremental = scope.level is ProbeLevel.INCREMENTAL and scope.base_commit is not None
    request = StaticScope(ProbeLevel.INCREMENTAL, scope.base_commit, scope.changed_files) if incremental else (
        StaticScope(ProbeLevel.FULL, None, scope.changed_files))
    result = client.static_tools(repo, scope.head, request, raw_dir)
    entries = {result.point.value: result.stats_entry()}
    if result.failure is not None:
        return ExtensionTools((), notes=(f"static-tools 失败：{result.failure.describe()}", *result.notes),
                              degraded=True, extensions=entries)
    if result.output is None:
        return ExtensionTools((), notes=result.notes, extensions=entries)
    notes = list(result.notes)
    degraded = False
    failed_tools = set()
    for tool in result.output["tools"]:
        if tool["status"] == "failed":
            degraded = True
            failed_tools.add(tool["name"])
            log = f"，输出见 {tool['logFile']}" if tool.get("logFile") else ""
            notes.append(f"确定性工具 {tool['name']} 失败：{tool['reason']}{log}")
    findings = tuple(finding for finding in map(to_finding, result.output["findings"])
                     if finding.tool not in failed_tools and keep(finding, scope))
    return ExtensionTools(findings, tuple(result.output["tools"]), tuple(notes), degraded, entries)
