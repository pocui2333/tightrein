import pytest

from tightrein.domain.enums import FixRiskLevel, ImpactKind, RiskCategory
from tightrein.domain.fix import ChangedLines, RiskRules, risk
from tightrein.guards.protected import contains, matches_path

RULES = RiskRules(
    paths={RiskCategory.SCHEMA: ("db/migrations/",), RiskCategory.AUTHZ: ("access_matrix.src",),
           RiskCategory.CONTRACT: ("src/api/",)},
    patterns={RiskCategory.SCHEMA: ("create_table(",), RiskCategory.AUTHZ: ("@requires_role",),
              RiskCategory.CONTRACT: ("@route(",)},
)


def judge(files=(), flags=None, impact=None, endpoints=(), rules=RULES, migration=False):
    return risk(files, flags or {}, impact, endpoints, rules, path_matches=matches_path, line_contains=contains,
                migration=migration)


def hits(result):
    return [(hit.category, hit.basis) for hit in result.hits]


@pytest.mark.parametrize("files,expected", [
    ([ChangedLines("db/migrations/0003.src")], (RiskCategory.SCHEMA, "path")),
    ([ChangedLines("src/models.src", added=("create_table(orders)",))], (RiskCategory.SCHEMA, "content")),
    ([ChangedLines("src/auth/access_matrix.src")], (RiskCategory.AUTHZ, "path")),
    ([ChangedLines("src/order.src", removed=("@requires_role('admin')",))], (RiskCategory.AUTHZ, "content")),
    ([ChangedLines("src/api/orders.src")], (RiskCategory.CONTRACT, "path")),
    ([ChangedLines("src/order.src", added=("@route('/orders')",))], (RiskCategory.CONTRACT, "content")),
])
def test_paths_and_changed_lines(files, expected):
    result = judge(files)
    assert result.level is FixRiskLevel.HIGH and hits(result) == [expected]


def test_triage_and_plan_markers():
    assert hits(judge(flags={"dataStructure": True})) == [(RiskCategory.SCHEMA, "flag")]
    assert hits(judge(migration=True)) == [(RiskCategory.SCHEMA, "migration")]
    assert hits(judge(flags={"publicContract": True})) == [(RiskCategory.CONTRACT, "flag")]
    assert hits(judge(impact=ImpactKind.AUTHORIZATION)) == [(RiskCategory.AUTHZ, "impact")]
    assert hits(judge(impact=ImpactKind.DATA_OWNERSHIP)) == [(RiskCategory.AUTHZ, "impact")]
    assert hits(judge([ChangedLines("src/order_handler.src")], endpoints={"src/order_handler.src"})) == [
        (RiskCategory.CONTRACT, "endpoint")]


def test_misses_are_normal():
    result = judge([ChangedLines("src/order.src", added=("return total;",))], flags={"design": True},
                   impact=ImpactKind.NON_CORE_ERROR, endpoints={"src/other.src"})
    assert result.level is FixRiskLevel.NORMAL and result.hits == ()
    assert judge([ChangedLines("db/migrations/0003.src")], rules=RiskRules()).level is FixRiskLevel.NORMAL


def test_categories_outside_the_triggers_do_not_count():
    rules = RiskRules(RULES.paths, RULES.patterns, frozenset({RiskCategory.AUTHZ}))
    assert judge([ChangedLines("db/migrations/0003.src")], rules=rules).level is FixRiskLevel.NORMAL
    result = judge([ChangedLines("src/auth/access_matrix.src")], flags={"dataStructure": True}, rules=rules)
    assert hits(result) == [(RiskCategory.AUTHZ, "path")]
    assert result.to_dict() == {"level": "high", "categories": ["authz"], "hits": [
        {"category": "authz", "basis": "path", "file": "src/auth/access_matrix.src", "excerpt": None,
         "rule": "access_matrix.src"}]}
