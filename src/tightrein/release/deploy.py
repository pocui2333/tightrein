"""部署记录：读取部署平台上最近的部署(只读)，记进 state 表供采集定 release、发布找包含合并提交的部署。

本文件的读取部分：
- 部署来源是方法库 `deploy_source/`：每种来源一个程序加同名清单(`<方法>.yaml`，只声明参数、适用条件、凭据条目名
  与限制)，按接入清单 release.deploy 的 method 经 collect/common/methods 加载；现有 github_actions(部署工作流的运行)、
  github_deployments(GitHub Deployments 与各自最新的状态)、vercel(Vercel REST API)。参数在 settings 的
  controls."release.deploy".<方法>，按清单的 optionsSchema 校验(必填参数为 null 即报缺少)；接新平台只加一对文件；
- gh 一律只读、以参数数组启动；gh 未安装、超时、非 0 退出、输出不是 JSON 分别报不同的错误种类，
  错误信息只带标准错误的最后 3 行；
- 凭据(如 Vercel 的 vercel.token)来自 secrets.json，条目名写在方法清单里，只放进请求头，不进输出、日志与错误信息；
- `remember` 把读到的部署并进 state 表的 DEPLOYMENTS_KEY(格式见 collect/common/signals.py)：同一部署只记一条，
  第一次看到的时间(detectedAt)不变，状态取最新。

发布的跟踪流程(`track`)：
- 找包含合并提交的部署：先找 commit 相同且没被跳过的最新一次；没有时找 commit 以它为祖先的最早一次(等待窗口内的
  多个提交由之后的一次部署统一上线)；祖先关系查不到(本地没有该 commit)时只 fetch 一次再判断，仍没有的不计入；
- 同一次运行内部署记录只读一次、fetch 至多一次：同一次部署的多个 Issue 一起确认；
- 没有配置部署来源(接入清单 release.deploy 不启用)时，以合并时间加 assumeDeployedAfter 视为已部署，不无限等待；
- 改动涉及需要手动部署的路径(manualPaths)时提示联系负责人；部署失败只报告与给链接，是否由本修复引起交人判断。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.collect.common.signals import DEPLOYMENTS_KEY
from tightrein.protocol import methods
from tightrein.protocol.boundaries import matching_pattern
from tightrein.protocol.external import Misconfigured
from tightrein.protocol.http import Transport, UrllibTransport
from tightrein.protocol.naming import Clock, format_iso, parse_duration
from tightrein.protocol.process import Command, ProcessRunner
from tightrein.protocol.security import child_env
from tightrein.settings.load import Settings
from tightrein.store.tables import state

if TYPE_CHECKING:
    from tightrein.protocol.git import Git
    from tightrein.protocol.runtime import Runtime

POINT = "release.deploy"
METHODS = "tightrein.release.deploy_source"  # 部署来源的方法库
GH = "gh"
STDERR_TAIL_LINES = 3
RUNNING, SUCCEEDED, FAILED, SKIPPED = "running", "succeeded", "failed", "skipped"

# 跟踪
PENDING = "pending"  # 还没有包含合并提交的部署
MERGE_TIME = "merge-time"  # 没有部署来源：合并时间加 assumeDeployedAfter 视为已部署
# 一次运行内读过的部署记录与已 fetch 过的运行(同一次部署的多个 Issue 共用)
_RECORDS: dict[str, list[DeployRecord] | None] = {}
_FETCHED: set[str] = set()


class DeployErrorKind(StrEnum):
    TOOL_MISSING = "tool_missing"  # gh 没装
    UNAVAILABLE = "unavailable"  # 超时、非 0 退出、HTTP 非 2xx、缺凭据
    INVALID = "invalid"  # 输出不是预期的 JSON
    MISCONFIGURED = "misconfigured"  # 不认识的方法或缺参数


class DeployError(Exception):
    def __init__(self, kind: DeployErrorKind, message: str) -> None:
        super().__init__(f"{kind.value}：{message}")
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class DeployRecord:
    id: str
    commit: str
    status: str  # running、succeeded、failed、skipped
    environment: str | None
    url: str | None
    created_at: datetime


@dataclass(frozen=True)
class Gh:
    """只读 gh 调用的上下文：在项目仓库目录执行。"""

    runner: ProcessRunner
    cwd: Path
    env: Mapping[str, str]
    timeout_s: float


@dataclass(frozen=True)
class SourceContext:
    """部署来源方法读取时用的东西：只读 gh、HTTP、项目的主干分支与 HTTP 时限。"""

    gh: Gh
    transport: Transport
    main_branch: str | None
    timeout_s: float


@dataclass(frozen=True)
class Deployed:
    """包含某个合并提交的部署：status 为 pending、running、succeeded、failed。"""

    status: str
    record: DeployRecord | None
    source: str  # 部署来源的方法名，或 merge-time
    at: datetime | None  # 视为已部署的时刻(merge-time 时为合并时间加 assumeDeployedAfter)


def recent(runtime: Runtime, *, transport: Transport | None = None) -> list[DeployRecord]:
    """按接入清单选的部署来源(deploy_source/ 下的方法)读取最近的部署，按 (时间, 编号) 排序。"""
    name = runtime.setup.module(POINT).method or ""
    if not methods.exists(METHODS, name):
        raise DeployError(DeployErrorKind.MISCONFIGURED, f"不认识的部署来源：{name or '没有写 method'}")
    method = methods.load(METHODS, name)
    try:
        configured = methods.configure(method, settings=runtime.settings, source=POINT, secrets=runtime.secrets)
    except Misconfigured as error:
        raise DeployError(DeployErrorKind.MISCONFIGURED, str(error)) from error
    gh = Gh(runtime.runner, runtime.git.repo, child_env(runtime.environ),
            runtime.settings.duration("limits.timeouts.command"))
    main = runtime.settings.project.main_branch if runtime.settings.project else None
    context = SourceContext(gh, transport or UrllibTransport(), main, runtime.settings.duration("limits.timeouts.http"))
    records: list[DeployRecord] = method.module.read(configured, context)
    return records


def remember(conn: sqlite3.Connection, records: Sequence[DeployRecord], clock: Clock) -> None:
    """并进 state 表：按部署编号合并，detectedAt 保留第一次看到的时间，状态与部署时间取最新。"""
    now = format_iso(clock.now())
    known = {item.get("id") or f"{item['commit']}@{item.get('deployedAt')}": item
             for item in state.get(conn, DEPLOYMENTS_KEY) or []}
    for record in records:
        previous = known.get(record.id, {})
        known[record.id] = {"id": record.id, "commit": record.commit, "status": record.status,
                            "deployedAt": format_iso(record.created_at), "detectedAt": previous.get("detectedAt", now),
                            "environment": record.environment, "url": record.url}
    ordered = sorted(known.values(),
                     key=lambda item: (item.get("deployedAt") or item["detectedAt"], item.get("id", "")))
    state.put(conn, DEPLOYMENTS_KEY, ordered, clock)


def track(runtime: Runtime, merge_commit: str, merged_at: datetime) -> Deployed:
    """找包含合并提交的部署；没有部署来源时按合并时间加 assumeDeployedAfter。"""
    records = deployments(runtime)
    if records is None:
        wait = parse_duration(str(runtime.settings.control(POINT, "assumeDeployedAfter")))
        due = merged_at + timedelta(seconds=wait)
        if runtime.clock.now() < due:
            return Deployed(PENDING, None, MERGE_TIME, due)
        assumed = DeployRecord(f"{MERGE_TIME}:{merge_commit}", merge_commit, SUCCEEDED, None, None, due)
        remember(runtime.conn, [assumed], runtime.clock)
        return Deployed(SUCCEEDED, None, MERGE_TIME, due)
    method = runtime.setup.module(POINT).method or ""
    record = containing(records, merge_commit, runtime.git, lambda: _fetch_once(runtime))
    if record is None:
        return Deployed(PENDING, None, method, None)
    return Deployed(record.status, record, method, record.created_at)


def deployments(runtime: Runtime) -> list[DeployRecord] | None:
    """本次运行读到的部署记录(读一次并记进 state 表)；接入清单不启用部署来源时为 None。"""
    if not runtime.setup.enabled(POINT):
        return None
    if runtime.run not in _RECORDS:
        records = recent(runtime)
        remember(runtime.conn, records, runtime.clock)
        _RECORDS[runtime.run] = records
    return _RECORDS[runtime.run]


def containing(records: Sequence[DeployRecord], commit: str, git: Git,
               fetch: Callable[[], None]) -> DeployRecord | None:
    """commit 相同且没被跳过的最新一次；否则 commit 以它为祖先的最早一次。祖先关系未知时 fetch 后再判断一次。"""
    exact = [record for record in records if record.commit == commit]
    if exact and exact[-1].status != SKIPPED:
        return exact[-1]
    for record in records:
        if record.status == SKIPPED or record.commit == commit:
            continue
        known = git.is_ancestor(commit, record.commit)
        if known is None:
            fetch()
            known = git.is_ancestor(commit, record.commit)
        if known:
            return record
    return None


def manual_paths(settings: Settings, changed: Sequence[str]) -> list[str]:
    """改动中需要手动部署的文件(controls."release.deploy".manualPaths)。"""
    patterns = tuple(settings.control(POINT, "manualPaths"))
    return [path for path in changed if matching_pattern(path, patterns) is not None]


def gh_json(gh: Gh, *args: str) -> Any:
    outcome = gh.runner.run(Command((GH, *args), gh.cwd, gh.env, timeout_s=gh.timeout_s))
    name = " ".join(args[:2])
    if outcome.start_error is not None:
        raise DeployError(DeployErrorKind.TOOL_MISSING,
                          f"无法启动 gh：{outcome.start_error}；安装 GitHub CLI 并执行 gh auth login")
    if outcome.stopped_by is not None:
        raise DeployError(DeployErrorKind.UNAVAILABLE, f"gh {name} 超过 {gh.timeout_s:g} 秒未结束({outcome.stopped_by})")
    if outcome.exit_code != 0:
        tail = outcome.stderr_tail.strip().splitlines()[-STDERR_TAIL_LINES:]
        raise DeployError(DeployErrorKind.UNAVAILABLE,
                          f"gh {name} 以 {outcome.exit_code} 退出：{' / '.join(tail) or '没有错误输出'}")
    try:
        return json.loads(outcome.stdout or "null")
    except ValueError as error:
        raise DeployError(DeployErrorKind.INVALID, f"gh {name} 的输出不是 JSON") from error


def sorted_records(records: Iterable[DeployRecord]) -> list[DeployRecord]:
    return sorted(records, key=lambda item: (item.created_at, item.id))


def _fetch_once(runtime: Runtime) -> None:
    if runtime.run not in _FETCHED:
        _FETCHED.add(runtime.run)
        runtime.git.fetch()
