"""评估各调用点共用的部分：拼提示(prompts/<控制键>.md)、按调用点取参数、调用模型、累计量化数据。

提示文字全在模板里；这里只给变量的值。输出格式(schema)放在评估的文件夹：取证与证伪复核共用
`assess.triage.schema.json`(复核是盲审，输出与取证完全相同)，查重用 `assess.dedup.schema.json`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.agents.call import call, params_for
from tightrein.agents.result import CallResult
from tightrein.prompts.build import build
from tightrein.protocol.handoff import Metrics, Tokens, load_schema

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

ASSESS_DIR = Path(__file__).resolve().parents[1]
SCHEMAS = {"assess.triage": "assess.triage.schema.json", "assess.refute": "assess.triage.schema.json",
           "assess.dedup": "assess.dedup.schema.json"}
NONE = "无"


@dataclass(frozen=True)
class Asked:
    point: str
    output: dict[str, Any] | None
    result: CallResult
    prompt_hash: str

    @property
    def ok(self) -> bool:
        return self.result.ok and self.output is not None

    @property
    def failure(self) -> str:
        detail = f"：{self.result.error}" if self.result.error else ""
        return f"执行没有完成({self.result.status.value}{detail})"


@dataclass
class Tally:
    """一个问题在评估中各次调用的量化数据，交接时写进 Metrics。"""

    calls: int = 0
    retries: int = 0
    rounds: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False
    tokens: Tokens = field(default_factory=Tokens)
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)

    def add(self, asked: Asked) -> None:
        result = asked.result
        self.calls += result.attempts
        self.retries += result.retries
        self.duration_ms += result.duration_ms
        self.cost_usd += result.cost_usd or 0.0
        self.cost_estimated = self.cost_estimated or result.cost_estimated
        self.tokens.add(result.tokens)
        self.prompt_hashes[asked.point] = asked.prompt_hash
        self.models[asked.point] = f"{result.tool}/{result.model}"

    def metrics(self, duration_ms: int) -> Metrics:
        return Metrics(duration_ms=duration_ms, calls=self.calls, retries=self.retries, rounds=self.rounds or None,
                       tokens=self.tokens, cost_usd=round(self.cost_usd, 4), cost_estimated=self.cost_estimated)


@cache
def schema_for(point: str) -> dict[str, Any]:
    return load_schema(ASSESS_DIR / SCHEMAS[point])


def ask(runtime: Runtime, point: str, variables: Mapping[str, str], *, subject: str, workdir: Path,
        round: int | None = None) -> Asked:
    schema = schema_for(point)
    model = runtime.settings.model_for(point)
    prompt = build(point, variables, language=runtime.language, schema=schema, tool=model.tool)
    params = params_for(point, settings=runtime.settings, run=runtime.run, subject=subject, prompt=prompt.text,
                        schema=schema, workdir=workdir, round=round, prompt_hash=prompt.hash)
    result = call(params, runtime.agents)
    output = result.output if result.ok else None
    return Asked(point, output, result, prompt.hash)


def feedback_text(items: Sequence[str]) -> str:
    """「需要处理的问题」：上一次没通过的逐项原因；没有时为「无」。"""
    return "\n".join(f"- {item}" for item in items) if items else NONE


def text_or_none(value: str | None) -> str:
    return value if value and value.strip() else NONE
