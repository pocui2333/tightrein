"""等 CI：提 PR 后等检查结束(`gh pr checks --watch`)，结束即交给 merge.py 判断合并，不靠每次运行轮询。

- `gh pr checks` 的退出码不反映结果：等完之后按每项的 bucket(pass、fail、cancel、pending、skipping)判断；
- 主分支要求必需检查时只等、只看必需检查(`--required`)；否则等全部检查，合并时再看 GitHub 的 mergeStateStatus；
- 没有任何检查为 skipped；等到 limits.timeouts.ci 仍没结束的按当时的状态返回(进行中)，下次发布再接着等。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.protocol.git import CommandFailed, GitHub, NetworkError
from tightrein.protocol.git.git import failure
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.runtime import Runtime

SKIPPED, PENDING, PASSED, FAILED = "skipped", "pending", "passed", "failed"
FAILED_BUCKETS = frozenset({"fail", "cancel"})
PENDING_BUCKETS = frozenset({"pending"})
CHECK_FIELDS = "name,state,bucket"
# gh 在 PR 上没有任何检查时以非零退出并只在错误输出里说明(gh 2.x)
NO_CHECKS = "no checks reported"


@dataclass(frozen=True)
class CiState:
    state: str
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = field(default=())

    @property
    def finished(self) -> bool:
        return self.state != PENDING

    @property
    def reason(self) -> str | None:
        """不满足自动合并条件时的原因；通过与跳过时为空。"""
        if self.state == FAILED:
            return f"CI 检查未通过：{'、'.join(self.failed)}"
        if self.state == PENDING:
            return f"CI 检查还在进行：{'、'.join(self.pending) or '等待开始'}"
        return None

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "failed": list(self.failed), "pending": list(self.pending)}


def judge(checks: Sequence[dict[str, Any]]) -> CiState:
    if not checks:
        return CiState(SKIPPED)
    failed = tuple(str(item.get("name")) for item in checks if item.get("bucket") in FAILED_BUCKETS)
    pending = tuple(str(item.get("name")) for item in checks if item.get("bucket") in PENDING_BUCKETS)
    if failed:
        return CiState(FAILED, failed, pending)
    if pending:
        return CiState(PENDING, (), pending)
    return CiState(PASSED)


def wait(runtime: Runtime, github: GitHub, number: int, *, required: bool) -> CiState:
    """等检查结束(最多 limits.timeouts.ci)，再读一次结果判断。"""
    interval = int(parse_duration(str(runtime.settings.control("release.ci", "watchInterval"))))
    watch_args = ("pr", "checks", str(number), "--watch", "--interval", str(interval),
                  *(("--required",) if required else ()))
    try:
        github.query(*watch_args, timeout_s=runtime.settings.duration("limits.timeouts.ci"), retry=False)
    except NetworkError:
        pass  # 到时限仍没结束：按当前状态返回进行中，下次接着等
    return current(runtime, github, number, required=required)


def current(runtime: Runtime, github: GitHub, number: int, *, required: bool) -> CiState:
    """不等待，读一次当前的检查结果。"""
    if required:
        return judge([{"name": item.name, "bucket": item.bucket} for item in github.pr_checks(number)])
    found = github.query("pr", "checks", str(number), "--json", CHECK_FIELDS)
    if found.exit_code != 0 and not found.stdout.strip():
        if NO_CHECKS in found.stderr_tail:
            return CiState(SKIPPED)
        raise failure(CommandFailed, (github.program, "pr", "checks", str(number)), found, runtime.redactor.text)
    return judge(json.loads(found.stdout or "[]"))
