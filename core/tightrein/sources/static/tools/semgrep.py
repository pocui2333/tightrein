"""Semgrep 的执行与解析(architecture/04 5.3)。

`<命令> scan --config <配置>... --json --metrics=off <文件…>`，命令取 resolve_command 的结果(缺省 semgrep，按 PATH
查找)，配置取 sources.static.semgrep.configs，没有配置时不运行；
incremental 档位只扫描范围内的文件，full、baseline 档位(与第一次巡检)扫描整个仓库。退出码 0 表示正常完成(不加 --error 时
有结果也返回 0)，其他退出码按失败处理，该工具的结果为空，探针为 partial。标准输出保存到 raw/static/semgrep.json。
解析 results 中的 check_id、path、start.line、start.col、extra.message、extra.severity，以及 errors(计数并写入 notes)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.sources.common.procs import Launcher, ToolCommand, tool_env
from tightrein.sources.static.reviewer import ToolFinding
from tightrein.sources.static.scope import Scope

TOOL = "semgrep"
COMMAND_ENV = "TIGHTREIN_SEMGREP"
OUTPUT_FILE = "semgrep.json"
LOG_FILE = "semgrep.log"
KIND = "lint"
SEVERITIES = {"CRITICAL": "critical", "ERROR": "high", "HIGH": "high", "WARNING": "medium", "MEDIUM": "medium",
              "INFO": "low", "LOW": "low"}
DEFAULT_SEVERITY = "medium"
ENVIRONMENT = {"SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0"}


@dataclass(frozen=True)
class SemgrepRun:
    status: str
    findings: tuple[ToolFinding, ...] = ()
    notes: tuple[str, ...] = ()
    exit_code: int | None = None


def resolve_command(configured: str, tool_root: Path) -> str:
    """Semgrep 命令：含 / 的相对路径相对本工具仓库根目录解析，其余(命令名或绝对路径)原样使用。"""
    if "/" in configured and not Path(configured).is_absolute():
        return str(tool_root / configured)
    return configured


def build_argv(configs: Sequence[str], targets: Sequence[str], command: str = TOOL) -> tuple[str, ...]:
    argv = [command, "scan"]
    for config in configs:
        argv += ["--config", config]
    return (*argv, "--json", "--metrics=off", *targets)


def parse(document: Mapping[str, Any]) -> tuple[list[ToolFinding], list[str]]:
    findings = []
    for item in document.get("results", []):
        extra = item.get("extra") or {}
        start = item.get("start") or {}
        severity = SEVERITIES.get(str(extra.get("severity", "")).upper(), DEFAULT_SEVERITY)
        findings.append(ToolFinding(TOOL, KIND, item["check_id"], item["path"], start.get("line"), start.get("col"),
                                    extra.get("message") or item["check_id"], severity))
    return findings, [_error(error) for error in document.get("errors", [])]


def _error(error: Mapping[str, Any]) -> str:
    lines = str(error.get("message") or error.get("type") or "").splitlines()
    return f"{error.get('path') or error.get('rule_id') or TOOL}：{lines[0] if lines else '未知错误'}"


def targets(scope: Scope) -> list[str]:
    if scope.whole_repo or scope.first_run:
        return ["."]
    return list(scope.files)


def run(launcher: Launcher, configs: Sequence[str], scope: Scope, repo: Path, raw_dir: Path,
        environ: Mapping[str, str], timeout: float, command: str = TOOL) -> SemgrepRun:
    if not configs:
        return SemgrepRun("skipped", notes=("没有配置 sources.static.semgrep.configs，不运行 Semgrep",))
    paths = targets(scope)
    if not paths:
        return SemgrepRun("skipped", notes=("扫描范围内没有文件，不运行 Semgrep",))
    raw_dir.mkdir(parents=True, exist_ok=True)
    result = launcher(ToolCommand(build_argv(configs, paths, command), repo, timeout, tool_env(environ, ENVIRONMENT),
                                  raw_dir / LOG_FILE))
    if result.stdout.strip():
        (raw_dir / OUTPUT_FILE).write_text(result.stdout, encoding="utf-8")
    if result.exit_code != 0:
        detail = result.describe()
        try:
            _, errors = parse(json.loads(result.stdout))
        except ValueError:
            errors = []
        return SemgrepRun("failed", notes=(f"Semgrep 失败：{detail}", *errors), exit_code=result.exit_code)
    try:
        document = json.loads(result.stdout)
    except ValueError:
        return SemgrepRun("failed", notes=("Semgrep 的输出不是 JSON",), exit_code=result.exit_code)
    findings, errors = parse(document)
    notes = (f"Semgrep 报告了 {len(errors)} 个错误：{'；'.join(errors)}",) if errors else ()
    return SemgrepRun("ok", tuple(findings), notes, result.exit_code)
