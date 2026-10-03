import pytest
from pipeline_world import make_world

from tightrein.domain.enums import OperationKind, OperationStatus, Stage
from tightrein.vcs import operations
from tightrein.vcs.executor import FollowUp, OperationRunner


def ask(world, key="plan:0007:aaa", kind=OperationKind.FIX_PLAN):
    return operations.confirmation(world.conn, world.clock, stage=Stage.FIX, subject_id="0007", kind=kind,
                                   repo="/tmp/demo-repo", text="确认修复计划", impact="开始按计划修改代码",
                                   preconditions={"planSha256": "a" * 64}, key=key)


def test_confirmations_have_no_commands_and_are_idempotent(tmp_path):
    world = make_world(tmp_path)
    first = ask(world)
    assert (first.commands, first.status) == ((), OperationStatus.PENDING)
    assert first.preconditions == {"planSha256": "a" * 64}
    assert first.to_dict()["commands"] == []
    assert ask(world).id == first.id
    assert ask(world, key="plan:0007:bbb").id != first.id
    with pytest.raises(ValueError, match="OperationPlanner"):
        ask(world, kind=OperationKind.COMMIT)
    assert "确认修复计划" in operations.describe(first)


def test_confirmed_operations_run_the_follow_up_without_commands(tmp_path):
    world = make_world(tmp_path)
    seen = []
    runner = OperationRunner(world.conn, None, None, None, world.layout, "R-20261005-030000-fix",
                             follow_ups={Stage.FIX: FollowUp(executed=seen.append)})
    operation = ask(world)
    runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    result = runner.execute(operation.id, clock=world.clock)
    assert (result.status, result.ran) == (OperationStatus.EXECUTED, True)
    assert [item.id for item in seen] == [operation.id]
