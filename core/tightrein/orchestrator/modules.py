"""编排调用各模块的入口(architecture/09 3.1)。实现在组装根 cli/assemble.App，编排层不 import cli。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from tightrein.pipeline.aggregate.service import AggregateService
from tightrein.pipeline.collect.service import CollectService
from tightrein.pipeline.common.deploys import DeploySource
from tightrein.pipeline.fix.service import FixService
from tightrein.pipeline.issue.service import IssueService
from tightrein.pipeline.learn.service import LearnService
from tightrein.pipeline.release.service import ReleaseService
from tightrein.runner.roles import Overrides
from tightrein.pipeline.triage.service import TriageService
from tightrein.pipeline.verify.service import VerifyService
from tightrein.store.retention import RetentionReport


class Modules(Protocol):
    overrides: Overrides
    """命令行的 --runner 与 --model；分诊按请求传入，其余模块已在组装时带上。"""

    def collect(self) -> CollectService: ...

    def aggregate(self) -> AggregateService: ...

    def triage(self) -> TriageService: ...

    def issue(self) -> IssueService: ...

    def fix(self) -> FixService: ...

    def verify(self) -> VerifyService: ...

    def release(self) -> ReleaseService: ...

    def learn(self) -> LearnService: ...

    def deploys(self) -> DeploySource: ...

    def sync_readonly(self, commit: str | None = None) -> str: ...

    def main_head(self) -> str | None:
        """fetch 后 origin 主分支的最新提交(只读)；没有时为空。"""
        ...

    def purge(self) -> RetentionReport: ...

    def disabled_sources(self) -> dict[str, str]:
        """未启用的采集方法与原因(sources/enabled.py)。"""
        ...

    def wait_until(self, moment: datetime) -> None: ...

    def run_command(self, argv: Sequence[str]) -> int:
        """以同一工作区执行一条 tightrein 子命令(定时任务的 command)，返回退出码。"""
        ...
