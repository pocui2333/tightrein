"""fix 各执行器任务共用的部分(architecture/07 4.4 到 4.10)：任务上下文、修复规则与验收标准、执行器调用。

任务说明依次为：角色说明(skills/fix/references/roles/)、修复规则与验收标准(修复评分表的条目文字，不出现评分的说法；评审者看到同样的条目作为评审项)、Issue 各节、
本角色需要的其他材料。上限取 stages.fix.roles.<角色>(按复杂度)，评审取 stages.fix.review.<light|deep>，截图评审取
stages.verify.screenshotReview；模型按调用点(fix.scout、fix.planner 等)的路由，核心不写死。高风险条件(high-risk)按
计划中的风险判定(risk_conditions)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.config.routes import HIGH_RISK
from tightrein.domain.clock import Clock
from tightrein.domain.enums import Complexity, FixRiskLevel, Stage
from tightrein.evaluation import rubric
from tightrein.runner.roles import ROLES, Overrides, RoleSetting, reference, run, setting
from tightrein.runner.result import RunnerResult
from tightrein.runner.service import Runner
from tightrein.runner.task import RunnerTask, Subject
from tightrein.store.files.layout import ToolLayout

STAGE = Stage.FIX
SKILL = "fix"
RULES = "references/fix-rules.md"
SUBJECT = "issue"


def json_block(title: str, value: Any) -> str:
    return f"## {title}\n\n```json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```"


def decisions_text(decisions: str) -> str:
    """「用户的决定与补充」一节；没有记录时为空。"""
    return f"## 用户的决定与补充\n\n{decisions}" if decisions else ""


def risk_conditions(plan: Mapping[str, Any] | None) -> tuple[str, ...]:
    """计划的风险判定为高风险时带 high-risk 条件；没有计划或没有判定时不带。"""
    risk = (plan or {}).get("risk") or {}
    return (HIGH_RISK,) if risk.get("level") == FixRiskLevel.HIGH.value else ()


def feedback_text(feedback: Sequence[str]) -> str:
    if not feedback:
        return ""
    return "\n".join(["## 需要处理的问题", "", "上一次的输出没有通过下面的检查：", *(f"- {item}" for item in feedback)])


@dataclass(frozen=True)
class FixPrompt:
    tool: ToolLayout
    config: ProjectConfig
    run_id: str
    workdir: Path

    def text(self, path: str) -> str:
        return reference(self.tool, SKILL, path)

    def role(self, name: str) -> str:
        return self.text(f"references/roles/{name}.md")

    def role_setting(self, name: str, complexity: Complexity, conditions: Sequence[str] = ()) -> RoleSetting:
        return setting(self.config, STAGE, ROLES, name, complexity, conditions)

    def rules(self) -> str:
        return f"{self.text(RULES)}\n\n## 验收标准\n\n{rubric.render(rubric.load(STAGE), 'generator')}"

    def subject(self, issue_id: str) -> Subject:
        return Subject(SUBJECT, issue_id)


@dataclass(frozen=True)
class FixCalls:
    runner: Runner
    clock: Clock
    prompt: FixPrompt
    overrides: Overrides = Overrides()
    conn: sqlite3.Connection | None = None

    def run(self, task: RunnerTask, resume_session: str | None = None) -> RunnerResult:
        """conn 给出时(写入模式)登记环节效益；resume_session 给出时续接该会话(同一会话的下一轮)。"""
        return run(self.runner, task, self.clock, self.overrides, self.conn, resume_session)
