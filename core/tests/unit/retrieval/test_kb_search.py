import pytest

from tightrein.domain.enums import KnowledgeStatus
from tightrein.retrieval import search
from tightrein.retrieval.errors import EntryNotFound, InvalidQuery
from tightrein.retrieval.indexers import FtsIndexer
from tightrein.retrieval.locations import routes_of
from tightrein.retrieval.models import SearchFilters
from tightrein.retrieval.ranking import FtsSource, RankedId, rrf_fuse
from tightrein.retrieval.sync import Synchronizer


def synced(world):
    report = Synchronizer(world.layout, world.conn, world.clock, [FtsIndexer()], routes_of(world.conn)).sync()
    assert report.errors == []
    return world


def ids(hits):
    return [hit.id for hit in hits]


def find(world, query, **filters):
    return search.search(world.conn, [FtsSource(world.conn)], query, SearchFilters(**filters))


def test_a_title_match_ranks_above_a_body_match(world):
    world.entry("DP-0001", "body", "分页参数", "页码从 1 开始", body="查询缺少公司过滤时会越权。\n")
    world.entry("DP-0002", "title", "公司过滤", "列表接口按公司过滤")
    synced(world)
    hits = find(world, "公司过滤")
    assert ids(hits) == ["DP-0002", "DP-0001"]
    assert hits[0].score > hits[1].score > 0
    assert (hits[0].type, hits[0].summary, hits[0].path) == (
        "defect-pattern", "列表接口按公司过滤", "knowledge/defect-pattern/DP-0002-title.md")


def test_filters_by_type_tags_and_status(world):
    world.entry("DP-0001", "a", "越权", "越权甲", tags=("path:src/", "权限"))
    world.entry("DP-0002", "b", "越权", "越权乙", tags=("权限",))
    world.entry("TO-0003", "c", "越权", "越权丙", tags=("path:src/", "权限"))
    world.entry("DP-0004", "d", "越权", "越权丁", status="superseded", superseded_by="DP-0001")
    world.entry("DP-0005", "e", "越权", "越权戊", status="archived")
    world.finding("P-0001", "越权", "越权文档", ["path:src/"])
    synced(world)

    def found(**filters):
        return sorted(ids(find(world, "越权", **filters)))

    assert found() == ["DP-0001", "DP-0002", "TO-0003", "triage-P-0001"]
    assert found(types=("defect-pattern",)) == ["DP-0001", "DP-0002"]
    assert found(tags=("path:src/", "权限")) == ["DP-0001", "TO-0003"]
    assert found(status=None) == ["DP-0001", "DP-0002", "DP-0004", "DP-0005", "TO-0003", "triage-P-0001"]
    assert found(status=KnowledgeStatus.ARCHIVED) == ["DP-0005"]
    assert len(found(limit=2)) == 2


@pytest.mark.parametrize("filters", [
    {"limit": 0}, {"limit": 51}, {"types": ("lesson",)}, {"tags": ("",)},
])
def test_invalid_filters(world, filters):
    with pytest.raises(InvalidQuery):
        find(world, "越权", **filters)


def test_filter_parsing_from_entry_points():
    assert SearchFilters.parse(status="any").status is None
    assert SearchFilters.parse(("issue",), ("x",), "archived", 5).to_dict() == {
        "types": ["issue"], "tags": ["x"], "status": "archived", "limit": 5}
    with pytest.raises(InvalidQuery, match="status 只能是 active、superseded、archived、any"):
        SearchFilters.parse(status="done")


def test_an_empty_query_is_rejected_before_any_source_is_asked(world):
    with pytest.raises(InvalidQuery):
        find(world, "***")


class FixedSource:
    name = "fixed"

    def __init__(self, *entry_ids):
        self.entry_ids = entry_ids
        self.limits = []

    def candidates(self, query, filters, limit):
        self.limits.append(limit)
        return [RankedId(entry_id, 1.0) for entry_id in self.entry_ids]


def test_several_sources_are_fused(world):
    for number in range(1, 4):
        world.entry(f"DP-000{number}", f"e{number}", f"条目{number}", f"摘要{number}")
    synced(world)
    first, second = FixedSource("DP-0001", "DP-0002"), FixedSource("DP-0002", "DP-0003")
    hits = search.search(world.conn, [first, second], "条目", SearchFilters(limit=2))
    assert ids(hits) == ["DP-0002", "DP-0001"]
    assert first.limits == second.limits == [6]
    assert hits[0].score == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_scores_and_ties():
    fused = rrf_fuse([[RankedId("a", 9), RankedId("b", 8)], [RankedId("c", 5), RankedId("a", 1)]], k=60)
    assert [(item.id, round(item.score, 6)) for item in fused] == [
        ("a", round(1 / 61 + 1 / 62, 6)), ("c", round(1 / 61, 6)), ("b", round(1 / 62, 6))]


def test_related_covers_the_four_relations(world):
    world.entry("DP-0001", "first", "最早", "被取代", status="superseded", superseded_by="DP-0002")
    world.entry("DP-0002", "second", "第二版", "也被取代", status="superseded", superseded_by="DP-0003",
                related=("TO-0001",))
    world.entry("DP-0003", "third", "当前", "当前有效")
    world.entry("TO-0001", "tradeoff", "取舍", "取舍", related=("DP-0002",))
    world.entry("FL-0001", "lesson", "经验", "经验", related=("DP-0002",), status="archived")
    synced(world)
    found = [(entry.hit.id, entry.relation, entry.status.value) for entry in search.related(world.conn, "DP-0002")]
    assert found == [
        ("TO-0001", "related", "active"),
        ("FL-0001", "related-by", "archived"),
        ("TO-0001", "related-by", "active"),
        ("DP-0003", "superseded-by", "active"),
        ("DP-0001", "supersedes", "superseded"),
    ]
    assert [entry.hit.id for entry in search.related(world.conn, "DP-0001") if entry.relation == "superseded-by"] == [
        "DP-0002", "DP-0003"]
    with pytest.raises(EntryNotFound, match="kb search"):
        search.related(world.conn, "DP-0099")


def test_read_entry_returns_frontmatter_and_body(world):
    world.entry("DP-0001", "owner", "按公司过滤", "查询缺少公司过滤")
    synced(world)
    document = search.read_entry(world.layout, search.require(world.conn, "DP-0001"))
    assert (document.id, document.frontmatter["summary"], document.body, document.path) == (
        "DP-0001", "查询缺少公司过滤", "# 按公司过滤\n\n正文。\n", "knowledge/defect-pattern/DP-0001-owner.md")
