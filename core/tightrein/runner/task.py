"""执行器任务(architecture/02 2.3，design 9.4)，与 runner/runner-task.schema.json 互转。

limits 中为空的项在执行时取 project.yaml 中该环节的值(Limits.filled)；工具与模型由 route(调用点)与 conditions
按路由表解析(config.routes)，命令行的 --runner、--model 可以改写。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.enums import Access, Stage

SCHEMA = "runner/runner-task.schema.json"


@dataclass(frozen=True)
class Subject:
    type: str
    id: str


@dataclass(frozen=True)
class SkillRef:
    name: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextItem:
    id: str
    type: str
    summary: str
    path: str
    reason: str


@dataclass(frozen=True)
class Instructions:
    prompt: str
    skills: tuple[SkillRef, ...] = ()
    context: tuple[ContextItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "skills": [{"name": skill.name, "references": list(skill.references)} for skill in self.skills],
            "context": [
                {"id": item.id, "type": item.type, "summary": item.summary, "path": item.path, "reason": item.reason}
                for item in self.context
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Instructions:
        return cls(
            data["prompt"],
            tuple(SkillRef(skill["name"], tuple(skill["references"])) for skill in data["skills"]),
            tuple(ContextItem(**item) for item in data["context"]),
        )


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    max_duration_ms: int | None = None
    max_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"maxTurns": self.max_turns, "maxDurationMs": self.max_duration_ms, "maxCostUsd": self.max_cost_usd}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Limits:
        return cls(data.get("maxTurns"), data.get("maxDurationMs"), data.get("maxCostUsd"))

    def filled(self, defaults: Limits) -> Limits:
        """为空的项取 defaults 中的值。"""
        return Limits(
            self.max_turns if self.max_turns is not None else defaults.max_turns,
            self.max_duration_ms if self.max_duration_ms is not None else defaults.max_duration_ms,
            self.max_cost_usd if self.max_cost_usd is not None else defaults.max_cost_usd,
        )


@dataclass(frozen=True)
class RunnerTask:
    run_id: str
    stage: Stage
    role: str
    subject: Subject
    attempt: int
    instructions: Instructions
    workdir: Path
    output_schema: str | None
    access: Access
    allowed_commands: tuple[str, ...] = ()
    limits: Limits = field(default_factory=Limits)
    interactive: bool = False
    route: str | None = None
    conditions: tuple[str, ...] = ()
    approved_protected_paths: tuple[str, ...] = ()
    web: bool = False
    read_paths: tuple[str, ...] = ()
    # 写复现测试的任务：只允许改动测试文件(testPaths)，第二层边界检查据此放行测试文件、拒绝其他改动
    tests_only: bool = False

    @property
    def subject_id(self) -> str:
        return self.subject.id

    @property
    def readonly(self) -> bool:
        return self.access is Access.READ_ONLY

    def with_limits(self, limits: Limits) -> RunnerTask:
        return replace(self, limits=limits)

    def to_dict(self) -> dict[str, Any]:
        """按 schema 校验后返回；不合格时抛出 SchemaValidationError。"""
        data = {
            "runId": self.run_id, "stage": self.stage.value, "role": self.role,
            "subject": {"type": self.subject.type, "id": self.subject.id}, "attempt": self.attempt,
            "instructions": self.instructions.to_dict(), "workdir": str(self.workdir),
            "outputSchema": self.output_schema, "access": self.access.value,
            "allowedCommands": list(self.allowed_commands), "limits": self.limits.to_dict(),
            "interactive": self.interactive, "route": self.route, "conditions": list(self.conditions),
            "approvedProtectedPaths": list(self.approved_protected_paths), "web": self.web,
            "readPaths": list(self.read_paths), "testsOnly": self.tests_only,
        }
        validate.check(SCHEMA, data)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunnerTask:
        validate.check(SCHEMA, data)
        return cls(
            run_id=data["runId"], stage=Stage(data["stage"]), role=data["role"],
            subject=Subject(data["subject"]["type"], data["subject"]["id"]), attempt=data["attempt"],
            instructions=Instructions.from_dict(data["instructions"]), workdir=Path(data["workdir"]),
            output_schema=data["outputSchema"], access=Access(data["access"]),
            allowed_commands=tuple(data["allowedCommands"]), limits=Limits.from_dict(data["limits"]),
            interactive=data["interactive"], route=data["route"], conditions=tuple(data["conditions"]),
            approved_protected_paths=tuple(data["approvedProtectedPaths"]), web=data["web"],
            read_paths=tuple(data["readPaths"]), tests_only=data.get("testsOnly", False),
        )
