import os
import signal
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tightrein.protocol.process import OVERFLOW_HINT, Command, Interrupted, SubprocessRunner

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def runner(**options):
    return SubprocessRunner(interrupt_grace_s=0.3, terminate_grace_s=0.3, **options)


def script(tmp_path: Path, code: str, **options) -> Command:
    path = tmp_path / "fake_tool.py"
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return Command((sys.executable, str(path)), tmp_path, ENV, **options)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_lines_are_read_in_order(tmp_path):
    lines = []
    command = script(tmp_path, """
        import sys
        for number in range(3):
            print(f'{{"n": {number}}}', flush=True)
        print("出错信息", file=sys.stderr)
        sys.exit(3)
    """, on_line=lambda line: lines.append(line))
    outcome = runner().run(command)
    assert lines == ['{"n": 0}', '{"n": 1}', '{"n": 2}']
    assert outcome.stdout == '{"n": 0}\n{"n": 1}\n{"n": 2}\n'
    assert (outcome.exit_code, outcome.stderr_tail, outcome.stopped_by, outcome.start_error) == (3, "出错信息", None, None)


def test_stdin_is_passed_to_the_process(tmp_path):
    command = script(tmp_path, """
        import sys
        print(sys.stdin.read().upper(), flush=True)
    """, stdin="提示 abc")
    assert runner().run(command).stdout == "提示 ABC\n"


def test_a_process_that_does_not_read_stdin_is_tolerated(tmp_path):
    command = Command((sys.executable, "-c", "pass"), tmp_path, ENV, stdin=b"x" * 1_000_000)
    assert runner().run(command).exit_code == 0


def test_the_process_does_not_inherit_the_terminal(tmp_path):
    outcome = runner().run(Command(("sh", "-c", "cat; tty || true"), tmp_path, {"PATH": "/usr/bin:/bin"}))
    assert outcome.exit_code == 0 and outcome.stdout.strip() == "not a tty"


def test_arguments_are_not_interpreted_by_a_shell(tmp_path):
    outcome = runner().run(Command(("echo", "a b; echo injected", "$HOME"), tmp_path, ENV))
    assert outcome.stdout == "a b; echo injected $HOME\n"


def test_timeout_interrupts_the_process(tmp_path):
    command = script(tmp_path, """
        import time
        print("started", flush=True)
        time.sleep(30)
    """, timeout_s=3)  # 留够子进程启动的时间：整批测试并行时 Python 启动可能超过 1s，否则来不及打印就被终止
    started = time.monotonic()
    outcome = runner().run(command)
    assert (outcome.stopped_by, outcome.exit_code) == ("timeout", None)
    assert outcome.stdout == "started\n"
    assert time.monotonic() - started < 15  # 远小于子进程的 30s：确实是超时终止的


def test_processes_that_ignore_signals_are_killed(tmp_path):
    command = script(tmp_path, """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("stubborn", flush=True)
        time.sleep(30)
    """, timeout_s=0.2)
    started = time.monotonic()
    outcome = runner().run(command)
    assert (outcome.stopped_by, outcome.exit_code) == ("timeout", None)
    assert time.monotonic() - started < 5


def test_the_whole_process_group_is_terminated(tmp_path):
    pids = []
    command = script(tmp_path, """
        import subprocess, sys, time
        code = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)"
        child = subprocess.Popen([sys.executable, "-c", code])
        print(child.pid, flush=True)
        time.sleep(30)
    """, timeout_s=0.5, on_line=lambda line: pids.append(int(line)))
    assert runner().run(command).stopped_by == "timeout"
    assert gone_soon(pids[0])


def test_grandchildren_holding_the_pipe_do_not_block_the_result(tmp_path):
    # 孙进程继承了输出管道；只等主进程退出时，读取输出会一直等到孙进程退出
    started = time.monotonic()
    outcome = runner().run(Command(("sh", "-c", "sleep 30 & echo done"), tmp_path, {"PATH": "/usr/bin:/bin"}))
    assert (outcome.exit_code, outcome.stdout) == (0, "done\n")
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, Interrupted])
def test_an_interrupt_terminates_the_process_group_and_is_raised(tmp_path, interrupt):
    pids = []

    def handler(line):
        pids.append(int(line))
        raise interrupt

    command = script(tmp_path, """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        print(child.pid, flush=True)
        time.sleep(30)
    """, on_line=handler)
    started = time.monotonic()
    with pytest.raises(interrupt):
        runner().run(command)
    assert time.monotonic() - started < 5
    assert gone_soon(pids[0])


