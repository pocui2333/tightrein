from pathlib import Path

from tightrein.knowledge import entries
from tightrein.knowledge.entries import Entry, EntryStatus
from tightrein.knowledge.match import BRIEF_HEADER, EMPTY, classify, estimate_tokens, match, render, select
from tightrein.store.files.layout import WorkspaceLayout


def entry(entry_id: str, locations: tuple[str, ...], updated: str = "2026-10-01", body: str = "正文。",
          status: EntryStatus = EntryStatus.ACTIVE) -> Entry:
    kind = {"PAT": "patterns", "CON": "conventions", "LES": "lessons"}[entry_id[:3]]
    return Entry(entry_id, kind, f"标题 {entry_id}", f"摘要 {entry_id}", body, status, updated, locations)


def test_locations_are_classified_before_matching() -> None:
    found = classify(["GET /api/orders", "/orders", "src/a.py:12", "./src/b.py:Order.save", "src/c.py", " "])
    assert found.routes == ("GET /api/orders",)
    assert found.pages == ("/orders",)
    assert found.paths == ("src/a.py", "src/b.py", "src/c.py")


def test_path_prefixes_match_longest_first_then_newest() -> None:
    candidates = [
        entry("PAT-0001", ("path:src/",), updated="2026-10-05"),
        entry("PAT-0002", ("path:src/services/",), updated="2026-09-01"),
        entry("PAT-0003", ("path:src/services/order.py",)),
        entry("PAT-0004", ("path:src/",), updated="2026-10-07"),
        entry("PAT-0005", ("path:src/serv",)),  # 不按半个路径段匹配
        entry("PAT-0006", ("path:lib/",)),
        entry("CON-0001", ()),  # 没有位置：整个项目都适用，排在最后
    ]
    chosen = select(candidates, classify(["src/services/order.py:40"]), limit_entries=10, limit_tokens=10_000)
    assert [item.id for item in chosen] == ["PAT-0003", "PAT-0002", "PAT-0004", "PAT-0001", "CON-0001"]


def test_routes_and_pages_match_exactly() -> None:
    candidates = [
        entry("PAT-0001", ("route:GET /api/orders",)),
        entry("PAT-0002", ("route:GET /api/orders/{id}",)),
        entry("PAT-0003", ("page:/orders",)),
        entry("PAT-0004", ("page:/orders/new",)),
    ]
    chosen = select(candidates, classify(["GET /api/orders", "/orders"]), limit_entries=10, limit_tokens=10_000)
    assert sorted(item.id for item in chosen) == ["PAT-0001", "PAT-0003"]


def test_entry_and_token_limits_cut_the_tail() -> None:
    candidates = [entry(f"PAT-000{number}", ("path:src/",)) for number in range(1, 6)]
    assert len(select(candidates, classify(["src/a.py"]), limit_entries=3, limit_tokens=10_000)) == 3
    one = estimate_tokens(BRIEF_HEADER) + estimate_tokens("- PAT-0001 [patterns] 标题 PAT-0001：摘要 PAT-0001")
    assert len(select(candidates, classify(["src/a.py"]), limit_entries=10, limit_tokens=one)) == 1


def test_small_entries_are_inlined_and_large_ones_listed_as_summaries() -> None:
    small = [entry("CON-0001", (), body="接口统一返回 Result 包装。")]
    assert "接口统一返回 Result 包装。" in render(small, limit_tokens=1000)
    large = [entry("CON-0001", (), body="很长的正文。" * 500), entry("CON-0002", ())]
    text = render(large, limit_tokens=200)
    assert text.startswith(BRIEF_HEADER)
    assert "- CON-0001 [conventions] 标题 CON-0001：摘要 CON-0001" in text and "很长的正文" not in text
    assert render([], limit_tokens=10) == EMPTY


def test_match_reads_only_active_entries_and_reports_broken_files(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    kept = entries.create(layout, kind="patterns", slug="kept", title="t", summary="s", body="b",
                          locations=("path:src/",), today="2026-10-08", commit=None, sources=())
    gone = entries.create(layout, kind="patterns", slug="gone", title="t", summary="s", body="b",
                          locations=("path:src/",), today="2026-10-08", commit=None, sources=())
    entries.mark_stale(gone, "src/a.py 已删除", "2026-10-08")
    broken = layout.knowledge_kind("lessons") / "LES-0001-x.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("坏的\n", encoding="utf-8")
    warnings: list[str] = []
    assert match(layout, ["src/a.py"], limit_entries=5, limit_tokens=1000, warnings=warnings) == [kept]
    assert len(warnings) == 1


def test_token_detection_and_estimate() -> None:
    assert estimate_tokens("权限校验") == 4
    assert estimate_tokens("abcd efgh i") == 3
    assert estimate_tokens("データ abc") == 4
    assert estimate_tokens("") == 0
