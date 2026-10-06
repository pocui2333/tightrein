"""triage 各执行器任务共用的部分：任务上下文、验收标准与重做说明的拼装、执行器调用。

任务说明依次为：角色或任务说明(skills/triage/references/)、取证底线、严重度标准与 Issue 报告的写法(取证角色；
项目的 triage.severityGuide 与标题上限 thresholds.issue.titleMaxLength 附在其后)、验收标准(分诊评分表的条目文字，
不出现评分的说法)、主张与事实、预取的相关知识、上一次未通过的原因。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import Complexity, Stage
from tightrein.evaluation import rubric
from tightrein.runner.roles import Overrides, RoleSetting, reference, run, setting
from tightrein.runner.result import RunnerResult
from tightrein.runner.service import Runner
from tightrein.runner.task import RunnerTask
from tightrein.store.files.layout import ToolLayout

STAGE = Stage.TRIAGE
SKILL = "triage"
EVIDENCE_STANDARD = "references/evidence-standard.md"
SEVERITY = "references/severity.md"


@dataclass(frozen=True)
class PromptContext:
    tool: ToolLayout
    config: ProjectConfig
    run_id: str
    workdir: Path

    def text(self, path: str) -> str:
        return reference(self.tool, SKILL, path)

    def role_setting(self, group: str, name: str, complexity: Complexity | None = None) -> RoleSetting:
        return setting(self.config, STAGE, group, name, complexity)


@dataclass(frozen=True)
class RoleCalls:
    """一次分诊运行中调用执行器所需的全部依赖；retries 为证据检查不通过时的重做次数；conn 给出时登记环节效益。"""

    runner: Runner
    clock: Clock
    context: PromptContext
    retries: int
    overrides: Overrides = Overrides()
    conn: sqlite3.Connection | None = None

    def run(self, task: RunnerTask) -> RunnerResult:
        return run(self.runner, task, self.clock, self.overrides, self.conn)


def severity_text(tool: ToolLayout, config: ProjectConfig) -> str:
    """严重度标准与 Issue 报告的写法，附项目说明与标题上限；分诊与静态巡检的取证共用。"""
    guide = config.get("triage.severityGuide")
    limit = config.whole_threshold("issue.titleMaxLength")
    parts = [reference(tool, SKILL, SEVERITY), f"`report.title` 不超过 {limit} 个字符。"]
    if isinstance(guide, str) and guide.strip():
        parts.append(f"## 项目说明\n\n{guide.strip()}")
    return "\n\n".join(parts)


def rubric_text() -> str:
    return "## 验收标准\n\n" + rubric.render(rubric.load(STAGE), "generator")


def knowledge_text(knowledge: str) -> str:
    return f"## 相关知识\n\n{knowledge}"


def feedback_text(feedback: Sequence[str]) -> str:
    if not feedback:
        return ""
    return "\n".join(["## 需要处理的问题", "", "上一次的输出没有通过下面的检查，或者有需要重新核实的事实：",
                      *(f"- {item}" for item in feedback)])
