import os
import sys
import textwrap
import time

import pytest

from tightrein.runner.process import Invocation, SubprocessLauncher

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def script(tmp_path, code):
    path = tmp_path / "fake_agent.py"
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return Invocation((sys.executable, str(path)), tmp_path, ENV)


def launcher():
    return SubprocessLauncher(interrupt_grace=0.3, terminate_grace=0.3)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_lines_are_read_in_order(tmp_path):
    invocation = script(tmp_path, """
        import sys
        for number in range(3):
            print(f'{{"n": {number}}}', flush=True)
        print("出错信息", file=sys.stderr)
        sys.exit(3)
    """)
    lines = []
    outcome = launcher().run(invocation, lambda line: lines.append(line), None)
    assert lines == ['{"n": 0}', '{"n": 1}', '{"n": 2}']
    assert (outcome.exit_code, outcome.stderr_tail, outcome.stopped_by) == (3, "出错信息", None)


def test_stdin_is_passed_to_the_process(tmp_path):
    invocation = script(tmp_path, """
        import sys
        print(sys.stdin.read().upper(), flush=True)
    """)
    lines = []
    launcher().run(Invocation(invocation.argv, invocation.cwd, invocation.env, "提示 abc".encode()), lines.append, None)
    assert lines == ["提示 ABC"]


def test_timeout_interrupts_the_process(tmp_path):
    invocation = script(tmp_path, """
        import time
        print("started", flush=True)
        time.sleep(30)
    """)
    started = time.monotonic()
    outcome = launcher().run(invocation, lambda line: None, 300)
    assert outcome.stopped_by == "timeout"
    assert outcome.exit_code != 0
    assert time.monotonic() - started < 5


def test_processes_that_ignore_signals_are_killed(tmp_path):
    invocation = script(tmp_path, """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("stubborn", flush=True)
        time.sleep(30)
    """)
    outcome = launcher().run(invocation, lambda line: None, 200)
    assert (outcome.stopped_by, outcome.exit_code) == ("timeout", -9)


def test_the_whole_process_group_is_terminated(tmp_path):
    invocation = script(tmp_path, """
        import subprocess, sys, time
        code = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)"
        child = subprocess.Popen([sys.executable, "-c", code])
        print(child.pid, flush=True)
        time.sleep(30)
    """)
    pids = []
    outcome = launcher().run(invocation, lambda line: pids.append(int(line)), 500)
    assert outcome.stopped_by == "timeout"
    time.sleep(0.2)
    assert not alive(pids[0])


def test_an_interrupt_terminates_the_process_group_and_is_raised(tmp_path):
    invocation = script(tmp_path, """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        print(child.pid, flush=True)
        time.sleep(30)
    """)
    pids = []

    def handler(line):
        pids.append(int(line))
        raise KeyboardInterrupt

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        launcher().run(invocation, handler, None)
    assert time.monotonic() - started < 5
    time.sleep(0.2)
    assert not alive(pids[0])


def test_the_line_handler_can_stop_the_process(tmp_path):
    invocation = script(tmp_path, """
        import time
        for number in range(100):
            print(number, flush=True)
            time.sleep(0.05)
    """)
    seen = []

    def handler(line):
        seen.append(line)
        return "turn-limit" if len(seen) == 3 else None

    outcome = launcher().run(invocation, handler, None)
    assert outcome.stopped_by == "turn-limit"
    assert seen == ["0", "1", "2"]


def test_missing_programs_raise(tmp_path):
    with pytest.raises(FileNotFoundError):
        launcher().run(Invocation(("no-such-agent-tool",), tmp_path, ENV), lambda line: None, None)


def test_interactive_runs_return_the_exit_code(tmp_path):
    invocation = script(tmp_path, "import sys; sys.exit(7)\n")
    assert launcher().run_interactive(invocation) == 7
