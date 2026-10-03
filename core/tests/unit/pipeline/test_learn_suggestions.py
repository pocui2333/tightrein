import json
from datetime import timedelta

import pytest
from learn_world import (
    WEEK,
    ZONE,
    learn_env,
    make_learn_world,
    problem_with,
    save_run,
    save_signal,
    triaged,
)
from pipeline_world import NOW

from tightrein.domain.enums import (
    Disposition,
    KnowledgeType,
    Probe,
    ProblemStatus,
    SuggestionKind,
    SuggestionStatus,
    Verdict,
)
from tightrein.domain.run import Coverage, Endpoint
from tightrein.pipeline.learn.render import decision
from tightrein.pipeline.learn.render.decision import SuggestionText
from tightrein.pipeline.learn.steps import suggestions as step
from tightrein.pipeline.learn.steps import weeks
from tightrein.pipeline.learn.steps.metrics import MetricContext
from tightrein.pipeline.learn.steps.suggestions import Draft, SuggestionError
from tightrein.store.files import documents, markdown
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import knowledge, suggestions

RUN = "R-20261005-010000-collect-api-fuzz"


def context(world):
    return MetricContext(world.conn, world.layout, world.config, weeks.week_of(WEEK, ZONE), NOW, ZONE)


def drafted(found):
    return [(item.kind, item.subject) for item in found]


def _signals(world, count, noisy, check="not_a_server_error"):
    for number in range(count):
        save_signal(world, number + 1 + (100 if check != "not_a_server_error" else 0), RUN, check=check,
                    suppressed=number < noisy)


def test_noisy_checks_need_both_ratio_and_samples(tmp_path):
    world = make_learn_world(tmp_path)
    _signals(world, 5, 3)
    _signals(world, 4, 4, check="response_schema_conformance")
    assert drafted(step.noisy_checks(context(world))) == [
        (SuggestionKind.PROBE_CONFIG, "api-fuzz:not_a_server_error")]
    world2 = make_learn_world(tmp_path / "second")
    _signals(world2, 6, 3)
    assert step.noisy_checks(context(world2)) == []


def test_a_judged_false_positive_counts_as_noise(tmp_path):
    world = make_learn_world(tmp_path)
    signal_list = [save_signal(world, number, RUN) for number in range(1, 6)]
    problem_with(world, "P-0001", *signal_list[:3], status=ProblemStatus.IGNORED)
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE)
    (found,) = step.noisy_checks(context(world))
    assert (found.evidence["noisy"], found.evidence["total"]) == (3, 5)


def test_coverage_gaps_list_operations_never_covered(tmp_path):
    world = make_learn_world(tmp_path)
    commit = "a" * 40
    world.layout.openapi(commit).parent.mkdir(parents=True)
    world.layout.openapi(commit).write_text(json.dumps({"paths": {"/a": {"get": {}}, "/b": {"get": {}}}}),
                                            encoding="utf-8")
    save_run(world, RUN, probe=Probe.API_FUZZ, commit=commit, coverage=Coverage((Endpoint("GET", "/a", None),)))
    found = step.coverage_gaps(context(world))
    assert [(item.subject, item.evidence["missing"]) for item in found] == [("api-fuzz", ["GET /b"])]


def test_coverage_gaps_need_the_cached_description(tmp_path):
    world = make_learn_world(tmp_path)
    save_run(world, RUN, probe=Probe.API_FUZZ, commit="a" * 40)
    assert step.coverage_gaps(context(world)) == []


def test_store_skips_pending_duplicates_and_rejections_without_new_evidence(tmp_path):
    world = make_learn_world(tmp_path)
    first = Draft(SuggestionKind.COVERAGE_GAP, "api-fuzz", {"missing": ["GET /a"]}, ("GET /a",))
    (created,) = step.store(world.conn, world.clock, [first])
    assert created.id == "LS-0001" and created.evidence["items"] == ["GET /a"]
    assert step.store(world.conn, world.clock, [first]) == []
    step.reject(world.conn, world.clock, "LS-0001", "该接口不需要测")
    assert step.store(world.conn, world.clock, [first]) == []
    grown = Draft(SuggestionKind.COVERAGE_GAP, "api-fuzz", {"missing": ["GET /a", "GET /b"]}, ("GET /a", "GET /b"))
    assert [item.id for item in step.store(world.conn, world.clock, [grown])] == ["LS-0002"]