def test_an_interrupt_while_waiting_terminates_the_process_group(tmp_path):
    child_file = tmp_path / "child.pid"
    code = ("import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n")
    calls = []

    def monotonic():
        # 真实的 Ctrl-C 只在主线程抛出；读 stderr 的线程也取时钟(子进程被中断时会打印 traceback)，在那里抛会变成线程里的异常
        calls.append(1)
        if (threading.current_thread() is threading.main_thread() and len(calls) > 2 and child_file.exists()
                and child_file.read_text(encoding="utf-8")):
            raise KeyboardInterrupt
        return time.monotonic()

    with pytest.raises(KeyboardInterrupt):
        runner(monotonic=monotonic).run(Command((sys.executable, "-c", code), tmp_path, ENV, timeout_s=30))
    assert gone_soon(int(child_file.read_text(encoding="utf-8")))


def test_a_second_interrupt_during_the_grace_wait_kills_the_group_at_once(tmp_path):
    pids = []

    def interrupt_again(*_):
        raise KeyboardInterrupt

    def handler(line):
        pids.append(int(line))
        signal.setitimer(signal.ITIMER_REAL, 0.3)  # 宽限等待(30s)中再被打断
        raise KeyboardInterrupt

    command = script(tmp_path, """
        import os, signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(os.getpid(), flush=True)
        time.sleep(60)
    """, on_line=handler)
    previous = signal.signal(signal.SIGALRM, interrupt_again)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            SubprocessRunner(interrupt_grace_s=30, terminate_grace_s=30).run(command)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 10
    assert gone_soon(pids[0])


def test_the_line_handler_can_stop_the_process(tmp_path):
    seen = []

    def handler(line):
        seen.append(line)
        return "turn_limit" if len(seen) == 3 else None

    command = script(tmp_path, """
        import time
        for number in range(100):
            print(number, flush=True)
            time.sleep(0.05)
    """, on_line=handler)
    outcome = runner().run(command)
    assert (outcome.stopped_by, outcome.exit_code) == ("turn_limit", None)
    assert seen == ["0", "1", "2"]


def test_no_output_for_too_long_is_idle(tmp_path):
    command = script(tmp_path, """
        import time
        print("thinking", flush=True)
        time.sleep(30)
    """, idle_s=0.3, timeout_s=20)
    assert runner().run(command).stopped_by == "idle"


def test_output_over_the_limit_is_an_overflow(tmp_path):
    command = script(tmp_path, """
        import sys
        sys.stdout.write("x" * 5000)
        sys.stdout.flush()
        import time; time.sleep(30)
    """, max_stdout=1024)
    outcome = runner().run(command)
    assert (outcome.stopped_by, outcome.exit_code) == ("overflow", None)
    assert len(outcome.stdout) <= 1024
    # 提示大结果写文件、以路径引用
    assert outcome.stderr_tail.splitlines()[-1] == OVERFLOW_HINT.format(limit=1024)


def test_stderr_keeps_only_the_tail(tmp_path):
    command = script(tmp_path, """
        import sys
        for number in range(60):
            print(f"trace line {number}", file=sys.stderr)
        sys.exit(3)
    """)
    outcome = runner().run(command)
    assert outcome.exit_code == 3
    assert outcome.stderr_tail.splitlines() == [f"trace line {number}" for number in range(10, 60)]


def test_stdout_can_be_written_to_a_file_line_by_line(tmp_path):
    target = tmp_path / "raw" / "stdout.jsonl"
    command = script(tmp_path, """
        for number in range(3):
            print(number, flush=True)
    """, stdout_path=target)
    outcome = runner().run(command)
    assert outcome.stdout == ""
    assert target.read_text(encoding="utf-8") == "0\n1\n2\n"


def test_missing_programs_are_a_start_error(tmp_path):
    outcome = runner().run(Command((str(tmp_path / "no-such-tool"),), tmp_path, ENV))
    assert outcome.exit_code is None and outcome.stopped_by is None
    assert outcome.start_error.startswith("FileNotFoundError")
