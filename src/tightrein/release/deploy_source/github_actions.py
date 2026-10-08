"""github_actions：部署工作流的运行即部署记录(`gh run list`，只读)。

- `gh run list --workflow <workflow> --branch <branch> --json ... --limit <limit>`；branch 没写时取项目的主干分支；
- 未完成的为 running；成功为 succeeded；skipped 的运行这次没有部署(由发布找之后包含该提交的运行)，不是失败；
  其余结论(failure、cancelled 等)为 failed。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tightrein.protocol.methods import Configured
from tightrein.protocol.naming import parse_iso
from tightrein.release.deploy import (
    FAILED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    DeployError,
    DeployErrorKind,
    DeployRecord,
    Gh,
    SourceContext,
    gh_json,
    sorted_records,
)

FIELDS = "databaseId,headSha,status,conclusion,createdAt,url"
COMPLETED, SUCCESS = "completed", "success"


def read(configured: Configured, context: SourceContext) -> list[DeployRecord]:
    options = configured.options
    branch = options.get("branch") or context.main_branch
    if branch is None:
        raise DeployError(DeployErrorKind.MISCONFIGURED, "github_actions 没有 branch，项目也没有主干分支")
    return github_actions(context.gh, workflow=str(options["workflow"]), branch=branch, limit=int(options["limit"]))


def github_actions(gh: Gh, *, workflow: str, branch: str, limit: int) -> list[DeployRecord]:
    runs = gh_json(gh, "run", "list", "--workflow", workflow, "--branch", branch, "--json", FIELDS,
                   "--limit", str(limit)) or []
    return sorted_records(DeployRecord(str(item["databaseId"]), item["headSha"], actions_status(item), None,
                                       item.get("url"), parse_iso(item["createdAt"])) for item in runs)


def actions_status(run: Mapping[str, Any]) -> str:
    if run.get("status") != COMPLETED:
        return RUNNING
    if run.get("conclusion") == SUCCESS:
        return SUCCEEDED
    return SKIPPED if run.get("conclusion") == SKIPPED else FAILED
