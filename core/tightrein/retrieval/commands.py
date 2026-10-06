"""tightrein admin kb 各子命令背后的函数(architecture/03 1.7)；命令行解析与表格输出属于 cli/kb.py。

- 每个函数只把参数转换成 KnowledgeService 的调用，返回可直接写成 JSON 的字典；命令行的 --json 输出与 MCP 工具的
  结构化结果都来自这里，两个入口的结果因此一致；
- open_service 按环境变量打开工作区：TIGHTREIN_SANDBOX 为 1 时以沙箱模式打开，事件写到 TIGHTREIN_OUTPUT_DIR
  (执行器在 --output 模式下设置)中的 events.jsonl；TIGHTREIN_RUN_ID 等变量使事件挂到当前运行的 trace 下；
- 退出码与其他命令统一(architecture/09 4.5，映射在 cli/exit_codes)：参数、查询串不合法与编号不存在为 2，
  知识文件格式错误(kb sync)为 1，数据库不可用为 3；
- kb queries 从事件日志汇总 search 事件，供用户挑选检索评测用例。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.config import project, user
from tightrein.domain.clock import Clock, format_iso
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.retrieval import benchmark
from tightrein.retrieval.errors import IndexUnavailable, SyncFailed
from tightrein.retrieval.models import DEFAULT_LIMIT, SearchFilters
from tightrein.retrieval.service import KnowledgeService, RetrievalSettings, open_index
from tightrein.runner.service import ENV_OUTPUT_DIR, ENV_SANDBOX, ENV_WORKSPACE
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.process import VcsProcess

SANDBOX_ON = "1"
KB_AGENT = "kb"
SEARCH = "search"


def workspace_from(environ: Mapping[str, str], given: Path | None = None) -> Path:
    """--workspace 优先，其次执行器传给 agent 进程的 TIGHTREIN_WORKSPACE。"""
    if given is not None:
        return given
    if environ.get(ENV_WORKSPACE):
        return Path(environ[ENV_WORKSPACE])
    raise IndexUnavailable(f"没有给出 --workspace，环境变量 {ENV_WORKSPACE} 也没有设置")


def open_service(workspace: Path, environ: Mapping[str, str], clock: Clock) -> KnowledgeService:
    """打开工作区的知识服务；调用方用完后关闭 service.conn。"""
    sandbox = environ.get(ENV_SANDBOX) == SANDBOX_ON
    output_dir = None
    if sandbox:
        if not environ.get(ENV_OUTPUT_DIR):
            raise IndexUnavailable(f"沙箱模式缺少 {ENV_OUTPUT_DIR}，无法确定事件的写入位置")
        output_dir = Path(environ[ENV_OUTPUT_DIR])
    layout = WorkspaceLayout(workspace, output_dir)
    conn = open_index(layout)
    config_path = layout.project_config()
    settings = RetrievalSettings.from_config(project.load(config_path, agents=user.load().agents)) \
        if config_path.is_file() else RetrievalSettings.default()
    tracer = Tracer.from_environment(EventLog(layout, Redactor()), clock, environ)
    return KnowledgeService(layout, conn, clock, tracer, settings=settings, sandbox=sandbox)


def search(service: KnowledgeService, query: str, types: tuple[str, ...] = (), tags: tuple[str, ...] = (),
           status: str = "active", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    return service.search(query, SearchFilters.parse(types, tags, status, limit)).to_dict()


def get(service: KnowledgeService, entry_id: str) -> dict[str, Any]:
    return service.get(entry_id).to_dict()


def related(service: KnowledgeService, entry_id: str) -> dict[str, Any]:
    return service.related(entry_id).to_dict()


def stale(service: KnowledgeService) -> dict[str, Any]:
    return service.stale().to_dict()


def sync(service: KnowledgeService, full: bool = False) -> dict[str, Any]:
    """显式同步；有格式或一致性错误时抛出 SyncFailed(退出码 1)，列出全部错误。"""
    report = service.sync(full)
    if report.errors:
        raise SyncFailed(tuple(report.errors))
    return {"added": report.added, "updated": report.updated, "removed": report.removed}


def evaluate(service: KnowledgeService, process: VcsProcess, clock: Clock,
             baseline_id: str | None = None) -> dict[str, Any]:
    """kb eval：检索评测，报告写入 data/evals/<评测编号>/。"""
    return benchmark.evaluate_retrieval(service, process, clock, baseline_id).to_dict()


@dataclass(frozen=True)
class RecordedQuery:
    timestamp: str
    run_id: str | None
    query: str
    filters: Mapping[str, Any]
    ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "runId": self.run_id, "query": self.query, "filters": dict(self.filters),
                "ids": self.ids}


def _log_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def queries(layout: WorkspaceLayout, since: date) -> list[RecordedQuery]:
    """since 当天及之后的事件日志中全部 kb search 事件，按时间排序。--output 模式只有输出目录中的一份事件日志；
    日志目录中文件名不是「events-<日期>.jsonl」的文件不读。"""
    log = layout.events_log(since)
    if layout.output_dir is not None:
        paths = [log] if log.is_file() else []
    else:
        prefix, suffix = log.name.split(since.isoformat())
        paths = [path for path in sorted(layout.logs_dir().glob(f"{prefix}*{suffix}"))
                 if (day := _log_day(path.name[len(prefix):-len(suffix)])) is not None and day >= since]
    found = []
    for path in paths:
        for event in events.read(path):
            attributes = event.attributes
            if event.agent == KB_AGENT and event.operation == "execute_tool" and attributes.get("operation") == SEARCH:
                found.append(RecordedQuery(format_iso(event.timestamp), event.run_id, attributes["query"],
                                           attributes["filters"], list(attributes["ids"])))
    return sorted(found, key=lambda item: item.timestamp)

