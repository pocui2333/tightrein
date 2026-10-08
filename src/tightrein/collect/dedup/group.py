"""指纹与归并：算指纹；已有问题累计出现，同一处代码被其他来源报过的合进去，都没有则新建待确认的问题。

指纹只用稳定字段，不用行号：
- 有 group_key(平台分组编号等)时原样作为指纹，便于回平台找同一组；
- 其余拼成带算法版本前缀的规范字符串(`v2|<来源>|<检查类型>|…`)，取 SHA-1 前 16 位；改算法时升版本，不与旧指纹混淆。
  v2 比 v1 多了检查类型：同一处的不同问题(报错与变慢)不再并成一个。

来源证据(Signal.evidence)中参与指纹与标题的键：projectFrames、exceptionType(平台堆栈)，status(接口响应状态码)，
category(日志类别)，rule(静态巡检规则)，role(接口角色)，sourceName(平台与项目探针的子来源)，
probeFingerprint(项目探针自己给的指纹)，targetFingerprints(回归信号：验收时复现检查失败，指向原来的问题)。

回归信号不算指纹，直接按 targetFingerprints 归到原来的问题；找不到目标时写进说明，不新建问题。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tightrein.collect.common.signals import Signal
from tightrein.collect.dedup import normalize
from tightrein.collect.dedup.changes import SOURCES, ChangeSet, signal_time
from tightrein.collect.dedup.status import CLEAN_RUNS, ROLES, SUB_SOURCE, ProblemStatus
from tightrein.store.tables.problems import Problem

VERSION = 2
FRAME_COUNT = 3
PERFORMANCE_CHECKS = frozenset({"latency"})  # 与报错类分开：同一处的报错与变慢不合并
NOT_MERGED_INTO = frozenset({ProblemStatus.CLOSED, ProblemStatus.MUTED})  # 人已下过结论的问题不吸收别的来源

# problems.extra 中的键
FINGERPRINT_VERSION = "fingerprintVersion"
SYMBOL = "symbol"
LINE = "line"
FIRST_COMMIT = "firstCommit"
# 来源证据中的键
TARGETS = "targetFingerprints"


@dataclass(frozen=True)
class Params:
    title_length: int
    nearby_lines: int


def fingerprint(signal: Signal, message: str, version: int = VERSION) -> str:
    """回归信号(带 targetFingerprints)不调用它：直接归到原来的问题。"""
    if signal.group_key:
        return signal.group_key
    return hashlib.sha1(canonical(signal, message, version).encode("utf-8")).hexdigest()[:16]


def canonical(signal: Signal, message: str, version: int = VERSION) -> str:
    return "|".join([f"v{version}", signal.source, signal.check_type, *_parts(signal, message)])


def targets_of(signal: Signal) -> list[str]:
    found = signal.evidence.get(TARGETS)
    return [str(item) for item in found] if isinstance(found, list) else []


def place(signal: Signal) -> str | None:
    """规范化后的位置；代码位置带上符号(同一文件的不同函数不是同一处)。"""
    found = normalize.location(signal.location)
    if found is not None and signal.symbol and _is_code(found):
        return f"{found}:{signal.symbol}"
    return found


def title_for(signal: Signal, message: str, length: int) -> str:
    """只写现象、只用规范化后的稳定字段。"""
    where = normalize.location(signal.location)
    evidence = signal.evidence
    status = evidence.get("status")
    if where is not None and not _is_code(where) and where.partition(" ")[0] in normalize.HTTP_METHODS:
        title = f"{where} {signal.check_type}" + (f" {status}" if status is not None else "")
    elif evidence.get("exceptionType") or signal.group_key:
        title = message  # 平台给的消息以异常开头
    elif evidence.get("category"):
        title = f"{evidence['category']}：{message}"
    elif evidence.get("rule"):
        title = f"{evidence['rule']}：{place(signal) or ''}"
    else:
        title = f"{place(signal)}：{message}" if where is not None else message
    return title[:length]


def new_problem(problem_id: str, signal: Signal, value: str, message: str, title_length: int) -> Problem:
    seen = signal_time(signal)
    role = signal.evidence.get("role")
    sub_source = signal.evidence.get("sourceName")
    return Problem(
        id=problem_id, fingerprint=value, source=signal.source, check_type=signal.check_type,
        status=ProblemStatus.PENDING.value, title=title_for(signal, message, title_length), first_seen=seen,
        last_seen=seen, location=normalize.location(signal.location), count=1, last_commit=signal.commit,
        extra={
            FINGERPRINT_VERSION: VERSION, SOURCES: [signal.source], SYMBOL: signal.symbol,
            LINE: normalize.line_of(signal.location), ROLES: [role] if role else [],
            SUB_SOURCE: str(sub_source) if sub_source else None, FIRST_COMMIT: signal.commit, CLEAN_RUNS: 0,
        },
    )


def apply_occurrence(problem: Problem, signal: Signal) -> None:
    """累计一次出现。信号可能乱序到达(重放、补读)：首次与最近出现按发生时间比较；信号没有 commit 时保留原值；
    每次出现把「干净的覆盖运行」计数清零。"""
    seen = signal_time(signal)
    problem.count += 1
    if seen < problem.first_seen:
        problem.first_seen = seen
        if signal.commit is not None:
            problem.extra[FIRST_COMMIT] = signal.commit
    if seen >= problem.last_seen:
        problem.last_seen = seen
        if signal.commit is not None:
            problem.last_commit = signal.commit
    problem.extra[CLEAN_RUNS] = 0
    role = signal.evidence.get("role")
    roles = problem.extra.setdefault(ROLES, [])
    if role and role not in roles:
        roles.append(role)
    sources = problem.extra.setdefault(SOURCES, [problem.source])
    if signal.source not in sources:
        sources.append(signal.source)


def code_paths(signals: Sequence[Signal]) -> set[str]:
    """本次信号涉及的代码文件：跨来源合并的候选按它一次读出。"""
    return {found for found in (normalize.location(signal.location) for signal in signals)
            if found is not None and _is_code(found)}


def apply(changeset: ChangeSet, signals: Sequence[Signal], messages: Mapping[str, str], params: Params) -> None:
    """signals 已按 (发生时间, 编号) 排好；messages 为各信号规范化后的消息。"""
    places = _places(changeset)
    for signal in signals:
        targets = targets_of(signal)
        if targets:
            _regression_signal(changeset, signal, targets)
            continue
        message = messages[signal.id]
        value = fingerprint(signal, message)
        problem = changeset.by_fingerprint(value)
        if problem is None:
            problem = _same_place(changeset, places, signal, params.nearby_lines)
            if problem is not None:
                changeset.alias(problem, value)
                changeset.merged += 1
        if problem is None:
            problem = new_problem(changeset.allocate(), signal, value, message, params.title_length)
            changeset.put(problem, created=True)
            if problem.location is not None:
                places.setdefault(problem.location, []).append(problem.id)
        else:
            apply_occurrence(problem, signal)
            changeset.put(problem)
        changeset.record(problem, signal)


def _regression_signal(changeset: ChangeSet, signal: Signal, targets: Sequence[str]) -> None:
    problem = next((found for found in map(changeset.by_fingerprint, targets) if found is not None), None)
    if problem is None:
        changeset.notes.append(f"回归信号 {signal.id} 指向的问题不存在(指纹 {'、'.join(targets)})，没有新建问题")
        return
    apply_occurrence(problem, signal)
    changeset.put(problem)
    changeset.record(problem, signal)
    changeset.regression_checks.add(problem.id)


def _parts(signal: Signal, message: str) -> list[str]:
    evidence = signal.evidence
    frames = evidence.get("projectFrames") or []
    if frames:
        return [str(evidence.get("exceptionType")), *(normalize.symbol(str(frame)) for frame in frames[:FRAME_COUNT])]
    if evidence.get("probeFingerprint"):
        return [str(evidence.get("sourceName")), str(evidence["probeFingerprint"])]
    where = normalize.location(signal.location)
    method, _, route = (where or "").partition(" ")
    if where is not None and method in normalize.HTTP_METHODS and route.startswith("/"):
        return [method, route, _status_class(evidence.get("status"))]
    if evidence.get("category"):
        return [str(evidence["category"]), message]
    if where is None:
        return [message]
    if signal.check_type.startswith("incidental"):
        return [place(signal) or "", message]
    return [str(evidence.get("rule") or ""), place(signal) or ""]


def _status_class(status: object) -> str:
    """502 与 503 是同一个问题：只用状态码大类。"""
    return f"{status // 100}xx" if isinstance(status, int) else "none"


def _is_code(location: str) -> bool:
    return not location.startswith("/") and location.partition(" ")[0] not in normalize.HTTP_METHODS


def _places(changeset: ChangeSet) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for problem in changeset.known.values():
        if problem.location is not None and _is_code(problem.location):
            found.setdefault(problem.location, []).append(problem.id)
    return found


def _same_place(changeset: ChangeSet, places: Mapping[str, list[str]], signal: Signal, nearby: int) -> Problem | None:
    """另一个来源报过的同一处：同一文件、同一函数(或行号相近)、同属报错类或同属性能类。"""
    where = normalize.location(signal.location)
    if where is None or not _is_code(where):
        return None
    line = normalize.line_of(signal.location)
    performance = signal.check_type in PERFORMANCE_CHECKS
    for problem_id in places.get(where, []):
        problem = changeset.known[problem_id]
        if problem.source == signal.source or problem.status in NOT_MERGED_INTO \
                or (problem.check_type in PERFORMANCE_CHECKS) != performance:
            continue
        if _near(problem.extra.get(SYMBOL), problem.extra.get(LINE), signal.symbol, line, nearby):
            return problem
    return None


def _near(left_symbol: str | None, left_line: int | None, right_symbol: str | None, right_line: int | None,
          nearby: int) -> bool:
    lines_close = left_line is not None and right_line is not None and abs(left_line - right_line) <= nearby
    if left_symbol and right_symbol:
        return left_symbol == right_symbol and (left_line is None or right_line is None or lines_close)
    return lines_close
