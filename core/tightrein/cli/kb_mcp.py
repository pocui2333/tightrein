"""知识检索的本地 MCP 服务(architecture/03 1.7)：以 stdio 方式运行，不监听端口。

- 使用 MCP 官方 Python SDK 的高层接口 MCPServer，四个工具以带类型注解与文档字符串的函数定义，SDK 据此生成参数的
  JSON schema；返回值与命令行的 --json 输出相同(都来自 retrieval.commands)；
- 每次调用打开一次工作区的知识服务、用完即关：服务进程长驻，SDK 在工作线程中执行工具函数，数据库连接不跨线程共用；
  调用前的增量同步使知识文件的改动无需重启即可生效；
- 查询串不合法、编号不存在、过滤条件不合法抛出 ToolError，agent 看到带 is_error 的结果，可以修正参数后重试；
  数据库不可用、工作区路径无效抛出内部错误码的 MCPError，整个调用失败；工具函数不以返回字符串的方式报告错误；
- stdout 是协议通道：进程内不使用 print，日志一律经 logging 写到 stderr。
启动：`python -m tightrein.cli.kb_mcp --workspace <工作区绝对路径>`，没有 --workspace 时取 TIGHTREIN_WORKSPACE；
`tightrein kb mcp` 调用同一个 main。
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INTERNAL_ERROR

from tightrein.domain.clock import Clock, SystemClock
from tightrein.retrieval import commands
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, InvalidQuery
from tightrein.retrieval.models import DEFAULT_LIMIT
from tightrein.retrieval.service import KnowledgeService

SERVER_NAME = "tightrein-kb"
INSTRUCTIONS = ("本工具积累的知识(缺陷模式、已接受的取舍、分诊与修复经验、Issue、报告与改进提案)的检索。"
                "检索只返回编号与摘要，需要正文时用 kb_get 按编号读取。")

ServiceFactory = Callable[[], KnowledgeService]


def _call(open_service: ServiceFactory, action: Callable[[KnowledgeService], dict[str, Any]]) -> dict[str, Any]:
    try:
        service = open_service()
    except (IndexUnavailable, sqlite3.Error) as error:
        raise MCPError(INTERNAL_ERROR, str(error)) from error
    try:
        return action(service)
    except (InvalidQuery, EntryNotFound) as error:
        raise ToolError(str(error)) from error
    except (IndexUnavailable, sqlite3.Error) as error:
        raise MCPError(INTERNAL_ERROR, str(error)) from error
    finally:
        service.conn.close()


def build_server(open_service: ServiceFactory) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.tool()
    def kb_search(query: str, types: list[str] | None = None, tags: list[str] | None = None, status: str = "active",
                  limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """按关键词检索知识，只返回编号、类型、摘要、路径与得分，需要正文时用 kb_get。

        types 取 defect-pattern、tradeoff、triage-lesson、fix-lesson、contract、reference、issue、finding、
        fix-report；tags 必须全部包含；status 取 active、superseded、archived、any；limit 为 1 到 50。
        """
        return _call(open_service, lambda service: commands.search(service, query, tuple(types or ()),
                                                                   tuple(tags or ()), status, limit))

    @server.tool()
    def kb_get(id: str) -> dict[str, Any]:
        """按编号读取一个条目或文档的 frontmatter 与正文。"""
        return _call(open_service, lambda service: commands.get(service, id))

    @server.tool()
    def kb_related(id: str) -> dict[str, Any]:
        """列出与该编号通过 related、supersededBy 关联的条目(含已取代与已归档的)，只返回摘要，需要正文时用 kb_get。"""
        return _call(open_service, lambda service: commands.related(service, id))

    @server.tool()
    def kb_stale() -> dict[str, Any]:
        """列出待复核的条目：过了复核日期的、长期未被命中的、可能相互矛盾的条目组；只返回摘要，需要正文时用 kb_get。"""
        return _call(open_service, commands.stale)

    return server


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tightrein kb mcp", description="以 stdio 方式运行知识检索的 MCP 服务")
    parser.add_argument("--workspace", type=Path, default=None, help="工作区绝对路径")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         clock: Clock | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    variables = dict(os.environ if environ is None else environ)
    workspace = commands.workspace_from(variables, parse_args(argv).workspace)
    chosen = clock or SystemClock()
    build_server(lambda: commands.open_service(workspace, variables, chosen)).run()


if __name__ == "__main__":
    main()
