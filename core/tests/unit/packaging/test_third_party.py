import json
import shutil
from datetime import date

import pytest
from packaging_world import (
    COMMIT_A,
    COMMIT_B,
    FACTS,
    REVIEW_DIR,
    SOURCE,
    Commands,
    Downloads,
    archive,
    make_world,
    skill_text,
    upstream,
)

from tightrein.packaging import third_party
from tightrein.packaging.third_party import LockedSkill
from tightrein.pipeline.learn.steps.third_party import RepoFacts
from tightrein.sources.common.http import HttpResponse
from tightrein.store.files.layout import ToolLayout
from tightrein.vcs.process import Completed, VcsProcess


def write_files(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def lock(world, skills=None, **changes):
    ctx = world.context()
    values = dict(ref=None, head=lambda source: COMMIT_A, facts=lambda source: FACTS, fetch=ctx.fetch,
                  cache=ctx.cache, config=ctx.config, clock=ctx.clock)
    values.update(changes)
    return third_party.lock(ctx.lock() if skills is None else skills, set(), **values)


def test_the_tree_hash_ignores_order_permissions_and_git(tmp_path):
    files = {"SKILL.md": "a\n", "references/x.md": "b\n"}
    write_files(tmp_path / "one", files)
    write_files(tmp_path / "two", dict(reversed(list(files.items()))))
    write_files(tmp_path / "two", {".git/HEAD": "ref\n"})
    (tmp_path / "two" / "SKILL.md").chmod(0o600)
    one, two = third_party.file_hashes(tmp_path / "one"), third_party.file_hashes(tmp_path / "two")
    assert one == two
    assert [item.path for item in one] == ["SKILL.md", "references/x.md"]
    assert third_party.tree_hash(one).startswith("sha256:")


def test_one_changed_byte_fails_verification(tmp_path):
    write_files(tmp_path / "skill", {"SKILL.md": "a\n"})
    files = third_party.file_hashes(tmp_path / "skill")
    skill = LockedSkill("demo", SOURCE, COMMIT_A, "demo", None, third_party.tree_hash(files), files)
    assert third_party.verify(skill, tmp_path / "skill") == []
    (tmp_path / "skill" / "SKILL.md").write_text("b\n", encoding="utf-8")
    (issue,) = third_party.verify(skill, tmp_path / "skill")
    assert (issue.path, issue.expected) == ("SKILL.md", files[0].sha256)
    assert issue.actual != issue.expected
    assert third_party.verify(skill, tmp_path / "gone")[0].actual is None


def test_extract_finds_the_skill_by_name_and_only_takes_its_directory(tmp_path):
    path = third_party.extract(upstream(), "differential-review", None, tmp_path / "cache")
    assert path == REVIEW_DIR
    assert sorted(item.path for item in third_party.file_hashes(tmp_path / "cache")) == [
        "SKILL.md", "references/methodology.md"]
    assert not (tmp_path / "cache.partial").exists()


def test_extract_rejects_paths_outside_the_skill_directory(tmp_path):
    escaping = archive({"skill/SKILL.md": skill_text("demo"), "skill/../escape.md": "x\n"})
    with pytest.raises(ValueError):
        third_party.extract(escaping, "demo", "skill", tmp_path / "cache")
    assert sorted(path.name for path in tmp_path.iterdir()) == []


def test_extract_needs_exactly_one_matching_skill(tmp_path):
    with pytest.raises(third_party.ThirdPartyError):
        third_party.extract(upstream(), "missing-skill", None, tmp_path / "cache")


def test_lock_records_commit_path_license_hashes_and_verification(tmp_path):
    world = make_world(tmp_path)
    locked, changes = lock(world)
    review = locked[0]
    assert (review.ref, review.path, review.license, review.locked_at) == (COMMIT_A, REVIEW_DIR, "CC-BY-SA-4.0",
                                                                           "2026-09-30")
    assert review.verification.to_dict() == {"verifiedAt": "2026-09-30", "stars": 7296, "lastCommit": "2026-09-28",
                                             "archived": False}
    assert review.tree_hash == third_party.tree_hash(review.files)
    assert [change.replaces for change in changes] == [False, False]
    third_party.write_lock(world.tool.third_party_lock(), locked)
    assert third_party.read_lock(world.tool.third_party_lock()) == locked


def test_lock_stops_when_the_source_misses_the_adoption_threshold(tmp_path):
    world = make_world(tmp_path)
    with pytest.raises(third_party.ThresholdError, match="星标数 100"):
        lock(world, facts=lambda source: RepoFacts(100, date(2026, 9, 28), False))
    assert world.downloads.urls == []


def test_relocking_to_another_commit_lists_the_file_changes(tmp_path):
    world = make_world(tmp_path)
    locked, _ = lock(world)
    same, unchanged = lock(world, locked)
    assert (same, unchanged) == (locked, [])
    _, changes = lock(world, locked, head=lambda source: COMMIT_B)
    review = changes[0]
    assert (review.replaces, review.old_ref, review.new_ref, review.changed) == (True, COMMIT_A, COMMIT_B,
                                                                                ("SKILL.md",))
    assert changes[1].changed == ()


def test_ensure_cached_downloads_once_and_never_overwrites_a_tampered_cache(tmp_path):
    world = make_world(tmp_path)
    locked, _ = lock(world)
    ctx = world.context()
    directory = ctx.cache("differential-review", COMMIT_A)
    for item in (directory, ctx.cache("variant-analysis", COMMIT_A)):
        shutil.rmtree(item)
    world.downloads.urls.clear()
    assert third_party.ensure_cached(locked[0], ctx.fetch, ctx.cache) == directory
    assert third_party.ensure_cached(locked[0], ctx.fetch, ctx.cache) == directory
    assert len(world.downloads.urls) == 1
    assert world.downloads.urls[0] == f"https://codeload.github.com/example/skills/tar.gz/{COMMIT_A}"
    (directory / "SKILL.md").write_text("改过\n", encoding="utf-8")
    with pytest.raises(third_party.HashMismatch):
        third_party.ensure_cached(locked[0], ctx.fetch, ctx.cache)
    assert len(world.downloads.urls) == 1


def test_a_download_that_does_not_match_the_lock_is_removed(tmp_path):
    world = make_world(tmp_path)
    locked, _ = lock(world)
    ctx = world.context()
    shutil.rmtree(ctx.cache("differential-review", COMMIT_A))
    world.downloads.archives[COMMIT_A] = upstream("被替换的正文。\n")
    with pytest.raises(third_party.HashMismatch):
        third_party.ensure_cached(locked[0], ctx.fetch, ctx.cache)
    assert not ctx.cache("differential-review", COMMIT_A).exists()


def test_the_repository_lock_lists_the_unlocked_skills():
    skills = third_party.read_lock(ToolLayout().third_party_lock())
    assert [(skill.name, skill.locked) for skill in skills] == [
        ("differential-review", False), ("variant-analysis", False), ("semgrep-rule-variant-creator", False),
        ("fp-check", False), ("sharp-edges", False)]


@pytest.mark.parametrize("text, message", [
    ("lockVersion: 2\nskills: []\n", "lockVersion"),
    (f"lockVersion: 1\nskills:\n  - {{name: a, source: {SOURCE}, ref: main}}\n", "skills[0].ref"),
    (f"lockVersion: 1\nskills:\n  - {{name: a, source: {SOURCE}, ref: {COMMIT_A}}}\n", "skills[0].treeHash"),
    (f"lockVersion: 1\nskills:\n  - {{name: a, source: {SOURCE}, extra: 1}}\n", "skills[0].extra"),
])
def test_an_invalid_lock_is_rejected_with_the_key(tmp_path, text, message):
    path = tmp_path / "skills.lock.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(third_party.LockError, match=message.replace("[", r"\[").replace("]", r"\]")):
        third_party.read_lock(path)


def test_source_queries_go_through_gh_api(tmp_path):
    commands = Commands()
    process = VcsProcess(execute=commands, environ={}, sleep=lambda seconds: None)
    assert third_party.repo_facts(process, tmp_path, SOURCE) == FACTS
    assert third_party.head_commit(process, tmp_path, SOURCE) == COMMIT_A
    assert [list(argv) for argv in commands.calls] == [["gh", "api", "repos/example/skills"],
                                                       ["gh", "api", "repos/example/skills/commits/HEAD"]]
    broken = VcsProcess(execute=lambda command: Completed(command.argv, 0, json.dumps({})), environ={},
                        sleep=lambda seconds: None)
    with pytest.raises(LookupError):
        third_party.head_commit(broken, tmp_path, SOURCE)


def test_downloads_switch_route_once_on_network_failures():
    noted = []
    direct = Downloads()
    patterns = ("timed? ?out", "connection reset")

    def failing(request):
        return HttpResponse(None, error="URLError: timed out")

    route = third_party.AlternateRoute(direct, "经代理", "直连", patterns, noted.append)
    assert third_party.fetcher(failing, 1.0, route)(SOURCE, COMMIT_A) == upstream()
    assert [(item.before, item.after, item.succeeded) for item in noted] == [("经代理", "直连", True)]
    assert noted[0].action.endswith(COMMIT_A) and noted[0].error == "URLError: timed out"

    def refused(request):
        return HttpResponse(None, error="URLError: certificate verify failed")

    with pytest.raises(third_party.ThirdPartyError):
        third_party.fetcher(refused, 1.0, route)(SOURCE, COMMIT_A)
    with pytest.raises(third_party.ThirdPartyError):
        third_party.fetcher(Downloads({"0" * 40: b""}), 1.0, route)(SOURCE, COMMIT_A)
    assert len(noted) == 1 and direct.urls == [noted[0].action]
