"""triage、collect、fix 与 verify 共用的只读执行器任务(architecture/05 2.5、architecture/06 4.4 到 4.11)。

- 上限取 stages.<环节>.<roles|tasks>.<名称>：roles 的上限按任务复杂度分 low、medium、high，tasks 只有一组；
  没有配置的项为空，由执行器取 stages.<环节>.limits；
- 工具与模型不在这里取：任务带上调用点(config.routes.call_point，例如 fix-scout → fix.scout)与条件，由执行器按路由表解析；
- 提示正文取自 skills/<模块>/references/ 下的文件，直接放进任务说明，不引用整份 SKILL.md(那是给用户读的命令说明)；
- 工作目录为只读 worktree，只允许只读 git 命令与文本搜索；
- 写入模式下由调用方给出数据库连接，每次调用之后登记一行环节效益。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.config.routes import call_point
from tightrein.domain.clock import Clock
from tightrein.domain.enums import Access, Complexity, Stage
from tightrein.packaging import third_party
from tightrein.runner import stage_yield
from tightrein.runner.result import RunnerResult
from tightrein.runner.service import Runner
from tightrein.runner.task import Instructions, Limits, RunnerTask, SkillRef, Subject
from tightrein.store.files.layout import ToolLayout, segment

READ_ONLY_COMMANDS = ("git log", "git show", "git blame", "git grep", "rg", "ls", "cat", "head", "wc")
ROLES = "roles"
TASKS = "tasks"

# 各角色任务按需加载的第三方 skill(third_party/skills.lock.yaml)；没有安装时不引用。
DIFFERENTIAL_REVIEW = "differential-review"
VARIANT_ANALYSIS = "variant-analysis"
SHARP_EDGES = "sharp-edges"
FP_CHECK = "fp-check"
SEMGREP_RULE_VARIANT_CREATOR = "semgrep-rule-variant-creator"


@dataclass(frozen=True)
class RoleSetting:
    """角色或任务的调用点、条件与上限。"""

    route: str
    limits: Limits = Limits()
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Overrides:
    """命令行的 --runner 与 --model。"""

    runner: str | None = None
    model: str | None = None


def _optional(config: ProjectConfig, key: str) -> object | None:
    try:
        return config.get(key)
    except MissingSetting:
        return None


def _limits(config: ProjectConfig, key: str) -> Limits:
    found = _optional(config, key)
    return Limits.from_dict(found) if isinstance(found, dict) else Limits()


def setting(config: ProjectConfig, stage: Stage, group: str, name: str, complexity: Complexity | None = None,
            conditions: Sequence[str] = ()) -> RoleSetting:
    """角色或任务的调用点(<环节>.<名称>，名称以环节名开头时去掉这一段)与上限。"""
    base = f"stages.{stage.value}.{group}.{name}"
    limits = _limits(config, f"{base}.limits" if complexity is None else f"{base}.limits.{complexity.value}")
    return RoleSetting(call_point(stage.value, name), limits, tuple(conditions))


def reviewer_setting(config: ProjectConfig, key: str, route: str, conditions: Sequence[str] = ()) -> RoleSetting:
    """评审类设置(stages.fix.review.<light|deep>、stages.verify.screenshotReview)的上限，调用点由调用方给出。"""
    return RoleSetting(route, _limits(config, f"{key}.limits"), tuple(conditions))


def reference(tool: ToolLayout, skill: str, path: str) -> str:
    """skills/<skill>/<path> 的正文；文件不存在时抛出 FileNotFoundError。"""
    parts = [segment(part) for part in Path(path).parts]
    return tool.skill(skill).parent.joinpath(*parts).read_text(encoding="utf-8").strip()


def installed_skills(tool: ToolLayout, names: Sequence[str]) -> tuple[SkillRef, ...]:
    """已安装的 skill；没有安装的不引用。锁定清单中已锁定的第三方 skill 先按清单核对已安装副本(architecture/09 7.2)，
    任一文件哈希不符时抛出 HashMismatch，不启动执行器。"""
    present = [name for name in names if tool.skill(name).is_file()]
    locked = {skill.name: skill for skill in third_party.read_lock(tool.third_party_lock()) if skill.locked}
    issues = [issue for name in present if name in locked
              for issue in third_party.verify(locked[name], tool.skill(name).parent)]
    if issues:
        raise third_party.HashMismatch(issues)
    return tuple(SkillRef(name) for name in present)


def read_only_task(*, run_id: str, stage: Stage, role: str, subject: Subject, attempt: int, prompt: str,
                   workdir: Path, output_schema: str, role_setting: RoleSetting,
                   skills: tuple[SkillRef, ...] = ()) -> RunnerTask:
    return RunnerTask(
        run_id=run_id, stage=stage, role=role, subject=subject, attempt=attempt,
        instructions=Instructions(prompt, skills), workdir=workdir, output_schema=output_schema,
        access=Access.READ_ONLY, allowed_commands=READ_ONLY_COMMANDS, limits=role_setting.limits,
        route=role_setting.route, conditions=role_setting.conditions,
    )


def run(runner: Runner, task: RunnerTask, clock: Clock, overrides: Overrides = Overrides(),
        conn: sqlite3.Connection | None = None, resume_session: str | None = None) -> RunnerResult:
    """conn 给出时(写入模式)在调用之后登记环节效益(design 14.2)；resume_session 见 Runner.run。"""
    if resume_session is None:
        result = runner.run(task, clock=clock, runner_override=overrides.runner, model_override=overrides.model)
    else:
        result = runner.run(task, clock=clock, runner_override=overrides.runner, model_override=overrides.model,
                            resume_session=resume_session)
    if conn is not None:
        stage_yield.record(conn, task, result, clock.now())
    return result


def join(*parts: str) -> str:
    """把各段拼成任务说明；空段省略。"""
    return "\n\n".join(part.strip() for part in parts if part and part.strip()) + "\n"
