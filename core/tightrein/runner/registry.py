"""按工具名取适配器，解析工具、模型与可执行文件(architecture/02 2.5)。

工具、模型与推理强度按任务的调用点(task.route)与条件在路由表中解析(config.routes：project.yaml 覆盖本机用户配置的
models 与 routes)，命令行的 --runner、--model 改写解析结果。可执行文件取本机用户配置的 tools.<工具>.path，
没有配置时按 PATH 查找。
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from collections.abc import Callable, Mapping
from pathlib import Path

from tightrein.config.routes import ModelChoice, RouteError
from tightrein.config.project import ProjectConfig
from tightrein.config.user import UserConfig
from tightrein.runner.adapters.base import Adapter
from tightrein.runner.adapters.agy import AgyAdapter
from tightrein.runner.adapters.claude import ClaudeAdapter
from tightrein.runner.adapters.codex import CodexAdapter
from tightrein.runner.result import RunnerConfigError
from tightrein.runner.task import RunnerTask



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
    if task.route is None and runner_override is None:
        raise RunnerConfigError(f"任务 {task.stage.value} {task.role} 没有调用点(route)")
    try:
        return config.model_choice(task.route or "default", task.conditions, tool=runner_override,
                                   model=model_override)
    except RouteError as error:
        raise RunnerConfigError(str(error)) from error
