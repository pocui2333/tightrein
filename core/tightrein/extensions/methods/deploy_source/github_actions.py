"""core/github-actions：部署工作流的运行即部署记录(redesign/07-release.md 第 4 节)。

`gh run list --workflow <工作流> --branch <分支> --json ... --limit <条数>` 在仓库目录执行(只读)。未完成的运行为
running；完成的按结论：success 为 succeeded，skipped 为 skipped(这次运行没有部署，由核心找之后包含该提交的运行)，
其余(失败、取消、超时等)为 failed。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.extensions.methods import deploys, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
FIELDS = "databaseId,headSha,status,conclusion,createdAt,url"
COMPLETED, SUCCESS, SKIPPED = "completed", "success", "skipped"


def status_of(run: dict) -> str:
    if run.get("status") != COMPLETED:
        return deploys.RUNNING
    if run.get("conclusion") == SUCCESS:
        return deploys.SUCCEEDED
    return deploys.SKIPPED if run.get("conclusion") == SKIPPED else deploys.FAILED


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    branch = options["branch"] or request.input["branch"]
    runs = deploys.gh_json(request, context, "run", "list", "--workflow", options["workflow"], "--branch", branch,
                           "--json", FIELDS, "--limit", str(options["limit"])) or []
    records = [deploys.record(item["databaseId"], item["headSha"], status_of(item), None, item.get("url"),
                              format_iso(parse_iso(item["createdAt"]))) for item in runs]
    return MethodResult(deploys.output(records))


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
