"""core/github-deployments：GitHub Deployments 的部署记录(redesign/07-release.md 第 4 节)。

在仓库目录执行只读的 `gh api repos/{owner}/{repo}/deployments`(gh 按当前仓库替换占位符)，再为每个部署读取最新的一条
状态：success、inactive(已被之后的部署取代，曾经成功)为 succeeded，failure、error 为 failed，其余(queued、pending、
in_progress)与还没有状态的为 running。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.extensions.methods import deploys, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
API = "repos/{owner}/{repo}/deployments"
SUCCEEDED_STATES = frozenset({"success", "inactive"})
FAILED_STATES = frozenset({"failure", "error"})


def status_of(state: str | None) -> str:
    if state in SUCCEEDED_STATES:
        return deploys.SUCCEEDED
    return deploys.FAILED if state in FAILED_STATES else deploys.RUNNING


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    query = {"per_page": options["limit"]}
    if options["environment"]:
        query["environment"] = options["environment"]
    found = deploys.gh_json(request, context, "api", f"{API}?{urlencode(query)}") or []
    records = []
    for item in found:
        statuses = deploys.gh_json(request, context, "api", f"{API}/{item['id']}/statuses?per_page=1") or []
        latest = statuses[0] if statuses else {}
        url = latest.get("environment_url") or latest.get("target_url") or None
        records.append(deploys.record(item["id"], item["sha"], status_of(latest.get("state")), item.get("environment"),
                                      url, format_iso(parse_iso(item["created_at"]))))
    return MethodResult(deploys.output(records))


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
