import io
import json
import re
from datetime import UTC
from typing import Any

import pytest

from tightrein.cli.render import live
from tightrein.cli.render.snapshot import watch_snapshot
from tightrein.cli.render.style import DEFAULT_HEIGHT, MIN_HEIGHT, display_width
from tightrein.cli.render.watch import render_watch
from tightrein.protocol import recovery
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.files.json import write_json
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def lines_of(text: str) -> list[str]:
    return ANSI.sub("", text).split("\n")


def render(world: Any, language: str = "zh", **options: Any) -> list[str]:
    options = {"width": 120, "color": False, "zone": UTC} | options
    return render_watch(watch_snapshot(world.source), language, **options).split("\n")


def section(lines: list[str], title: str) -> list[str]:
    """分节线「── 标题 ───」之后、下一条分节线之前的各行。"""
    start = next(index for index, line in enumerate(lines) if line.startswith(f"── {title}"))
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("──"))
    return lines[start + 1:end]


def implementing(world: Any, identifier: str, point: str, *, github: int | None = None, minutes_ago: float = 3,
                 model: str = "opus") -> None:
    """一个正在实施、有进行中模型调用的 Issue(并行实施的另一个会话)。"""
    extra = {"github": {"number": github}} if github is not None else {}
    issues.save(world.source.conn, Issue(identifier, "implementing", f"并行 {identifier}", "bug", "problem",
                                         severity="P2", stage="implement", step=point.split(".")[1], extra=extra),
                world.clock)
    world.handoff(identifier, "implement.prepare", minutes_ago=minutes_ago + 2)
    marker = world.layout.step_file(identifier, FileName(point, "started", "json"))
    write_json(marker, {"point": point, "subject": identifier, "run": world.run, "tool": "claude", "model": model,
                        "effort": "high", "startedAt": format_iso(world.at(minutes_ago)), "endedAt": None,
                        "status": None, "durationMs": None, "turns": 7,
                        "tokens": {"input": 372_027, "output": 107, "cacheRead": 307_193, "cacheWrite": 64_822}})


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("collect_view", [False, True])
@pytest.mark.parametrize("width", [72, 96, 120])
@pytest.mark.parametrize("height", [MIN_HEIGHT, DEFAULT_HEIGHT, 40])
def test_every_refresh_has_the_same_number_of_lines_and_width(world: Any, language: str, collect_view: bool,
                                                              width: int, height: int):
    output = render_watch(watch_snapshot(world.source), language, collect_view=collect_view, width=width,
                          height=height, color=True, zone=UTC)
    lines = lines_of(output)
    assert len(lines) == height
    assert {display_width(line) for line in lines} == {width}


def test_the_layout_shows_run_quota_work_attention_events_and_queue(world: Any):
    lines = render(world)
    assert len(lines) == DEFAULT_HEIGHT
    assert lines[0].startswith(" tightrein watch  ai-interview-collector") and lines[0].endswith("10-07 03:19:00 ")
    assert lines[1].startswith(
        " ● 运行中  R-20261007T013000Z-implement · 定时 · 阶段 实施 · 开始 02:37 · 已跑 42m · 心跳 3s 前")
    assert lines[2].startswith(" Claude 5h ━━━━━━──── 62%（离留量线 8%） 重置 1h18m    周 ━━━━────── 41%（离 19%） 重置 3d")
    assert lines[3].startswith(" agy    5h ━━━─────── 34%（离留量线 36%） 重置 3h40m")
    assert lines[4].startswith("── 进行中 1 ──")
    row = lines[5]
    assert row.startswith(" 0023   P2 删除追问显示")
    assert "✓✓✓✓◐···  编码 r2" in row and "    8m  opus" in row and row.endswith("  1.2M/2M")
    assert lines[6].startswith("── 需要你处理 2 ──")
    assert lines[7].startswith(" ■ 0022  P1 出问题 · 方案 · 已等 1h15m  题型排序")
    assert lines[7].endswith(" tightrein show 0022 ")
    assert lines[8].startswith(" ▲ 0019  P2 待审核 · 定案 · 已等 48m  笔记自动保存")
    assert lines[8].endswith(" tightrein approve 0019 ")
    assert lines[9].startswith("── 最近事件 ──")
    assert lines[10].startswith(" 03:18:54  0023   实施·编码 r2   ✓ 改 src/notes.ts")
    assert lines[11].startswith(" 03:17:30  0023   实施·审查 r1   ■ claude/opus：failed，不通过 2 条")
    assert lines[12].startswith(" 03:15:00  0023   实施·自检 r1   ✓ lint、类型检查、测试 12/12")
    assert lines[13].startswith(" 03:13:54  —      协议·重试      ▲ 临时错误 429")
    assert lines[14].strip() == ""  # 事件不足时补空行，行数不变
    assert lines[-3] == "─" * 120
    assert lines[-2].startswith(" 排队  待修 2 · 待评估 16    下一批 0024 0025")
    assert lines[-1].startswith(" [q] 退出  [p] 暂停  [s] 急停  [c] 切换采集看板")


