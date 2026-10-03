"""静态巡检的审查接口与数据类(architecture/04 5.4)。

Reviewer 的实现在流水线模块 collect 中(经 runner 以只读任务调用 agent)，探针只依赖这里的协议：
- review：在只读 worktree 中审查扫描范围，筛查确定性工具的结果，输出 runner/roles/static-review.schema.json 的候选主张；
- review_baseline：基线审查的一批，审查这批文件的现有代码本身(不看 diff)，输出同上，主张带严重度；
- defect_patterns：全量扫描使用的缺陷模式编号；
- scan_variants：以一个缺陷模式为种子在全仓库查找同类实例，输出同上；
- verify：只给主张与位置，由 claim-verifier 取证，输出 runner/roles/claim-verifier.schema.json。
每次调用的执行器状态随结果返回：guard-violation 带出违规种类，说明环境已不可信的(只读 worktree 被改动、git 状态改变、
不可写路径被修改、凭证或 git 状态读取问题，见 ENVIRONMENT_VIOLATIONS)使整次巡检失败，其余只作废该次调用的产出；
limit-reached 视为预算用尽，其余非 ok 状态使该次调用的主张不产出信号；审查结果另带该次调用的耗时与费用，供运行摘要汇总。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from tightrein.domain.enums import RunnerStatus, Verdict, ViolationKind
from tightrein.sources.static.baseline import Batch
from tightrein.sources.static.scope import Scope

# 主张的严重度(runner/roles/static-review.schema.json)，从高到低；基线审查超出取证上限时按它保留
SEVERITIES = ("high", "medium", "low")
# 说明环境已不可信(或任务根本没有启动)的违规种类：出现时整次巡检作废，其余违规只作废该次调用的产出
ENVIRONMENT_VIOLATIONS = frozenset({
    ViolationKind.READONLY_MODIFIED.value, ViolationKind.FORBIDDEN_PATH_MODIFIED.value,
    ViolationKind.CREDENTIAL_PRESENT.value, ViolationKind.GIT_UNREADABLE.value,
    *(kind.value for kind in ViolationKind if kind.value.startswith("git-")),
})


def environment_violated(violations: Sequence[str]) -> bool:
    """违规中有环境级的种类；执行器没有给出种类时按环境级处理。"""
    return not violations or any(kind in ENVIRONMENT_VIOLATIONS for kind in violations)


@dataclass(frozen=True)
class ToolFinding:
    tool: str
    kind: str
    rule: str
    file: str
    line: int | None
    column: int | None
    message: str
    severity: str
    package: Mapping[str, Any] | None = None

    @property
    def vulnerability(self) -> bool:
        return self.kind == "vulnerability"

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "kind": self.kind, "rule": self.rule, "file": self.file, "line": self.line,
                "column": self.column, "message": self.message, "severity": self.severity,
                "package": None if self.package is None else dict(self.package)}


@dataclass(frozen=True)
class Claim:
    file: str
    line: int
    rule_or_pattern: str
    layer: str
    statement: str
    trigger: str
    severity: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Claim:
        return cls(data["file"], data["line"], data["ruleOrPattern"], data["layer"], data["statement"],
                   data["trigger"], data.get("severity"))

    @property
    def severity_rank(self) -> int:
        """严重度的次序，高为 0；没有标注的排在最后。"""
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else len(SEVERITIES)


@dataclass(frozen=True)
class ReviewResult:
    status: RunnerStatus
    claims: tuple[Claim, ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()
    transcript: str | None = None
    detail: str | None = None
    duration_ms: int = 0
    cost_usd: float | None = None
    exhausted: bool = False  # 当天预算已用尽(执行器没有启动或中途停止)
    violations: tuple[str, ...] = ()  # guard-violation 时的违规种类


@dataclass(frozen=True)
class Verification:
    status: RunnerStatus
    output: Mapping[str, Any] | None = None
    transcript: str | None = None
    detail: str | None = None
    completed_at: datetime | None = None
    violations: tuple[str, ...] = ()  # guard-violation 时的违规种类

    @property
    def verdict(self) -> Verdict | None:
        return None if self.output is None else Verdict(self.output["verdict"])

    def location(self) -> str | None:
        """取证给出的「文件:类名.方法名」：取第一个根因的文件与符号，没有符号时为文件。"""
        causes = [] if self.output is None else self.output.get("rootCauses") or []
        if not causes:
            return None
        cause = causes[0]
        return f"{cause['file']}:{cause['symbol']}" if cause.get("symbol") else cause["file"]


class Reviewer(Protocol):
    def review(self, scope: Scope, tool_findings: list[ToolFinding]) -> ReviewResult: ...

    def review_baseline(self, batch: Batch, tool_findings: list[ToolFinding]) -> ReviewResult: ...

    def defect_patterns(self) -> tuple[str, ...]: ...

    def scan_variants(self, pattern_id: str, scope: Scope) -> ReviewResult: ...

    def verify(self, claim: Claim) -> Verification: ...


@dataclass(frozen=True)
class VerifiedClaim:
    claim: Claim
    verification: Verification
    review_transcript: str | None = None
    tool_finding: ToolFinding | None = None
    introduced_by: Mapping[str, Any] | None = None
