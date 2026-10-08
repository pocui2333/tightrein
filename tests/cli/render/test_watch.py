import io
import re
from datetime import UTC
from typing import Any

import pytest

from tightrein.cli.render import live
from tightrein.cli.render.snapshot import watch_snapshot
from tightrein.cli.render.style import display_width
from tightrein.cli.render.watch import LINES, render_watch

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def lines_of(text: str) -> list[str]:
    return ANSI.sub("", text).split("\n")


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("collect_view", [False, True])
@pytest.mark.parametrize("width", [72, 96, 120])
def test_the_view_fits_wide_and_narrow_terminals(world: Any, language: str, collect_view: bool, width: int):
    output = render_watch(watch_snapshot(world.source), language, collect_view=collect_view, width=width,
                          color=True, zone=UTC)
    lines = lines_of(output)
    assert len(lines) == LINES
    assert {display_width(line) for line in lines} == {width}


def test_the_subject_card_shows_the_example_data(world: Any):
    lines = render_watch(watch_snapshot(world.source), "zh", width=120, color=False, zone=UTC).split("\n")
    assert lines[0].startswith(
        " tightrein watch  R-20261007T013000Z-implement  定时  ● 运行中  开始 02:37  已跑 42m  心跳 3s 前")
    assert lines[1].startswith(" Claude 5h 62%（离留量线 8%） · 周 41%（离 19%）    当前 claude · opus · 推理 high")
    assert lines[2].startswith("┌─ 0023 P2 缺陷 删除追问显示 ─") and lines[2].endswith(" 1.2M / 2M token（60%） ─┐")
    assert ("│ 准备 ✓1m ─ 定位 ✓2m ─ 方案 ✓4m ─ 定案 ✓自动 ─ 编码 ◐ r2/3 ─ 自检 ○ ─ 审查 ○ ─ 交付 ○"
            in lines[3])
    assert "│ 当前 实施·编码 r2 · 等模型回复 · 模型轮 —/30 · 时长 8m/30m" in lines[4]
    assert "│ 上轮 审查 ■ 不通过 2 条：src/notes.ts:88 未处理保存失败；src/api.ts:31 缺参数" in lines[5]
    assert "│ 进展 —（还没有上一轮）" in lines[6]
    assert "│ 累计 调用 11 · 重试 0 · 交回 1 · 改动 4/10 文件 60/400 行 · 已用 22m/2h" in lines[7]
    assert "│ 关卡 合并前需人工确认：改动命中高风险路径 package.json" in lines[8]
    assert lines[9].startswith("└─")
    assert lines[10].startswith(" 03:18:54  0023  实施·编码 r2")
    assert "✓ 改 src/notes.ts" in lines[10]
    assert "■ claude/opus：failed" in lines[11]
    assert lines[13].startswith(" 03:13:54  —") and "▲ 临时错误 429" in lines[13]
    assert lines[14].startswith(" 接下来 自检 r2 · 本次还排 0024 0025 · 结束后复盘")
    assert lines[15].startswith(" [q] 退出  [p] 暂停  [s] 急停  [c] 切换采集看板")


def test_english_uses_control_keys_and_has_the_same_layout(world: Any):
    snapshot = watch_snapshot(world.source)
    zh = render_watch(snapshot, "zh", width=120, color=False, zone=UTC).split("\n")
    en = render_watch(snapshot, "en", width=120, color=False, zone=UTC).split("\n")
    assert len(zh) == len(en) == LINES
    assert [line[:1] for line in zh] == [line[:1] for line in en]
    assert "prepare ✓1m ─ locate ✓2m ─ design ✓4m ─ approve ✓auto ─ code ◐ r2/3 ─ check ○" in en[3]
    assert "Now implement.code r2 · waiting for model" in en[4]
    assert en[14].startswith(" Next check r2 · queued 0024 0025 · retro after run")
    assert en[15].startswith(" [q] quit  [p] pause  [s] stop  [c] collect view")


def test_the_collect_card_replaces_the_subject_card_with_the_same_height(world: Any):
    lines = render_watch(watch_snapshot(world.source), "zh", collect_view=True, width=120, color=False,
                         zone=UTC).split("\n")
    assert len(lines) == LINES
    assert lines[2].startswith("┌─ 采集 ─")
    assert "告警 ⊘ 不启用" in lines[3] + lines[4] + lines[5]
    assert "API 模糊测试 ⊘ 不启用" in lines[3] + lines[4] + lines[5]
    assert "去重 ○ 等待" in lines[6]
    assert "熔断 无" in lines[7]
    assert lines[9].startswith("└─")


def test_runs_whose_process_is_gone_show_as_interrupted_with_the_take_over_command(world: Any):
    world.alive.clear()
    lines = render_watch(watch_snapshot(world.source), "zh", width=120, color=False, zone=UTC).split("\n")
    assert "● 中断（进程已不在）" in lines[0]
    assert lines[14].startswith(" 接下来 接管 tightrein run")


def test_a_failed_previous_call_is_shown_so_a_retry_does_not_look_stuck(world: Any):
    from tightrein.protocol.naming import FileName, format_iso
    from tightrein.store.files.json import write_json

    marker = world.layout.step_file("0023", FileName("implement.code", "started", "json", round=1))
    write_json(marker, {"point": "implement.code", "subject": "0023", "run": world.run, "tool": "claude",
                        "model": "opus", "effort": "high", "startedAt": format_iso(world.at(25)),
                        "endedAt": format_iso(world.at(10)), "status": "timeout", "durationMs": 1})
    lines = render_watch(watch_snapshot(world.source), "zh", width=120, color=False, zone=UTC).split("\n")
    assert "上次调用 timeout，重试中" in lines[4]


def test_colors_can_be_turned_off(world: Any):
    snapshot = watch_snapshot(world.source)
    assert "\x1b[" not in render_watch(snapshot, "zh", color=False, zone=UTC)
    assert "\x1b[" in render_watch(snapshot, "zh", color=True, zone=UTC)


def test_live_redraws_toggles_the_collect_view_and_passes_control_keys(world: Any):
    keys = iter(["c", "p", "s", "q"])
    seen: list[str] = []
    out = io.StringIO()
    live.run(lambda: watch_snapshot(world.source), "zh", collect_view=False, on_key=seen.append,
             stdout=out, read_key=lambda timeout: next(keys))
    frames = [frame for frame in out.getvalue().split("\n\n") if frame.strip()]
    assert len(frames) == 4
    assert "┌─ 0023" in frames[0] and "┌─ 采集" in frames[1]
    assert seen == ["p", "s"]
    assert "\x1b[" not in out.getvalue()  # 不是终端：不着色、不切备用屏幕
