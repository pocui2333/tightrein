"""status 的渲染：一次性快照，不超过 20 行，小窗口不滚屏。

自上而下：系统总览框(接入、控制、时段、配置、下次定时；当前与上次运行；两个工具的额度) → 等你处理(每条两行，
要敲的命令单独一行便于复制) → 进行中与排队 → 各阶段存量 → 今天与本周的汇总 → 健康与盲区。
每行按显示宽度截断并补齐到同一宽度；文字取自文案表，布局、行数、颜色两种语言相同。
"""

from __future__ import annotations

import sys
from datetime import datetime, tzinfo

from tightrein.cli.render import labels
from tightrein.cli.render.labels import NONE, t
from tightrein.cli.render.snapshot import (
    VERDICTS,
    ActiveItem,
    QuotaInfo,
    StatusSnapshot,
    Totals,
    WaitingItem,
)
from tightrein.cli.render.style import (
    DEFAULT_WIDTH,
    Line,
    bar,
    box_bottom,
    box_row,
    box_top,
    join,
    render,
    span,
    use_color,
)

MAX_LINES = 20
MAX_WAITING = 2  # 等你处理最多列出的条数(每条两行)；其余在标题里给出总数
COMMAND_COLUMN = 44  # 第二行中文档路径的起始列，命令与路径各自整段可复制


def render_status(snapshot: StatusSnapshot, language: str, *, width: int = DEFAULT_WIDTH,
                  color: bool | None = None, zone: tzinfo | None = None) -> str:
    lines = [*_overview(snapshot, language, width, zone), *_waiting(snapshot, language), *_active(snapshot, language),
             *_stages(snapshot, language, zone), *_totals(snapshot, language), _health(snapshot, language)]
    if len(lines) > MAX_LINES:
        raise AssertionError(f"status 超过 {MAX_LINES} 行：{len(lines)}")
    return render(lines, width, use_color(sys.stdout) if color is None else color)


# 系统总览框


def _overview(snapshot: StatusSnapshot, language: str, width: int, zone: tzinfo | None) -> list[Line]:
    now = snapshot.now
    title = [span("tightrein status", "value"), span(" ─ ", "frame"), span(snapshot.project, "text")]
    right = [span(snapshot.commit or NONE, "text"), span(" ─ ", "frame"),
             span(now.astimezone(zone).strftime("%m-%d %H:%M"), "text")]
    rows = [_system(snapshot, language, zone), _runs(snapshot, language, zone)]
    rows += [_quota(snapshot, language, quota, index) for index, quota in enumerate(snapshot.quotas[:2])]
    return [box_top(title, right, width), *(box_row(row, width) for row in rows), box_bottom(width)]


def _system(snapshot: StatusSnapshot, language: str, zone: tzinfo | None) -> Line:
    gap = [span("   ")]
    if snapshot.setup_missing:
        setup = [span("● " + t(language, "status.setup_missing", count=len(snapshot.setup_missing)), "bad"),
                 span(" " + snapshot.setup_missing[0], "text")]
    else:
        setup = [span("● " + t(language, "status.ready"), "ok")]
    control = snapshot.control
    if control.mode == "normal":
        mode = [span("● " + t(language, "status.control_normal"), "ok")]
    else:
        role = "warn" if control.mode == "paused" else "bad"
        mode = [span("● " + t(language, f"status.control_{control.mode}"), role),
                span(" " + labels.clock(control.since, zone), "value")]
        if control.note:
            mode.append(span(" " + control.note, "text"))
    window = t(language, "status.window_in" if snapshot.in_window else "status.window_out")
    if snapshot.config_issues:
        config = [span(t(language, "status.config_invalid", count=len(snapshot.config_issues)), "bad")]
    elif snapshot.config_pending:
        config = [span(t(language, "status.config_pending"), "warn")]
    else:
        config = [span(t(language, "status.config_ok"), "ok")]
    next_run = (t(language, "status.next_run_none") if snapshot.next_run is None else
                t(language, "status.next_run_at", time=labels.clock(snapshot.next_run, zone),
                  later=labels.later(language, snapshot.now, snapshot.next_run)))
    return join([
        [span(t(language, "status.setup") + " ", "text"), *setup],
        [span(t(language, "status.control") + " ", "text"), *mode],
        [span(t(language, "status.window") + " ", "text"), span(window, "value")],
        [span(t(language, "status.config") + " ", "text"), *config],
        [span(t(language, "status.next_run") + " ", "text"), span(next_run, "value")],
    ], gap)


