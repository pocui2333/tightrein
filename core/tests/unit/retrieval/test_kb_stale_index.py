from datetime import date, datetime, timedelta, timezone

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval import index_md
from tightrein.retrieval.stale import stale
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import KnowledgeRecord

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 5)


def record(entry_id, summary="摘要", tags=("x",), status=KnowledgeStatus.ACTIVE, updated=date(2026, 9, 1),
           review_by=date(2027, 1, 1), last_hit_at=None, superseded_by=None):
    kind = KnowledgeType.from_prefix(entry_id.split("-")[0])
    return KnowledgeRecord(
        id=entry_id, type=kind.value, status=status, title=entry_id, summary=summary, updated=updated,
        path=f"knowledge/{kind.value}/{entry_id}-e.md", content_sha256="0" * 64, file_mtime=1.0, file_size=1,
        indexed_at=NOW, tags=tuple(tags), review_by=review_by, last_hit_at=last_hit_at, superseded_by=superseded_by,
        hits=0 if last_hit_at is None else 1,
    )


def save(world, *records):
    for item in records:
        knowledge.save(world.conn, item)
        if item.last_hit_at is not None:
            world.conn.execute("UPDATE knowledge_meta SET hits = 1, last_hit_at = ? WHERE id = ?",
                               (item.last_hit_at.strftime("%Y-%m-%dT%H:%M:%SZ"), item.id))


def ids(hits):
    return [hit.id for hit in hits]


def test_overdue_and_unused_entries(world):
    save(world,
         record("DP-0001", review_by=date(2026, 10, 4)),
         record("DP-0002", review_by=date(2026, 10, 5)),
         record("DP-0003", updated=date(2026, 7, 6)),
         record("DP-0004", updated=date(2026, 7, 7)),
         record("DP-0005", updated=date(2026, 1, 1), last_hit_at=NOW - timedelta(days=91)),
         record("DP-0006", updated=date(2026, 1, 1), last_hit_at=NOW - timedelta(days=89)),
         record("DP-0007", review_by=date(2026, 1, 1), status=KnowledgeStatus.ARCHIVED))
    world.finding("P-0001", "文档", "文档不参与", ["x"])
    report = stale(world.conn, NOW, TODAY, 90)
    assert ids(report.overdue) == ["DP-0001"]
    assert ids(report.unused) == ["DP-0003", "DP-0005"]


def test_contradiction_groups_share_at_least_two_tags_within_a_type(world):
    save(world,
         record("DP-0001", tags=("权限", "公司", "查询")),
         record("DP-0002", tags=("权限", "公司")),
         record("DP-0003", tags=("公司", "分页", "查询")),
         record("DP-0004", tags=("权限", "分页")),
         record("TO-0001", tags=("权限", "公司")),
         record("TO-0002", tags=("权限", "公司"), status=KnowledgeStatus.SUPERSEDED, superseded_by="TO-0001"),
         record("FL-0001", tags=("a", "b")),
         record("FL-0002", tags=("a", "b")))
    groups = [ids(group) for group in stale(world.conn, NOW, TODAY, 90).contradiction_groups]
    assert groups == [["DP-0001", "DP-0002", "DP-0003"], ["FL-0001", "FL-0002"]]


def test_large_groups_are_split_evenly(world):
    save(world, *[record(f"DP-{number:04d}", tags=("a", "b")) for number in range(1, 10)])
    groups = [ids(group) for group in stale(world.conn, NOW, TODAY, 90).contradiction_groups]
    assert [len(group) for group in groups] == [5, 4]
    assert groups[0][0] == "DP-0001" and groups[1][-1] == "DP-0009"


EXPECTED_TYPE_INDEX = """<!-- 本文件由 tightrein kb sync 生成，不要手工编辑 -->
# 缺陷模式(defect-pattern)

- DP-0001 查询缺少公司过滤 (DP-0001-e.md)
- DP-0003 分页从 0 开始 (DP-0003-e.md)
"""

