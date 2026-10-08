"""Semgrep：按配置的规则集扫描检查范围；也用来跑规则库(工作区 rules/ 中由已修缺陷生成、已用原补丁验证过的规则)。

`<命令> scan --config <配置>... --json --metrics=off <文件…>`：
- 不加 `--error` 时有结果也返回 0，所以只有 0 算正常，其他退出码按失败：该工具的结果为空，巡检为 partial；
- `--metrics=off` 并设 SEMGREP_SEND_METRICS=off、SEMGREP_ENABLE_VERSION_CHECK=0，不联网；
- 增量档只扫范围内的文件，全量、基线与第一次扫 `.`；
- 严重度映射到 critical、high、medium、low(未知为 medium)；结果路径去掉开头的 `./`；errors 计数写进说明；
- 命令取 settings 的 tools.semgrep.path(含 / 的相对路径相对 tightrein 仓库根)，没有时按 PATH 找 semgrep。
标准输出保存到原始输出目录的 semgrep.json。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.collect.static.claims import ToolFinding
from tightrein.collect.static.scope import Scope
from tightrein.protocol.process import Command, ProcessRunner
from tightrein.protocol.security import child_env
from tightrein.store.files.atomic import write_text

TOOL = "semgrep"
OUTPUT_FILE = "semgrep.json"
KIND = "lint"
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
SEVERITIES = {"CRITICAL": "critical", "ERROR": "high", "HIGH": "high", "WARNING": "medium", "MEDIUM": "medium",
              "INFO": "low", "LOW": "low"}
DEFAULT_SEVERITY = "medium"
ENVIRONMENT = {"SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0"}
CURRENT_DIR_PREFIX = "./"
WHOLE_REPO = "."


@dataclass(frozen=True)
class SemgrepRun:
    status: str  # ok、failed、skipped
    findings: tuple[ToolFinding, ...] = ()
    notes: tuple[str, ...] = ()


def resolve_command(configured: str | None, tool_root: Path) -> str:
    """含 / 的相对路径相对 tightrein 仓库根解析，其余(命令名或绝对路径)原样使用。"""
    if not configured:
        return TOOL
    if "/" in configured and not Path(configured).is_absolute():
        return str(tool_root / configured)
    return configured


def build_argv(configs: Sequence[str], targets: Sequence[str], command: str = TOOL) -> tuple[str, ...]:
    argv = [command, "scan"]
    for config in configs:
        argv += ["--config", config]
    return (*argv, "--json", "--metrics=off", *targets)


def targets(scope: Scope) -> list[str]:
    if scope.whole_repo or scope.first_run:
        return [WHOLE_REPO]
    return list(scope.files)


def parse(document: Mapping[str, Any]) -> tuple[list[ToolFinding], list[str]]:
    findings = []
    for item in document.get("results", []):
        extra = item.get("extra") or {}
        start = item.get("start") or {}
        severity = SEVERITIES.get(str(extra.get("severity", "")).upper(), DEFAULT_SEVERITY)
        findings.append(ToolFinding(TOOL, KIND, item["check_id"], str(item["path"]).removeprefix(CURRENT_DIR_PREFIX),
                                    start.get("line"), start.get("col"), extra.get("message") or item["check_id"],
                                    severity))
    return findings, [_error(error) for error in document.get("errors", [])]


def run(runner: ProcessRunner, configs: Sequence[str], scope: Scope, repo: Path, raw_dir: Path,
        environ: Mapping[str, str], timeout_s: float, command: str = TOOL) -> SemgrepRun:
    if not configs:
        return SemgrepRun(SKIPPED, notes=("没有配置规则集(controls.\"collect.static\".semgrep.configs)，不运行 Semgrep",))
    paths = targets(scope)
    if not paths:
        return SemgrepRun(SKIPPED, notes=("检查范围内没有文件，不运行 Semgrep",))
    outcome = runner.run(Command(build_argv(configs, paths, command), repo,
                                 child_env(environ, set_values=ENVIRONMENT), timeout_s=timeout_s))
    if outcome.stdout.strip():
        write_text(raw_dir / OUTPUT_FILE, outcome.stdout)
    if outcome.start_error is not None:
        return SemgrepRun(FAILED, notes=(f"Semgrep 无法启动：{outcome.start_error}",))
    if outcome.stopped_by is not None:
        return SemgrepRun(FAILED, notes=(f"Semgrep 被终止({outcome.stopped_by})",))
    try:
        document = json.loads(outcome.stdout)
    except ValueError:
        document = None
    if outcome.exit_code != 0:
        errors = parse(document)[1] if isinstance(document, Mapping) else []
        return SemgrepRun(FAILED, notes=(f"Semgrep 失败：退出码 {outcome.exit_code}", *errors))
    if not isinstance(document, Mapping):
        return SemgrepRun(FAILED, notes=("Semgrep 的输出不是 JSON",))
    findings, errors = parse(document)
    notes = (f"Semgrep 报告了 {len(errors)} 个错误：{'；'.join(errors)}",) if errors else ()
    return SemgrepRun(OK, tuple(findings), notes)


def _error(error: Mapping[str, Any]) -> str:
    lines = str(error.get("message") or error.get("type") or "").splitlines()
    return f"{error.get('path') or error.get('rule_id') or TOOL}：{lines[0] if lines else '未知错误'}"
