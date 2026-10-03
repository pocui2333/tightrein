"""模型能力档到各工具具体模型的映射与模型价格(architecture/01 5.2 的 capabilities 段，architecture/02 2.5、2.7)。

`capabilities.<能力档>.<工具>` 给出该档在该工具上的模型、推理强度(可省略)与每百万输入、输出 token 的价格。选择模型的次序：
显式给出的模型优先；其次是显式给出的能力档；再次是 `stages.<环节>` 中的模型(只在工具没有被改写时沿用)与能力档；
都没有时模型为空，由工具使用自己的缺省模型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TOKENS_PER_PRICE_UNIT = 1_000_000
NO_TOOL = ("没有配置工具：在本机用户配置 ~/.config/tightrein/config.yaml 中写 agents.defaultTool"
           "(例如 `agents: {defaultTool: claude}`)，或在 project.yaml 中写 defaultTool 或这个键")


class CapabilityError(LookupError):
    """能力档或工具的配置不完整；key 为出问题的完整键名。"""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        total = input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok
        return total / TOKENS_PER_PRICE_UNIT


@dataclass(frozen=True)
class ModelChoice:
    """选定的工具与模型；model 为空表示使用工具自己的缺省模型。capability 记录模型取自哪个能力档，effort 为该档在
    该工具上的推理强度，显式给出模型或档中没有写时为空。"""

    tool: str
    model: str | None
    capability: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class _Entry:
    model: str
    effort: str | None = None


class Capabilities:
    """各层合并后的 capabilities 段(本机用户配置的 agents.capabilities 与 project.yaml)。同一工具的同一模型在不同能力档中价格不同时视为配置错误。"""

    def __init__(self, data: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._prices: dict[tuple[str, str], ModelPrice] = {}
        for capability, tools in (data or {}).items():
            for tool, item in tools.items():
                price = ModelPrice(float(item["inputUsdPerMTok"]), float(item["outputUsdPerMTok"]))
                known = self._prices.get((tool, item["model"]))
                if known is not None and known != price:
                    raise CapabilityError(
                        f"capabilities.{capability}.{tool}", f"模型 {item['model']} 的价格与其他能力档中的不一致"
                    )
                self._entries[(capability, tool)] = _Entry(item["model"], item.get("effort"))
                self._prices[(tool, item["model"])] = price

    def _entry(self, capability: str, tool: str) -> _Entry:
        entry = self._entries.get((capability, tool))
        if entry is None:
            raise CapabilityError(f"capabilities.{capability}.{tool}", f"能力档 {capability} 没有为工具 {tool} 配置模型")
        return entry

    def model(self, capability: str, tool: str) -> str:
        return self._entry(capability, tool).model

    def resolve(self, tool: str, model: str | None = None, capability: str | None = None) -> ModelChoice:
        """model 给出时直接使用；否则按能力档查映射；两者都没有时 model 为空。"""
        if model is not None:
            return ModelChoice(tool, model)
        if capability is not None:
            entry = self._entry(capability, tool)
            return ModelChoice(tool, entry.model, capability, entry.effort)
        return ModelChoice(tool, None)

    def choose(
        self,
        setting: Mapping[str, Any],
        key: str,
        *,
        tool: str | None = None,
        model: str | None = None,
        capability: str | None = None,
    ) -> ModelChoice:
        """按 `stages.<环节>`(或其中的 review、refuter、session)与显式覆盖选择工具和模型；key 为 setting 的完整键名。

        工具被改写为另一个工具时，setting 中的模型属于原工具，不再沿用，改用 setting 的能力档在新工具上的映射。
        """
        configured_tool = setting.get("tool")
        chosen_tool = tool or configured_tool
        if chosen_tool is None:
            raise CapabilityError(f"{key}.tool", NO_TOOL)
        if model is not None or capability is not None:
            return self.resolve(chosen_tool, model, capability)
        if chosen_tool == configured_tool and setting.get("model") is not None:
            return self.resolve(chosen_tool, setting["model"])
        return self.resolve(chosen_tool, capability=setting.get("capability"))

    def price(self, tool: str, model: str) -> ModelPrice | None:
        return self._prices.get((tool, model))

    def estimate_cost(self, tool: str, model: str | None, input_tokens: int, output_tokens: int) -> float | None:
        """工具不返回费用时按价格估算；模型为空或没有价格时返回 None。"""
        if model is None:
            return None
        price = self.price(tool, model)
        return None if price is None else price.cost(input_tokens, output_tokens)
