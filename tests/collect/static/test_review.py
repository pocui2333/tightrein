import json

import pytest

from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.common.source import SourceUnavailable
from tightrein.collect.static import review
from tightrein.protocol import vendor
from tightrein.store.files.layout import ToolLayout


@pytest.mark.parametrize(("status", "violations", "environment"), [
    (CallStatus.BOUNDARY, ["read_only_changed：/w 在调用前后有变化"], True),
    (CallStatus.BOUNDARY, ["outside：/repo 在调用前后有变化"], True),
    (CallStatus.BOUNDARY, [], True),
    (CallStatus.BOUNDARY, ["command：不在白名单的命令 rm -rf ."], False),
    (CallStatus.SCHEMA_INVALID, [], False),
])
def test_environment_violations_void_the_whole_run(status, violations, environment):
    result = CallResult(status, "claude", "opus", violations=violations)
    assert review.violated_environment(result) is environment


def test_the_review_prompt_lists_the_verified_skills_and_gives_their_directories(static_runtime, fake_caller,
                                                                                 make_claim, tmp_path):
    runtime = static_runtime()
    caller = fake_caller({"collect.static.review": {"claims": [make_claim()], "excluded": []}})
    done = review.review(runtime, workdir=tmp_path, range_text="a..b", changes="## diff", findings=[],
                         knowledge="没有与本次相关的知识条目。", max_claims=10, caller=caller)
    params = caller.calls[0]
    assert done.ok and [item.file for item in done.claims] == ["src/orders.py"]
    skills = runtime.tool.vendor_skill("differential-review")
    assert skills in params.read_paths and f"{skills / 'SKILL.md'}" in params.prompt
    assert params.point == "collect.static.review" and params.schema["title"].startswith("静态巡检")
    assert "a..b" in params.prompt and "## 主张条数上限\n\n10" in params.prompt


def test_a_tampered_skill_is_not_loaded(static_runtime, tmp_path):
    root = tmp_path / "tool"
    (root / "vendor" / "skills" / "sharp-edges").mkdir(parents=True)
    (root / "vendor" / "skills" / "sharp-edges" / "SKILL.md").write_text("改过的内容\n", encoding="utf-8")
    lock = json.loads(ToolLayout.discover().vendor_lock.read_text(encoding="utf-8"))
    (root / "vendor" / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    runtime = static_runtime()
    runtime.tool = ToolLayout(root)
    with pytest.raises(SourceUnavailable, match="sharp-edges"):
        review.load_skills(runtime, ("sharp-edges",))
    assert isinstance(vendor.HashMismatch([]), vendor.VendorError)


def test_failed_calls_carry_no_claims(make_failed):
    done = review.to_review("增量审查", make_failed(CallStatus.QUOTA_EXHAUSTED))
    assert done.claims == () and done.exhausted and "增量审查 未完成(quota_exhausted)" in done.problem()
