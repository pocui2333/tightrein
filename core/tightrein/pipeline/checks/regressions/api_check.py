"""接口类复现检查(architecture/04 7.1、7.2)：重放 `<检查编号>.request.json`，按 `<检查编号>.expect.json` 判定。

请求文件的字段同 api_fuzz.replay.RecordedRequest：method、path、pathTemplate、query、body；以清单中的 role 登录发送。
期望文件：`status` 为 `{"equals": 200}`、`{"in": [401, 403, 404]}` 或 `{"notIn": ["5xx", 409]}`(`4xx`、`5xx` 表示整类)；
可选的 `bodySchema` 为响应体须满足的 JSON Schema。
可选的前置条件请求 `<检查编号>.precondition.json`(同请求文件的字段)：响应为 2xx 且响应体不为空才算满足，不满足时
仍执行检查，结果标为前置条件不满足(验证中记为弱证据)。
登录失败、目标没有响应时为 not-run。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from tightrein.domain.enums import RegressionResult
from tightrein.sources.api_fuzz.replay import RecordedRequest, send
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.http import HttpResponse, Transport
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.session import LoginFailed, Session
from tightrein.pipeline.checks.regressions.manifest import EXPECT_SUFFIX, PRECONDITION_SUFFIX, CheckEntry
from tightrein.pipeline.checks.regressions.runner import Execution, not_run

STATUS_CLASS_SUFFIX = "xx"
STATUS_CLASS_SIZE = 100


def _status_matches(status: int, item: Any) -> bool:
    if isinstance(item, str) and item.endswith(STATUS_CLASS_SUFFIX):
        return status // STATUS_CLASS_SIZE == int(item[0])
    return status == int(item)


@dataclass(frozen=True)
class ApiExpectation:
    status: Mapping[str, Any]
    body_schema: Mapping[str, Any] | None = None

    @classmethod
    def from_file(cls, path: Path) -> ApiExpectation:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["status"], data.get("bodySchema"))

    def problems(self, response: HttpResponse) -> list[str]:
        status = response.status
        if status is None:
            return ["没有响应"]
        found = []
        if "equals" in self.status and status != int(self.status["equals"]):
            found.append(f"状态码为 {status}，期望 {self.status['equals']}")
        if "in" in self.status and not any(_status_matches(status, item) for item in self.status["in"]):
            found.append(f"状态码为 {status}，期望属于 {self.status['in']}")
        if "notIn" in self.status and any(_status_matches(status, item) for item in self.status["notIn"]):
            found.append(f"状态码为 {status}，期望不属于 {self.status['notIn']}")
        if self.body_schema is not None:
            try:
                document = json.loads(response.text())
            except ValueError:
                return [*found, "响应体不是 JSON"]
            errors = sorted(Draft202012Validator(self.body_schema).iter_errors(document), key=lambda error: error.path)
            found += [f"响应体 {'/'.join(str(part) for part in error.path) or '$'}：{error.message}" for error in errors]
        return found


def _request(path: Path) -> RecordedRequest:
    return RecordedRequest.from_context(json.loads(path.read_text(encoding="utf-8")))


class ApiCheck:
    def __init__(self, session: Session, transport: Transport, redactor: ProbeRedactor,
                 timeout_seconds: float) -> None:
        self.session = session
        self.transport = transport
        self.redactor = redactor
        self.timeout_seconds = timeout_seconds

    def __call__(self, entry: CheckEntry, directory: Path, target: ProbeTarget) -> Execution:
        role = entry.role or ""
        try:
            met = self._precondition(entry, directory, target, role)
            response = send(_request(directory / entry.file), role, target, self.session, self.transport,
                            self.timeout_seconds)
        except LoginFailed as error:
            return not_run(f"角色 {role} 登录失败：{error.reason}")
        if response.status is None:
            return not_run(f"目标没有响应：{response.error}")
        problems = ApiExpectation.from_file(directory / f"{entry.id}{EXPECT_SUFFIX}").problems(response)
        excerpt = self.redactor.excerpt(response.text())
        if problems:
            return Execution(RegressionResult.FAILED, f"{'；'.join(problems)}；响应摘录：{excerpt}", met)
        return Execution(RegressionResult.PASSED, f"状态码 {response.status}", met)

    def _precondition(self, entry: CheckEntry, directory: Path, target: ProbeTarget, role: str) -> bool:
        path = directory / f"{entry.id}{PRECONDITION_SUFFIX}"
        if not path.is_file():
            return True
        response = send(_request(path), role, target, self.session, self.transport, self.timeout_seconds)
        return response.ok and response.text().strip() not in ("", "[]", "{}", "null")
