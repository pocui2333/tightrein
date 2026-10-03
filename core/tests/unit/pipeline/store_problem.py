"""在数据库中写入样例问题；与真实流程一样推进问题编号的序列。"""

from tightrein.domain.enums import Probe, ProblemStatus
from tightrein.domain.problem import Problem, ProblemScope
from tightrein.domain.ids import parse_sequence
from tightrein.store import sequences
from tightrein.store.repos import problems
from pipeline_world import NOW, RELEASE


def make_problem(problem_id="P-0001", fingerprint="a1b2c3d4e5f60718", **changes):
    values = dict(
        id=problem_id, fingerprint=fingerprint, fingerprint_version=1, probe=Probe.API_FUZZ,
        title="GET /api/Order/{id} not_a_server_error 500", status=ProblemStatus.NEW, first_seen_at=NOW,
        last_seen_at=NOW, scope=ProblemScope("GET /api/Order/{id}", frozenset({"Admin"})),
        first_seen_release=RELEASE, last_seen_release=RELEASE,
    )
    values.update(changes)
    return Problem(**values)


def save_problem(conn, problem_id="P-0001", fingerprint="a1b2c3d4e5f60718", **changes):
    problem = make_problem(problem_id, fingerprint, **changes)
    problems.save(conn, problem)
    sequences.ensure_at_least(conn, sequences.PROBLEM, parse_sequence(problem_id))
    return problem