def test_english_uses_control_keys_and_has_the_same_layout(world: Any):
    zh, en = render(world, "zh"), render(world, "en")
    assert len(zh) == len(en) == DEFAULT_HEIGHT
    assert [line[:1] for line in zh] == [line[:1] for line in en]
    assert en[1].startswith(" ● running  R-20261007T013000Z-implement · scheduled · stage implement")
    assert en[4].startswith("── In progress 1 ──")
    assert "✓✓✓✓◐···  code r2" in en[5]
    assert en[7].startswith(" ■ 0022  P1 failed · design · waiting 1h15m")
    assert en[10].startswith(" 03:18:54  0023   implement.cod…")
    assert en[-2].startswith(" Queue  todo 2 · to assess 16    next 0024 0025")
    assert en[-1].startswith(" [q] quit  [p] pause  [s] stop  [c] collect view")


def test_parallel_issues_each_get_a_row(world: Any):
    implementing(world, "0030", "implement.design", github=41)
    implementing(world, "0031", "implement.code", model="sonnet")
    implementing(world, "0032", "implement.review.deep", minutes_ago=1)
    rows = section(render(world), "进行中 4")
    assert [row.split()[0] for row in rows] == ["0023", "0030", "0031", "0032"]
    assert rows[1].startswith(" 0030   #41 P2 并行 0030")
    assert "✓·◐·····  方案" in rows[1] and "    3m  372k 7轮 opus" in rows[1]
    assert "372k 7轮 sonnet" in rows[2]
    assert "✓·····◐·  审查" in rows[3] and "    1m  " in rows[3]  # 子步骤 implement.review.deep 记在审查那一格


def test_more_rows_than_fit_end_with_a_count(world: Any):
    for number in range(30, 40):
        implementing(world, f"00{number}", "implement.code")
    lines = render(world, height=MIN_HEIGHT)
    assert len(lines) == MIN_HEIGHT
    rows = section(lines, "进行中 11")
    assert rows[-1].startswith(" … 另 ")
    assert len(section(lines, "最近事件")) >= 2


def test_needs_attention_appears_only_when_something_waits_for_you(world: Any):
    assert any(line.startswith("── 需要你处理 2") for line in render(world))
    issues.save(world.source.conn, Issue("0019", "done", "笔记自动保存", "feature", "problem", severity="P2"),
                world.clock)
    world.layout.human_document("0022", "failure").unlink()
    calm = render(world)
    assert not any("需要你处理" in line for line in calm)
    assert len(calm) == DEFAULT_HEIGHT
    assert section(calm, "进行中 2")[0].startswith(" 0022 ")  # 出问题的文档删掉后回到进行中
    assert len(section(calm, "最近事件")) == DEFAULT_HEIGHT - 9 - 2  # 让出来的行给事件流水

    recovery.pause(world.layout, world.clock, note="看一下")
    paused = render(world)
    waiting = section(paused, "需要你处理 1")
    assert waiting[0].startswith(" ▲ 运行已暂停 0s 前  看一下") and waiting[0].endswith(" tightrein resume ")
    assert "● 暂停中" in paused[1]


def test_problems_handed_to_you_are_listed_with_the_command(world: Any):
    conn = world.source.conn
    conn.execute("UPDATE problems SET extra = json_set(extra, '$.assess', json('{\"manual\": true}')) "
                 "WHERE id IN ('P-0001', 'P-0002')")
    lines = render(world)
    waiting = section(lines, "需要你处理 3")
    assert waiting[-1].startswith(" ● 评估转人工的问题 2  P-0001 P-0002")
    assert waiting[-1].endswith(" tightrein show P-0001 ")
    assert "待评估 14" in lines[-2]


