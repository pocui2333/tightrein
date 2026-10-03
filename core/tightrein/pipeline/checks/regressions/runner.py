"""按 Issue 与类型执行复现检查(architecture/04 7.2)，实现 collect 的 RegressionRunner 协议，verify 共用。

1. 按 Issue 读取清单：不合格时该 Issue 的检查记为 invalid；
2. 逐条核对哈希，不一致抛出 ManifestTampered；
3. 按类型交给注入的执行部分(api、page、static、test)：没有提供该类执行部分、目标地址或 worktree 缺失时记为 not-run，
   与 failed 区分；页面类同一 Issue 的检查一次执行。
执行部分返回 Execution；precondition_met 为假表示执行了但前置条件不满足(verify 记为弱证据)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.domain.enums import RegressionKind, RegressionResult
from tightrein.sources.base import ProbeTarget
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.manifest import CheckEntry, ManifestInvalid
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos.regressions import RegressionCheck


@dataclass(frozen=True)
class Execution:
    result: RegressionResult
    detail: str = ""
    precondition_met: bool = True
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegressionOutcome:
    """一条检查的执行结果；location 取自清单的 checks[].location。"""

    check: RegressionCheck
    result: RegressionResult
    location: str
    detail: str = ""
    precondition_met: bool = True
    artifacts: tuple[str, ...] = ()


ApiRun = Callable[[CheckEntry, Path, ProbeTarget], Execution]
StaticRun = Callable[[CheckEntry, Path, Path, Path], Execution]
TestRun = StaticRun
PageRun = Callable[[Sequence[CheckEntry], Path, ProbeTarget], dict[str, Execution]]


def not_run(reason: str) -> Execution:
    return Execution(RegressionResult.NOT_RUN, reason)


class RegressionExecutor:
    def __init__(self, layout: WorkspaceLayout, *, api: ApiRun | None = None, page: PageRun | None = None,
                 static: StaticRun | None = None, test: TestRun | None = None,
                 directory: Callable[[str], Path] | None = None) -> None:
        self.layout = layout
        self.api = api
        self.page = page
        self.static = static
        self.test = test
        self.directory = directory or layout.regression_dir

    def run_checks(self, checks: Sequence[RegressionCheck], target: ProbeTarget) -> list[RegressionOutcome]:
        by_issue: dict[str, list[RegressionCheck]] = defaultdict(list)
        for check in checks:
            by_issue[check.issue_id].append(check)
        outcomes: list[RegressionOutcome] = []
        for issue_id, items in by_issue.items():
            outcomes += self._issue(issue_id, items, target)
        order = {(check.issue_id, check.check_id): index for index, check in enumerate(checks)}
        return sorted(outcomes, key=lambda item: order[(item.check.issue_id, item.check.check_id)])

    def _issue(self, issue_id: str, checks: list[RegressionCheck], target: ProbeTarget) -> list[RegressionOutcome]:
        directory = self.directory(issue_id)
        try:
            loaded = manifest.load(directory)
        except ManifestInvalid as error:
            return [RegressionOutcome(check, RegressionResult.INVALID, check.check_id, str(error)) for check in checks]
        entries: dict[str, CheckEntry] = {}
        outcomes: list[RegressionOutcome] = []
        for check in checks:
            entry = loaded.entry(check.check_id)
            if entry is None:
                outcomes.append(RegressionOutcome(check, RegressionResult.INVALID, check.check_id,
                                                  f"清单中没有检查 {check.check_id}"))
                continue
            manifest.verify(directory, entry, check.hash)
            entries[check.check_id] = entry
        pages = [entries[check.check_id] for check in checks
                 if check.check_id in entries and entries[check.check_id].kind is RegressionKind.PAGE]
        page_results = self._pages(pages, directory, target)
        for check in checks:
            entry = entries.get(check.check_id)
            if entry is None:
                continue
            if entry.kind is RegressionKind.PAGE:
                execution = page_results.get(entry.id, not_run("用例没有给出结果"))
            elif entry.kind is RegressionKind.API:
                execution = self._api(entry, directory, target)
            else:
                execution = self._in_worktree(entry, directory, target)
            outcomes.append(RegressionOutcome(check, execution.result, entry.location, execution.detail,
                                              execution.precondition_met, execution.artifacts))
        return outcomes

    def _api(self, entry: CheckEntry, directory: Path, target: ProbeTarget) -> Execution:
        if self.api is None:
            return not_run("没有提供接口类检查的执行部分")
        if target.base_url is None:
            return not_run("没有目标地址")
        return self.api(entry, directory, target)

    def _in_worktree(self, entry: CheckEntry, directory: Path, target: ProbeTarget) -> Execution:
        run = self.static if entry.kind is RegressionKind.STATIC else self.test
        if run is None:
            return not_run(f"没有提供{entry.kind.label}类检查的执行部分")
        if target.worktree is None:
            return not_run("没有可执行检查的 worktree")
        return run(entry, directory, target.worktree, target.raw_dir)

    def _pages(self, entries: list[CheckEntry], directory: Path, target: ProbeTarget) -> dict[str, Execution]:
        if not entries:
            return {}
        if self.page is None:
            return {entry.id: not_run("没有提供页面类检查的执行部分") for entry in entries}
        if target.base_url is None:
            return {entry.id: not_run("没有目标地址") for entry in entries}
        return self.page(entries, directory, target)