def test_expire_and_reject_rules(tmp_path):
    world = make_learn_world(tmp_path)
    step.store(world.conn, world.clock, [Draft(SuggestionKind.COVERAGE_GAP, "api-fuzz", {}, ("GET /users",))])
    assert step.expire(world.conn, NOW + timedelta(weeks=3), 4) == []
    assert step.expire(world.conn, NOW + timedelta(weeks=5), 4) == ["LS-0001"]
    with pytest.raises(SuggestionError, match="已是「已过期」"):
        step.reject(world.conn, world.clock, "LS-0001", "不需要")
    with pytest.raises(SuggestionError, match="原因"):
        step.reject(world.conn, world.clock, "LS-0001", " ")
    with pytest.raises(SuggestionError, match="不存在"):
        step.accept(learn_env(world), "LS-0009")


def control_draft():
    text = SuggestionText("关卡 merge 改为交用户确认", "本周撤销合并 3 次", "把 gates.merge 改为 user", "保持自动",
                          True, "纠正频繁", "在 project.yaml 中写 gates: {merge: user}")
    return Draft(SuggestionKind.CONTROL, "gate:merge", {"gate": "merge"}, ("merge:2026-10-05",), text)


def write_document(world):
    def write(suggestion_id, draft):
        path = decision.write(world.layout, suggestion_id, draft.subject, draft.document, NOW, "zh", ZONE)
        return world.layout.relative(path)

    return write


def test_accepting_a_control_only_records_the_decision(tmp_path):
    world = make_learn_world(tmp_path)
    world.layout.project_config().write_text("gates:\n  merge: auto\n", encoding="utf-8")
    (created,) = step.store(world.conn, world.clock, [control_draft()], write_document(world))
    assert created.target_path == "data/improve/LS-0001.md"
    accepted = step.accept(learn_env(world), "LS-0001")
    assert accepted.status is SuggestionStatus.ACCEPTED
    assert world.layout.project_config().read_text(encoding="utf-8") == "gates:\n  merge: auto\n"
    document = documents.read(world.layout.root / created.target_path)
    assert document.header["status"] == "done" and "用户批准" in document.body


def test_rejecting_a_control_records_the_reason_in_its_document(tmp_path):
    world = make_learn_world(tmp_path)
    (created,) = step.store(world.conn, world.clock, [control_draft()], write_document(world))
    step.reject(world.conn, world.clock, "LS-0001", "本周是特殊情况", world.layout.root)
    assert "用户拒绝：本周是特殊情况" in documents.read(world.layout.root / created.target_path).body


def _entries(world, *ids):
    for entry_id in ids:
        frontmatter = {"id": entry_id, "type": "triage-lesson", "summary": f"经验 {entry_id}", "tags": ["topic"],
                       "status": "active", "supersededBy": None, "updated": "2026-06-01", "reviewBy": "2026-09-01",
                       "related": []}
        markdown.write(world.layout.knowledge_file(KnowledgeType.TRIAGE_LESSON, entry_id, "lesson"),
                       MarkdownDocument(frontmatter, f"# 经验 {entry_id}\n\n正文\n"))
    world.knowledge_service().sync()


@pytest.mark.parametrize("action,expected", [
    ("renew", {"TL-0001": ("active", "2027-01-03", None), "TL-0002": ("active", "2027-01-03", None)}),
    ("archive", {"TL-0001": ("archived", "2026-09-01", None), "TL-0002": ("archived", "2026-09-01", None)}),
    ("merge", {"TL-0001": ("superseded", "2026-09-01", "TL-0002"), "TL-0002": ("active", "2026-09-01", None)}),
])
def test_review_suggestions_apply_the_chosen_action(tmp_path, action, expected):
    world = make_learn_world(tmp_path)
    _entries(world, "TL-0001", "TL-0002")
    knowledge.record_hit(world.conn, "TL-0002", NOW)
    step.store(world.conn, world.clock, [Draft(SuggestionKind.KNOWLEDGE_REVIEW, "TL-0001,TL-0002",
                                               {"ids": ["TL-0001", "TL-0002"], "reason": "重复"},
                                               ("TL-0001", "TL-0002"))])
    with pytest.raises(SuggestionError, match="--action"):
        step.accept(learn_env(world), "LS-0001")
    step.accept(learn_env(world), "LS-0001", action)
    found = {}
    for entry_id in ("TL-0001", "TL-0002"):
        record = knowledge.get(world.conn, entry_id)
        found[entry_id] = (record.status.value, record.review_by.isoformat(), record.superseded_by)
    assert found == expected
    assert suggestions.get(world.conn, "LS-0001").reason == action


def test_other_suggestions_only_record_the_decision(tmp_path):
    world = make_learn_world(tmp_path)
    step.store(world.conn, world.clock, [Draft(SuggestionKind.PROBE_CONFIG, "api-fuzz:x", {}, ("S-1",))])
    assert step.accept(learn_env(world), "LS-0001").status is SuggestionStatus.ACCEPTED
    with pytest.raises(SuggestionError, match="--output"):
        step.accept(learn_env(world, tmp_path / "out"), "LS-0001")
    assert knowledge.find(world.conn) == []
