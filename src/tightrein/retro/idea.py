"""新记录的简单解决思路(retro/README.md「解决思路」)：只在新建记录时调用一次模型(调用点 retro.idea)。

发现、合并、评级都由程序做；模型只写一两句解决思路，以及值不值得沉淀为经验或规则。
调用失败不影响记录的新建：解决思路留空，原因记进复盘交接的 errors。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.agents.call import AgentContext, call, params_for
from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult
from tightrein.prompts.build import build
from tightrein.protocol.handoff import load_schema
from tightrein.retro.records import KIND_LABELS, Record

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = "retro.idea"
SCHEMA_PATH = Path(__file__).with_name("idea.schema.json")

Invoke = Callable[[CallParams, AgentContext], CallResult]


@dataclass(frozen=True)
class Idea:
    idea: str | None
    settle: str | None
    result: CallResult


def write_idea(runtime: Runtime, record: Record, *, invoke: Invoke = call) -> Idea:
    schema = load_schema(SCHEMA_PATH)
    variables = {
        "kind": KIND_LABELS[record.kind],
        "point": record.point,
        "call_point": record.call_point,
        "fact": record.fact,
        "details": "\n".join(f"- {detail}" for item in record.occurrences for detail in item.details),
    }
    model = runtime.settings.model_for(POINT)
    prompt = build(POINT, variables, language=runtime.language, schema=schema, tool=model.tool)
    workdir = runtime.workspace.retro_dir
    workdir.mkdir(parents=True, exist_ok=True)
    params = params_for(POINT, settings=runtime.settings, run=runtime.run, subject=None, prompt=prompt.text,
                        schema=schema, workdir=workdir, prompt_hash=prompt.hash)
    result = invoke(params, runtime.agents)
    if not result.ok or result.output is None:
        return Idea(None, None, result)
    settle = result.output.get("settle")
    return Idea(str(result.output["idea"]).strip(), str(settle).strip() if settle else None, result)
