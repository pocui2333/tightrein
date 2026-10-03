"""部署后确认的判定(redesign/06-verify.md 第 2 节)，纯函数。

- 来自平台(错误追踪、日志、业务告警、访问日志)与项目探针的问题，以及没有可执行复现检查(或检查无法执行)的问题：
  部署之后再出现即为回归；
  观察期(thresholds.verify.observationHours)满而未再出现即为通过；观察期内为等待。
- 来自 api-fuzz 的问题由接口与页面类复现检查对目标环境重放，来自静态巡检与修复第 5 步的由静态类与测试类检查对部署
  commit 重跑；检查的结论即确认的结论。
整体：有回归即回归；有未确认的(检查未执行、观察期未满)为等待；否则通过。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import CheckResult, Probe
from tightrein.pipeline.verify.steps.verdict import Item

REPLAY = "replay"      # 对目标环境重放
RERUN = "rerun"        # 对部署 commit 重跑
OBSERVE = "observe"    # 观察期内是否再出现
REGRESSED = "regressed"
WAITING = "waiting"
VERIFIED = "verified"
OBSERVED_SOURCES = frozenset({Probe.PLATFORM_ERRORS, Probe.ACCESS_LOG, Probe.ALERTS, Probe.PROJECT_PROBE})
CONCLUSIVE = (CheckResult.PASS, CheckResult.FAIL)


def observe(last_seen_at: datetime, deployed_at: datetime, now: datetime, hours: int) -> tuple[CheckResult, str]:
    if last_seen_at > deployed_at:
        return CheckResult.FAIL, f"部署后再次出现(最近一次 {format_iso(last_seen_at)})"
    due = deployed_at + timedelta(hours=hours)
    if now >= due:
        return CheckResult.PASS, f"部署后 {hours} 小时内没有再出现"
    return CheckResult.UNVERIFIED, f"观察期到 {format_iso(due)}"


def observed(sources: Mapping[str, Probe], checks: Sequence[Item]) -> list[str]:
    """需要按观察期确认的关联问题：来源为平台或项目探针的，以及复现检查不能给出结论(没有检查，或有检查未执行、前置条件
    不满足)时的全部问题。"""
    conclusive = bool(checks) and all(item.result in CONCLUSIVE for item in checks)
    return [problem_id for problem_id, probe in sources.items() if probe in OBSERVED_SOURCES or not conclusive]


def decide(confirmations: Sequence[Mapping[str, Any]]) -> str:
    """confirmations 为每个对象的 {method, result}。复现检查无法给出结论、而关联问题已按观察期确认通过时，以观察为准。"""
    results = [CheckResult(item["result"]) for item in confirmations]
    if CheckResult.FAIL in results:
        return REGRESSED
    open_items = [item for item in confirmations if CheckResult(item["result"]) not in CONCLUSIVE]
    if not open_items:
        return VERIFIED
    observed_items = [item for item in confirmations if item["method"] == OBSERVE]
    if observed_items and all(item["method"] != OBSERVE for item in open_items) and all(
            CheckResult(item["result"]) is CheckResult.PASS for item in observed_items):
        return VERIFIED
    return WAITING
