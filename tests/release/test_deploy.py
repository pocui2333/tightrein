"""跟踪部署的流程部分：找包含合并提交的部署(同一 commit 没被跳过的最新一次，否则以它为祖先的最早一次)；
祖先关系查不到时一次运行只 fetch 一次；没有部署来源时以合并时间加 assumeDeployedAfter 视为已部署并记进 state；
需要手动部署的路径。"""

from datetime import timedelta

from tightrein.collect.common.signals import DEPLOYMENTS_KEY
from tightrein.onboard.setup import ModuleStatus
from tightrein.release import deploy
from tightrein.release.deploy import FAILED, SKIPPED, SUCCEEDED, DeployRecord
from tightrein.store.tables import state


class AncestryGit:
    """祖先关系的替身：known 中没有的对(本地没有该 commit)为 None，fetch 之后 fetched 中的才认得。"""

    def __init__(self, known, fetched=None):
        self.known = known
        self.after_fetch = fetched or {}
        self.fetches = 0

    def is_ancestor(self, commit, of):
        return self.known.get((commit, of))

    def fetch(self):
        self.fetches += 1
        self.known = {**self.known, **self.after_fetch}


def _record(identifier, commit, status, minute, kit):
    return DeployRecord(identifier, commit, status, None, f"https://ci/{identifier}",
                        kit.NOW + timedelta(minutes=minute))


def test_the_exact_commit_is_used_unless_it_was_skipped(kit):
    exact = [_record("1", "m", SUCCEEDED, 1, kit), _record("2", "m", FAILED, 2, kit)]
    git = AncestryGit({})
    assert deploy.containing(exact, "m", git, git.fetch).id == "2"
    skipped = [_record("1", "m", SKIPPED, 1, kit), _record("2", "x", SUCCEEDED, 2, kit),
               _record("3", "y", SUCCEEDED, 3, kit)]
    git = AncestryGit({("m", "x"): True, ("m", "y"): True})
    assert deploy.containing(skipped, "m", git, git.fetch).id == "2"  # 以它为祖先的最早一次


def test_unknown_commits_are_fetched_once(kit):
    records = [_record("1", "x", SUCCEEDED, 1, kit), _record("2", "y", SUCCEEDED, 2, kit)]
    git = AncestryGit({}, fetched={("m", "y"): True})
    fetched = []

    def fetch():
        if not fetched:
            fetched.append(1)
            git.fetch()

    assert deploy.containing(records, "m", git, fetch).id == "2"
    assert git.fetches == 1
    assert deploy.containing(records, "z", AncestryGit({}), lambda: None) is None  # fetch 之后仍不认得：不计入


def test_without_a_deploy_source_the_merge_time_plus_a_delay_counts_as_deployed(kit, runtime):
    merged_at = kit.NOW - timedelta(minutes=30)
    waiting = deploy.track(runtime, "m" * 40, merged_at)
    assert waiting.status == deploy.PENDING and waiting.source == deploy.MERGE_TIME
    assert waiting.at == merged_at + timedelta(hours=1)
    runtime.clock.advance(timedelta(hours=1))
    done = deploy.track(runtime, "m" * 40, merged_at)
    assert done.status == SUCCEEDED and done.record is None
    remembered = state.get(runtime.conn, DEPLOYMENTS_KEY)
    assert [(item["commit"], item["status"]) for item in remembered] == [("m" * 40, SUCCEEDED)]


def test_deploy_records_are_read_once_per_run(kit, tmp_path, repos, monkeypatch):
    runtime = kit.make_runtime(tmp_path, repos.repo, setup=kit.make_setup(ModuleStatus.ENABLED, "github_actions"))
    reads = []

    def recent(found_runtime, **_):
        reads.append(1)
        return [_record("9", repos.head(repos.repo), SUCCEEDED, 1, kit)]

    monkeypatch.setattr(deploy, "recent", recent)
    for _ in range(2):
        found = deploy.track(runtime, repos.head(repos.repo), kit.NOW)
        assert found.status == SUCCEEDED and found.record.id == "9" and found.source == "github_actions"
    assert reads == [1]
    assert state.get(runtime.conn, DEPLOYMENTS_KEY)[0]["id"] == "9"


def test_manual_deploy_paths(kit):
    settings = kit.make_settings({"controls": {"release.deploy": {"manualPaths": ["infra/", "*.tf"]}}})
    assert deploy.manual_paths(settings, ["src/a.py", "infra/db.yml", "main.tf"]) == ["infra/db.yml", "main.tf"]
