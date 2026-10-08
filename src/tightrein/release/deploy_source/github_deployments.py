"""github_deployments：GitHub Deployments 与各自最新的状态(`gh api`，只读)。

- `gh api repos/{owner}/{repo}/deployments?per_page=<limit>[&environment=<environment>]`，每个部署再取最新的一条
  状态(`.../statuses?per_page=1`)；
- success 与 inactive(已被之后的部署取代，曾经成功)为 succeeded；failure、error 为 failed；其余与还没有状态的为
  running；链接取状态的 environment_url，没有时取 target_url。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from tightrein.protocol.methods import Configured
from tightrein.protocol.naming import parse_iso
from tightrein.release.deploy import (
    FAILED,
    RUNNING,
    SUCCEEDED,
    DeployRecord,
    Gh,
    SourceContext,
    gh_json,
    sorted_records,
)

API = "repos/{owner}/{repo}/deployments"  # gh 按当前仓库替换占位符
SUCCEEDED_STATES = frozenset({"success", "inactive"})
FAILED_STATES = frozenset({"failure", "error"})


def read(configured: Configured, context: SourceContext) -> list[DeployRecord]:
    options = configured.options
    return github_deployments(context.gh, environment=options.get("environment"), limit=int(options["limit"]))


def github_deployments(gh: Gh, *, environment: str | None, limit: int) -> list[DeployRecord]:
    query: dict[str, Any] = {"per_page": limit}
    if environment:
        query["environment"] = environment
    found = gh_json(gh, "api", f"{API}?{urlencode(query)}") or []
    records = []
    for item in found:
        statuses = gh_json(gh, "api", f"{API}/{item['id']}/statuses?per_page=1") or []
        latest = statuses[0] if statuses else {}
        records.append(DeployRecord(
            str(item["id"]), item["sha"], deployments_status(latest.get("state")), item.get("environment"),
            latest.get("environment_url") or latest.get("target_url") or None, parse_iso(item["created_at"]),
        ))
    return sorted_records(records)


def deployments_status(value: str | None) -> str:
    if value in SUCCEEDED_STATES:
        return SUCCEEDED
    return FAILED if value in FAILED_STATES else RUNNING