def _runs(snapshot: StatusSnapshot, language: str, zone: tzinfo | None) -> Line:
    dot = [span(" · ", "frame")]
    now = snapshot.now
    current, last = snapshot.current, snapshot.last
    parts: list[Line] = []
    if current is None:
        parts.append([span(t(language, "status.now") + " ", "text"), span(t(language, "status.idle"), "value")])
    elif current.status == "interrupted":
        parts += [
            [span(t(language, "status.now") + " ", "text"), span(current.id, "value")],
            [span("● " + t(language, f"common.gone_{current.gone_reason}"), "bad")],
            [span(t(language, "common.take_over") + " ", "text"), span("tightrein run", "command")],
        ]
    else:
        parts += [
            [span(t(language, "status.now") + " ", "text"), span(current.id, "value")],
            [span(t(language, f"common.trigger_{current.trigger or 'unknown'}"), "text")],
            [span(labels.point_name(language, current.stage), "active")],
            [span(t(language, "status.elapsed", value=labels.duration((now - current.started_at).total_seconds())),
                  "value")],
            [span(t(language, "status.beat", value=labels.ago(language, now, current.heartbeat_at)), "text")],
        ]
    if last is not None:
        role = "ok" if last.status == "done" else "bad" if last.status in ("failed", "interrupted") else "text"
        parts.append([span(t(language, "status.last") + " ", "text"),
                      span(t(language, f"common.run_{last.status}"), role),
                      span(" " + labels.clock(last.ended_at, zone), "text")])
    return join(parts, dot)


def _quota(snapshot: StatusSnapshot, language: str, quota: QuotaInfo, index: int) -> Line:
    now = snapshot.now
    name = labels.tool_name(quota.tool)
    line: Line = [span(f"{name:<6} 5h ", "text"), *_bar(quota.five_hour, quota.reserve_five_hour, quota.rejected),
                  span(" " + labels.percent(quota.five_hour), "value"),
                  span(" " + t(language, "status.reset", value=labels.duration(_until(now, quota.five_hour_resets_at))),
                       "text"),
                  span("   " + t(language, "status.week") + " ", "text"),
                  *_bar(quota.weekly, quota.reserve_weekly, quota.rejected),
                  span(" " + labels.percent(quota.weekly), "value"),
                  span(" " + t(language, "status.reset", value=labels.duration(_until(now, quota.weekly_resets_at))),
                       "text")]
    for model, ratio in quota.models:
        line += [span(f" {model} ", "text"), span(labels.percent(ratio), "value")]
    if index == 0:
        reached = bool(snapshot.reserve_reasons)
        line += [span("   " + t(language, "status.reserve") + " ", "text"),
                 span(t(language, "status.reserve_hit" if reached else "status.reserve_ok"),
                      "warn" if reached else "ok")]
    else:
        halted = snapshot.halted_until
        line += [span("   " + t(language, "status.halted") + " ", "text"),
                 span(t(language, "status.halted_no") if halted is None else
                      t(language, "status.halted_until", value=labels.left(language, now, halted)),
                      "ok" if halted is None else "bad")]
    return line


def _bar(ratio: float | None, reserve: float, rejected: bool) -> Line:
    cells = bar(ratio)
    if rejected or (ratio is not None and ratio >= reserve):
        return [span(cells[0].text, "bad" if rejected else "warn"), cells[1]]
    return cells


def _until(now: datetime, moment: datetime | None) -> float | None:
    return None if moment is None else (moment - now).total_seconds()


# 等你处理


def _waiting(snapshot: StatusSnapshot, language: str) -> list[Line]:
    items = snapshot.waiting
    count = len(items)
    head = t(language, "status.waiting", count=count)
    if count > MAX_WAITING:
        head += t(language, "status.waiting_more", count=count - MAX_WAITING)
    lines: list[Line] = [[span(("▲ " if count else "✓ ") + head, "warn" if count else "ok")]]
    for item in items[:MAX_WAITING]:
        lines += _waiting_item(snapshot, language, item)
    return lines