EXPECTED_ROOT_INDEX = """<!-- 本文件由 tightrein kb sync 生成，不要手工编辑 -->
# 知识索引

## 缺陷模式(defect-pattern)

共 2 条，见 [defect-pattern/INDEX.md](defect-pattern/INDEX.md)

- DP-0001 查询缺少公司过滤 (defect-pattern/DP-0001-e.md)
- DP-0003 分页从 0 开始 (defect-pattern/DP-0003-e.md)

## 已接受的取舍(tradeoff)

共 0 条，见 [tradeoff/INDEX.md](tradeoff/INDEX.md)
"""


def test_index_files_match_the_expected_text(world):
    for kind in (KnowledgeType.DEFECT_PATTERN, KnowledgeType.TRADEOFF):
        world.layout.knowledge_type_dir(kind).mkdir(parents=True)
    save(world, record("DP-0003", "分页从 0 开始"), record("DP-0001", "查询缺少公司过滤"),
         record("DP-0002", "已被取代", status=KnowledgeStatus.SUPERSEDED, superseded_by="DP-0001"),
         record("TO-0001", "已归档", status=KnowledgeStatus.ARCHIVED))
    changed = index_md.regenerate(world.conn, world.layout)
    assert changed == ["knowledge/INDEX.md", "knowledge/defect-pattern/INDEX.md", "knowledge/tradeoff/INDEX.md"]
    read = world.layout.knowledge_index
    assert read(KnowledgeType.DEFECT_PATTERN).read_text(encoding="utf-8") == EXPECTED_TYPE_INDEX
    assert read().read_text(encoding="utf-8") == EXPECTED_ROOT_INDEX
    assert read(KnowledgeType.TRADEOFF).read_text(encoding="utf-8").endswith("# 已接受的取舍(tradeoff)\n\n暂无有效条目。\n")
    stamp = read().stat().st_mtime_ns
    assert index_md.regenerate(world.conn, world.layout) == []
    assert read().stat().st_mtime_ns == stamp


def test_large_types_are_paged_and_the_root_only_links(world):
    world.layout.knowledge_type_dir(KnowledgeType.DEFECT_PATTERN).mkdir(parents=True)
    save(world, *[record(f"DP-{number:04d}", f"条目 {number}") for number in range(1, 399)])
    changed = index_md.regenerate(world.conn, world.layout)
    assert changed == ["knowledge/INDEX.md", "knowledge/defect-pattern/INDEX-DP-0001-DP-0180.md",
                       "knowledge/defect-pattern/INDEX-DP-0181-DP-0360.md",
                       "knowledge/defect-pattern/INDEX-DP-0361-DP-0398.md", "knowledge/defect-pattern/INDEX.md"]
    type_index = world.layout.knowledge_index(KnowledgeType.DEFECT_PATTERN).read_text(encoding="utf-8").splitlines()
    assert type_index[3:] == ["- [DP-0001 到 DP-0180](INDEX-DP-0001-DP-0180.md)",
                              "- [DP-0181 到 DP-0360](INDEX-DP-0181-DP-0360.md)",
                              "- [DP-0361 到 DP-0398](INDEX-DP-0361-DP-0398.md)"]
    page = world.layout.knowledge_index_page(KnowledgeType.DEFECT_PATTERN, "DP-0001", "DP-0180")
    lines = page.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 183 and lines[1] == "# 缺陷模式(defect-pattern) DP-0001 到 DP-0180"
    root = world.layout.knowledge_index().read_text(encoding="utf-8")
    assert "- DP-0001" not in root and "共 398 条" in root
    for number in range(181, 399):
        world.conn.execute("DELETE FROM knowledge_meta WHERE id = ?", (f"DP-{number:04d}",))
    changed = index_md.regenerate(world.conn, world.layout)
    assert changed == ["knowledge/INDEX.md", "knowledge/defect-pattern/INDEX-DP-0001-DP-0180.md",
                       "knowledge/defect-pattern/INDEX-DP-0181-DP-0360.md",
                       "knowledge/defect-pattern/INDEX-DP-0361-DP-0398.md", "knowledge/defect-pattern/INDEX.md"]
    assert not world.layout.knowledge_index_page(KnowledgeType.DEFECT_PATTERN, "DP-0181", "DP-0360").exists()
    assert not page.exists() and len(world.layout.knowledge_index(KnowledgeType.DEFECT_PATTERN).read_text(
        encoding="utf-8").splitlines()) == 183
