from pathlib import Path

from tightrein.assess import notes
from tightrein.assess.notes import CORE, RELATED, CodeNotes, build_entries
from tightrein.store.files.layout import WorkspaceLayout


def test_core_entries_get_the_excerpt_and_related_entries_only_the_signature(repo: Path) -> None:
    entries = build_entries([
        {"location": "services/orders.py:3", "description": "按编号查询", "role": CORE},
        {"location": "services/orders.py:4", "description": "返回订单", "role": RELATED},
    ], repo)
    core, related = entries
    assert core.signature is None and core.excerpt is not None
    assert "    1  class OrderService:" in core.excerpt and "    5  " in core.excerpt  # 前后各 2 行
    assert related.excerpt is None
    assert related.signature == "2: def get(self, order_id, user):"


def test_without_a_definition_line_the_signature_is_the_line_itself(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text("TIMEOUT = 30\nresult = helper(TIMEOUT)\nreturn_value = result\n",
                                        encoding="utf-8")
    entry = build_entries([{"location": "config.py:2", "description": "调用", "role": RELATED}], tmp_path)[0]
    assert entry.signature == "2: result = helper(TIMEOUT)"


def test_the_same_location_is_kept_once_and_core_wins(repo: Path) -> None:
    entries = build_entries([
        {"location": "services/orders.py:3", "description": "联动", "role": RELATED},
        {"location": "services/orders.py:3", "description": "出问题的位置", "role": CORE},
        {"location": "missing.py:1", "description": "不存在", "role": CORE},
        {"location": "不是位置", "description": "跳过"},
    ], repo)
    assert [(entry.location, entry.role) for entry in entries] == [("services/orders.py:3", CORE)]


def test_excerpts_are_capped(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(f"x{n} = {n}" for n in range(1, 101)), encoding="utf-8")
    entry = build_entries([{"location": "big.py:10-90", "description": "大段", "role": CORE}], tmp_path)[0]
    assert entry.excerpt is not None and len(entry.excerpt.splitlines()) == notes.MAX_LINES


def test_add_only_fills_what_is_missing_and_upgrades_related_to_core(repo: Path) -> None:
    found = CodeNotes("P-0001", "abc1234")
    found.add([{"location": "services/orders.py:3", "description": "联动", "role": RELATED}], repo)
    found.add([{"location": "services/orders.py:3", "description": "核心", "role": CORE},
               {"location": "routes/orders.py:4", "description": "路由", "role": RELATED}], repo)
    found.add([{"location": "routes/orders.py:4", "description": "不会再读", "role": RELATED}], repo)
    assert [(entry.location, entry.role, entry.description) for entry in found.entries] == [
        ("services/orders.py:3", CORE, "核心"), ("routes/orders.py:4", RELATED, "路由")]
    assert found.files == ["services/orders.py", "routes/orders.py"]


def test_save_load_copy_and_render(repo: Path, layout: WorkspaceLayout) -> None:
    found = CodeNotes("P-0001", "abc1234", trigger="登录用户请求他人订单")
    found.add([{"location": "services/orders.py:3", "description": "查询", "role": CORE}], repo)
    notes.save(layout, found)
    assert notes.path_of(layout, "P-0001").name == "00-problem-notes.json"
    loaded = notes.load(layout, "P-0001")
    assert loaded == found
    copied = notes.copy_to(layout, found, "0007")
    assert notes.path_of(layout, "0007").name == "00-issue-notes.json" and copied.entries == found.entries
    text = found.render()
    assert "abc1234" in text and "### 核心" in text and "登录用户请求他人订单" in text
    # 固定说明：直接采用、只打开要改或核对的几行、补看要注明
    assert notes.CORE_NOTE in text
    assert notes.CORE_NOTE.startswith("以下原文由程序截取、位置已核对：直接采用")
    assert "只打开要改动或核对的那几行" in notes.CORE_NOTE and notes.CORE_NOTE.endswith("注明补看了哪里。")
    assert CodeNotes("P-0002", "abc").render() == "无"
    assert notes.load(layout, "P-0009") is None
