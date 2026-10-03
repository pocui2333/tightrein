"""runner 测试共用的样例：任务、调用文件与夹具路径。"""

from dataclasses import replace
from pathlib import Path

from tightrein.domain.enums import Access, Stage
from tightrein.runner.adapters.base import InvocationFiles
from tightrein.runner.task import Instructions, Limits, RunnerTask, Subject

FIXTURES = Path(__file__).parent / "fixtures"
RUN = "R-20261005-030000-triage"
ENV = {"PATH": "/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0"}


def task(workdir=Path("/ws/worktrees/readonly"), **changes):
    base = RunnerTask(
        run_id=RUN, stage=Stage.TRIAGE, role="claim-verifier", subject=Subject("problem", "P-0042"), attempt=1,
        instructions=Instructions("判断以下主张是否成立"), workdir=workdir,
        output_schema="runner/roles/claim-verifier.schema.json", access=Access.READ_ONLY,
        allowed_commands=("git log", "git show"), limits=Limits(max_turns=40, max_duration_ms=600000),
    )
    return replace(base, **changes)


def invocation_files(directory, call=1, schema='{"type": "object"}'):
    files = InvocationFiles(directory, call)
    directory.mkdir(parents=True, exist_ok=True)
    files.prompt.write_text("# 任务\n判断以下主张是否成立\n", encoding="utf-8")
    files.schema.write_text(schema, encoding="utf-8")
    return files


def fixture(tool, name):
    return FIXTURES / tool / name
