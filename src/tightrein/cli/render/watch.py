"""watch 的渲染：实时界面，行数随终端高度(同一高度下每次刷新行数相同)，刷新时不滚屏、不抖动。

自上而下：标题与时刻 → 运行(状态、阶段、已跑、心跳) → 两个工具的额度 → 进行中(并行的每个对象一行：
编号、标题、步骤进度、当前步骤、在这一步多久、模型与轮数、token) → 需要你处理(只在有要你处理的事时出现，
每条给出要敲的命令) → 最近事件(行数随终端高度) → 排队 → 按键提示。按 [c] 时「进行中」换成采集看板。
"""

from __future__ import annotations

import sys
from datetime import datetime, tzinfo

from tightrein.cli.render import labels
from tightrein.cli.render.labels import NONE, t
from tightrein.cli.render.snapshot import (
    ActiveRow,
    CollectCard,
    EventLine,
    QuotaInfo,
    RunInfo,
    SourceMark,
    StepMark,
    WaitingItem,
    WatchSnapshot,
)
from tightrein.cli.render.style import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MIN_HEIGHT,
    Line,
    bar,
    display_width,
    fit,
    join,
    pad_between,
    render,
    rule,
    span,
    use_color,
)

FIXED_LINES = 9  # 标题、运行、两行额度、进行中与事件的分节线、底线、排队、按键
MIN_EVENTS = 2
SOURCE_COLUMNS = 3
ID_COLUMN = 6  # 0025、P-0038
STEP_COLUMN = 10
TIME_COLUMN = 6
POINT_COLUMN = 14  # 事件流水中「调用点 轮次」一列
SUBJECT_COLUMN = 6
STEP_MARKS = {"done": ("✓", "ok"), "active": ("◐", "active"), "waiting": ("·", "frame"), "skipped": ("⊘", "frame"),
              "failed": ("■", "bad"), "gate": ("▲", "warn"), "returned": ("↺", "warn")}
SOURCE_MARKS = {"done": ("✓", "ok"), "active": ("◐", "active"), "waiting": ("·", "frame"), "skipped": ("⊘", "frame"),
                "off": ("⊘", "frame"), "failed": ("■", "bad")}
# 事件：一步的结果、程序做完的动作为 ok；模型调用成功是例行的，整行淡显；决定为 info
EVENT_MARKS = {"ok": ("✓", "ok"), "bad": ("■", "bad"), "warn": ("▲", "warn"), "gate": ("▲", "warn"),
               "info": ("●", "active"), "routine": ("·", "frame")}
EVENT_TEXT = {"ok": "text", "bad": "bad", "warn": "warn", "gate": "warn", "info": "text", "routine": "frame"}
RUN_ROLES = {"running": "ok", "paused": "warn", "finishing": "active", "interrupted": "bad", "done": "ok",
             "failed": "bad", "skipped": "text"}
QUOTA_LINES = 2


