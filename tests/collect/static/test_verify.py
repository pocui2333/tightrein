import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.static import verify
from tightrein.collect.static.claims import Claim
from tightrein.collect.static.verify import Verification

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


def make(file, line):
    return Claim(file, line, "r1", "incremental", "high", "会崩溃", "x 为空时")


def answering(statuses):
    """statuses[(file, line)] 为该主张取证的状态；记下每条在哪个线程、第几号。"""
    seen = []
    lock = threading.Lock()

    def verify_one(claim, number):
        with lock:
            seen.append((claim.file, claim.line, number, threading.get_ident()))
        status = statuses.get((claim.file, claim.line), CallStatus.OK)
        output = {"verdict": "confirmed"} if status is CallStatus.OK else None
        return Verification(claim, CallResult(status, "claude", "opus", output=output,
                                              violations=["read_only_changed：x"] if status is CallStatus.BOUNDARY
                                              else []), NOW)

    return verify_one, seen


def test_claims_of_one_file_run_in_order_and_files_run_in_parallel():
    claims = [make("a.py", 1), make("b.py", 1), make("a.py", 2)]
    verify_one, seen = answering({})
    result = verify.run(claims, verify_one, workers=4)
    assert sorted(item.claim.line for item in result.done) == [1, 1, 2] and not result.degraded
    numbers = {(file, line): number for file, line, number, _ in seen}
    assert numbers == {("a.py", 1): 1, ("b.py", 1): 2, ("a.py", 2): 3}
    order = [(file, line) for file, line, _, _ in seen if file == "a.py"]
    assert order == [("a.py", 1), ("a.py", 2)] and len(result.calls) == 3


def test_an_exhausted_budget_leaves_the_rest_for_next_time():
    claims = [make("a.py", 1), make("a.py", 2), make("a.py", 3)]
    verify_one, _ = answering({("a.py", 1): CallStatus.BUDGET_LIMIT})
    result = verify.run(claims, verify_one, workers=1)
    assert result.done == [] and [item.line for item in result.not_started] == [1, 2, 3]
    assert result.degraded and len(result.notes) == 1


def test_an_environment_violation_stops_everything_and_other_failures_drop_one_claim():
    verify_one, _ = answering({("a.py", 1): CallStatus.BOUNDARY})
    assert verify.run([make("a.py", 1), make("a.py", 2)], verify_one, workers=1).violated
    verify_one, _ = answering({("a.py", 1): CallStatus.SCHEMA_INVALID})
    result = verify.run([make("a.py", 1), make("a.py", 2)], verify_one, workers=1)
    assert not result.violated and result.degraded and [item.claim.line for item in result.done] == [2]
    assert "a.py:1 的取证未完成(schema_invalid)" in result.notes[0]


@contextmanager
def same(runtime):
    yield runtime


def test_the_verifier_gives_the_claim_and_the_cut_code(static_runtime, fake_caller, make_verdict, repos):
    runtime = static_runtime()
    caller = fake_caller({"collect.static.verify": make_verdict()})
    verify_one = verify.verifier(runtime, workdir=repos.repo, caller=caller, isolate=same)
    found = verify_one(make("src/orders.py", 3), 7)
    params = caller.calls[0]
    assert found.verdict == "confirmed" and params.round == 7 and params.workdir == repos.repo
    assert "`src/orders.py:3` 存在以下问题(r1，严重度 high)" in params.prompt
    assert "    3      rows = query(page * size)" in params.prompt and "delete_order" not in params.prompt


def test_each_verification_calls_the_model_with_its_own_threads_connection(static_runtime, fake_caller, make_verdict,
                                                                          repos):
    """并行取证在线程池的线程里调用模型：连接(与熔断、额度、用量计数)在那个线程里另开，不用别的线程的。"""
    runtime = static_runtime()
    opened = []
    lock = threading.Lock()

    @contextmanager
    def isolate(given):
        local = replace(given, agents=SimpleNamespace(thread=threading.get_ident()))
        with lock:
            opened.append(local.agents.thread)
        yield local

    seen = []

    def answer(params):
        return make_verdict()

    caller = fake_caller({"collect.static.verify": answer})

    def recording(params, context):
        seen.append((threading.get_ident(), context.thread))
        return caller(params, context)

    verify_one = verify.verifier(runtime, workdir=repos.repo, caller=recording, isolate=isolate)
    result = verify.run([make("src/orders.py", 3), make("src/users.py", 1)], verify_one, workers=2)
    assert len(result.done) == 2 and len(opened) == 2
    assert all(thread == used for thread, used in seen)  # 调用模型的线程就是开连接的线程
    assert threading.get_ident() not in opened
