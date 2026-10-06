"""分数驱动的控制措施(redesign/08-learn.md 第 2 节)：只生成建议进收件箱，凡改配置的都由用户批准后自己修改。

- 模型一次通过率：first-pass 的 model= 维度连续 learn.controls.weeks 周(含本周)样本不少于 learn.minSamples 且都低于
  learn.controls.firstPassFloor 时，建议写代码的调用点换用更强或其他模型的别名(附对比评测的命令)；
- 用户频繁纠正：本周驳回修复计划、关闭 Issue、确认撤销 PR 分别达到 learn.controls.corrections 次，且对应关卡
  (plan-confirm、issue-approve、merge)为 auto 时，建议改为 user；
- 连续失败停下转待决定由编排的熔断完成(不改配置)，这里不生成建议。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.domain.enums import SuggestionKind
from tightrein.pipeline.learn.render.decision import SuggestionText
from tightrein.pipeline.learn.steps import fix_metrics
from tightrein.pipeline.learn.steps.metric_base import MetricContext, MetricValue
from tightrein.pipeline.learn.steps.metrics import trend
from tightrein.pipeline.learn.steps.suggestions import Draft

FIRST_PASS = "first-pass"
MODEL_DIMENSION = "model="
WRITER_ROLE = "routes.fix.executor"
GATES = {"plan-rejected": Gate.PLAN_CONFIRM, "issue-closed": Gate.ISSUE_APPROVE, "reverted": Gate.MERGE}
CORRECTION_LABELS = {"plan-rejected": "驳回修复计划", "issue-closed": "关闭 Issue", "reverted": "撤销合并"}


def _low_models(ctx: MetricContext, week: date, current: Sequence[MetricValue]) -> list[Draft]:
    weeks = ctx.config.whole_threshold("learn.controls.weeks")
    floor = ctx.config.threshold("learn.controls.firstPassFloor")
    minimum = ctx.config.whole_threshold("learn.minSamples")
    found = []
    for value in current:
        if value.metric != FIRST_PASS or not value.dimension.startswith(MODEL_DIMENSION):
            continue
        history = [(item.value, item.sample_size)
                   for item in trend(ctx.conn, FIRST_PASS, value.dimension, week - timedelta(weeks=1), weeks - 1)]
        series = [*history, (value.value, value.sample_size)]
        if len(series) < weeks or any(rate is None or size < minimum or rate >= floor for rate, size in series):
            continue
        model = value.dimension[len(MODEL_DIMENSION):]
        rates = "、".join(f"{rate:.0%}" for rate, _ in series)
        text = SuggestionText(
            conclusion=f"写代码的模型 {model} 一次通过率连续 {weeks} 周低于 {floor:.0%}，建议换用更强的模型",
            background=f"修复一次通过率(复现测试与项目检查第一轮即通过)按模型统计，{model} 最近 {weeks} 周依次为 "
                       f"{rates}，每周样本不少于 {minimum}。",
            accept=f"把写代码的调用点改用更强或其他模型的别名(配置键 `{WRITER_ROLE}`，或该别名在 models 中的模型)",
            reject="保持当前模型，继续观察",
            recommended=True, reason="一次通过率持续偏低会增加修正轮数与费用，写代码出错有复现测试与评审兜底，换模型的收益可由评测确认",
            apply=f"先运行 `tightrein admin eval run --module fix --model {model},<候选模型>` 对比，再自己修改 `{WRITER_ROLE}`")
        found.append(Draft(SuggestionKind.CONTROL, f"first-pass:{model}",
                           {"metric": FIRST_PASS, "model": model, "rates": [rate for rate, _ in series],
                            "advice": {"recommendation": text.accept, "reason": text.reason}},
                           (f"{FIRST_PASS}:{model}:{week.isoformat()}",), text))
    return found


def _corrections(ctx: MetricContext, week: date) -> list[Draft]:
    limit = ctx.config.whole_threshold("learn.controls.corrections")
    found = []
    for kind, subjects in fix_metrics.corrections(ctx).items():
        gate = GATES.get(kind)
        if gate is None or len(subjects) < limit or not gates.auto(ctx.config, gate):
            continue
        label = CORRECTION_LABELS[kind]
        text = SuggestionText(
            conclusion=f"本周用户{label} {len(subjects)} 次，建议关卡 {gate.value} 改为交用户确认",
            background=f"关卡 `gates.{gate.value}` 为 auto；本周用户{label}的对象：{'、'.join(subjects)}。",
            accept=f"把 `gates.{gate.value}` 改为 `user`，这类决定改由用户确认",
            reject="保持自动，继续观察纠正次数",
            recommended=True, reason=f"用户频繁{label}说明这类自动决定的门槛偏松，交用户确认能减少返工",
            apply=f"在工作区 project.yaml 中写 `gates: {{{gate.value}: user}}`")
        found.append(Draft(SuggestionKind.CONTROL, f"gate:{gate.value}",
                           {"gate": gate.value, "kind": kind, "subjects": list(subjects),
                            "advice": {"recommendation": text.accept, "reason": text.reason}},
                           (f"{gate.value}:{week.isoformat()}",), text))
    return found


def drafts(ctx: MetricContext, week: date, current: Sequence[MetricValue]) -> list[Draft]:
    """week 为本周一；current 为本周已算出的指标。"""
    return [*_low_models(ctx, week, current), *_corrections(ctx, week)]
