"""core/semgrep：以 Semgrep 作为 static-tools 的确定性工具(architecture/10 1.4、3.6)。

复用 static 探针的 Semgrep 调用(probes/static/tools/semgrep.py)：规则取 options.configs，incremental 档位只扫描
改动文件中仍然存在的文件，full 档位扫描整个仓库；输出与日志写在 rawDir 下的 core-semgrep/，与探针自己运行的
Semgrep 分开。命令取扩展进程环境变量 TIGHTREIN_SEMGREP(由核心按 runtime.tools.semgrep 与本机用户配置
tools.semgrep.path 解析后传入)，没有时为 semgrep。Semgrep 失败或没有可扫描的文件时该工具为 failed 或 skipped，发现为空；探针据此判定 partial。
sources.static.semgrep.configs 与本方法的规则各自运行，同一组规则只应配置在其中一处。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ProbeLevel
from tightrein.extensions.methods import runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodRequest, MethodResult
from tightrein.sources.common.procs import SubprocessLauncher
from tightrein.sources.static.scope import Scope
from tightrein.sources.static.tools import semgrep

MANIFEST = Path(__file__).with_suffix(".yaml")
OUTPUT_DIR = "core-semgrep"
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
CURRENT_DIR_PREFIX = "./"


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    repo = runtime.require_repo(request)
    level = ProbeLevel(request.input["level"])
    changed = tuple(request.input["changedFiles"])
    files = tuple(path for path in changed if (repo / path).is_file()) if level is ProbeLevel.INCREMENTAL else ()
    scope = Scope(level, request.input["baseCommit"], request.commit or "", files, changed)
    raw_dir = Path(request.input["rawDir"]) / OUTPUT_DIR
    result = semgrep.run(SubprocessLauncher(context.runner), request.options["configs"], scope, repo, raw_dir,
                         context.environ, request.options["timeoutSeconds"],
                         context.environ.get(semgrep.COMMAND_ENV) or semgrep.TOOL)
    ran = result.status != STATUS_SKIPPED and (raw_dir / semgrep.LOG_FILE).is_file()
    tool = {"name": semgrep.TOOL, "status": result.status, "exitCode": result.exit_code,
            "logFile": f"{OUTPUT_DIR}/{semgrep.LOG_FILE}" if ran else None,
            "reason": None if result.status == STATUS_OK else "；".join(result.notes)}
    findings = []
    for finding in result.findings:
        item = finding.to_dict()
        item["file"] = item["file"].removeprefix(CURRENT_DIR_PREFIX)
        findings.append(item)
    notes = result.notes if result.status == STATUS_OK else ()
    return MethodResult({"tools": [tool], "findings": findings}, notes)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
