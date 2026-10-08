from pathlib import Path

import pytest

from tightrein.knowledge import entries
from tightrein.knowledge.entries import Entry, EntryInvalid, EntryStatus
from tightrein.store.files.layout import WorkspaceLayout

TODAY = "2026-10-08"


def write_entry(layout: WorkspaceLayout, kind: str, name: str, text: str) -> Path:
    path = layout.knowledge_kind(kind) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make(layout: WorkspaceLayout, kind: str = "patterns", slug: str = "missing-filter",
         locations: tuple[str, ...] = ("path:src/services/",)) -> Entry:
    return entries.create(layout, kind=kind, slug=slug, title="列表接口漏了公司过滤", summary="按编号查询时没有按公司过滤",
                          body="服务层直接按编号查询。", locations=locations, today=TODAY, commit="c" * 40,
                          sources=("0018",))


def test_create_assigns_the_kind_prefix_and_round_trips(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    first = make(layout)
    second = make(layout, slug="another")
    lesson = make(layout, kind="lessons", slug="a-lesson")
    assert (first.id, second.id, lesson.id) == ("PAT-0001", "PAT-0002", "LES-0001")
    assert first.path == layout.knowledge_kind("patterns") / "PAT-0001-missing-filter.md"
    assert entries.read_entry(first.path, "patterns") == first
    loaded = entries.load(layout)
    assert [entry.id for entry in loaded.entries] == ["PAT-0001", "PAT-0002", "LES-0001"]
    assert loaded.warnings == []


def test_id_prefix_must_match_kind_and_every_problem_is_listed(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    path = write_entry(layout, "patterns", "CON-0001-wrong.md", (
        "---\nid: CON-0001\nkind: patterns\nsummary: s\nstatus: active\nupdated: '2026-10-08'\n"
        "locations: ['path:/abs', 'route:get /x', 'page:x', 'other']\n---\n\n正文没有标题\n"))
    with pytest.raises(EntryInvalid) as caught:
        entries.read_entry(path, "patterns")
    found = caught.value.problems
    assert any("id：CON-0001 的前缀与类 patterns 不符" in item for item in found)
    for index in range(4):
        assert any(f"locations[{index}]" in item for item in found)
    assert any("没有一级标题" in item for item in found)
    assert all(item.startswith(str(path)) for item in found)


@pytest.mark.parametrize("location, ok", [
    ("path:src/a.py", True), ("path:src/", True), ("path:/src", False), ("path:src/../x", False),
    ("path:a b", False), ("route:POST /api/orders", True), ("route:post /api", False),
    ("route:GET api", False), ("page:/orders", True), ("page:orders", False), ("tag:x", False),
])
def test_location_tags(location: str, ok: bool) -> None:
    assert (entries.location_problem(location) is None) is ok


def test_superseded_requires_a_successor(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    path = write_entry(layout, "lessons", "LES-0001-x.md", (
        "---\nid: LES-0001\nkind: lessons\nsummary: s\nstatus: superseded\nupdated: '2026-10-08'\n---\n\n# 标题\n"))
    with pytest.raises(EntryInvalid, match="supersededBy"):
        entries.read_entry(path, "lessons")


def test_supersede_and_mark_stale_never_delete_the_file(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    old = make(layout)
    new = make(layout, slug="newer")
    replaced = entries.supersede(old, new.id, "2026-10-09")
    assert old.path is not None and old.path.exists()
    again = entries.read_entry(old.path)
    assert (again.status, again.superseded_by, again.updated) == (EntryStatus.SUPERSEDED, new.id, "2026-10-09")
    assert replaced == again
    stale = entries.mark_stale(new, "src/a.py 已删除", "2026-10-09")
    assert entries.read_entry(stale.path).stale_reason == "src/a.py 已删除"
    assert entries.active(entries.load(layout).entries) == []


def test_create_rejects_a_bad_draft_without_writing(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    with pytest.raises(EntryInvalid) as caught:
        make(layout, slug="Bad Slug", locations=("route:x",))
    assert any("slug" in item for item in caught.value.problems)
    assert any("locations[0]" in item for item in caught.value.problems)
    assert not layout.knowledge_kind("patterns").exists()


def test_a_broken_file_keeps_the_last_good_content_and_warns(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    entry = make(layout)
    assert entries.load(layout).entries == [entry]
    assert entry.path is not None
    entry.path.write_text("---\nid: PAT-0001\nkind: [broken\n---\n", encoding="utf-8")
    loaded = entries.load(layout)
    assert loaded.entries == [entry]
    assert len(loaded.warnings) == 1 and "沿用上一次读好的内容" in loaded.warnings[0]
    write_entry(layout, "patterns", "PAT-0009-new.md", "没有头信息\n")
    loaded = entries.load(layout)
    assert [item.id for item in loaded.entries] == ["PAT-0001"]
    assert any("跳过" in item for item in loaded.warnings)


def test_a_failed_write_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = WorkspaceLayout(tmp_path)
    entry = make(layout)
    before = entry.path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    def broken(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", broken)
    with pytest.raises(OSError):
        entries.supersede(entry, "PAT-0002", "2026-10-09")
    assert entry.path.read_text(encoding="utf-8") == before  # type: ignore[union-attr]
    assert [path.name for path in entry.path.parent.iterdir()] == [entry.path.name]  # type: ignore[union-attr]


def test_frontmatter_round_trip() -> None:
    text = entries.render_frontmatter({"id": "0003", "at": "2026-10-07T09:30:00Z", "list": ["a"]}, "# 标题\n\n正文")
    data, body = entries.parse_frontmatter(text)
    assert data == {"id": "0003", "at": "2026-10-07T09:30:00Z", "list": ["a"]}
    assert body == "# 标题\n\n正文\n"
    with pytest.raises(ValueError):
        entries.parse_frontmatter("# 没有头信息\n")
