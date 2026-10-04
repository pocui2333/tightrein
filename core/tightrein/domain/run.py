"""Run 实体与覆盖范围 Coverage(architecture/01 2.2、architecture/04 各探针的覆盖范围)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.domain.enums import Probe, ProbeLevel, RunStage, RunStatus

METHODS_GET = "GET"
METHODS_ALL = "all"


@dataclass(frozen=True)
class Endpoint:
    """已测试的操作：方法、路由模板与角色。"""

    method: str
    route: str
    role: str | None = None


@dataclass(frozen=True)
class Coverage:
    endpoints: tuple[Endpoint, ...] = ()
    endpoints_total: int | None = None
    files: tuple[str, ...] = ()
    methods: str | None = None
    sources: tuple[str, ...] = ()  # 本次读到数据的平台或项目探针(platform-errors、access-log、alerts、project-probe)

    def __post_init__(self) -> None:
        if self.methods not in (None, METHODS_GET, METHODS_ALL):
            raise ValueError(f"methods 只能是 GET 或 all：{self.methods}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Coverage":
        return cls(
            endpoints=tuple(
                Endpoint(item["method"], item["route"], item.get("role")) for item in data.get("endpoints", [])
            ),
            endpoints_total=data.get("endpointsTotal"),
            files=tuple(data.get("files", [])),
            methods=data.get("methods"),
            sources=tuple(data.get("sources", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.endpoints:
            result["endpoints"] = [
                {"method": item.method, "route": item.route, "role": item.role} for item in self.endpoints
            ]
        if self.endpoints_total is not None:
            result["endpointsTotal"] = self.endpoints_total
        if self.files:
            result["files"] = list(self.files)
        if self.methods is not None:
            result["methods"] = self.methods
        if self.sources:
            result["sources"] = list(self.sources)
        return result

    def tested_endpoints(self) -> frozenset[tuple[str, str]]:
        """测过的 (方法, 路由模板)，不区分角色。"""
        return frozenset((item.method, item.route) for item in self.endpoints)

    def covers_endpoint(self, method: str, route: str, role: str | None) -> bool:
        if self.methods == METHODS_GET and method != METHODS_GET:
            return False
        return any(
            item.method == method and item.route == route and (role is None or item.role == role)
            for item in self.endpoints
        )


@dataclass(frozen=True)
class HealthCheck:
    """运行开始前的健康检查；status 为 None 表示请求失败或超时。"""

    status: int | None
    elapsed_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass(frozen=True)
class EnvironmentDetail:
    """探针记录的环境事实；health 为 None 表示本次没有做健康检查。"""

    health: HealthCheck | None = None
    failed_roles: tuple[str, ...] = ()
    report_complete: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnvironmentDetail":
        health = data.get("health")
        return cls(
            health=HealthCheck(health.get("status"), health.get("elapsedMs")) if health is not None else None,
            failed_roles=tuple(data.get("failedRoles", [])),
            report_complete=bool(data.get("reportComplete", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        health = None if self.health is None else {"status": self.health.status, "elapsedMs": self.health.elapsed_ms}
        return {"health": health, "failedRoles": list(self.failed_roles), "reportComplete": self.report_complete}


@dataclass(frozen=True)
class Run:
    id: str
    stage: RunStage
    started_at: datetime
    status: RunStatus
    probe: Probe | None = None
    level: ProbeLevel | None = None
    parent_run_id: str | None = None
    ended_at: datetime | None = None
    target_commit: str | None = None
    coverage: Coverage = field(default_factory=Coverage)
    environment_detail: EnvironmentDetail = field(default_factory=EnvironmentDetail)
    aggregated_at: datetime | None = None
    trace_id: str | None = None
    # 开始运行的进程(store/repos/runs.save 在新建进行中的运行时填入)：中断识别据此判断没有持有锁的运行是否仍在执行
    holder_pid: int | None = None
    holder_host: str | None = None

    def __post_init__(self) -> None:
        for name in ("started_at", "ended_at", "aggregated_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} 必须带时区")
        if (self.stage is RunStage.COLLECT) != (self.probe is not None):
            raise ValueError("只有 collect 运行带探针，且 collect 运行必须带探针")
