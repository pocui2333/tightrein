"""周报 data/reports/weekly-<周一日期>.md(redesign/08-learn.md)：由程序生成的 result 类型交接文档。

- 结论：本周新发现、已修复、回归、一次通过率，以及需要处理的事项与健康异常的数量；读这一节就能判断本周要不要处理；
- 做了什么：统计周期与生成时间(绝对日期并注明时区)；
- 产出：指标(本周、上周、趋势、样本数；比率的分母为 0 时显示「无样本」，分母未知时显示「未知」)、环节效益、本周的规则、
  经验清理与新建议；
- 证据：链路健康与第三方 skill 的核实(数据块 checks)；偏离与原因：统计出错的项；
- 需要决定：待处理的学习建议，每条带推荐与理由；下一步：需要处理的事项与命令；引用：learn 的 JSON 交接文档。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from tightrein.domain.enums import DocumentStatus, Stage, SuggestionKind
from tightrein.domain.handoff.document import Decision, Event, HandoffDocument, Header, NextStep, Reference
from tightrein.pipeline.learn.steps.health import CHECK_LABELS
from tightrein.pipeline.learn.steps.weeks import Window

KIND = "result"
SOURCE = "learn"
TARGET = "user"
NONE = "无"
NO_SAMPLE = "无样本"
UNKNOWN = "未知"
PASSED, FAILED = "passed", "failed"
METRIC_LABELS = {
    "new-problems": "新发现问题数", "regressed-problems": "回归的问题数", "api-coverage": "接口覆盖率",
    "noise-ratio": "噪声占比", "noise-count": "噪声信号数",
    "triage-accuracy": "分诊准确率", "false-refutes": "误判为不成立", "manual-queue": "人工队列积压",
    "manual-queue-max-wait": "人工队列最长等待(工作日)", "lead-time-hours": "各段耗时中位数(小时)",
    "verify-first-pass": "验证一次通过率", "change-files": "修复改动文件数", "change-lines": "修复变更行数",
    "change-over-cap": "超出改动上限的次数", "fixed-issues": "已修复数", "regression-rate": "回归率",
    "pr-rejection-rate": "PR 被拒率", "first-pass": "修复一次通过率", "fix-cost-usd": "每个修复的费用(美元)",
    "revert-rate": "被撤销的比例", "user-corrections": "用户纠正次数", "tokens": "token", "cost-usd": "费用(美元)",
    "cost-usd-estimated": "其中按 token 估算的费用(美元)", "run-minutes": "运行时长合计(分钟)",
    "run-minutes-median": "运行时长中位数(分钟)",
}
RATIOS = frozenset({"api-coverage", "noise-ratio", "triage-accuracy", "verify-first-pass",
                    "regression-rate", "pr-rejection-rate", "first-pass", "revert-rate"})
ATTENTION_LABELS = {
    "issue-review": "Issue 等待放行", "pr-review": "PR 等待审核", "pr-mergeable": "PR 可以合并",
    "manual-queue": "人工队列", "suppression-expiring": "抑制规则即将到期", "false-positive": "自动判为误报(请抽查)",
    "regressed-issue": "回归两次及以上", "suggestion": "待处理的建议", "lesson-failed": "需要手写的经验",
}
DEFAULT_ADVICE = ("阅读建议中的证据后接受或拒绝", "建议只在用户接受后生效")


def zone_text(zone: tzinfo | None, at: datetime) -> str:
    offset = (at.astimezone(zone) if zone is not None else at.astimezone()).strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}"


def week_text(window: Window, zone: tzinfo | None) -> str:
    def day(at: datetime) -> str:
        return (at.astimezone(zone) if zone is not None else at.astimezone()).date().isoformat()

    return f"{day(window.start)} 至 {day(window.end - timedelta(seconds=1))}({zone_text(zone, window.start)})"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def value_text(metric: str, value: float | None, numerator: float | None, denominator: float | None) -> str:
    if metric in RATIOS:
        if value is None:
            return UNKNOWN if denominator is None else NO_SAMPLE
        return f"{value:.1%}({_number(numerator or 0)}/{_number(denominator or 0)})"
    return NO_SAMPLE if value is None else _number(value)


def _find(values: Sequence[Mapping[str, Any]], metric: str) -> Mapping[str, Any] | None:
    return next((item for item in values if item["metric"] == metric and item["dimension"] == "all"), None)


def _count(item: Mapping[str, Any] | None) -> str:
    return NO_SAMPLE if item is None or item["value"] is None else _number(item["value"])


def summary(outputs: Mapping[str, Any], previous: Mapping[tuple[str, str], float | None]) -> str:
    values = outputs.get("metrics", [])
    parts = []
    headline = (("new-problems", "新发现问题"), ("fixed-issues", "已修复 Issue"), ("regressed-problems", "回归问题"))
    for metric, label in headline:
        last = previous.get((metric, "all"))
        parts.append(f"{label} {_count(_find(values, metric))} 个(上周 {NO_SAMPLE if last is None else _number(last)})")
    unhealthy = sum(not item["passed"] for item in outputs.get("health", []))
    return (f"本周{'，'.join(parts)}；需要处理的事项 {len(outputs.get('attention', []))} 项，"
            f"链路健康异常 {unhealthy} 项。")


def _rate(item: Mapping[str, Any] | None) -> str:
    if item is None or item["value"] is None:
        return NO_SAMPLE
    return f"{item['value']:.0%}"


def conclusion(outputs: Mapping[str, Any], previous: Mapping[tuple[str, str], float | None]) -> str:
    first = _rate(_find(outputs.get("metrics", []), "first-pass"))
    return f"{summary(outputs, previous)}修复一次通过率 {first}；待处理的建议 {len(outputs.get('suggestions', []))} 条。"


def _metrics(outputs: Mapping[str, Any], trends: Mapping[tuple[str, str], Sequence[float | None]]) -> str:
    rows = ["| 指标 | 维度 | 本周 | 上周 | 趋势 | 样本数 |", "|---|---|---|---|---|---|"]
    for item in outputs.get("metrics", []):
        metric, dimension = item["metric"], item["dimension"]
        history = list(trends.get((metric, dimension), ()))
        last = history[-1] if history else None
        trend = " → ".join(NO_SAMPLE if value is None else _number(value) for value in [*history, item["value"]])
        rows.append(f"| {METRIC_LABELS.get(metric, metric)} | {dimension} | "
                    f"{value_text(metric, item['value'], item['numerator'], item['denominator'])} | "
                    f"{NO_SAMPLE if last is None else _number(last)} | {trend} | {item['sampleSize']} |")
    lines = ["**指标**", "\n".join(rows)]
    yields = outputs.get("yields", [])
    if yields:
        table = ["| 环节 | 角色 | 调用 | token | 费用(美元) | 有效 | 无效 | 未定 | 每条有效产出的 token | 最近一次有效产出后的调用 |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for item in yields:
            per = "无产出" if item["tokensPerUseful"] is None else _number(item["tokensPerUseful"])
            table.append(f"| {Stage(item['stage']).label} | {item['role']} | {item['calls']} | "
                         f"{item['inputTokens'] + item['outputTokens']} | {_number(item['costUsd'])} | "
                         f"{item['useful']} | {item['noYield']} | {item['pending']} | {per} | "
                         f"{item['callsSinceUseful']} |")
        lines += ["**环节效益**(本周登记的 LLM 调用；有效产出只计结果已确定的调用)", "\n".join(table)]
    return "\n\n".join(lines)


def _list(title: str, items: Sequence[str]) -> str:
    return f"**{title}**\n\n" + ("\n".join(f"- {item}" for item in items) or NONE)


def _produced(outputs: Mapping[str, Any], trends: Mapping[tuple[str, str], Sequence[float | None]]) -> str:
    rules = [f"{item['issueId']}：{'收入规则库 ' + item['path'] if item['accepted'] else '未收录，' + item['reason']}"
             for item in outputs.get("rules", [])]
    cleanup = [f"{item['id']}：{item['action']}" for item in outputs.get("cleanup", [])]
    created = [f"{item['id']} [{SuggestionKind(item['kind']).label}] {item['subject']}"
               for item in outputs.get("suggestions", [])]
    return "\n\n".join([_metrics(outputs, trends), _list("缺陷变规则", rules), _list("经验清理", cleanup),
                         _list("待处理的建议", created)])


def checks(outputs: Mapping[str, Any]) -> list[dict[str, str]]:
    found = [{"name": CHECK_LABELS.get(item["check"], item["check"]), "verdict": PASSED if item["passed"] else FAILED,
              "summary": item["detail"] + ("(需立即处理)" if item["notify"] and not item["passed"] else "")}
             for item in outputs.get("health", [])]
    found += [{"name": f"第三方 skill {item['name']}({item['source']})", "verdict": PASSED if item["passed"] else FAILED,
               "summary": item["detail"]} for item in outputs.get("thirdParty", [])]
    return found


def _evidence(outputs: Mapping[str, Any]) -> str:
    failing = [f"{CHECK_LABELS.get(item['check'], item['check'])}{'(需立即处理)' if item['notify'] else ''}："
               f"{item['detail']}" for item in outputs.get("health", []) if not item["passed"]]
    third = [f"{item['name']}：{'满足' if item['passed'] else '不满足'}，{item['detail']}"
             for item in outputs.get("thirdParty", []) if not item["passed"]]
    if not failing and not third:
        return "链路健康检查全部正常，逐项结果见数据块。"
    return "\n".join(f"- {line}" for line in [*failing, *third])


def _decision(item: Mapping[str, Any]) -> Decision:
    advice = item.get("advice") or {}
    return Decision(f"{item['id']} [{SuggestionKind(item['kind']).label}] {item['subject']}",
                    advice.get("recommendation") or DEFAULT_ADVICE[0], advice.get("reason") or DEFAULT_ADVICE[1])


def build(outputs: Mapping[str, Any], week: date, window: Window, zone: tzinfo | None,
          trends: Mapping[tuple[str, str], Sequence[float | None]], now: datetime, handoff: str) -> HandoffDocument:
    """outputs 为 learn 交接文档的 outputs；待处理的建议逐条写进「需要决定」，带 advice 时用其中的推荐与理由。"""
    previous = {key: (values[-1] if values else None) for key, values in trends.items()}
    local = now.astimezone(zone) if zone is not None else now.astimezone()
    errors = [f"{item['item']}：{item['reason']}" for item in outputs.get("errors", [])]
    found = checks(outputs)
    header = Header(KIND, f"weekly-{week.isoformat()}", DocumentStatus.DONE, SOURCE, TARGET, week.isoformat(), now, now,
                    next="处理需要决定的建议与需要处理的事项")
    return HandoffDocument(
        header, conclusion(outputs, previous),
        {"done": f"统计周期 {week_text(window, zone)}，生成于 {local:%Y-%m-%d %H:%M}({zone_text(zone, now)})。",
         "outputs": _produced(outputs, trends), "evidence": _evidence(outputs),
         "deviations": "\n".join(f"- 统计出错：{line}" for line in errors) or NONE},
        {"checks": found} if found else {},
        decisions=tuple(_decision(item) for item in outputs.get("suggestions", [])),
        next_steps=tuple(NextStep(f"[{ATTENTION_LABELS.get(item['kind'], item['kind'])}] {item['subjectId']}："
                                  f"{item['summary']}；`{item['command']}`", TARGET)
                         for item in outputs.get("attention", [])),
        references=(Reference(handoff, "learn 的 JSON 交接文档"),),
        history=(Event(now, "生成周报"),))
