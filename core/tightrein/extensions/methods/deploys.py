"""部署来源方法的共用部分(redesign/07-release.md 第 4 节)：执行只读的 gh 命令、组装部署记录、按时间排序。

各方法只读取平台上的最近部署；包含某个合并提交的部署由核心按 git 祖先关系判断(pipeline/common/deploys.py)。
gh 未安装以 tool-missing 结束；命令超时、退出码非 0 以 source-unavailable 结束；输出不是 JSON 以 parse-failed 结束。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.invoke import ProcessRequest
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest

GH = "gh"
STDERR_TAIL_LINES = 3
RUNNING, SUCCEEDED, FAILED, SKIPPED = "running", "succeeded", "failed", "skipped"


def gh_json(request: MethodRequest, context: MethodContext, *args: str) -> Any:
    timeout = float(request.options["timeoutSeconds"])
    cwd = request.repo or request.workspace
    outcome = context.runner(ProcessRequest((GH, *args), cwd, dict(context.environ), b"", timeout))
    if outcome.start_error is not None:
        raise MethodError(ExtensionErrorCode.TOOL_MISSING, f"无法启动 gh：{outcome.start_error}",
                          "安装 GitHub CLI 并执行 gh auth login")
    if outcome.timed_out:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"gh {' '.join(args[:2])} 超过 {timeout:g} 秒未结束")
    if outcome.exit_code != 0:
        tail = outcome.stderr.decode("utf-8", errors="replace").strip().splitlines()[-STDERR_TAIL_LINES:]
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE,
                          f"gh {' '.join(args[:2])} 以 {outcome.exit_code} 退出：{' / '.join(tail) or '没有错误输出'}")
    try:
        return json.loads(outcome.stdout.decode("utf-8") or "null")
    except ValueError as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"gh {' '.join(args[:2])} 的输出不是 JSON") from error


def record(identifier: Any, commit: str, status: str, environment: str | None, url: str | None,
           created_at: str) -> dict[str, Any]:
    return {"id": str(identifier), "commit": commit, "status": status, "environment": environment, "url": url,
            "createdAt": created_at}


def output(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {"deployments": sorted((dict(item) for item in records), key=lambda item: (item["createdAt"], item["id"]))}
