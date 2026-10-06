"""`tightrein project config [--key <键>]`(architecture/01 5.1、architecture/09 4.1)：合成后每个键的生效值与来源层。

层按 core、stack:<名称>(按 stacks 的顺序)、user(agents 段)、project、user(其余个人键)合成，后一层覆盖前一层：
- core：config/defaults.yaml；
- stack:<名称>：extensions/stacks/<名称>/defaults.yaml(没有该文件的技术栈不出现)；
- user 的 agents 段：agent 工具与模型的个人缺省，以生效键名显示(例如 stages.collect.tool、capabilities.light.claude.model、
  defaultTool)，位于 project 之下，项目写了同一个键时来源为 project；
- project：工作区的 project.yaml；
- user 的其余个人键：键名与用户配置文件中的写法相同；network.proxy 中的密码显示为 [已脱敏]；tools.semgrep.path
  同时作为 runtime.tools.semgrep 的值显示(组装根用它取代核心缺省的 Semgrep 命令)。
两类 user 键不重名。映射逐级展开为点分键名；列表与 thresholds 下的 {value, min, max} 可调项作为一个叶子。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.config import layers, network
from tightrein.config.layers import CORE, flatten
from tightrein.config.project import ProjectConfig
from tightrein.config.user import UserConfig

PROJECT = "project"
USER = "user"
SEMGREP = "semgrep"
SEMGREP_COMMAND = "runtime.tools.semgrep"


@dataclass(frozen=True)
class ShownValue:
    key: str
    value: Any
    source: str
    layers: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "source": self.source, "layers": dict(self.layers)}


def _user(user: UserConfig) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if user.branch_prefix is not None:
        found["branchPrefix"] = user.branch_prefix
    if user.default_workspace is not None:
        found["defaultWorkspace"] = str(user.default_workspace)
    if user.notify_method is not None:
        found["notify.method"] = user.notify_method
    for tool, path in user.tools.items():
        found[f"tools.{tool}.path"] = str(path)
    if SEMGREP in user.tools:
        found[SEMGREP_COMMAND] = str(user.tools[SEMGREP])
    for tool, target in user.install_targets.items():
        if target.path is not None:
            found[f"install.targets.{tool}.path"] = str(target.path)
        found[f"install.targets.{tool}.enabled"] = target.enabled
    if user.network_proxy is not None:
        found["network.proxy"] = network.masked_proxy(user.network_proxy)
    if user.no_proxy:
        found["network.noProxy"] = list(user.no_proxy)
    return found


def _within(key: str, wanted: str | None) -> bool:
    return wanted is None or key == wanted or key.startswith(f"{wanted}.")


def show(config: ProjectConfig, user: UserConfig, key: str | None = None) -> list[ShownValue]:
    found = [(CORE, flatten(layers.core_defaults())),
             *((layer.name, flatten(layer.data)) for layer in config.stack_layers),
             (USER, flatten(config.agents)), (PROJECT, flatten(config.data)), (USER, _user(user))]
    keys = sorted({name for _, values in found for name in values if _within(name, key)})
    shown = []
    for name in keys:
        present: dict[str, Any] = {}
        for layer, values in found:
            if name in values:
                present.pop(layer, None)
                present[layer] = values[name]
        source = list(present)[-1]
        shown.append(ShownValue(name, present[source], source, present if key is not None else {}))
    return shown
