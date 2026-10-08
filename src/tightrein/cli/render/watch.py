"""watch 的渲染：实时界面，固定 16 行，刷新时不滚屏、不抖动。

自上而下：运行顶栏 → 额度 → 当前对象卡片(8 行；运行的是采集或按 [c] 切换时换成采集卡片，行数不变) →
事件流水(4 行，不足补空行) → 后续走向 → 按键提示。进程已不在或心跳失效的运行显示为中断，并给出接管命令。
"""

from __future__ import annotations

import sys
from datetime import tzinfo

from tightrein.cli.render import labels
from tightrein.cli.render.labels import NONE, t
from tightrein.cli.render.snapshot import (
    CollectCard,
    EventLine,
    QuotaInfo,
    RunInfo,
    SourceMark,
    StepMark,
    SubjectCard,
    WatchSnapshot,
)
from tightrein.cli.render.style import (
    DEFAULT_WIDTH,
    Line,
    box_bottom,
    box_row,
    box_top,
    fit,
    join,
    render,
    span,
    use_color,
)

LINES = 16
CARD_ROWS = 6  # 卡片框内的行数(不含上下框线)
EVENT_ROWS = 4
SOURCE_COLUMNS = 3
POINT_COLUMN = 14  # 事件流水中「调用点 轮次」一列的显示宽度
STEP_MARKS = {"done": ("✓", "ok"), "active": ("◐", "active"), "waiting": ("○", "frame"), "skipped": ("⊘", "frame"),
              "failed": ("■", "bad"), "gate": ("▲", "warn")}
SOURCE_MARKS = {"done": ("✓", "ok"), "active": ("◐", "active"), "waiting": ("○", "frame"), "skipped": ("⊘", "frame"),
                "off": ("⊘", "frame"), "failed": ("■", "bad")}
EVENT_MARKS = {"ok": ("✓", "ok"), "bad": ("■", "bad"), "warn": ("▲", "warn"), "info": ("●", "active")}
RUN_ROLES = {"running": "ok", "paused": "warn", "finishing": "active", "interrupted": "bad", "done": "ok",
             "failed": "bad", "skipped": "text"}


def render_watch(snapshot: WatchSnapshot, language: str, *, collect_view: bool = False, width: int = DEFAULT_WIDTH,
                 color: bool | None = None, zone: tzinfo | None = None) -> str:
    collecting = collect_view or (snapshot.run is not None and snapshot.run.stage == "collect")
    card = _collect(snapshot, language, snapshot.collect, width) if collecting else _subject(snapshot, language, width)
    lines = [_top(snapshot, language, zone), _quota(snapshot, language), *card, *_events(snapshot, language, zone),
             _next(snapshot, language), _keys(language)]
    if len(lines) != LINES:
        raise AssertionError(f"watch 应为 {LINES} 行：{len(lines)}")
    return render(lines, width, use_color(sys.stdout) if color is None else color)


# 顶栏与额度


def _top(snapshot: WatchSnapshot, language: str, zone: tzinfo | None) -> Line:
    run = snapshot.run
    head: Line = [span(" tightrein watch", "value"), span("  ")]
    if run is None:
        return head + [span(t(language, "watch.no_run"), "text")]
    state = _run_state(snapshot, run)
    line = head + [span(run.id, "value"), span("  "), span(t(language, f"common.trigger_{run.trigger or 'unknown'}"),
                                                         "text"), span("  "),
                   span("● " + _run_label(language, run, state), RUN_ROLES.get(state, "text"))]
    if state == "interrupted":
        return line
    end = snapshot.now if run.ended_at is None else run.ended_at
    line += [span("  " + t(language, "watch.start") + " ", "text"), span(labels.clock(run.started_at, zone), "value"),
             span("  " + t(language, "watch.elapsed") + " ", "text"),
             span(labels.duration((end - run.started_at).total_seconds()), "value")]
    if run.ended_at is None:
        line += [span("  " + t(language, "watch.beat") + " ", "text"),
                 span(labels.ago(language, snapshot.now, run.heartbeat_at), "value")]
    return line


def _run_state(snapshot: WatchSnapshot, run: RunInfo) -> str:
    if run.status == "running" and snapshot.control.mode == "paused":
        return "paused"
    return run.status


def _run_label(language: str, run: RunInfo, state: str) -> str:
    if state == "interrupted":
        return t(language, f"common.gone_{run.gone_reason}")
    return t(language, f"common.run_{state}")


