"""第 6 步 落库(architecture/05 2.5、2.8)：一个事务内写信号、更新运行、记录复现检查结果、保存读取位置、项目探针
状态、待处理疑点与已读记录。

事务中途失败时整体回滚：没有该运行的任何信号，读取位置与已读记录不前进，下次重读。
replace 用于重新解析尚未聚合的运行：先删除该运行已有的信号再写入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.sources.base import ProbeOutcome, save_state
from tightrein.store.db import transaction
from tightrein.store.repos import regressions, runs, signals as signal_repo


def commit(conn: sqlite3.Connection, run: Run, outcome: ProbeOutcome, signals: Sequence[Signal],
           outcomes: Sequence[RegressionOutcome], *, replace: bool = False) -> None:
    if run.ended_at is None:
        raise ValueError(f"运行 {run.id} 还没有结束时间")
    with transaction(conn):
        if replace:
            signal_repo.delete_for_run(conn, run.id)
        runs.save(conn, run)
        for signal in signals:
            signal_repo.save(conn, signal)
        for item in outcomes:
            regressions.record_result(conn, item.check.issue_id, item.check.check_id, item.result, run.id,
                                      run.ended_at, run.target_commit)
        save_state(conn, outcome)