def _waiting_item(snapshot: StatusSnapshot, language: str, item: WaitingItem) -> list[Line]:
    role = "warn" if item.kind == "review" else "bad"
    first: Line = [span("  "), span(item.id, "value"), span(" "), span(item.severity or NONE, role), span(" "),
                   span(t(language, f"status.kind_{item.kind}"), role), span(" "), span(item.title, "text"),
                   span(" · ", "frame"), span(labels.point_name(language, item.point, item.round), "active"),
                   span(" · ", "frame"),
                   span(t(language, "status.waited",
                          value=labels.duration((snapshot.now - item.since).total_seconds())), "text")]
    if item.summary:
        first += [span(" · ", "frame"), span(item.summary, "text")]
    indent = "       "
    gap = max(1, COMMAND_COLUMN - len(indent) - len(item.command))
    second: Line = [span(indent), span(item.command, "command"), span(" " * gap), span(item.document, "frame")]
    return [first, second]


# 进行中与排队


def _active(snapshot: StatusSnapshot, language: str) -> list[Line]:
    stock = snapshot.stock
    if snapshot.active:
        head = _active_item(snapshot, language, snapshot.active[0])
    else:
        head = [span("○ " + t(language, "status.active") + " ", "text"), span(t(language, "common.none"), "text")]
    queue: Line = [span("  " + t(language, "status.queued") + " ", "text"), span(str(len(stock.queued)), "value"),
                   span(" · ", "frame"), span(t(language, "status.next") + " ", "text"),
                   span(stock.queued[0] if stock.queued else t(language, "common.none"), "value")]
    if len(snapshot.active) > 1:
        queue += [span(" · ", "frame"), span(t(language, "status.active_more") + " ", "text"),
                  span(" ".join(item.id for item in snapshot.active[1:]), "active")]
    queue += [span(" · ", "frame"), span(t(language, "status.held") + " ", "text"),
              span(" ".join(stock.held) if stock.held else t(language, "common.none"),
                   "warn" if stock.held else "text")]
    return [head, queue]


def _active_item(snapshot: StatusSnapshot, language: str, item: ActiveItem) -> Line:
    step = None if item.step_started is None else (snapshot.now - item.step_started).total_seconds()
    line: Line = [span("● " + t(language, "status.active") + " ", "active"), span(item.id, "value"), span(" "),
                  span(item.severity or NONE, "value"), span(" "), span(item.title, "text"), span(" · ", "frame"),
                  span(labels.point_name(language, item.point, item.round), "active"), span(" · ", "frame"),
                  span(t(language, "status.step") + " ", "text"),
                  span(f"{labels.duration(step)}/{labels.duration(item.step_limit_s)}", "value"), span(" · ", "frame"),
                  span(f"{labels.count(item.tokens_used)}/{labels.count(item.tokens_limit)} token",
                       "warn" if item.tokens_used >= item.tokens_limit * 0.8 else "value")]
    if item.files is not None:
        line += [span(" · ", "frame"), span(t(language, "status.files", count=item.files), "value")]
        if item.added is not None and item.deleted is not None:
            line += [span(f" +{item.added}", "ok"), span(f" −{item.deleted}", "bad")]
        elif item.lines is not None:
            line.append(span(" " + t(language, "status.lines", count=item.lines), "value"))
    return line


# 各阶段存量


def _stages(snapshot: StatusSnapshot, language: str, zone: tzinfo | None) -> list[Line]:
    stock = snapshot.stock
    now = snapshot.now
    dot = [span(" · ", "frame")]
    collect: Line = [span("  " + t(language, "points.collect") + " ", "active"),
                     span(t(language, "status.sources", enabled=stock.sources_enabled, total=stock.sources_total),
                          "value"),
                     *dot, span(t(language, "status.last") + " " + labels.clock(stock.last_collect, zone), "text")]
    for breaker in snapshot.health.breakers:
        if breaker.dependency.startswith("collect."):
            collect += [*dot, span(t(language, "status.breaker", source=labels.step_name(language, breaker.dependency),
                                     value=labels.left(language, now, breaker.reopens_at)), "bad")]
    problems = stock.problems
    collect += [span("   " + t(language, "points.collect.dedup") + " ", "active"),
                span(t(language, "status.dedup", new=problems.get("new", 0), watching=problems.get("watching", 0),
                       muted=problems.get("muted", 0), regressed=problems.get("regressed", 0)), "text")]
    assess: Line = [span("  " + t(language, "points.assess") + " ", "active"),
                    span(t(language, "status.verdicts", **{name: stock.verdicts.get(name, 0) for name in VERDICTS}),
                         "text"),
                    *dot, span(" ".join(f"{name} {value}" for name, value in stock.severities.items()), "value")]
    release: Line = [span("  " + t(language, "points.release") + " ", "active"),
                     span(t(language, "status.prs", count=stock.prs_open, ci=stock.prs_ci, merge=stock.prs_to_merge,
                            waited=labels.duration(None if stock.to_merge_since is None else
                                                   (now - stock.to_merge_since).total_seconds())), "text")]
    if stock.awaiting_deploy:
        release += [*dot, span(t(language, "status.awaiting_deploy", count=stock.awaiting_deploy), "warn")]
    if stock.accept_until is not None:
        release += [*dot, span(t(language, "status.accepting", value=labels.left(language, now, stock.accept_until)),
                               "active")]
    retro_open = sum(stock.retro_open.values())
    release += [span("   " + t(language, "points.retro") + " ", "active"),
                span(t(language, "status.retro", count=retro_open, week=stock.retro_new_week), "text"),
                *dot, span(t(language, "status.knowledge", pending=stock.knowledge_pending,
                             stale=stock.knowledge_stale), "text")]
    return [[span("┄ " + t(language, "status.stages"), "frame")], collect, assess, release]


