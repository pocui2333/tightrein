import json
import subprocess
import sys

import anyio
import pytest
from mcp import Client, MCPError
from mcp.types import INTERNAL_ERROR

from tightrein.cli.kb_mcp import build_server
from tightrein.retrieval import commands
from tightrein.retrieval.errors import IndexUnavailable

PROTOCOL_VERSION = "2025-06-18"
TOOLS = ["kb_get", "kb_related", "kb_search", "kb_stale"]


def seed(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤", tags=("path:src/",), related=("TO-0001",))
    world.entry("TO-0001", "soft-delete", "软删除", "软删除是取舍")


def opener(world):
    return lambda: commands.open_service(world.layout.root, {}, world.clock)


def call(server, name, arguments):
    """调用一个工具；整个调用失败时返回 MCPError 而不是抛出，便于断言。"""

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            try:
                return await client.call_tool(name, arguments)
            except MCPError as error:
                return error

    return anyio.run(scenario)


def test_four_tools_say_they_only_return_summaries(world):
    async def scenario():
        async with Client(build_server(opener(world)), raise_exceptions=True) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(scenario)}
    assert sorted(tools) == TOOLS
    for name in ("kb_search", "kb_related", "kb_stale"):
        assert "需要正文时用 kb_get" in tools[name].description
    schema = tools["kb_search"].input_schema
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "types", "tags", "status", "limit"}


def test_tool_results_equal_the_command_json(world):
    seed(world)
    server = build_server(opener(world))
    service = opener(world)()
    expected = {
        "kb_search": commands.search(service, "公司过滤", ("defect-pattern",), (), "any", 5),
        "kb_get": commands.get(service, "DP-0001"),
        "kb_related": commands.related(service, "TO-0001"),
        "kb_stale": commands.stale(service),
    }
    service.conn.close()
    arguments = {"kb_search": {"query": "公司过滤", "types": ["defect-pattern"], "status": "any", "limit": 5},
                 "kb_get": {"id": "DP-0001"}, "kb_related": {"id": "TO-0001"}, "kb_stale": {}}
    for name in TOOLS:
        result = call(server, name, arguments[name])
        assert result.is_error is False
        assert result.structured_content == expected[name]


@pytest.mark.parametrize("name, arguments, message", [
    ("kb_search", {"query": "***"}, "查询串中没有可检索的文字"),
    ("kb_search", {"query": "公司", "status": "done"}, "status 只能是"),
    ("kb_search", {"query": "公司", "limit": 99}, "limit 须在 1 到 50 之间"),
    ("kb_get", {"id": "DP-0099"}, "没有编号为 DP-0099 的条目"),
])
def test_fixable_errors_are_tool_errors(world, name, arguments, message):
    seed(world)
    result = call(build_server(opener(world)), name, arguments)
    assert result.is_error is True and result.structured_content is None
    assert message in result.content[0].text


def test_an_unavailable_index_fails_the_whole_call():
    def unavailable():
        raise IndexUnavailable("数据库不存在，先执行 tightrein init")

    error = call(build_server(unavailable), "kb_stale", {})
    assert isinstance(error, MCPError)
    assert (error.error.code, error.error.message) == (INTERNAL_ERROR, "数据库不存在，先执行 tightrein init")


def test_stdio_mode_writes_only_protocol_messages(world):
    seed(world)
    process = subprocess.Popen([sys.executable, "-m", "tightrein.cli.kb_mcp", "--workspace", str(world.layout.root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8")

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}})
        initialized = json.loads(process.stdout.readline())
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "kb_search", "arguments": {"query": "公司过滤"}}})
        answered = json.loads(process.stdout.readline())
        process.stdin.close()
        rest = process.stdout.read()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()
    assert initialized["result"]["serverInfo"]["name"] == "tightrein-kb"
    assert [hit["id"] for hit in answered["result"]["structuredContent"]["hits"]] == ["DP-0001"]
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in rest.splitlines() if line.strip())
