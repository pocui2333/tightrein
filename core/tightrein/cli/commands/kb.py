"""kb 的命令(architecture/03 1.7)：search、get、related、stale、sync、eval、queries、mcp。

结果与 MCP 工具的结构化结果同样来自 retrieval.commands；退出码与其他命令相同(architecture/09 4.5，cli/exit_codes)。
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli import kb_mcp
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome, error
from tightrein.retrieval import commands as kb
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, InvalidQuery, SyncFailed
from tightrein.retrieval.models import DEFAULT_LIMIT

KB_ERRORS = (InvalidQuery, EntryNotFound, SyncFailed, IndexUnavailable, sqlite3.Error)


def _kb(name: str, action: Any) -> Any:
    def handler(invocation: Any) -> Outcome:
        try:
            values = action(invocation)
        except KB_ERRORS as failure:
            return Outcome(name, exit_codes.for_error(failure), [str(failure)], errors=[error(type(failure).__name__,
                                                                                      str(failure))])
        return Outcome(name, exit_codes.OK, [f"{name} 完成"], result=values)

    return handler


def _search(invocation: Any) -> Any:
    args = invocation.args
    return kb.search(invocation.app.knowledge(), args.query, tuple(args.type), tuple(args.tags), args.status,
                     args.limit)


def _queries(invocation: Any) -> Any:
    text = invocation.args.since
    try:
        since = date.fromisoformat(text) if text else invocation.app.clock.now().date()
    except ValueError as failure:
        raise UsageError(f"日期须为 YYYY-MM-DD：{text}") from failure
    return [item.to_dict() for item in kb.queries(invocation.app.layout, since)]


def _mcp(invocation: Any) -> Outcome:
    kb_mcp.main(["--workspace", str(invocation.app.root)], dict(invocation.app.environ), invocation.app.clock)
    return Outcome("kb mcp", exit_codes.OK, ["MCP 服务已退出"])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    group_ = group(commands, "kb", "知识检索")
    search = leaf(group_, common, "search", _kb("kb search", _search), "按关键词检索", "kb search")
    search.add_argument("query")
    search.add_argument("--type", action="append", default=[], help="类型，可重复")
    search.add_argument("--tags", action="append", default=[], help="标签，可重复，须全部包含")
    search.add_argument("--status", default="active")
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    get = leaf(group_, common, "get", _kb("kb get", lambda i: kb.get(i.app.knowledge(), i.args.entry)),
               "按编号读取", "kb get")
    get.add_argument("entry")
    related = leaf(group_, common, "related", _kb("kb related", lambda i: kb.related(i.app.knowledge(),
                                                                                    i.args.entry)),
                   "关联条目", "kb related")
    related.add_argument("entry")
    leaf(group_, common, "stale", _kb("kb stale", lambda i: kb.stale(i.app.knowledge())), "待复核的条目", "kb stale")
    sync = leaf(group_, common, "sync", _kb("kb sync", lambda i: kb.sync(i.app.knowledge(), i.args.full)),
                "同步索引与 INDEX.md", "kb sync")
    sync.add_argument("--full", action="store_true")
    evaluate = leaf(group_, common, "eval", _kb("kb eval", lambda i: kb.evaluate(
        i.app.knowledge(), i.app.process, i.app.clock, i.args.baseline)), "检索评测", "kb eval")
    evaluate.add_argument("--baseline", help="基线评测编号")
    queries = leaf(group_, common, "queries", _kb("kb queries", _queries), "汇总检索记录", "kb queries")
    queries.add_argument("--since", help="起始日期 YYYY-MM-DD，缺省为今天")
    leaf(group_, common, "mcp", _mcp, "以 stdio 运行知识检索的 MCP 服务", "kb mcp")
