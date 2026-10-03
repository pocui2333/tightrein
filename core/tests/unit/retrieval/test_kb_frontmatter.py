from datetime import date

import pytest

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.errors import FrontmatterIssue
from tightrein.retrieval.frontmatter import parse_source, tag_problem
from tightrein.retrieval.sources import list_sources, source_for


def no_routes(problem_ids):
    return []


def parse(source, routes_of=no_routes):
    return parse_source(source, source.path.read_text(encoding="utf-8"), routes_of)


def test_sources_cover_entries_and_the_three_document_kinds(world):
    world.entry("DP-0001", "owner-check", "按公司过滤", "查询缺少公司过滤")
    world.entry("TO-0002", "soft-delete", "软删除", "软删除是已接受的取舍")
    world.issue("0007", "order-owner", "订单查询缺少公司过滤")
    world.finding("P-0042", "越权读取", "订单越权", ["route:GET /api/Order"])
    world.fix_report("0007", "修复报告", "补上公司过滤", ["path:src/OrderService.cs"])
    index = world.layout.knowledge_index(KnowledgeType.DEFECT_PATTERN)
    index.write_text("# 索引\n", encoding="utf-8")
    page = world.layout.knowledge_index_page(KnowledgeType.DEFECT_PATTERN, "DP-0001", "DP-0180")
    page.write_text("x", encoding="utf-8")
    assert [(source.relative, source.type, source.schema) for source in list_sources(world.layout)] == [
        ("data/findings/P-0042.md", "finding", "handoff/frontmatter/report.schema.json"),
        ("data/fixes/0007/report.md", "fix-report", "handoff/frontmatter/report.schema.json"),
        ("issues/0007-order-owner.md", "issue", "handoff/frontmatter/issue.schema.json"),
        ("knowledge/defect-pattern/DP-0001-owner-check.md", "defect-pattern", "data/knowledge.schema.json"),
        ("knowledge/tradeoff/TO-0002-soft-delete.md", "tradeoff", "data/knowledge.schema.json"),
    ]
    assert source_for(world.layout, index) is None
    assert source_for(world.layout, world.layout.root / "knowledge" / "notes.md") is None


def test_an_entry_takes_title_from_the_first_heading(world):
    path = world.entry("DP-0001", "owner-check", "按公司过滤", "查询缺少公司过滤",
                       tags=("path:src/Services/", "权限"), related=("TO-0002",), updated="2026-09-02")
    entry, problems = parse(source_for(world.layout, path))
    assert problems == []
    assert (entry.id, entry.type, entry.status, entry.title, entry.summary) == (
        "DP-0001", "defect-pattern", KnowledgeStatus.ACTIVE, "按公司过滤", "查询缺少公司过滤")
    assert (entry.tags, entry.related, entry.superseded_by) == (("path:src/Services/", "权限"), ("TO-0002",), None)
    assert (entry.updated, entry.review_by, entry.path) == (
        date(2026, 9, 2), date(2027, 3, 1), "knowledge/defect-pattern/DP-0001-owner-check.md")
    assert entry.body == "# 按公司过滤\n\n正文。\n"


def test_an_issue_derives_tags_from_root_causes_and_problem_routes(world):
    path = world.issue("0007", "order-owner", "订单查询缺少公司过滤",
                       root_cause=("src/OrderService.cs:3", "src/OrderService.cs:9-12", "src/Query.cs:1"),
                       problems=("P-0001", "P-0002"))
    asked = []

    def routes_of(problem_ids):
        asked.append(list(problem_ids))
        return ["GET /api/Order"]

    entry, problems = parse(source_for(world.layout, path), routes_of)
    assert problems == [] and asked == [["P-0001", "P-0002"]]
    assert entry.tags == ("path:src/OrderService.cs", "path:src/Query.cs", "route:GET /api/Order")
    assert (entry.title, entry.summary, entry.status, entry.updated) == (
        "订单查询缺少公司过滤", "订单查询缺少公司过滤", KnowledgeStatus.ACTIVE, date(2026, 9, 21))


def test_reports_use_heading_summary_and_update_date(world):
    path = world.finding("P-0042", "越权读取", "订单越权", ["route:GET /api/Order"])
    entry, problems = parse(source_for(world.layout, path))
    assert problems == []
    assert (entry.id, entry.type, entry.title, entry.summary, entry.updated) == (
        "triage-P-0042", "finding", "越权读取", "订单越权", date(2026, 9, 22))


@pytest.mark.parametrize("tag, ok", [
    ("path:src/Services/Material/", True), ("path:/abs", False), ("path:src/../x", False), ("path:", False),
    ("route:POST /api/Material/Query", True), ("route:post /api", False), ("route:GET api", False),
    ("page:/material/list", True), ("page:material", False),
    ("stage:triage", True), ("stage:deploy", False), ("自由文本", True), ("path-like", True),
])
def test_tag_prefixes(tag, ok):
    assert (tag_problem(tag) is None) is ok


def test_problems_carry_file_pointer_and_reason(world):
    missing = world.write(world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0003", "missing"),
                          {"id": "DP-0003", "type": "defect-pattern", "tags": ["x"], "status": "active",
                           "updated": "2026-09-01", "reviewBy": "2027-01-01"}, "# 标题\n")
    _, problems = parse(source_for(world.layout, missing))
    assert problems == [FrontmatterIssue("knowledge/defect-pattern/DP-0003-missing.md", "$",
                                         "'summary' is a required property")]
    wrong = world.entry("DP-0004", "wrong", "标题", "摘要", tags=("route:get /x",), type="tradeoff")
    _, problems = parse(source_for(world.layout, wrong))
    assert [(problem.pointer, problem.reason[:10]) for problem in problems] == [
        ("$.tags[0]", "route: 须为「"), ("$.type", "位于 defect-")]
    renamed = world.entry("DP-0005", "x", "标题", "摘要")
    moved = renamed.with_name("DP-0006-x.md")
    renamed.rename(moved)
    _, problems = parse(source_for(world.layout, moved))
    assert [problem.pointer for problem in problems] == ["$.id"]
    headless = world.write(world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0007", "headless"),
                           {"id": "DP-0007", "type": "defect-pattern", "summary": "s", "tags": ["x"],
                            "status": "active", "updated": "2026-09-01", "reviewBy": "2027-01-01"}, "没有标题\n")
    _, problems = parse(source_for(world.layout, headless))
    assert [problem.reason for problem in problems] == ["正文没有一级标题(# 开头的行)，无法取得标题"]
    broken = world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0008", "broken")
    broken.write_text("没有 frontmatter\n", encoding="utf-8")
    _, problems = parse(source_for(world.layout, broken))
    assert problems[0].pointer == "$" and "frontmatter" in problems[0].reason
