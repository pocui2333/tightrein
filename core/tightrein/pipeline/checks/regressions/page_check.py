"""页面类复现检查(architecture/04 7.1、7.2)：经页面运行器执行，把复现检查目录作为 regress-<角色> 项目的 testDir。

同一 Issue 的页面检查一次执行，按用例文件对应到检查：expected、flaky 为通过，unexpected 为失败(失败步骤与错误写入
detail)，skipped 表示用例因数据缺失而跳过，记为前置条件不满足；没有结果的检查为 not-run，原因取运行器的说明。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tightrein.domain.enums import RegressionResult
from tightrein.pipeline.checks.pages import plan, result_parser
from tightrein.pipeline.checks.pages.result_parser import CaseResult
from tightrein.pipeline.checks.pages.runner import PageRunner
from tightrein.sources.base import ProbeTarget
from tightrein.pipeline.checks.regressions.manifest import CheckEntry
from tightrein.pipeline.checks.regressions.runner import Execution, not_run

ALL_TITLES = "."
PASSED = frozenset({"expected", "flaky"})


def _execution(case: CaseResult) -> Execution:
    artifacts = case.screenshots
    if case.outcome in PASSED:
        return Execution(RegressionResult.PASSED, f"用例 {case.title} 通过", artifacts=artifacts)
    if case.outcome == "skipped":
        return Execution(RegressionResult.NOT_RUN, f"用例 {case.title} 被跳过：{case.error or '前置数据缺失'}",
                         precondition_met=False, artifacts=artifacts)
    return Execution(RegressionResult.FAILED, f"失败步骤：{case.failed_step or '未知'}；{case.error or ''}".rstrip("；"),
                     artifacts=artifacts)


class PageCheck:
    def __init__(self, runner: PageRunner) -> None:
        self.runner = runner

    def __call__(self, entries: Sequence[CheckEntry], directory: Path, target: ProbeTarget) -> dict[str, Execution]:
        roles = tuple(dict.fromkeys(entry.role for entry in entries if entry.role))
        outcome = self.runner.run(target, roles=roles, spec_dirs=(directory,), grep=ALL_TITLES)
        cases = [case for case in result_parser.parse(target.raw_dir / plan.RESULTS_FILE).cases if not case.setup]
        reason = "；".join(outcome.notes) or "用例没有给出结果"
        found: dict[str, Execution] = {}
        for entry in entries:
            case = next((item for item in cases if item.file.endswith(entry.file)), None)
            found[entry.id] = not_run(reason) if case is None else _execution(case)
        return found