def _quota(snapshot: WatchSnapshot, language: str) -> Line:
    quota = snapshot.quota
    line: Line = [span(" ")]
    if quota is not None:
        line += _window(language, quota, "5h", quota.five_hour, quota.reserve_five_hour)
        line += [span(" · ", "frame")]
        line += _window(language, quota, t(language, "watch.week"), quota.weekly, quota.reserve_weekly, short=True)
    if snapshot.tool:
        parts = [snapshot.tool, snapshot.model or NONE]
        line += [span("    " + t(language, "watch.now") + " ", "text"), span(" · ".join(parts), "value")]
        if snapshot.effort:
            line += [span(" · " + t(language, "watch.effort") + " ", "text"), span(snapshot.effort, "value")]
    return line


def _window(language: str, quota: QuotaInfo, name: str, used: float | None, reserve: float, *,
            short: bool = False) -> Line:
    head = [span(f"{labels.tool_name(quota.tool)} " if not short else "", "text"), span(f"{name} ", "text"),
            span(labels.percent(used), "value")]
    if used is None:
        return head
    gap = reserve - used
    if quota.rejected or gap <= 0:
        return head + [span(t(language, "watch.reserve_hit"), "bad" if quota.rejected else "warn")]
    key = "watch.reserve_gap_short" if short else "watch.reserve_gap"
    return head + [span(t(language, key, value=labels.percent(gap)), "text")]


# 当前对象卡片


def _subject(snapshot: WatchSnapshot, language: str, width: int) -> list[Line]:
    card = snapshot.subject
    if card is None:
        rows: list[Line] = [[span(t(language, "watch.no_subject"), "text")]]
        return [box_top([span(t(language, "watch.subject_none"), "text")], [], width),
                *_rows(rows, width), box_bottom(width)]
    title: Line = [span(card.id, "value"), span(" "), span(card.severity or NONE, "value"), span(" "),
                   span(t(language, f"watch.kind_{card.kind}"), "text"), span(" "), span(card.title, "text")]
    share = card.tokens_used / card.tokens_limit if card.tokens_limit else None
    right: Line = [span(f"{labels.count(card.tokens_used)} / {labels.count(card.tokens_limit)} token", "value"),
                   span(t(language, "watch.share", value=labels.percent(share)),
                        "warn" if share is not None and share >= 0.8 else "text")]
    rows = [_steps(language, card.steps), _current(snapshot, language, card), _review(language, card),
            _progress(language, card), _total(language, card), _gate(language, card)]
    return [box_top(title, right, width), *_rows(rows, width), box_bottom(width)]


def _rows(rows: list[Line], width: int) -> list[Line]:
    rows = rows[:CARD_ROWS] + [[] for _ in range(CARD_ROWS - len(rows))]
    return [box_row(row, width) for row in rows]


def _steps(language: str, steps: tuple[StepMark, ...]) -> Line:
    parts = []
    for step in steps:
        mark, role = STEP_MARKS[step.state]
        name_role = "active" if step.state == "active" else "text"
        part: Line = [span(labels.step_name(language, step.point) + " ", name_role), span(mark, role)]
        if step.state == "done":
            detail = t(language, f"watch.approve_{step.note}") if step.note else labels.duration(step.duration_s)
            part.append(span(detail, "text"))
        elif step.state == "skipped" and step.note:
            part.append(span(step.note, "frame"))
        elif step.state == "active" and step.round:
            limit = f"/{step.rounds_limit}" if step.rounds_limit and step.rounds_limit > 1 else ""
            part.append(span(f" r{step.round}{limit}", "active"))
        parts.append(part)
    return join(parts, [span(" ─ ", "frame")])


def _current(snapshot: WatchSnapshot, language: str, card: SubjectCard) -> Line:
    dot = [span(" · ", "frame")]
    step = None if card.step_started is None else (snapshot.now - card.step_started).total_seconds()
    turns = card.call.turns if card.call else None
    parts: list[Line] = [
        [span(t(language, "watch.current") + " ", "text"), span(labels.point_name(language, card.point, card.round),
                                                                "active")],
        [span(t(language, f"watch.action_{card.action}"), "active")],
        [span(t(language, "watch.turns") + " ", "text"),
         span(f"{NONE if turns is None else turns}/{NONE if card.turns_limit is None else card.turns_limit}", "value")],
        [span(t(language, "watch.time") + " ", "text"),
         span(f"{labels.duration(step)}/{labels.duration(card.step_limit_s)}",
              "warn" if step and card.step_limit_s and step >= card.step_limit_s * 0.8 else "value")],
    ]
    tokens = card.step_tokens
    if tokens is not None:
        parts.append([span(t(language, "watch.tokens", input=labels.count(tokens.input),
                             output=labels.count(tokens.output), cache=labels.count(tokens.cache_read)), "text")])
    if card.last_failure:
        parts.append([span(t(language, "watch.last_failure", status=card.last_failure,
                             retrying=t(language, "watch.retrying") if card.call else ""), "warn")])
    return join(parts, dot)


