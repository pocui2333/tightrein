"""部署跟踪的核心判断(pipeline/common/deploys.py)：在部署来源读到的记录中找包含合并提交的部署。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tightrein.domain.enums import DeploymentStatus, ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions.result import ExtensionFailure, PointResult
from tightrein.pipeline.common.deploys import DeploySource, DeploySourceError, observed_at
from tightrein.vcs.errors import RefNotFound

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
T = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
RECORDS = [
    {"id": "101", "commit": A, "status": "succeeded", "environment": None, "url": "https://ci/101",
     "createdAt": "2026-09-29T01:00:00Z"},
    {"id": "103", "commit": C, "status": "running", "environment": None, "url": "https://ci/103",
     "createdAt": "2026-09-29T02:00:31Z"},
    {"id": "102", "commit": B, "status": "skipped", "environment": None, "url": "https://ci/102",
     "createdAt": "2026-09-29T02:00:00Z"},
]


class Ancestry:
    """contains 为 (commit, 包含它的 commit)；missing 中的 commit 在 fetch 前不存在。"""

    def __init__(self, contains=(), missing=(), still_missing=()):
        self.contains = set(contains)
        self.missing = set(missing)
        self.still_missing = set(still_missing)
        self.fetches = 0

    def is_ancestor(self, repo, commit, of):
        if of in self.still_missing or (of in self.missing and not self.fetches):
            raise RefNotFound(f"{of} 不存在")
        return (commit, of) in self.contains

    def fetch(self, repo, prune=False):
        self.fetches += 1

    def head(self, repo):
        return SimpleNamespace(commit=D)


class Client:
    def __init__(self, records=None, failure=None, layer=ExtensionLayer.CORE):
        self.records = records
        self.failure = failure
        self.resolution = SimpleNamespace(get=lambda point: SimpleNamespace(layer=layer))

    def deploy_source(self, repo, commit):
        output = None if self.records is None else {"deployments": self.records}
        return PointResult(ExtensionPoint.DEPLOY_SOURCE, ExtensionLayer.CORE, output, self.failure)


def source(tmp_path, ancestry=None, **client):
    found = Client(**client)
    return DeploySource(lambda: found, ancestry or Ancestry(), tmp_path)


def test_the_exact_commit_is_used_unless_it_was_skipped(tmp_path):
    found = source(tmp_path, records=RECORDS).find(A)
    assert (found.status, found.record.id) == (DeploymentStatus.SUCCEEDED, "101")
    later = source(tmp_path, Ancestry(contains={(B, C)}), records=RECORDS).find(B)
    assert (later.status, later.record.id) == (DeploymentStatus.RUNNING, "103")
    assert source(tmp_path, records=RECORDS).find("f" * 40).status is DeploymentStatus.PENDING


def test_unknown_commits_are_fetched_once(tmp_path):
    ancestry = Ancestry(contains={(B, C)}, missing={A, C})
    assert (source(tmp_path, ancestry, records=RECORDS).find(B).record.id, ancestry.fetches) == ("103", 1)
    gone = Ancestry(still_missing={A, C})
    assert source(tmp_path, gone, records=RECORDS).find(B).status is DeploymentStatus.PENDING and gone.fetches == 1


def test_latest_success_configuration_and_failures(tmp_path):
    assert source(tmp_path, records=RECORDS).latest_success().commit == A
    unconfigured = source(tmp_path, layer=ExtensionLayer.DEFAULT)
    assert not unconfigured.configured() and unconfigured.records() == []
    failing = source(tmp_path, failure=ExtensionFailure(ExtensionErrorCode.SOURCE_UNAVAILABLE, "HTTP 502"))
    with pytest.raises(DeploySourceError, match="HTTP 502"):
        failing.records()


def test_the_observation_period_starts_at_the_merge(make_config):
    assert observed_at(make_config(), T) == T + timedelta(hours=24)
