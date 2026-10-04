import json
import os
import time
from datetime import datetime, timezone

import pytest
from extension_world import PYTHON, SCRIPT, STACK

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionMode, ExtensionPoint
from tightrein.extensions import defaults
from tightrein.extensions.invoke import (
    ENV_CACHE_DIR,
    ENV_POINT,
    ENV_PROTOCOL,
    OVERFLOW_MESSAGE,
    Invoker,
    ProcessRequest,
    SubprocessRunner,
)
from tightrein.extensions.resolve import Implementation, default
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import REDACTED, Redactor
from tightrein.observability.tracing import Tracer
from tightrein.store.files.layout import WorkspaceLayout

RUN = "R-20260929-021503-collect-static"
COMMIT = "d6f37025"
ROUTES = {"routes": [{"path": "/orders", "name": "orders", "componentFile": "src/views/Orders.vue", "meta": None}],
          "sourceFiles": ["src/router/index.js"]}
TOOLS = {"tools": [{"name": "build", "status": "ok", "exitCode": 0, "logFile": "build.log", "reason": None}],
         "findings": []}
EXTRA_TOOL = {"name": "audit", "status": "skipped", "exitCode": None, "logFile": None, "reason": "前端没有改动"}
STATIC_INPUT = {"level": "full", "baseCommit": None, "changedFiles": [], "rawDir": "/w/raw/static"}
ENVIRON = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/Users/cty",
           "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123"}


def project(world, point, options, *, timeout=30, mode=ExtensionMode.REPLACE, base=None):
    directory = world.project_extension()
    return Implementation(point, ExtensionLayer.PROJECT, timeout, command=("{python}", SCRIPT),
                          argv=(PYTHON, str(directory / SCRIPT)), cwd=directory, options=options,
                          cache_name="demo", mode=mode, base=base)


def stack(world, point, options, env=None):
    directory = world.stack({point.value: {"command": ["{python}", SCRIPT]}}, env=env)
    return Implementation(point, ExtensionLayer.STACK, 30, command=("{python}", SCRIPT),
                          argv=(PYTHON, str(directory / SCRIPT)), cwd=directory, options=options,
                          env=env or {}, cache_name=STACK, version="1.0.0")


def invoker(world, layout=None, **options):
    return Invoker(layout or world.workspace, world.user, run_id=RUN, environ=ENVIRON,
                   scratch_root=world.root, **options)


def routes(world, implementation, invoke=None):
    return (invoke or invoker(world)).call(implementation, repo=world.root / "repo", commit=COMMIT, input={})


def test_a_successful_call_gets_the_request_environment_and_working_directory(world):
    record = world.root / "record.json"
    implementation = project(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES, "notes": ["1 个路由无法解析"],
                                                                 "record": str(record)})
    result = routes(world, implementation)
    assert (result.implementation, result.output, result.failure, result.notes, result.cached) == (
        ExtensionLayer.PROJECT, ROUTES, None, ("1 个路由无法解析",), False)
    seen = json.loads(record.read_text(encoding="utf-8"))
    request = seen["request"]
    assert {key: request[key] for key in ("protocol", "point", "workspace", "repo", "commit", "base", "input")} == {
        "protocol": 1, "point": "page-routes", "workspace": str(world.workspace.root),
        "repo": str(world.root / "repo"), "commit": COMMIT, "base": None, "input": {},
    }
    assert request["scratchDir"].startswith(str(world.root))
    assert not os.path.exists(request["scratchDir"])
    assert seen["cwd"] == os.path.realpath(world.workspace.extensions_dir())
    env = seen["env"]
    assert (env[ENV_POINT], env[ENV_PROTOCOL], env[ENV_CACHE_DIR]) == (
        "page-routes", "1", str(world.user.extension_cache("demo")))
    assert world.user.extension_cache("demo").is_dir()
    assert "GH_TOKEN" not in env and env["HOME"] == "/Users/cty"


def test_tool_commands_given_by_the_core_reach_the_extension(world):
    record = world.root / "record.json"
    implementation = project(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES, "record": str(record)})
    found = invoker(world, tools={"TIGHTREIN_SEMGREP": "/opt/tightrein/local/semgrep/bin/semgrep"}).call(
        implementation, repo=world.root / "repo", commit=COMMIT, input={})
    assert found.failure is None
    env = json.loads(record.read_text(encoding="utf-8"))["env"]
    assert env["TIGHTREIN_SEMGREP"] == "/opt/tightrein/local/semgrep/bin/semgrep"


def test_stack_variables_are_added_and_the_cache_is_per_stack(world):
    record = world.root / "record.json"
    implementation = stack(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES, "record": str(record)},
                           env={"DOTNET_NOLOGO": "1"})
    assert routes(world, implementation).implementation is ExtensionLayer.STACK
    env = json.loads(record.read_text(encoding="utf-8"))["env"]
    assert env["DOTNET_NOLOGO"] == "1"
    assert env[ENV_CACHE_DIR] == str(world.user.extension_cache(STACK))


