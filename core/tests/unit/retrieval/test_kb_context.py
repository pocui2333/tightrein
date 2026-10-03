from tightrein.domain.enums import ContextKind
from tightrein.retrieval import context
from tightrein.retrieval.context import ContextRequest
from tightrein.retrieval.indexers import FtsIndexer
from tightrein.retrieval.locations import routes_of
from tightrein.retrieval.ranking import FtsSource
from tightrein.retrieval.sync import Synchronizer
from tightrein.retrieval.text import estimate_tokens


def build(world, request, limit=15, inline=3000):
    report = Synchronizer(world.layout, world.conn, world.clock, [FtsIndexer()], routes_of(world.conn)).sync()
    assert report.errors == []
    return context.build(world.conn, world.layout, [FtsSource(world.conn)], request, limit, inline)


def picked(bundle):
    return [(item.id, item.reason) for item in bundle.items]


def test_static_review_matches_path_prefixes_longest_first(world):
    world.entry("DP-0001", "broad", "服务层", "服务层的通用问题", tags=("path:src/",))
    world.entry("DP-0002", "narrow", "物料服务", "物料服务缺少过滤", tags=("path:src/Services/Material/",))
    world.entry("DP-0003", "newer", "服务层新", "较新的服务层问题", tags=("path:src/",), updated="2026-09-20")
    world.entry("DP-0004", "other", "前端", "与改动无关", tags=("path:web/",))
    world.entry("DP-0005", "old", "旧的", "已取代", tags=("path:src/",), status="superseded", superseded_by="DP-0001")
    world.entry("FL-0001", "lesson", "修复经验", "不是缺陷模式", tags=("path:src/",))
    bundle = build(world, ContextRequest(ContextKind.STATIC_REVIEW, paths=("src/Services/Material/Query.cs",)))
    assert picked(bundle) == [
        ("DP-0002", "path:src/Services/Material/ 前缀匹配"),
        ("DP-0003", "path:src/ 前缀匹配"),
        ("DP-0001", "path:src/ 前缀匹配"),
    ]
    assert bundle.inline_documents == []


def test_triage_matches_routes_pages_paths_and_documents_and_inlines_small_tradeoffs(world):
    world.entry("DP-0001", "route", "订单越权", "订单接口越权", tags=("route:GET /api/Order",))
    world.entry("TL-0001", "page", "页面经验", "订单页面经验", tags=("page:/order/list",))
    world.entry("TO-0001", "soft-delete", "软删除", "软删除是取舍", tags=("route:GET /api/Order",))
    world.finding("P-0001", "越权读取", "同一路由的旧发现", ["route:GET /api/Order"])
    world.finding("P-0002", "当前问题", "正在分诊的问题", ["route:GET /api/Order"])
    world.issue("0007", "order", "订单查询缺少公司过滤", root_cause=("src/OrderService.cs:3",))
    bundle = build(world, ContextRequest(ContextKind.TRIAGE, paths=("src/OrderService.cs",),
                                         routes=("GET /api/Order",), pages=("/order/list",),
                                         exclude_ids=("triage-P-0002",)))
    assert picked(bundle) == [
        ("0007", "path:src/OrderService.cs 前缀匹配"),
        ("triage-P-0001", "route:GET /api/Order 匹配"),
        ("DP-0001", "route:GET /api/Order 匹配"),
        ("TL-0001", "page:/order/list 匹配"),
    ]
    assert [document.id for document in bundle.inline_documents] == ["TO-0001"]
    text = bundle.render()
    assert text.startswith("以下是与本任务相关的知识条目摘要，需要正文时用 `kb get <编号>` 读取：\n- 0007 [issue] ")
    assert "以下类别的条目总量较小，直接附上全文：\n\n## TO-0001(knowledge/tradeoff/TO-0001-soft-delete.md)\n\n# 软删除" in text
    assert bundle.estimated_tokens == estimate_tokens(text)


def test_tradeoffs_over_the_inline_limit_are_listed_as_summaries(world):
    world.entry("TO-0001", "big", "大取舍", "很长的取舍", tags=("route:GET /api/Order",), body="取舍" * 50 + "\n")
    bundle = build(world, ContextRequest(ContextKind.TRIAGE, routes=("GET /api/Order",)), inline=50)
    assert picked(bundle) == [("TO-0001", "route:GET /api/Order 匹配")]
    assert bundle.inline_documents == []


def test_fix_takes_lessons_contracts_and_reports_then_fills_with_keywords(world):
    world.entry("FL-0001", "lesson", "修复经验", "订单服务的修复经验", tags=("path:src/OrderService.cs",))
    world.entry("CT-0001", "contract", "字段契约", "分页字段契约", tags=("path:src/",))
    world.fix_report("0007", "修复报告", "订单服务的修复", ["path:src/OrderService.cs"])
    world.entry("FL-0002", "keyword", "分页", "分页参数从 1 开始", tags=("path:web/",))
    world.entry("DP-0001", "wrong-type", "分页", "分页的缺陷模式", tags=("path:web/",))
    bundle = build(world, ContextRequest(ContextKind.FIX, paths=("src/OrderService.cs",), keywords="分页"), limit=4)
    assert picked(bundle) == [
        ("fix-0007", "path:src/OrderService.cs 前缀匹配"),
        ("FL-0001", "path:src/OrderService.cs 前缀匹配"),
        ("CT-0001", "path:src/ 前缀匹配"),
        ("FL-0002", "关键词检索"),
    ]
    assert picked(build(world, ContextRequest(ContextKind.FIX, paths=("src/OrderService.cs",), keywords="分页"),
                      limit=2)) == [("fix-0007", "path:src/OrderService.cs 前缀匹配"),
                                    ("FL-0001", "path:src/OrderService.cs 前缀匹配")]
    assert len(build(world, ContextRequest(ContextKind.FIX, keywords="!!!")).items) == 0


def test_routes_are_taken_from_the_problem_when_not_given(world):
    world.problem("P-0042", "GET /api/Order", "src/OrderService.cs:OrderService.Page")
    world.entry("DP-0001", "route", "订单越权", "订单接口越权", tags=("route:GET /api/Order",))
    world.entry("DP-0002", "path", "订单服务", "订单服务问题", tags=("path:src/OrderService.cs",))
    bundle = build(world, ContextRequest(ContextKind.TRIAGE, problem_id="P-0042"))
    assert picked(bundle) == [("DP-0002", "path:src/OrderService.cs 前缀匹配"),
                              ("DP-0001", "route:GET /api/Order 匹配")]


def test_an_empty_bundle_says_so(world):
    bundle = build(world, ContextRequest(ContextKind.STATIC_REVIEW, paths=("src/x.cs",)))
    assert bundle.render() == "没有与本任务相关的知识条目。\n"
