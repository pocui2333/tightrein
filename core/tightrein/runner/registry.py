"""按工具名取适配器，解析工具、模型与可执行文件(architecture/02 2.5)。

工具与模型的次序：task.tool 与 task.model 优先，其次是命令行的 --runner、--model，再次是各层合并后的
stages.<环节>(project.yaml 覆盖本机用户配置的 agents 段；交互会话取 stages.<环节>.session；没有工具时取 defaultTool)
与能力档映射。任务没有给出能力档时取该角色的模型档
(ProjectConfig.role_capability)；推理强度取任务的 effort，没有时取所选能力档在该工具上的 effort。可执行文件取本机用户配置的 tools.<工具>.path，
没有配置时按 PATH 查找。
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from collections.abc import Callable, Mapping
from pathlib import Path

from tightrein.config.capabilities import CapabilityError, ModelChoice
from tightrein.config.project import ProjectConfig
from tightrein.config.user import UserConfig
from tightrein.runner.adapters.base import Adapter
from tightrein.runner.adapters.agy import AgyAdapter
from tightrein.runner.adapters.claude import ClaudeAdapter
from tightrein.runner.adapters.codex import CodexAdapter
from tightrein.runner.result import RunnerConfigError
from tightrein.runner.task import RunnerTask

SESSION = "session"


def default_adapters(home: Path, clock_skew: timedelta | None = None) -> dict[str, Adapter]:
    """三个工具的适配器；Claude Code 与 Codex CLI 本机保存会话的目录在用户主目录下(agy 不读取会话记录)。
    clock_skew 为查找会话记录时容许的时钟偏差(runtime.runner.sessionClockSkewSeconds)。"""
    return {
        "claude": ClaudeAdapter(home / ".claude"),
        "codex": CodexAdapter(home / ".codex", clock_skew),
        "agy": AgyAdapter(),
    }


class Registry:
    def __init__(self, adapters: Mapping[str, Adapter], user_config: UserConfig,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        self.adapters = dict(adapters)
        self.user_config = user_config
        self.which = which

    def adapter(self, tool: str) -> Adapter:
        if tool not in self.adapters:
            raise RunnerConfigError(f"没有工具 {tool} 的适配器，可用的有 {', '.join(sorted(self.adapters))}")
        return self.adapters[tool]

    def executable(self, tool: str) -> str | None:
        configured = self.user_config.tool_path(tool)
        if configured is not None:
            return str(configured) if configured.exists() else None
        return self.which(tool)


def choose(config: ProjectConfig, task: RunnerTask, *, runner_override: str | None = None,
           model_override: str | None = None) -> ModelChoice:
    part = SESSION if task.interactive and SESSION in config.stage_setting(task.stage) else None
    capability = task.capability or config.role_capability(task.stage, task.role)
    try:
        choice = config.model_choice(task.stage, part, tool=task.tool or runner_override,
                                     model=task.model or model_override, capability=capability)
    except CapabilityError as error:
        raise RunnerConfigError(str(error)) from error
    return replace(choice, effort=task.effort) if task.effort is not None else choice