def test_error_responses_are_taken_even_with_a_nonzero_exit(world):
    options = {"behavior": "error", "code": "tool-missing", "message": "未找到 dotnet", "hint": "安装 .NET SDK 8",
               "notes": ["检查过 PATH"], "exitCode": 2}
    result = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, options))
    assert result.failed and result.output is None
    assert (result.failure.code, result.failure.message, result.failure.hint) == (
        ExtensionErrorCode.TOOL_MISSING, "未找到 dotnet", "安装 .NET SDK 8")
    assert result.notes == ("检查过 PATH",)


def test_not_applicable_falls_back_to_the_default(world):
    options = {"behavior": "error", "code": "not-applicable", "message": "仓库中没有路由文件"}
    result = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, options))
    assert (result.implementation, result.output, result.failure) == (ExtensionLayer.DEFAULT, None, None)
    assert result.notes == (defaults.DEFAULT_NOTES[ExtensionPoint.PAGE_ROUTES], "项目扩展报告不适用：仓库中没有路由文件")


def test_a_crash_keeps_the_last_fifty_lines_of_stderr(world):
    result = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"behavior": "crash"}))
    assert result.failure.code is ExtensionErrorCode.CRASHED
    assert result.failure.message == "退出码 3，标准输出没有合法的响应"
    assert result.failure.details == tuple(f"trace line {number}" for number in range(10, 60))
    saved = world.workspace.extension_stderr(RUN, ExtensionPoint.PAGE_ROUTES).read_text(encoding="utf-8")
    assert saved.startswith("trace line 0\n")


@pytest.mark.parametrize("options, message", [
    ({"behavior": "garbage"}, "标准输出不是单个 JSON 对象"),
    ({"output": ROUTES, "protocol": 2}, "响应的 protocol 为 2，请求为 1"),
])
def test_protocol_errors(world, options, message):
    result = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, options))
    assert (result.failure.code, result.failure.message) == (ExtensionErrorCode.PROTOCOL_ERROR, message)


def test_output_over_the_limit_is_a_protocol_error(world):
    implementation = project(world, ExtensionPoint.PAGE_ROUTES, {"behavior": "flood", "bytes": 5000})
    result = routes(world, implementation, invoker(world, runner=SubprocessRunner(max_stdout_bytes=1024)))
    assert (result.failure.code, result.failure.message) == (ExtensionErrorCode.PROTOCOL_ERROR, OVERFLOW_MESSAGE)


def test_timeout_terminates_the_whole_process_group(world):
    child_file = world.root / "child.pid"
    options = {"behavior": "sleep", "seconds": 30, "childPidFile": str(child_file)}
    implementation = project(world, ExtensionPoint.PAGE_ROUTES, options, timeout=1)
    started = time.monotonic()
    result = routes(world, implementation, invoker(world, runner=SubprocessRunner(kill_grace_seconds=1)))
    assert time.monotonic() - started < 10
    assert (result.failure.code, result.failure.message) == (
        ExtensionErrorCode.TIMEOUT, "超过 1 秒未结束，进程组已终止")
    child = int(child_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"子进程 {child} 仍在运行")


def test_schema_violations_list_every_path(world):
    broken = {"routes": [{"path": "orders", "name": None, "componentFile": None, "meta": None}]}
    result = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"output": broken}))
    assert (result.failure.code, result.failure.message) == (
        ExtensionErrorCode.SCHEMA_INVALID, "output 不符合该扩展点的 schema")
    assert result.failure.details == (
        "$.output: 'sourceFiles' is a required property",
        "$.output.routes[0].path: 'orders' does not match '^/'",
    )
    envelope = routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"behavior": "error", "code": "timeout",
                                                                        "message": "x"}))
    assert (envelope.failure.code, envelope.failure.message) == (
        ExtensionErrorCode.SCHEMA_INVALID, "响应不符合外层 schema")


def test_unstartable_commands_are_reported_as_crashed(world):
    implementation = Implementation(ExtensionPoint.PAGE_ROUTES, ExtensionLayer.PROJECT, 30,
                                    command=("page-routes-tool",), argv=(str(world.root / "missing-tool"),),
                                    cwd=world.root, cache_name="demo")
    result = routes(world, implementation)
    assert result.failure.code is ExtensionErrorCode.CRASHED
    assert result.failure.message.startswith("无法启动扩展：FileNotFoundError")


def test_stderr_is_redacted_and_numbered_per_call(world):
    call = invoker(world)
    options = {"output": ROUTES, "stderr": "login password=hunter2\n"}
    implementation = project(world, ExtensionPoint.PAGE_ROUTES, options)
    routes(world, implementation, call)
    routes(world, implementation, call)
    first = world.workspace.extension_stderr(RUN, ExtensionPoint.PAGE_ROUTES)
    second = world.workspace.extension_stderr(RUN, ExtensionPoint.PAGE_ROUTES, 2)
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8") == f"login password={REDACTED}\n"
    quiet = project(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES})
    routes(world, quiet, invoker(world))
    assert not world.workspace.extension_stderr(RUN, ExtensionPoint.PAGE_ROUTES, 3).exists()