def _review(language: str, card: SubjectCard) -> Line:
    review = card.review
    line: Line = [span(t(language, "watch.last_round") + " ", "text")]
    if review is None:
        return line + [span(NONE, "text")]
    line += [span(labels.step_name(language, review.point) + " ", "text")]
    if review.passed:
        return line + [span("✓ " + t(language, "watch.passed"), "ok")]
    line.append(span("■ " + t(language, "watch.failed", count=len(review.blockers)), "bad"))
    if review.blockers:
        separator = t(language, "common.item_separator")
        detail = separator.join(f"{location} {summary}".strip() for location, summary in review.blockers)
        line.append(span(t(language, "common.colon") + detail, "text"))
    return line


def _progress(language: str, card: SubjectCard) -> Line:
    line: Line = [span(t(language, "watch.progress") + " ", "text")]
    if card.progress is None:
        return line + [span(t(language, "watch.progress_first"), "text")]
    reasons = []
    if card.diff_changed is not None:
        reasons.append(t(language, "watch.diff_changed" if card.diff_changed else "watch.diff_same"))
    if card.blockers_changed is not None:
        reasons.append(t(language, "watch.blockers_changed" if card.blockers_changed else "watch.blockers_same"))
    head = t(language, "watch.progress_yes" if card.progress else "watch.progress_no")
    return line + [span(head, "ok" if card.progress else "bad"),
                   span(t(language, "common.colon") + t(language, "common.list_separator").join(reasons)
                        if reasons else "", "text")]


def _total(language: str, card: SubjectCard) -> Line:
    dot = [span(" · ", "frame")]
    files = NONE if card.files is None else str(card.files)
    lines = NONE if card.lines is None else str(card.lines)
    return [span(t(language, "watch.total") + " ", "text"),
            span(t(language, "watch.calls", count=card.calls), "text"), *dot,
            span(t(language, "watch.retries", count=card.retries), "warn" if card.retries else "text"), *dot,
            span(t(language, "watch.returned", count=card.returned), "warn" if card.returned else "text"), *dot,
            span(t(language, "watch.changed", files=f"{files}/{card.files_limit}", lines=f"{lines}/{card.lines_limit}"),
                 "text"), *dot,
            span(t(language, "watch.spent", value=f"{labels.duration(card.spent_s)}/"
                   f"{labels.duration(card.spent_limit_s)}"), "text")]


def _gate(language: str, card: SubjectCard) -> Line:
    line: Line = [span(t(language, "watch.gate") + " ", "text")]
    if card.gate_paths:
        return line + [span(t(language, "watch.gate_high_risk", paths=" ".join(card.gate_paths)), "warn")]
    if card.gate:
        return line + [span(t(language, "watch.gate_at", gate=card.gate), "warn")]
    return line + [span(t(language, "watch.gate_none"), "ok")]


# 采集卡片


def _collect(snapshot: WatchSnapshot, language: str, card: CollectCard, width: int) -> list[Line]:
    elapsed = None if card.started_at is None else (snapshot.now - card.started_at).total_seconds()
    title = [span(t(language, "points.collect"), "value")]
    right = [span(t(language, "watch.elapsed") + " ", "text"), span(labels.duration(elapsed), "value")]
    inner = width - 4
    column = inner // SOURCE_COLUMNS
    rows: list[Line] = []
    sources = list(card.sources)
    for start in range(0, len(sources), SOURCE_COLUMNS):
        row: Line = []
        for mark in sources[start:start + SOURCE_COLUMNS]:
            row += fit(_source(language, mark), column)
        rows.append(row)
    rows.append(_dedup(language, card))
    rows.append(_breaker(snapshot, language, card))
    return [box_top(title, right, width), *_rows(rows, width), box_bottom(width)]


