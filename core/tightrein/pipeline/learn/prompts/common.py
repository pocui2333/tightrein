"""learn 各执行器任务共用：任务上下文、执行器调用与写入知识所需的依赖(architecture/08 2.1)。

提示正文取自 skills/learn/references/roles/；上限与能力档取 stages.learn.tasks.<角色>(不按复杂度分档)。
工作目录缺省为工作区的 knowledge/ 目录，只读(rule-writer 为只读 worktree，improvement-writer 为本工具仓库)；
来源材料(发现报告、修复报告、出问题的经过、原补丁)直接写进任务说明。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, tzinfo
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import Stage
from tightrein.runner.roles import TASKS, Overrides, RoleSetting, join, read_only_task, reference, run, setting
from tightrein.retrieval.dedup import WriteOrigin
from tightrein.retrieval.service import KnowledgeService
from tightrein.runner.result import RunnerResult
from tightrein.runner.service import Runner
from tightrein.runner.task import RunnerTask, SkillRef, Subject
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

STAGE = Stage.LEARN
SKILL = "learn"
LESSON_WRITER = "lesson-writer"
RULE_WRITER = "rule-writer"
IMPROVEMENT_WRITER = "improvement-writer"
SCHEMAS = {LESSON_WRITER: "runner/roles/lesson-writer.schema.json", RULE_WRITER: "runner/roles/rule-writer.schema.json",
           IMPROVEMENT_WRITER: "runner/roles/improvement-writer.schema.json"}


@dataclass(frozen=True)
class LearnPrompt:
    tool: ToolLayout
    config: ProjectConfig
    run_id: str
    workdir: Path

    def task(self, name: str, role: str, subject: Subject, *parts: str,
             skills: tuple[SkillRef, ...] = (), workdir: Path | None = None) -> RunnerTask:
        """name 为角色说明与配置所用的角色名，role 为任务中的角色(同一运行中同一对象的多次比对以序号区分)；
        workdir 缺省为工作区的 knowledge/ 目录。"""
        prompt = join(reference(self.tool, SKILL, f"references/roles/{name}.md"), *parts)
        found: RoleSetting = setting(self.config, STAGE, TASKS, name)
        return read_only_task(run_id=self.run_id, stage=STAGE, role=role, subject=subject, attempt=1, prompt=prompt,
                              workdir=workdir or self.workdir, output_schema=SCHEMAS[name], role_setting=found,
                              skills=skills)


@dataclass(frozen=True)
class LearnCalls:
    """conn 给出时(写入模式)登记环节效益。"""

    runner: Runner
    clock: Clock
    prompt: LearnPrompt
    overrides: Overrides = Overrides()
    conn: sqlite3.Connection | None = None

    def run(self, task: RunnerTask) -> RunnerResult:
        return run(self.runner, task, self.clock, self.overrides, self.conn)


@dataclass(frozen=True)
class LearnEnv:
    """学习回路各步骤的依赖。--output 模式下 knowledge 为沙箱模式的检索服务，不写知识、不标记幂等键。"""

    conn: sqlite3.Connection
    layout: WorkspaceLayout
    config: ProjectConfig
    clock: Clock
    zone: tzinfo | None
    calls: LearnCalls
    knowledge: KnowledgeService

    @property
    def writable(self) -> bool:
        return self.layout.output_dir is None

    @property
    def run_id(self) -> str:
        return self.calls.prompt.run_id

    def today(self) -> date:
        return local_date(self.clock.now(), self.zone)

    def origin(self, subject: Subject) -> WriteOrigin:
        overrides = self.calls.overrides
        return WriteOrigin(self.run_id, STAGE, subject, overrides.runner, overrides.model)


def section(title: str, text: str) -> str:
    return f"## {title}\n\n{text.strip() or '(无)'}"
