"""评分表的加载、校验与渲染(architecture/03 2.3.3，design 12.2)。

evaluation/rubrics/<环节>.json 是 12.2 评分表的机读形式(与 case.json、manifest.json 同为 JSON)，每一项：
- id：评分项编号，以「<环节>.」开头，不重复；
- text：评分项原文；生成者的提示中作为验收标准(不出现评分的说法与评分方式)，评审者的提示中作为评审项；
- method：code 或 judge；12.2 中写「代码加独立评审」的项拆成两个评分项；
- scorer：code 项在注册表中的名称，judge 项不写；
- appliesWhen：可选，适用条件 {path, op, value}，与期望断言的写法相同；不满足时该项记为不适用；
- params：可选，评分器参数。
加载时任何一项不合格抛出 RubricInvalid。条目表放在 evaluation/ 内，不在任何 agent 的可写范围内，改进建议的补丁也不能触及。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tightrein.domain.enums import ScoreMethod, Stage
from tightrein.evaluation.errors import RubricInvalid
from tightrein.evaluation.scorers.code import REGISTRY

RUBRICS_DIR = Path(__file__).parent / "rubrics"
SUFFIX = ".json"
ITEM_KEYS = frozenset({"id", "text", "method", "scorer", "appliesWhen", "params"})
CONDITION_OPS = frozenset({"equals", "in", "contains", "matches", "exists", "absent"})
INTRO = {
    "generator": "交付的结果须满足下面的验收标准：",
    "judge": "按下面的评审项逐项独立判断；每一项都是硬门槛。",
}

Audience = Literal["generator", "judge"]


@dataclass(frozen=True)
class Condition:
    path: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class RubricItem:
    id: str
    text: str
    method: ScoreMethod
    scorer: str | None = None
    applies_when: Condition | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rubric:
    stage: Stage
    items: tuple[RubricItem, ...]

    def ids(self) -> set[str]:
        return {item.id for item in self.items}

    def by_method(self, method: ScoreMethod) -> list[RubricItem]:
        return [item for item in self.items if item.method is method]


def _item(stage: Stage, index: int, data: Any, source: Path) -> RubricItem:
    where = f"{source.name} 第 {index + 1} 项"
    if not isinstance(data, Mapping):
        raise RubricInvalid(f"{where} 不是映射")
    unknown = sorted(set(data) - ITEM_KEYS)
    missing = sorted({"id", "text", "method"} - set(data))
    if unknown or missing:
        raise RubricInvalid(f"{where}：缺少 {missing}，不认识 {unknown}")
    if not str(data["id"]).startswith(f"{stage.value}."):
        raise RubricInvalid(f"{where}：编号 {data['id']} 须以 {stage.value}. 开头")
    try:
        method = ScoreMethod(data["method"])
    except ValueError:
        raise RubricInvalid(f"{where}：method 只能是 code 或 judge") from None
    scorer = data.get("scorer")
    if method is ScoreMethod.CODE and scorer not in REGISTRY:
        raise RubricInvalid(f"{where}：code 项的评分器 {scorer} 没有注册")
    if method is ScoreMethod.JUDGE and scorer is not None:
        raise RubricInvalid(f"{where}：judge 项不写 scorer")
    if method is ScoreMethod.USER:
        raise RubricInvalid(f"{where}：method 只能是 code 或 judge")
    condition = data.get("appliesWhen")
    if condition is not None:
        if not isinstance(condition, Mapping) or condition.get("op") not in CONDITION_OPS or "path" not in condition:
            raise RubricInvalid(f"{where}：appliesWhen 须为 {{path, op, value}}")
        condition = Condition(condition["path"], condition["op"], condition.get("value"))
    return RubricItem(str(data["id"]), str(data["text"]), method, scorer, condition, dict(data.get("params") or {}))


def load(stage: Stage, directory: Path = RUBRICS_DIR) -> Rubric:
    source = directory / f"{stage.value}{SUFFIX}"
    if not source.is_file():
        raise RubricInvalid(f"没有 {stage.value} 的评分表：{source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RubricInvalid(f"{source.name} 不是合法的 JSON：{error}") from error
    if not isinstance(data, Mapping) or data.get("stage") != stage.value or not isinstance(data.get("items"), list):
        raise RubricInvalid(f"{source.name} 须为 {{stage: {stage.value}, items: [...]}}")
    items = tuple(_item(stage, index, item, source) for index, item in enumerate(data["items"]))
    ids = [item.id for item in items]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        raise RubricInvalid(f"{source.name} 中评分项编号重复：{'、'.join(duplicated)}")
    return Rubric(stage, items)


def item_ids(stage: Stage) -> set[str]:
    """供用例校验检查 excludeItems；没有评分表的环节没有评分项。"""
    source = RUBRICS_DIR / f"{stage.value}{SUFFIX}"
    return load(stage).ids() if source.is_file() else set()


def render(rubric: Rubric, audience: Audience) -> str:
    """写进提示的条目：生成者看到验收标准(只有条目文字，评分方式与结果只给系统与用户看)；评审者看到带编号与判定方式的
    评审项，对其中的评审(judge)项逐项给出结论。"""
    lines = [INTRO[audience], ""]
    if audience == "generator":
        lines += [f"- {item.text}" for item in rubric.items]
    else:
        lines += [f"- [{item.id}]({item.method.label}) {item.text}" for item in rubric.items]
    return "\n".join(lines) + "\n"