def _source(language: str, mark: SourceMark) -> Line:
    symbol, role = SOURCE_MARKS[mark.state]
    line: Line = [span(labels.step_name(language, mark.key) + " ", "text"), span(symbol, role)]
    if mark.state == "off":
        return line + [span(" " + t(language, "watch.source_off"), "frame")]
    if mark.state == "skipped":
        return line + [span(" " + t(language, "watch.source_skipped", reason=mark.reason or NONE), "frame")]
    if mark.state == "waiting":
        return line + [span(" " + t(language, "watch.source_waiting"), "frame")]
    if mark.read is not None:
        line.append(span(" " + t(language, "watch.read", count=labels.count(mark.read)), "text"))
    if mark.produced is not None:
        line.append(span(" " + t(language, "watch.produced", count=labels.count(mark.produced)), "value"))
    if mark.duration_s is not None:
        line.append(span(" " + labels.duration(mark.duration_s), "text"))
    return line


def _dedup(language: str, card: CollectCard) -> Line:
    line: Line = [span(labels.step_name(language, "collect.dedup") + " ", "text")]
    if card.dedup is None:
        return line + [span("○ " + t(language, "watch.source_waiting"), "frame")]
    new, merged, muted, regressed = card.dedup
    return line + [span(t(language, "watch.dedup", new=new, merged=merged, muted=muted), "text"),
                   span(" · " + t(language, "watch.regressed", count=regressed), "bad" if regressed else "text")]


def _breaker(snapshot: WatchSnapshot, language: str, card: CollectCard) -> Line:
    line: Line = [span(t(language, "watch.breaker") + " ", "text")]
    breaker = card.breaker
    if breaker is None:
        return line + [span(t(language, "common.none"), "ok")]
    return line + [span(t(language, "watch.breaker_open", source=labels.step_name(language, breaker.dependency),
                          failures=breaker.failures, value=labels.left(language, snapshot.now, breaker.reopens_at)),
                        "bad")]


# 事件、后续、按键


def _events(snapshot: WatchSnapshot, language: str, zone: tzinfo | None) -> list[Line]:
    lines = [_event(language, event, zone) for event in snapshot.events[:EVENT_ROWS]]
    if not lines:
        lines = [[span(" " + t(language, "watch.no_events"), "frame")]]
    return lines + [[] for _ in range(EVENT_ROWS - len(lines))]


def _event(language: str, event: EventLine, zone: tzinfo | None) -> Line:
    symbol, role = EVENT_MARKS[event.mark]
    point = fit([span(labels.point_name(language, event.point, event.round), "active")], POINT_COLUMN)
    return [span(" " + labels.clock(event.at, zone, seconds=True) + "  ", "frame"),
            span(f"{event.subject or NONE:<6}", "value"), *point, span(" "), span(symbol + " ", role),
            span(event.summary, "warn" if event.mark == "warn" else "text")]


def _next(snapshot: WatchSnapshot, language: str) -> Line:
    line: Line = [span(" " + t(language, "watch.next") + " ", "text")]
    run = snapshot.run
    if run is not None and run.status == "interrupted":
        return line + [span(t(language, "common.take_over") + " ", "warn"), span("tightrein run", "command")]
    if snapshot.control.mode != "normal":
        return line + [span(t(language, f"watch.control_{snapshot.control.mode}") + " ", "warn"),
                       span("tightrein resume", "command")]
    if run is None or run.status not in ("running", "finishing"):
        return line + [span(t(language, "watch.start_run") + " ", "text"), span("tightrein run", "command")]
    parts: list[Line] = []
    if snapshot.next_point:
        round_ = snapshot.subject.round if snapshot.subject is not None else None
        parts.append([span(labels.step_name(language, snapshot.next_point) + (f" r{round_}" if round_ else ""),
                           "active")])
    if snapshot.queued:
        parts.append([span(t(language, "watch.queued") + " ", "text"), span(" ".join(snapshot.queued), "value")])
    if snapshot.retro_after:
        parts.append([span(t(language, "watch.retro_after"), "text")])
    return line + (join(parts, [span(" · ", "frame")]) if parts else [span(NONE, "text")])


def _keys(language: str) -> Line:
    keys = (("q", "watch.key_quit"), ("p", "watch.key_pause"), ("s", "watch.key_stop"), ("c", "watch.key_collect"))
    return [span(" ")] + join([[span(f"[{key}]", "value"), span(" " + t(language, name), "frame")]
                               for key, name in keys], [span("  ")])
