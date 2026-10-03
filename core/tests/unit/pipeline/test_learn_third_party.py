from datetime import date

from learn_world import ZONE, make_learn_world

from tightrein.pipeline.learn.steps import third_party
from tightrein.pipeline.learn.steps.third_party import RepoFacts

LOCK = """lockVersion: 1
skills:
  - name: variant-analysis
    source: https://github.com/example/skills
    ref: 0123456789abcdef0123456789abcdef01234567
    path: plugins/variant-analysis
  - name: stale-skill
    source: https://github.com/example/old
    ref: 0123456789abcdef0123456789abcdef01234567
    path: skill
"""


class Query:
    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    def __call__(self, source):
        self.calls.append(source)
        found = self.facts[source]
        if isinstance(found, Exception):
            raise found
        return found


def lock(tmp_path):
    path = tmp_path / "skills.lock.yaml"
    path.write_text(LOCK, encoding="utf-8")
    return path


def test_missing_lock_file_is_skipped(tmp_path):
    world = make_learn_world(tmp_path)
    assert third_party.check(world.conn, world.clock, world.config, tmp_path / "none.yaml", Query({}), ZONE) == []


def test_monthly_check_lists_failures_and_is_reused_within_the_month(tmp_path):
    world = make_learn_world(tmp_path)
    query = Query({"https://github.com/example/skills": RepoFacts(7296, date(2026, 9, 28), False),
                   "https://github.com/example/old": RepoFacts(1200, date(2026, 3, 1), True)})
    found = third_party.check(world.conn, world.clock, world.config, lock(tmp_path), query, ZONE)
    assert [(item.name, item.passed) for item in found] == [("variant-analysis", True), ("stale-skill", False)]
    assert found[1].detail == "星标数 1200 低于 5000；最近提交 2026-03-01 已超过 90 天；来源仓库已归档"
    again = third_party.check(world.conn, world.clock, world.config, lock(tmp_path), query, ZONE)
    assert again == found and len(query.calls) == 2


def test_a_failed_query_is_reported_and_retried_next_time(tmp_path):
    world = make_learn_world(tmp_path)
    query = Query({"https://github.com/example/skills": LookupError("网络不可用"),
                   "https://github.com/example/old": RepoFacts(9000, date(2026, 9, 1), False)})
    found = third_party.check(world.conn, world.clock, world.config, lock(tmp_path), query, ZONE)
    assert (found[0].passed, found[0].detail) == (False, "只读查询失败：网络不可用")
    third_party.check(world.conn, world.clock, world.config, lock(tmp_path), query, ZONE)
    assert len(query.calls) == 4
