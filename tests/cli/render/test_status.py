import re
from datetime import UTC, timedelta
from typing import Any

import pytest

from tightrein.cli.render.snapshot import status_snapshot
from tightrein.cli.render.status import MAX_LINES, render_status
from tightrein.cli.render.style import display_width
from tightrein.store.files.atomic import write_text
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def lines_of(text: str) -> list[str]:
    return ANSI.sub("", text).split("\n")


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("width", [72, 96, 120])
def test_status_fits_the_line_limit_and_every_line_has_the_same_width(world: Any, language: str, width: int):
    output = render_status(status_snapshot(world.source), language, width=width, color=False, zone=UTC)
    lines = lines_of(output)
    assert len(lines) <= MAX_LINES
    assert {display_width(line) for line in lines} == {width}


def test_both_languages_have_the_same_layout(world: Any):
    snapshot = status_snapshot(world.source)
    zh = lines_of(render_status(snapshot, "zh", color=True, zone=UTC))
    en = lines_of(render_status(snapshot, "en", color=True, zone=UTC))
    assert len(zh) == len(en) == 20
    assert [line[:1] for line in zh] == [line[:1] for line in en]  # 框线与区块符号相同


def test_status_shows_the_example_data_in_chinese(world: Any):
    output = render_status(status_snapshot(world.source), "zh", width=120, color=False, zone=UTC)
    lines = output.split("\n")
    assert lines[0].startswith("┌─ tightrein status ─ ai-interview-collector ")
    assert "8f3a9e1 ─ 10-07 03:19 ─┐" in lines[0]
    assert "接入 ● 就绪   控制 ● 正常   时段 允许内   配置 全部生效   下次定时 03:30（11m 后）" in lines[1]
    assert "当前 R-20261007T013000Z-implement · 定时 · 实施 · 已跑 42m · 心跳 3s 前 · 上次 完成" in lines[2]
    assert "Claude 5h ━━━━━━──── 62% 重置 1h18m   周 ━━━━────── 41% 重置 3d   留量 未触线" in lines[3]
    assert "agy    5h ━━━─────── 34% 重置 3h40m" in lines[4] and "停机 否" in lines[4]
    assert lines[6].startswith("▲ 等你处理 2")
    assert lines[7].startswith("  0022 P1 出问题 题型排序 · 实施·方案 · 已等 1h15m · 被安全分类拒绝，备用模型也拒绝")
    assert lines[8].split() == ["tightrein", "show", "0022", "data/issues/0022/90-issue-failure.md"]
    assert lines[9].startswith("  0019 P2 待审核 笔记自动保存 · 实施·定案 · 已等 48m · 方案需确认：保存改为防抖 2s")
    assert lines[10].split() == ["tightrein", "approve", "0019", "data/issues/0019/90-issue-pending.md"]
    assert lines[11].startswith(
        "● 进行中 0023 P2 删除追问显示 · 实施·编码 r2 · 本步 8m/30m · 1.2M/2M token · 4 文件 +48 −12")
    assert lines[12].startswith("  排队 2 · 下一个 0024 · 手动接管 无")
    assert lines[13].startswith("┄ 各阶段")
    assert lines[14].startswith("  采集 来源 5/7 启用 · 上次 00:00   去重 待评 14 观察 4 抑制 3 回归 2")
    assert lines[15].startswith("  评估 成立 ")
    assert "P0 " in lines[15] and "P3 " in lines[15]
    assert lines[16].startswith("  发布 PR 0（CI 中 0 · 待合并 0 已等 —）   复盘 待看 0")
    assert lines[17].startswith("┄ 今天 交付 0 出问题 2 · 新信号 0 新问题 23 新 Issue 5")
    assert "确认 自动 1 人工 0 · token 509k" in lines[17]
    assert lines[18].startswith("  本周 交付 0 出问题 2 · 一次通过 — · ")
    assert lines[19].startswith("▲ 健康 盲区 2（告警、API 模糊测试：不启用且无兜底） · 残留 worktree 0 · 漏跑 0 · 磁盘 ")


def test_status_in_english_keeps_identifiers_and_control_keys(world: Any):
    lines = render_status(status_snapshot(world.source), "en", width=120, color=False, zone=UTC).split("\n")
    assert "Setup ● ready   Control ● normal   Window open   Config all valid   Next run 03:30 (in 11m)" in lines[1]
    assert "Now R-20261007T013000Z-implement · scheduled · implement · 42m · beat 3s ago" in lines[2]
    assert lines[7].startswith("  0022 P1 failed 题型排序 · implement.design · waiting 1h15m")
    assert lines[11].startswith("● Running 0023 P2 删除追问显示 · implement.code r2 · step 8m/30m · 1.2M/2M token")
    assert lines[19].startswith("▲ Health blind spots 2 (alerts, api_fuzz: off, no fallback)")


def test_colors_can_be_turned_off_and_commands_are_underlined(world: Any):
    snapshot = status_snapshot(world.source)
    plain = render_status(snapshot, "zh", color=False, zone=UTC)
    colored = render_status(snapshot, "zh", color=True, zone=UTC)
    assert "\x1b[" not in plain
    assert "\x1b[38;5;80;1;4mtightrein approve 0019\x1b[0m" in colored
    assert not re.search(r"\x1b\[(?:[0-9;]*;)?3?4m", colored.replace("38;5;80;1;4m", ""))  # 不用暗蓝(ANSI 4)
    assert ANSI.sub("", colored) == plain


def test_more_waiting_items_than_shown_keep_the_line_limit(world: Any):
    for number in range(30, 35):
        identifier = f"00{number}"
        issues.save(world.source.conn, Issue(identifier, "needs_decision", f"待定 {number}", "bug", "problem",
                                             severity="P3", gate="design", stage="implement", step="approve"),
                    world.clock)
        write_text(world.layout.human_document(identifier, "pending"), "# 待审核\n")
    lines = lines_of(render_status(status_snapshot(world.source), "zh", color=False, zone=UTC))
    assert len(lines) <= MAX_LINES
    assert lines[6].startswith("▲ 等你处理 7（另 5 条未列出）")


def test_an_interrupted_run_shows_the_take_over_command(world: Any):
    world.alive.clear()
    lines = render_status(status_snapshot(world.source), "zh", width=120, color=False, zone=UTC).split("\n")
    assert "当前 R-20261007T013000Z-implement · ● 中断（进程已不在） · 接管 tightrein run" in lines[2]
    assert "心跳失效 1" in lines[-1]


def test_an_empty_workspace_still_renders(world: Any):
    world.source.conn.execute("DELETE FROM issues")
    world.source.conn.execute("DELETE FROM runs")
    world.clock.advance(timedelta(days=30))
    lines = lines_of(render_status(status_snapshot(world.source), "en", color=False, zone=UTC))
    assert len(lines) <= MAX_LINES
    assert any(line.startswith("✓ Needs you 0") for line in lines)
    assert any(line.startswith("○ Running none") for line in lines)