def test_runs_whose_process_is_gone_show_as_interrupted_with_the_take_over_command(world: Any):
    world.alive.clear()
    lines = render(world)
    assert lines[1].startswith(" ● 中断（进程已不在）  R-20261007T013000Z-implement · 定时 · 心跳 3s 前")
    waiting = section(lines, "需要你处理 3")
    assert waiting[0].startswith(" ■ 中断（进程已不在）") and waiting[0].endswith(" tightrein run ")


def test_a_failed_previous_call_is_shown_so_a_retry_does_not_look_stuck(world: Any):
    marker = world.layout.step_file("0023", FileName("implement.code", "started", "json", round=1))
    write_json(marker, {"point": "implement.code", "subject": "0023", "run": world.run, "tool": "claude",
                        "model": "opus", "effort": "high", "startedAt": format_iso(world.at(25)),
                        "endedAt": format_iso(world.at(10)), "status": "timeout", "durationMs": 1})
    row = section(render(world), "进行中 1")[0]
    assert "上次调用 timeout，重试中  删除追问显示" in row


def test_routine_events_are_dimmed_and_noise_is_left_out(world: Any):
    path = world.layout.events(world.run)
    with path.open("a", encoding="utf-8") as handle:
        for second, kind, summary in ((58, "action", "子进程环境去掉了：CLAUDECODE"), (59, "action", "claude/opus：ok"),
                                      (0, "effect", "passed：第 2 轮编码完成")):
            at = f"2026-10-07T03:{18 if second else 19}:{second:02d}Z"
            handle.write(json.dumps({"at": at, "run": world.run, "subject": "0023", "point": "implement.code",
                                     "kind": kind, "summary": summary, "refs": {}}, ensure_ascii=False) + "\n")
    events = section(render(world), "最近事件")
    assert events[0].startswith(" 03:19:00  0023   实施·编码      ✓ 第 2 轮编码完成")
    assert events[1].startswith(" 03:18:59  0023   实施·编码      · claude/opus：ok")
    assert not any("子进程环境" in line for line in events)
    colored = render_watch(watch_snapshot(world.source), "zh", width=120, color=True, zone=UTC)
    assert "\x1b[38;5;244m· \x1b[0m\x1b[38;5;244mclaude/opus：ok\x1b[0m" in colored  # 例行的整行淡显


def test_the_collect_view_replaces_the_work_rows(world: Any):
    lines = render(world, collect_view=True)
    assert len(lines) == DEFAULT_HEIGHT
    rows = section(lines, "采集")
    assert any("⊘ 告警 不启用" in row for row in rows)
    assert any("⊘ API 模糊测试 不启用" in row for row in rows)
    assert rows[-2].startswith(" 去重 · 等待")
    assert rows[-1].startswith(" 熔断 无")


def test_text_and_values_use_the_terminal_foreground_on_any_background(world: Any):
    colored = render_watch(watch_snapshot(world.source), "zh", width=120, color=True, zone=UTC)
    assert set(re.findall(r"\x1b\[38;5;(\d+)", colored)) == {"244"}  # 只有辅助文字用 256 色中灰
    assert "\x1b[1m0023\x1b[0m" in colored  # 数值：缺省前景色加粗
    assert "P2 删除追问显示" in colored  # 普通文字：不加转义，用终端的缺省前景色
    assert "\x1b[36;1;4mtightrein approve 0019\x1b[0m" in colored
    assert "\x1b[" not in render_watch(watch_snapshot(world.source), "zh", color=False, zone=UTC)


def test_live_redraws_toggles_the_collect_view_and_passes_control_keys(world: Any):
    keys = iter(["c", "p", "s", "q"])
    seen: list[str] = []
    out = io.StringIO()
    live.run(lambda: watch_snapshot(world.source), "zh", collect_view=False, on_key=seen.append,
             stdout=out, read_key=lambda timeout: next(keys))
    frames = [frame for frame in out.getvalue().split("\n\n") if frame.strip()]
    assert len(frames) == 4
    assert "── 进行中 1" in frames[0] and "── 采集" in frames[1]
    assert all(len(frame.split("\n")) == DEFAULT_HEIGHT for frame in frames)  # 不是终端：按缺省高度
    assert seen == ["p", "s"]
    assert "\x1b[" not in out.getvalue()  # 不是终端：不着色、不切备用屏幕