# 汇总


def _totals(snapshot: StatusSnapshot, language: str) -> list[Line]:
    today, week = snapshot.today, snapshot.week
    dot = [span(" · ", "frame")]
    first: Line = [span("┄ " + t(language, "status.today") + " ", "frame"), *_outcome(language, today), *dot,
                   span(t(language, "status.found", signals=today.signals, problems=today.problems,
                          issues=today.issues), "text"), *dot,
                   span(t(language, "status.average", value=labels.duration(today.avg_duration_s)), "text"), *dot,
                   span(t(language, "status.approvals", auto=today.approvals_auto, manual=today.approvals_manual),
                        "text"), *dot, span(f"token {labels.count(today.total_tokens)}", "value")]
    second: Line = [span("  " + t(language, "status.week_total") + " ", "frame"), *_outcome(language, week), *dot,
                    span(t(language, "status.first_pass", value=labels.percent(week.first_pass)), "text"), *dot,
                    span(t(language, "status.avg_tokens", value=labels.count(week.avg_tokens)), "text"), *dot,
                    span(t(language, "status.cache_hit", value=labels.percent(week.cache_hit)), "text"), *dot,
                    span(f"token {labels.count(week.total_tokens)}", "value")]
    return [first, second]


def _outcome(language: str, totals: Totals) -> Line:
    return [span(t(language, "status.delivered") + " ", "text"), span(str(totals.delivered), "ok"),
            span(" " + t(language, "status.failed") + " ", "text"),
            span(str(totals.failed), "bad" if totals.failed else "value")]


# 健康与盲区


def _health(snapshot: StatusSnapshot, language: str) -> Line:
    health = snapshot.health
    now = snapshot.now
    trouble = bool(health.blind_spots or health.stray_worktrees or health.missed or health.stale_runs)
    line: Line = [span(("▲ " if trouble else "✓ ") + t(language, "status.health") + " ", "warn" if trouble else "ok")]
    blind = t(language, "status.blind", count=len(health.blind_spots))
    if health.blind_spots:
        names = t(language, "common.list_separator").join(labels.step_name(language, key) for key in health.blind_spots)
        blind += t(language, "status.blind_detail", names=names)
    parts: list[Line] = [[span(blind, "warn" if health.blind_spots else "text")]]
    parts.append([span(t(language, "status.stray", count=health.stray_worktrees),
                       "warn" if health.stray_worktrees else "text")])
    parts.append([span(t(language, "status.missed", count=health.missed), "warn" if health.missed else "text")])
    if health.stale_runs:
        parts.append([span(t(language, "status.stale", count=len(health.stale_runs)), "bad")])
    for breaker in health.breakers:
        if not breaker.dependency.startswith("collect."):
            parts.append([span(t(language, "status.breaker", source=breaker.dependency,
                                 value=labels.left(language, now, breaker.reopens_at)), "bad")])
    disk = t(language, "status.disk", value=labels.size(health.disk_bytes))
    if health.purge_runs:
        disk += " " + t(language, "status.purge", count=health.purge_runs)
    parts.append([span(disk, "text")])
    return line + join(parts, [span(" · ", "frame")])