def test_output_mode_keeps_request_and_response_copies(world, tmp_path):
    layout = WorkspaceLayout(world.workspace.root, output_dir=tmp_path / "out")
    routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES}), invoker(world, layout))
    request = json.loads((tmp_path / "out" / "extensions" / "page-routes.request.json").read_text(encoding="utf-8"))
    response = json.loads((tmp_path / "out" / "extensions" / "page-routes.response.json").read_text(encoding="utf-8"))
    assert request["point"] == "page-routes"
    assert response == {"protocol": 1, "status": "ok", "output": ROUTES}


def test_extend_gives_the_lower_output_as_base_and_merges_notes(world):
    options = {"output": TOOLS, "notes": ["技术栈工具已运行"], "extendNotes": ["追加了前端工具"], "key": "tools",
               "append": [EXTRA_TOOL]}
    base = stack(world, ExtensionPoint.STATIC_TOOLS, options)
    implementation = project(world, ExtensionPoint.STATIC_TOOLS, options, mode=ExtensionMode.EXTEND, base=base)
    result = invoker(world).call(implementation, repo=world.root, commit=COMMIT, input=STATIC_INPUT)
    assert result.implementation is ExtensionLayer.PROJECT
    assert result.output == {"tools": [*TOOLS["tools"], EXTRA_TOOL], "findings": []}
    assert result.notes == ("技术栈工具已运行", "追加了前端工具")


def test_extend_stops_when_the_lower_layer_fails(world):
    record = world.root / "record.json"
    options = {"behavior": "error", "code": "build-failed", "message": "构建失败", "record": str(record)}
    base = stack(world, ExtensionPoint.STATIC_TOOLS, options)
    implementation = project(world, ExtensionPoint.STATIC_TOOLS, options, mode=ExtensionMode.EXTEND, base=base)
    result = invoker(world).call(implementation, repo=world.root, commit=COMMIT, input=STATIC_INPUT)
    assert (result.implementation, result.failure.code) == (ExtensionLayer.STACK, ExtensionErrorCode.BUILD_FAILED)
    assert json.loads(record.read_text(encoding="utf-8"))["request"]["base"] is None


def test_the_default_implementation_starts_nothing(world):
    result = invoker(world).call(default(ExtensionPoint.LOG_PLATFORM), repo=None, commit=None, input={})
    assert result == defaults.result(ExtensionPoint.LOG_PLATFORM)


def test_bad_input_from_the_caller_is_rejected_before_starting(world):
    record = world.root / "record.json"
    implementation = project(world, ExtensionPoint.STATIC_TOOLS, {"output": TOOLS, "record": str(record)})
    with pytest.raises(SchemaValidationError):
        invoker(world).call(implementation, repo=world.root, commit=COMMIT, input={"level": "full"})
    assert not record.exists()


def test_each_process_writes_a_run_script_span(world):
    clock = FixedClock(datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc))
    tracer = Tracer(EventLog(world.workspace, Redactor()), clock, run_id=RUN, stage="collect")
    call = invoker(world, tracer=tracer)
    routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"output": ROUTES}), call)
    routes(world, project(world, ExtensionPoint.PAGE_ROUTES, {"behavior": "error", "code": "parse-failed",
                                                              "message": "无法解析"}), call)
    written = events.read(world.workspace.events_log(clock.now().date()))
    assert [event.operation for event in written] == ["run_script", "run_script"]
    ok, failed = (dict(event.attributes) for event in written)
    assert {key: ok[key] for key in ("point", "implementation", "command", "exitCode", "status", "errorCode",
                                     "cached")} == {
        "point": "page-routes", "implementation": "project", "command": "{python} fake_extension.py", "exitCode": 0,
        "status": "ok", "errorCode": None, "cached": False,
    }
    assert isinstance(ok["durationMs"], int)
    assert (failed["status"], failed["errorCode"], written[1].status) == ("error", "parse-failed", "error")


def test_an_interrupt_while_waiting_terminates_the_process_group(tmp_path):
    child_file = tmp_path / "child.pid"
    code = ("import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n")
    calls = []

    def monotonic():
        calls.append(1)
        if len(calls) > 1 and child_file.exists() and child_file.read_text(encoding="utf-8"):
            raise KeyboardInterrupt
        return time.monotonic()

    runner = SubprocessRunner(kill_grace_seconds=0.3, monotonic=monotonic)
    request = ProcessRequest((PYTHON, "-c", code), tmp_path, {"PATH": os.environ.get("PATH", "")}, b"", 30)
    with pytest.raises(KeyboardInterrupt):
        runner(request)
    child = int(child_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"子进程 {child} 仍在运行")
