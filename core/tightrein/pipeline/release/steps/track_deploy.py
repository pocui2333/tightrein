"""部署跟踪的手动部署提示(architecture/07 19.7)：改动文件匹配 target.manualDeployPaths 时提示用户联系负责人手动部署。
包含合并提交的部署由 pipeline/common/deploys.py 判断。"""

from __future__ import annotations

from collections.abc import Sequence

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.guards.protected import matches_path


def manual_paths(config: ProjectConfig, changed: Sequence[str]) -> list[str]:
    try:
        patterns = config.get("target.manualDeployPaths")
    except MissingSetting:
        return []
    return [path for path in changed if any(matches_path(path, pattern) for pattern in patterns)]
