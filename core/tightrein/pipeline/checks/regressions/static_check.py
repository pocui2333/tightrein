"""静态类复现检查(architecture/04 7.1)：`<Semgrep 命令> scan --config <规则> --json --metrics=off <targets>`，没有结果为通过。

检查文件有两种写法：Semgrep 规则文件(含 rules)，直接作为 --config；或者引用巡检已用的规则
`{configs: [<sources.static.semgrep.configs>], rule: <规则编号>}`，以这些配置扫描，只计编号为该规则(或以它结尾)的结果。
有结果为失败，命中的位置写入 detail；Semgrep 无法运行或输出无法解析为 not-run。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from tightrein.domain.enums import RegressionResult
from tightrein.sources.common.procs import Launcher, ToolCommand, tool_env
from tightrein.pipeline.checks.regressions.manifest import CheckEntry
from tightrein.pipeline.checks.regressions.runner import Execution, not_run
from tightrein.sources.static.tools import semgrep
from tightrein.store.files import yaml_text

LOG_FILE = "{check}.semgrep.log"


def _reference(path: Path) -> Mapping[str, object] | None:
    """引用巡检规则的写法时返回 {configs, rule}；是规则文件时为空。"""
    try:
        data = yaml_text.load(path.read_text(encoding="utf-8"))
    except yaml_text.YamlError:
        return None
    if isinstance(data, Mapping) and "rule" in data and "configs" in data:
        return data
    return None


class StaticCheck:
    def __init__(self, launcher: Launcher, environ: Mapping[str, str], timeout_seconds: float,
                 command: str = semgrep.TOOL) -> None:
        self.launcher = launcher
        self.environ = environ
        self.timeout_seconds = timeout_seconds
        self.command = command

    def __call__(self, entry: CheckEntry, directory: Path, worktree: Path, raw_dir: Path) -> Execution:
        reference = _reference(directory / entry.file)
        configs = [str(directory / entry.file)] if reference is None else list(reference["configs"])
        argv = semgrep.build_argv(configs, list(entry.targets), self.command)
        raw_dir.mkdir(parents=True, exist_ok=True)
        run = self.launcher(ToolCommand(argv, worktree, self.timeout_seconds,
                                        tool_env(self.environ, semgrep.ENVIRONMENT),
                                        raw_dir / LOG_FILE.format(check=f"{directory.name}-{entry.id}")))
        if run.exit_code != 0:
            return not_run(f"Semgrep 失败：{run.describe()}")
        try:
            findings, errors = semgrep.parse(json.loads(run.stdout))
        except (ValueError, KeyError):
            return not_run("Semgrep 的输出无法解析")
        if errors:
            return not_run(f"Semgrep 报告了错误：{'；'.join(errors)}")
        if reference is not None:
            findings = [item for item in findings if item.rule.endswith(str(reference["rule"]))]
        if findings:
            where = "、".join(f"{item.file}:{item.line}" for item in findings)
            return Execution(RegressionResult.FAILED, f"规则仍然命中：{where}")
        return Execution(RegressionResult.PASSED, "规则没有命中")
