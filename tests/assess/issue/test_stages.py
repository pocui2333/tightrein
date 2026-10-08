"""Issue 状态到推进阶段的映射只定义一处：调度与实施入口都读它。"""

from tightrein.assess.issue import stages
from tightrein.assess.issue.stages import IMPLEMENT, RELEASE, stage_for, statuses_for
from tightrein.assess.issue.transitions import IssueStatus
from tightrein.protocol import schedule


def test_each_status_maps_to_the_stage_that_advances_it() -> None:
    assert {status: stage_for(status) for status in IssueStatus} == {
        IssueStatus.NEEDS_DECISION: None, IssueStatus.TODO: IMPLEMENT, IssueStatus.IMPLEMENTING: IMPLEMENT,
        IssueStatus.RELEASING: RELEASE, IssueStatus.ACCEPTING: RELEASE, IssueStatus.DONE: None,
        IssueStatus.CANCELLED: None, IssueStatus.HELD: None}
    assert stage_for(None) is None
    # 实施中的优先于待修
    assert statuses_for(IMPLEMENT) == ("implementing", "todo") and statuses_for(RELEASE) == ("releasing", "accepting")
    assert set(stages.STAGE_FOR_STATUS) <= {status.value for status in IssueStatus}


def test_the_scheduler_reads_the_same_mapping() -> None:
    assert schedule.FOR_IMPLEMENT == statuses_for(IMPLEMENT) and schedule.FOR_RELEASE == statuses_for(RELEASE)
