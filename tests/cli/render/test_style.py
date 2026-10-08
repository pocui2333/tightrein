import io

from tightrein.cli.render import style
from tightrein.cli.render.style import (
    ROLES,
    Span,
    box_bottom,
    box_row,
    box_top,
    display_width,
    fit,
    line_width,
    paint,
    plain,
    render,
    span,
    use_color,
)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_wide_characters_take_two_cells_and_symbols_one():
    assert display_width("tightrein") == 9
    assert display_width("等你处理") == 8
    assert display_width("（41m 后）") == 2 + 3 + 1 + 2 + 2
    assert display_width("● ━ ◐ ✓ ⊘ ▲ ■ · —") == 17
    assert display_width("é") == 1  # 组合字符不占格


def test_fit_pads_and_truncates_to_the_exact_display_width():
    assert line_width(fit([span("短")], 10)) == 10
    cut = fit([span("实施·编码 r2 很长很长很长", "active")], 9)
    assert line_width(cut) == 9
    assert plain(cut).startswith("实施·编") and "…" in plain(cut)
    odd = fit([span("中文中文")], 6)  # 宽字符放不下半个：用空格补齐
    assert line_width(odd) == 6 and plain(odd) == "中文… "


def test_box_lines_have_the_same_width():
    width = 40
    top = box_top([span("0023 P2 缺陷 删除追问显示")], [span("1.2M / 2M token")], width)
    lines = [top, box_row([span("准备 ✓1m ─ 定位 ✓2m ─ 方案 ✓4m ─ 定案 ✓自动")], width), box_row([], width),
             box_bottom(width)]
    assert {line_width(fit(line, width)) for line in lines} == {width}
    assert plain(top).startswith("┌─ ") and plain(top).endswith(" ─┐")


def test_colors_follow_the_table_and_never_use_dark_blue():
    assert {number for number, _, _ in ROLES.values()} == {248, 254, 231, 78, 81, 214, 203, 80}
    assert ROLES["command"] == (80, True, True)
    assert all(bold for name, (_, bold, _) in ROLES.items() if name in ("value", "ok", "active", "warn", "bad"))
    assert paint([span("tightrein run", "command")], True) == "\x1b[38;5;80;1;4mtightrein run\x1b[0m"
    assert paint([span("   ", "command")], True) == "   "  # 空白不画下划线
    assert paint([span("tightrein run", "command")], False) == "tightrein run"


def test_color_is_off_for_pipes_and_with_no_color():
    assert use_color(Terminal(), {})
    assert not use_color(Terminal(), {"NO_COLOR": "1"})
    assert not use_color(io.StringIO(), {})
    assert style.terminal_width(io.StringIO()) == style.DEFAULT_WIDTH


def test_render_joins_fitted_lines():
    output = render([[span("a")], [Span("等等", "warn")]], 6, color=False)
    assert output.split("\n") == ["a     ", "等等  "]
