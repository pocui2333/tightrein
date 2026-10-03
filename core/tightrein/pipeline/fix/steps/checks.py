"""确定性检查(architecture/07 4.9)：每一项不通过时给出性质。

| 检查 | 性质 |
|---|---|
| 项目检查(project_checks，只运行受影响的测试) | 局部问题；命令无法启动记为「未运行」 |
| 改动量上限 thresholds.change(含未跟踪的新文件，不含测试文件) | 局部问题：交回 fix-executor 按计划收敛，同一次实施中仍超出时由调用方转人工 |
| 计划外文件：已跟踪的文件 | 计划没覆盖 |
| 受保护文件(计划确认时列出的不算) | 规范要求用户确认 |
| 改动了测试或复现检查、新增跳过标记 | 局部问题(撤销对应改动) |
| 本 Issue 与相关的其他 Issue 在 worktree 上执行的复现检查(静态类、测试类) | 局部问题；未执行记为「未运行」 |
| 本 Issue 的复现测试被改动或删除(调用方已按登记副本恢复) | 局部问题 |
| 交付规则：新增行匹配残留模式、未跟踪且不在计划中的文件(临时文件) | 局部问题 |
新增行中与复现检查输入相同的字面量(suspected-hardcode)不判不通过，交给评审核对是否为特判。本模块只做判定，命令与
复现检查由调用方执行后把结果传入；本 Issue 的复现测试由调用方从 changes 与 untracked 中去掉(它随修复提交，不是修复改动)。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.domain.enums import RegressionResult, ReviewCategory
from tightrein.guards import diff_rules
from tightrein.guards.diff_rules import ChangedFile
from tightrein.guards.policy import GuardSettings
from tightrein.guards.report import Violation
from tightrein.pipeline.checks.project_checks import CheckRun
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome


SIZE = "size"
SIZE_GUIDANCE = "按计划收敛：撤回计划外与非必要的改动，不得以放宽上限处理"


@dataclass(frozen=True)
class Finding:
    check: str
    location: str | None
    problem: str
    category: ReviewCategory

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "location": self.location, "problem": self.problem,
                "category": self.category.value}

    def text(self) -> str:
        return f"[{self.check}] {self.location + '：' if self.location else ''}{self.problem}"


@dataclass(frozen=True)
class CheckInputs:
    changes: Sequence[ChangedFile]
    untracked: Collection[str]
    plan: Mapping[str, Any]
    settings: GuardSettings
    project: Sequence[CheckRun] = ()
    approved_protected: Sequence[str] = ()
    reproduction: set[str] = field(default_factory=set)
    static_repro: Sequence[RegressionOutcome] = ()
    other_regressions: Sequence[RegressionOutcome] = ()
    repro_restored: Sequence[str] = ()


@dataclass(frozen=True)
class CheckReport:
    findings: tuple[Finding, ...]
    not_run: tuple[str, ...]
    hardcode: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not self.findings and not self.not_run


def _violations(check: str, violations: Sequence[Violation], category: ReviewCategory) -> list[Finding]:
    return [Finding(check, item.path, item.detail, category) for item in violations]


def _project(runs: Sequence[CheckRun]) -> tuple[list[Finding], list[str]]:
    findings, not_run = [], []
    for run in runs:
        if run.not_run:
            not_run.append(f"项目检查 {run.name} 未运行：{run.not_run_reason}；检查 checks.prepare 是否已准备好依赖")
        elif run.modified:
            findings.append(Finding(f"project:{run.name}", None, f"执行后工作区出现新改动：{'、'.join(run.modified)}",
                                    ReviewCategory.LOCAL))
        elif not run.passed:
            code = "超时" if run.exit_code is None else f"退出码 {run.exit_code}"
            findings.append(Finding(f"project:{run.name}", None, f"`{run.command}` 失败({code})，日志 {run.log}",
                                    ReviewCategory.LOCAL))
    return findings, not_run


def _regressions(check: str, outcomes: Sequence[RegressionOutcome], text: str) -> tuple[list[Finding], list[str]]:
    findings, not_run = [], []
    for outcome in outcomes:
        if outcome.result is RegressionResult.FAILED:
            findings.append(Finding(check, outcome.location, f"{text}：{outcome.detail}", ReviewCategory.LOCAL))
        elif outcome.result is not RegressionResult.PASSED:
            not_run.append(f"{outcome.check.issue_id}/{outcome.check.check_id} 未执行：{outcome.detail}")
    return findings, not_run


def evaluate(inputs: CheckInputs) -> CheckReport:
    findings, not_run = _project(inputs.project)
    plan = inputs.plan
    findings += [Finding(SIZE, item.path, f"{item.detail}；{SIZE_GUIDANCE}", ReviewCategory.LOCAL)
                 for item in diff_rules.size_violations(inputs.changes, inputs.settings)]
    planned = [item["path"] for item in plan["files"]]
    outside = diff_rules.outside_plan_violations(inputs.changes, planned)
    findings += _violations("outside-plan", [item for item in outside if item.path not in inputs.untracked],
                            ReviewCategory.PLAN_GAP)
    findings += [Finding("temporary-file", item.path, "未跟踪且不在计划中的文件(临时文件)，交付前删除", ReviewCategory.LOCAL)
                 for item in outside if item.path in inputs.untracked]
    findings += _violations("protected", diff_rules.protected_violations(inputs.changes, inputs.settings,
                                                                         inputs.approved_protected),
                            ReviewCategory.NEEDS_USER)
    findings += _violations("tests", diff_rules.modified_test_violations(inputs.changes, inputs.settings),
                            ReviewCategory.LOCAL)
    findings += _violations("skip-marker", diff_rules.skip_violations(inputs.changes, inputs.settings),
                            ReviewCategory.LOCAL)
    findings += _violations("residue", diff_rules.residue_violations(inputs.changes, inputs.settings),
                            ReviewCategory.LOCAL)
    findings += [Finding("repro-test", path, "本 Issue 的复现测试被改动或删除，已按本工具登记的内容恢复；修复不得改动复现测试",
                         ReviewCategory.LOCAL) for path in inputs.repro_restored]
    own, own_missing = _regressions("repro", inputs.static_repro, "本 Issue 的复现检查仍然失败")
    others, other_missing = _regressions("other-regressions", inputs.other_regressions,
                                         "改动使相关的其他 Issue 的回归检查失败")
    hardcode = tuple(diff_rules.hardcode_violations(inputs.changes, inputs.reproduction,
                                                    inputs.settings.min_literal_length))
    return CheckReport(tuple([*findings, *own, *others]), tuple([*not_run, *own_missing, *other_missing]), hardcode)
