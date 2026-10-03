"""evaluation 的异常类型(architecture/03 2.10)。所有停止都写明原因与下一步命令，不以部分结果给出 pass。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class CaseProblem:
    """一个用例的问题：用例键(`<模块>/<用例编号>` 或 `retrieval/cases.jsonl`)与原因。"""

    case: str
    reason: str

    def __str__(self) -> str:
        return f"{self.case}: {self.reason}"


class EvaluationError(Exception):
    """evaluation 全部异常的基类。"""


class _CaseError(EvaluationError):
    headline = ""
    next_step = ""

    def __init__(self, problems: Sequence[CaseProblem]) -> None:
        self.problems = tuple(sorted(problems))
        lines = "\n".join(str(problem) for problem in self.problems)
        super().__init__(f"{self.headline}：\n{lines}\n{self.next_step}")


class EvalCaseTampered(_CaseError):
    """用例哈希与 manifest 不一致、用例多出或缺少、evals/ 有未提交的改动。"""

    headline = "评测用例与封存的 manifest 不一致或有未提交的改动"
    next_step = "检查这些改动；确认无误并提交后，由用户在终端执行 tightrein eval seal 重新封存"


class EvalCaseInvalid(_CaseError):
    """case.json 不合 schema、引用了不存在的评分项或输入文件。"""

    headline = "评测用例不合格"
    next_step = "按原因修改 case.json 或补上输入文件，提交后执行 tightrein eval seal"


class SealRefused(EvaluationError):
    """重新封存只能由用户在交互终端中确认后执行。"""


class RubricInvalid(EvaluationError):
    """评分表引用了未注册的评分器、编号重复或字段不合格。"""


class EvaluationRefused(EvaluationError):
    """候选版本触及防作弊路径。"""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(sorted(paths))
        super().__init__("候选版本改动了评测不允许改动的文件，拒绝评测：\n" + "\n".join(self.paths))


class SnapshotFailed(EvaluationError):
    """git archive 失败、diff 不能干净应用、数据库备份失败；已生成的目录保留便于排查。"""
