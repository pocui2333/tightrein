"""采集的执行器任务(architecture/05 2.5)：静态巡检的增量审查、基线审查、全量扫描、候选主张的取证，以及接口描述
没有自动导出时起草接口描述(redesign/01-collect.md 第 5 节)。

各类任务的工作目录都是只读 worktree，只允许只读 git 命令与文本搜索，上限取 stages.collect.tasks.<任务>，
模型按调用点 collect.<任务> 的路由。
会话记录按「角色-运行编号」命名：基线审查的角色带批次序号，全量扫描的角色带模式编号，取证的角色带主张序号，
同一次运行中互不覆盖。
取证只给主张与位置，不给审查过程。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import Stage
from tightrein.runner.roles import (
    DIFFERENTIAL_REVIEW,
    SHARP_EDGES,
    TASKS,
    VARIANT_ANALYSIS,
    installed_skills,
    join,
    read_only_task,
    reference,
    setting,
)
from tightrein.pipeline.triage.prompts.common import severity_text
from tightrein.sources.static.baseline import Batch
from tightrein.sources.static.reviewer import Claim, ToolFinding
from tightrein.sources.static.scope import Scope
from tightrein.runner.task import RunnerTask, Subject
from tightrein.store.files.layout import ToolLayout

STAGE = Stage.COLLECT
REVIEW = "static-review"
BASELINE = "baseline-review"
VARIANT = "variant-scan"
VERIFY = "claim-verifier"
SPEC_DRAFTER = "spec-drafter"
REVIEW_SCHEMA = "runner/roles/static-review.schema.json"
VERIFY_SCHEMA = "runner/roles/claim-verifier.schema.json"
SPEC_SCHEMA = "runner/roles/spec-drafter.schema.json"


@dataclass(frozen=True)
class TaskContext:
    tool: ToolLayout
    config: ProjectConfig
    run_id: str
    workdir: Path

    @property
    def subject(self) -> Subject:
        return Subject("run", self.run_id)


def _scope_text(scope: Scope) -> str:
    changed = "\n".join(f"- {path}" for path in scope.changed_files) or "- (无)"
    return (f"## 扫描范围\n\n档位 {scope.level.value}，起点 {scope.base_commit or '(第一次巡检，全部文件)'}，"
            f"终点 {scope.head}。\n\n改动的文件：\n{changed}")


def review_task(ctx: TaskContext, scope: Scope, findings: Sequence[ToolFinding], knowledge: str) -> RunnerTask:
    items = json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2)
    prompt = join(reference(ctx.tool, "collect", "references/roles/static-review.md"), _scope_text(scope),
                  f"## 确定性工具的结果\n\n```json\n{items}\n```", f"## 相关知识\n\n{knowledge}")
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=REVIEW, subject=ctx.subject, attempt=1,
                          prompt=prompt, workdir=ctx.workdir, output_schema=REVIEW_SCHEMA,
                          role_setting=setting(ctx.config, STAGE, TASKS, REVIEW),
                          skills=installed_skills(ctx.tool, (DIFFERENTIAL_REVIEW, SHARP_EDGES)))


def baseline_task(ctx: TaskContext, batch: Batch, findings: Sequence[ToolFinding], knowledge: str) -> RunnerTask:
    files = "\n".join(f"- `{item.path}`({item.lines} 行)" for item in batch.files)
    items = json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2)
    prompt = join(reference(ctx.tool, "collect", "references/roles/baseline-review.md"),
                  f"## 本批文件\n\n{batch.describe()}，模块 `{batch.module}`：\n\n{files}",
                  f"## 确定性工具的结果\n\n```json\n{items}\n```", f"## 相关知识\n\n{knowledge}")
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=f"{BASELINE}-{batch.number}", subject=ctx.subject,
                          attempt=1, prompt=prompt, workdir=ctx.workdir, output_schema=REVIEW_SCHEMA,
                          role_setting=setting(ctx.config, STAGE, TASKS, BASELINE),
                          skills=installed_skills(ctx.tool, (SHARP_EDGES,)))


def variant_task(ctx: TaskContext, pattern_id: str, pattern: str, tradeoffs: str) -> RunnerTask:
    prompt = join(reference(ctx.tool, "collect", "references/roles/variant-scan.md"),
                  f"## 缺陷模式 {pattern_id}\n\n{pattern}", f"## 已接受的取舍\n\n{tradeoffs}")
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=f"{VARIANT}-{pattern_id.lower()}",
                          subject=ctx.subject, attempt=1, prompt=prompt, workdir=ctx.workdir,
                          output_schema=REVIEW_SCHEMA, role_setting=setting(ctx.config, STAGE, TASKS, VARIANT),
                          skills=installed_skills(ctx.tool, (VARIANT_ANALYSIS,)))


def verify_task(ctx: TaskContext, claim: Claim, number: int) -> RunnerTask:
    stated = (f"## 主张\n\n`{claim.file}:{claim.line}` 存在以下问题({claim.rule_or_pattern})：{claim.statement}\n\n"
              f"触发条件：{claim.trigger}\n\n入口：`{claim.file}:{claim.line}`")
    prompt = join(reference(ctx.tool, "triage", "references/roles/claim-verifier.md"),
                  reference(ctx.tool, "triage", "references/evidence-standard.md"),
                  severity_text(ctx.tool, ctx.config), stated)
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=f"{VERIFY}-{number}", subject=ctx.subject,
                          attempt=1, prompt=prompt, workdir=ctx.workdir, output_schema=VERIFY_SCHEMA,
                          role_setting=setting(ctx.config, STAGE, TASKS, VERIFY))


def spec_draft_task(ctx: TaskContext, base_url: str | None) -> RunnerTask:
    """在只读 worktree 中读路由与控制器代码，起草 OpenAPI 3 接口描述；结果交用户确认后才登记(接入清单「接口描述」)。"""
    target = f"被测地址：{base_url}" if base_url else "被测地址：未配置"
    prompt = join(reference(ctx.tool, "collect", "references/roles/spec-drafter.md"), f"## 项目\n\n{target}")
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=SPEC_DRAFTER, subject=ctx.subject, attempt=1,
                          prompt=prompt, workdir=ctx.workdir, output_schema=SPEC_SCHEMA,
                          role_setting=setting(ctx.config, STAGE, TASKS, SPEC_DRAFTER))
