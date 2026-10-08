from datetime import UTC, datetime

from tightrein.assess import claims, main_diff
from tightrein.protocol.git import GitError
from tightrein.protocol.git.git import Commit
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
BASE, HEAD = "abc1234", "c" * 40


def found() -> list[Occurrence]:
    frames = {"projectFrames": [{"file": "services/orders.py", "line": 3}]}
    return [Occurrence(problem="P-0001", seen_at=NOW, source="collect.platform_errors", id=1,
                       evidence={"signal": "S-1", "location": "OrderService.get", "message": "x", "evidence": frames})]


def claim(source: str = "collect.platform_errors", location: str = "OrderService.get") -> claims.Claim:
    problem = Problem(id="P-0001", fingerprint="fp", source=source, check_type="error", status="new", title="t",
                      first_seen=NOW, last_seen=NOW, location=location)
    return claims.build(problem, found())


def test_main_diff_lists_commits_touching_candidate_files(kit):
    files = main_diff.candidate_files(claim(), found())
    assert files == ("services/orders.py",)  # 符号与路由不算文件
    git = kit.FakeGit(None, commits=[Commit("d" * 40, "zhang", NOW, "修复订单查询")])
    diff = main_diff.collect(git, BASE, HEAD, files)
    assert git.calls == [("log", (f"{BASE}..{HEAD}", files))]
    assert diff.value()["commits"] == [{"commit": "d" * 40, "subject": "修复订单查询"}]
    unchanged = main_diff.collect(kit.FakeGit(None), BASE, HEAD, files)
    assert "期间候选文件未修改" in unchanged.value()
    assert main_diff.collect(kit.FakeGit(None), None, HEAD, files).value() == "发现问题时的版本未知，未比较"
    assert main_diff.collect(kit.FakeGit(None), BASE, HEAD, ()).value() == "没有可定位的候选文件，未比较"
    failed = main_diff.collect(kit.FakeGit(None, error=GitError("bad revision")), BASE, HEAD, files)
    assert failed.value().startswith("查询提交记录失败")  # 查询失败只写原因，不影响评估


def test_file_of_keeps_only_paths_with_a_directory_and_extension():
    assert main_diff.file_of("services/orders.py:3") == "services/orders.py"
    assert main_diff.file_of("GET /api/orders") is None
    assert main_diff.file_of("/orders") is None
    assert main_diff.file_of("OrderService.get") is None
    assert main_diff.file_of("Makefile:3") is None
    assert main_diff.file_of(None) is None