def render_watch(snapshot: WatchSnapshot, language: str, *, collect_view: bool = False, width: int = DEFAULT_WIDTH,
                 height: int = DEFAULT_HEIGHT, color: bool | None = None, zone: tzinfo | None = None) -> str:
    height = max(height, MIN_HEIGHT)
    head = [_title(snapshot, width, zone), _run(snapshot, language, zone), *_quotas(snapshot, language, width)]
    attention = _attention(snapshot, language, width)
    room = height - FIXED_LINES
    shown_attention = attention[:max(2, room // 4)] if attention else []
    if len(shown_attention) < len(attention):
        hidden = len(attention) - len(shown_attention) + 1
        shown_attention[-1] = [span("   " + t(language, "watch.more", count=hidden), "frame")]
    room -= len(shown_attention) + (1 if attention else 0)
    if collect_view:
        title: Line = [span(t(language, "watch.collect_title"), "value")]
        section = _collect(snapshot, language, snapshot.collect, width)
        right = _collect_elapsed(snapshot, language, snapshot.collect)
    else:
        title = [span(t(language, "watch.active", count=len(snapshot.active)), "value")]
        section = _active(snapshot, language, width)
        right = []
    rows = _cap(section, room - MIN_EVENTS, language)
    events = _events(snapshot, language, zone, room - len(rows))
    lines = [*head, rule(title, width, right), *rows]
    if attention:
        lines += [rule([span(t(language, "watch.attention", count=len(attention)), "warn")], width),
                  *shown_attention]
    lines += [rule([span(t(language, "watch.events"), "value")], width), *events, rule([], width),
              _queue(snapshot, language), _keys(language)]
    if len(lines) != height:
        raise AssertionError(f"watch 应为 {height} 行：{len(lines)}")
    return render(lines, width, use_color(sys.stdout) if color is None else color)


def _cap(rows: list[Line], limit: int, language: str) -> list[Line]:
    """放不下时最后一行换成「另 N 个」。"""
    limit = max(limit, 1)
    if len(rows) <= limit:
        return rows
    hidden = len(rows) - limit + 1
    return rows[:limit - 1] + [[span(" " + t(language, "watch.more", count=hidden), "frame")]]


# 标题、运行与额度


def _title(snapshot: WatchSnapshot, width: int, zone: tzinfo | None) -> Line:
    left = [span(" tightrein watch", "value"), span("  "), span(snapshot.project, "text")]
    right = [span(snapshot.now.astimezone(zone).strftime("%m-%d %H:%M:%S"), "value"), span(" ")]
    return pad_between(left, right, width)


def _run(snapshot: WatchSnapshot, language: str, zone: tzinfo | None) -> Line:
    run = snapshot.run
    if run is None:
        return [span(" " + t(language, "watch.no_run"), "text")]
    state = _run_state(snapshot, run)
    dot = [span(" · ", "frame")]
    parts: list[Line] = [[span(" ● " + _run_label(language, run, state), RUN_ROLES.get(state, "text")),
                          span("  "), span(run.id, "value")],
                         [span(t(language, f"common.trigger_{run.trigger or 'unknown'}"), "text")]]
    if snapshot.stage:
        parts.append([span(t(language, "watch.stage") + " ", "text"),
                      span(labels.point_name(language, snapshot.stage), "active")])
    if state != "interrupted":
        end = snapshot.now if run.ended_at is None else run.ended_at
        parts += [[span(t(language, "watch.start") + " ", "text"), span(labels.clock(run.started_at, zone), "value")],
                  [span(t(language, "watch.elapsed") + " ", "text"),
                   span(labels.duration((end - run.started_at).total_seconds()), "value")]]
    if run.ended_at is not None:
        parts.append([span(t(language, "watch.ended") + " ", "text"), span(labels.clock(run.ended_at, zone), "value")])
    else:
        parts.append([span(t(language, "watch.beat") + " ", "text"),
                      span(labels.ago(language, snapshot.now, run.heartbeat_at),
                           "bad" if state == "interrupted" else "value")])
    return join(parts, dot)


def _run_state(snapshot: WatchSnapshot, run: RunInfo) -> str:
    if run.status == "running" and snapshot.control.mode == "paused":
        return "paused"
    return run.status


def _run_label(language: str, run: RunInfo, state: str) -> str:
    if state == "interrupted":
        return t(language, f"common.gone_{run.gone_reason}")
    return t(language, f"common.run_{state}")


def _quotas(snapshot: WatchSnapshot, language: str, width: int) -> list[Line]:
    cells = 10 if width >= DEFAULT_WIDTH else 6
    lines = [_quota(snapshot, language, quota, cells) for quota in snapshot.quotas[:QUOTA_LINES]]
    return lines + [[] for _ in range(QUOTA_LINES - len(lines))]


def _quota(snapshot: WatchSnapshot, language: str, quota: QuotaInfo, cells: int) -> Line:
    return [span(f" {labels.tool_name(quota.tool):<7}", "value"),
            *_window(snapshot, language, quota, "5h", quota.five_hour, quota.reserve_five_hour,
                     quota.five_hour_resets_at, cells, short=False),
            span("    "),
            *_window(snapshot, language, quota, t(language, "watch.week"), quota.weekly, quota.reserve_weekly,
                     quota.weekly_resets_at, cells, short=True)]


def _window(snapshot: WatchSnapshot, language: str, quota: QuotaInfo, name: str, used: float | None,
            reserve: float, resets_at: datetime | None, cells: int, *, short: bool) -> Line:
    over = used is not None and used >= reserve
    role = "bad" if quota.rejected else "warn" if over else "active"
    line: Line = [span(name + " ", "text"), *bar(used, cells, role), span(" " + labels.percent(used),
                                                                          "value" if role == "active" else role)]
    if quota.rejected:
        line.append(span(t(language, "watch.rejected"), "bad"))
    elif over:
        line.append(span(t(language, "watch.reserve_hit"), "warn"))
    elif used is not None:
        key = "watch.reserve_gap_short" if short else "watch.reserve_gap"
        line.append(span(t(language, key, value=labels.percent(reserve - used)), "frame"))
    if resets_at is not None:
        line.append(span(" " + t(language, "status.reset",
                                 value=labels.duration((resets_at - snapshot.now).total_seconds())), "frame"))
    return line


# 进行中


def _active(snapshot: WatchSnapshot, language: str, width: int) -> list[Line]:
    rows = snapshot.active
    if not rows:
        return [[span(" " + t(language, "watch.no_subject"), "frame")]]
    github = max((display_width(f"#{row.github}") for row in rows if row.github is not None), default=0)
    severity = 2 if any(row.severity for row in rows) else 0
    strip = max(len(row.steps) for row in rows)
    budget = 9 if width >= 100 and any(row.tokens_limit for row in rows) else 0
    detail = 22 if width >= 110 else 18 if width >= DEFAULT_WIDTH else 14
    fixed = 1 + ID_COLUMN + 1 + (github + 1 if github else 0) + (severity + 1 if severity else 0) \
        + 2 + strip + 2 + STEP_COLUMN + 1 + TIME_COLUMN + 2 + detail + (2 + budget if budget else 0)
    title = max(width - fixed, 6)
    widths = (github, severity, title, strip, detail, budget)
    return [_row(snapshot, language, row, widths) for row in rows]


def _row(snapshot: WatchSnapshot, language: str, row: ActiveRow,
         widths: tuple[int, int, int, int, int, int]) -> Line:
    github, severity, title, strip, detail, budget = widths
    line: Line = [span(" "), *fit([span(_row_id(language, row), "value")], ID_COLUMN), span(" ")]
    if github:
        line += [*fit([span(f"#{row.github}" if row.github is not None else "", "frame")], github), span(" ")]
    if severity:
        role = "bad" if row.severity in ("P0", "P1") else "text"
        line += [*fit([span(row.severity or "", role)], severity), span(" ")]
    line += [*fit([*_notes(language, row), span(_row_title(language, row), "text")], title), span("  "),
             *fit(_strip(row.steps), strip), span("  "),
             *fit(_step(language, row), STEP_COLUMN), span(" "),
             *_right(_elapsed(snapshot, row), TIME_COLUMN), span("  "),
             *fit(_detail(language, row), detail)]
    if budget:
        line += [span("  "), *_right(_budget(row), budget)]
    return line


def _row_id(language: str, row: ActiveRow) -> str:
    return labels.point_name(language, row.id) if row.kind in ("collect", "run") else row.id


def _row_title(language: str, row: ActiveRow) -> str:
    if row.kind == "collect":
        sources = [mark for mark in row.steps if mark.point != "collect.dedup"]
        done = sum(1 for mark in sources if mark.state in ("done", "skipped", "failed"))
        return t(language, "watch.sources", done=done, total=len(sources))
    if row.kind == "run":
        return labels.point_name(language, row.point)
    return row.title


def _strip(steps: tuple[StepMark, ...]) -> Line:
    return [span(STEP_MARKS[mark.state][0], STEP_MARKS[mark.state][1]) for mark in steps]


def _step(language: str, row: ActiveRow) -> Line:
    point = row.step or row.point
    if point is None:
        return [span(NONE, "frame")]
    name = labels.step_name(language, point) if "." in point else labels.point_name(language, point)
    if row.round and row.round > 1:
        name += f" r{row.round}"
    return [span(name, "active" if row.action != "idle" else "text")]


def _elapsed(snapshot: WatchSnapshot, row: ActiveRow) -> Line:
    if row.step_started is None:
        return [span(NONE, "frame")]
    seconds = (snapshot.now - row.step_started).total_seconds()
    limit = row.step_limit_s
    role = "bad" if limit and seconds >= limit else "warn" if limit and seconds >= limit * 0.8 else "value"
    return [span(labels.duration(seconds), role)]


def _notes(language: str, row: ActiveRow) -> Line:
    """要留意的情况放在标题前：上一次调用没成功(正在重试，不是卡死)、与上一轮比没有进展。"""
    line: Line = []
    if row.last_failure:
        retrying = t(language, "watch.retrying") if row.call else ""
        line.append(span(t(language, "watch.last_failure", status=row.last_failure, retrying=retrying) + "  ", "warn"))
    if row.stalled:
        line.append(span(t(language, "watch.stalled") + "  ", "warn"))
    return line


def _detail(language: str, row: ActiveRow) -> Line:
    """正在调用模型时：这次调用读入的 token、第几轮、模型；否则是在做什么(跑测试、等 CI 等)。"""
    call = row.call
    if call is None:
        return [span(t(language, f"watch.action_{row.action}"), "frame" if row.action == "idle" else "text")]
    parts: list[Line] = []
    if call.tokens is not None:
        parts.append([span(labels.count(call.tokens.input), "value")])
    if call.turns is not None:
        parts.append([span(t(language, "watch.turn", count=call.turns), "text")])
    parts.append([span(call.model or call.tool or NONE, "text")])
    return join(parts, [span(" ")])


def _budget(row: ActiveRow) -> Line:
    if row.tokens_used is None or not row.tokens_limit:
        return []
    share = row.tokens_used / row.tokens_limit
    role = "bad" if share >= 1 else "warn" if share >= 0.8 else "text"
    return [span(f"{labels.count(row.tokens_used)}/{labels.count(row.tokens_limit)}", role)]


def _right(line: Line, width: int) -> Line:
    """右对齐到 width；放不下时截断。"""
    gap = width - sum(display_width(item.text) for item in line)
    return [span(" " * gap), *line] if gap >= 0 else fit(line, width)


# 需要你处理


def _attention(snapshot: WatchSnapshot, language: str, width: int) -> list[Line]:
    """要人处理的事：运行中断、暂停或急停、待审核与出问题的 Issue、评估转人工的问题；每条右边是要敲的命令。"""
    items: list[tuple[Line, str]] = []
    run = snapshot.run
    if run is not None and run.status == "interrupted":
        items.append(([span(" ■ ", "bad"), span(t(language, f"common.gone_{run.gone_reason}"), "bad")],
                      "tightrein run"))
    if snapshot.control.mode != "normal":
        note = [span("  " + snapshot.control.note, "text")] if snapshot.control.note else []
        items.append(([span(" ▲ ", "warn"), span(t(language, f"watch.{snapshot.control.mode}"), "warn"),
                       span(" " + labels.ago(language, snapshot.now, snapshot.control.since), "frame"), *note],
                      "tightrein resume"))
    items += [(_waiting(snapshot, language, item), item.command) for item in snapshot.waiting]
    if snapshot.manual:
        ids = " ".join(snapshot.manual)
        items.append(([span(" ● ", "active"), span(t(language, "watch.manual", count=len(snapshot.manual)), "text"),
                       span("  " + ids, "value")], f"tightrein show {snapshot.manual[0]}"))
    return [pad_between(left, [span(command, "command"), span(" ")], width) for left, command in items]


def _waiting(snapshot: WatchSnapshot, language: str, item: WaitingItem) -> Line:
    review = item.kind == "review"
    mark, role = ("▲", "warn") if review else ("■", "bad")
    line: Line = [span(f" {mark} ", role), *fit([span(item.id, "value")], ID_COLUMN)]
    if item.github is not None:
        line.append(span(f"#{item.github} ", "frame"))
    if item.severity:
        line.append(span(item.severity + " ", "bad" if item.severity in ("P0", "P1") else "text"))
    step = labels.step_name(language, item.point) if item.point else NONE
    line += [span(t(language, "watch.review" if review else "watch.failed"), role),
             span(" · " + step, "active"),
             span(" · " + t(language, "watch.waited",
                            value=labels.duration((snapshot.now - item.since).total_seconds())), "text"),
             span("  " + item.title, "text")]
    return line


# 采集看板


def _collect_elapsed(snapshot: WatchSnapshot, language: str, card: CollectCard) -> Line:
    if card.started_at is None:
        return []
    elapsed = (snapshot.now - card.started_at).total_seconds()
    return [span(t(language, "watch.elapsed") + " ", "text"), span(labels.duration(elapsed), "value")]


def _collect(snapshot: WatchSnapshot, language: str, card: CollectCard, width: int) -> list[Line]:
    column = (width - 2) // SOURCE_COLUMNS
    rows: list[Line] = []
    sources = list(card.sources)
    for start in range(0, len(sources), SOURCE_COLUMNS):
        row: Line = [span(" ")]
        for mark in sources[start:start + SOURCE_COLUMNS]:
            row += fit(_source(language, mark), column)
        rows.append(row)
    rows.append(_dedup(language, card))
    rows.append(_breaker(snapshot, language, card))
    return rows


def _source(language: str, mark: SourceMark) -> Line:
    symbol, role = SOURCE_MARKS[mark.state]
    line: Line = [span(symbol + " ", role), span(labels.step_name(language, mark.key), "text")]
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
        line.append(span(" " + labels.duration(mark.duration_s), "frame"))
    return line


def _dedup(language: str, card: CollectCard) -> Line:
    line: Line = [span(" "), span(labels.step_name(language, "collect.dedup") + " ", "text")]
    if card.dedup is None:
        return line + [span("· " + t(language, "watch.source_waiting"), "frame")]
    new, merged, muted, regressed = card.dedup
    return line + [span(t(language, "watch.dedup", new=new, merged=merged, muted=muted), "text"),
                   span(" · " + t(language, "watch.regressed", count=regressed), "bad" if regressed else "text")]


def _breaker(snapshot: WatchSnapshot, language: str, card: CollectCard) -> Line:
    line: Line = [span(" " + t(language, "watch.breaker") + " ", "text")]
    breaker = card.breaker
    if breaker is None:
        return line + [span(t(language, "common.none"), "ok")]
    return line + [span(t(language, "watch.breaker_open", source=labels.step_name(language, breaker.dependency),
                          failures=breaker.failures, value=labels.left(language, snapshot.now, breaker.reopens_at)),
                        "bad")]


# 事件、排队、按键


def _events(snapshot: WatchSnapshot, language: str, zone: tzinfo | None, count: int) -> list[Line]:
    lines = [_event(language, event, zone) for event in snapshot.events[:count]]
    if not lines:
        lines = [[span(" " + t(language, "watch.no_events"), "frame")]]
    return lines + [[] for _ in range(count - len(lines))]


def _event(language: str, event: EventLine, zone: tzinfo | None) -> Line:
    symbol, role = EVENT_MARKS[event.mark]
    routine = event.mark == "routine"
    point = fit([span(labels.point_name(language, event.point, event.round), "frame" if routine else "active")],
                POINT_COLUMN)
    subject = fit([span(event.subject or NONE, "frame" if routine else "value")], SUBJECT_COLUMN)
    return [span(" " + labels.clock(event.at, zone, seconds=True) + "  ", "frame"), *subject, span(" "), *point,
            span(" "), span(symbol + " ", role), span(event.summary, EVENT_TEXT[event.mark])]


def _queue(snapshot: WatchSnapshot, language: str) -> Line:
    queue = snapshot.queue
    dot = [span(" · ", "frame")]
    parts: list[Line] = [[span(t(language, "watch.todo", count=len(queue.todo)), "value" if queue.todo else "text")],
                         [span(t(language, "watch.assess_pending", count=queue.assess_pending),
                               "value" if queue.assess_pending else "text")]]
    for key, value in (("ci", queue.ci), ("to_merge", queue.to_merge), ("deploying", queue.deploying),
                       ("accepting", queue.accepting)):
        if value:
            parts.append([span(t(language, f"watch.{key}", count=value), "text")])
    if queue.retro_after:
        parts.append([span(t(language, "watch.retro_after"), "text")])
    line: Line = [span(" " + t(language, "watch.queue") + "  ", "value"), *join(parts, dot)]
    if queue.todo:
        line += [span("    " + t(language, "watch.next_up") + " ", "frame"), span(" ".join(queue.todo), "text")]
    return line


def _keys(language: str) -> Line:
    keys = (("q", "watch.key_quit"), ("p", "watch.key_pause"), ("s", "watch.key_stop"), ("c", "watch.key_collect"))
    return [span(" ")] + join([[span(f"[{key}]", "value"), span(" " + t(language, name), "frame")]
                               for key, name in keys], [span("  ")])
