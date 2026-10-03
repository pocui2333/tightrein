"""复现确认的策略与判定(redesign/02-aggregate.md 第 3 节)：能确定性重放的信号重放确认，其余首次观察即有效。"""

from __future__ import annotations

from typing import Sequence

from tightrein.domain.enums import Probe, ProblemEvent, ReproduceStrategy

# api-fuzz 中重放能判定的检查：重放器(sources/api_fuzz/replay.signal_replayer)只凭响应状态码判定，
# 其余检查重放的结果总是「无法判断」
API_FUZZ_REPLAY_CHECKS = frozenset({"not_a_server_error"})


def strategy(probe: Probe, check: str) -> ReproduceStrategy:
    """api-fuzz 的服务端错误重放请求；其余直接视为有效(api-fuzz 的其他检查是对照接口描述与权限模型的确定性判定，
    平台来源与项目探针的信号是已发生的事实，静态巡检的主张已经取证)。"""
    if probe is Probe.API_FUZZ and check in API_FUZZ_REPLAY_CHECKS:
        return ReproduceStrategy.REPLAY
    return ReproduceStrategy.IMMEDIATE


def judge_replays(results: Sequence[bool | None], attempts: int = 2) -> ProblemEvent | None:
    """每次重放的结果：True 复现、False 未复现、None 无法执行(目标不可用或登录失败)。

    至少一次复现为有效；有无法执行的重放且没有复现时返回 None，问题保持待确认、下次重试。
    """
    if len(results) != attempts:
        raise ValueError(f"重放结果应有 {attempts} 次，实际 {len(results)} 次")
    if any(result is True for result in results):
        return ProblemEvent.REPRODUCED
    if any(result is None for result in results):
        return None
    return ProblemEvent.NOT_REPRODUCED
