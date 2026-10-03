"""由 project.yaml、任务与工作区布局推出本次的检查范围(architecture/02 3.2、3.5、3.8)。

- protectedPaths、protectedPatterns、testPaths 与项目有关，未配置时为空；skipMarkers 与 credentialFiles 的通用缺省值
  在 config/defaults.yaml 中，credentialFiles 各层依次追加(不能去掉缺省的模式)；改动量上限取 thresholds.change.maxFiles、
  thresholds.change.maxLines(不计 testPaths 匹配的文件)；交付规则的残留模式取 checks.residuePatterns；复现检查字面量的最短长度取
  runtime.guards.minLiteralLength。没有工作区配置时(测试与评测的缺省)各项取核心缺省值。
- 工作区中 agent 不可写的路径：evals/、regressions/、project.yaml、normalize.yaml、suppressions.yaml，以及本工具仓库的
  skills/、core/；它们不做权限锁定，只在前后快照中比较。快照时跳过虚拟环境、缓存与依赖目录。
- 隐藏路径：工作区的 regressions/ 与 evals/，agent 不得读取。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tightrein.config import layers
from tightrein.config.project import ProjectConfig
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

TOOL_FORBIDDEN_DIRS = ("skills", "core")
SKIPPED_DIRS = frozenset({".venv", "__pycache__", ".pytest_cache", "node_modules", ".git"})
SKIPPED_SUFFIXES = (".egg-info",)


@dataclass(frozen=True)
class GuardSettings:
    protected_paths: tuple[str, ...] = ()
    protected_patterns: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ()
    skip_markers: tuple[str, ...] = field(default_factory=lambda: tuple(layers.core_value("skipMarkers")))
    credential_files: tuple[str, ...] = field(default_factory=lambda: tuple(layers.core_value("credentialFiles")))
    max_files: int = field(default_factory=lambda: int(layers.core_value("thresholds.change.maxFiles")["value"]))
    max_lines: int = field(default_factory=lambda: int(layers.core_value("thresholds.change.maxLines")["value"]))
    residue_patterns: tuple[str, ...] = ()
    min_literal_length: int = field(default_factory=lambda: int(layers.core_value("runtime.guards.minLiteralLength")))

    @classmethod
    def from_config(cls, config: ProjectConfig) -> GuardSettings:
        data = config.data
        return cls(
            protected_paths=tuple(data.get("protectedPaths", ())),
            protected_patterns=tuple(data.get("protectedPatterns", ())),
            test_paths=tuple(data.get("testPaths", ())),
            skip_markers=tuple(config.get("skipMarkers")),
            credential_files=config.appended("credentialFiles"),
            max_files=config.whole_threshold("change.maxFiles"),
            max_lines=config.whole_threshold("change.maxLines"),
            residue_patterns=tuple(config.get("checks.residuePatterns")),
            min_literal_length=int(config.get("runtime.guards.minLiteralLength")),
        )


def forbidden_paths(layout: WorkspaceLayout, tool: ToolLayout) -> tuple[Path, ...]:
    return (
        layout.evals_dir(), layout.regressions_dir(), layout.project_config(), layout.normalize_rules(),
        layout.suppressions(), *(tool.root / name for name in TOOL_FORBIDDEN_DIRS),
    )


def hidden_paths(layout: WorkspaceLayout) -> tuple[Path, ...]:
    return (layout.regressions_dir(), layout.evals_dir())


def skipped(parts: Sequence[str]) -> bool:
    return any(part in SKIPPED_DIRS or part.endswith(SKIPPED_SUFFIXES) for part in parts)
