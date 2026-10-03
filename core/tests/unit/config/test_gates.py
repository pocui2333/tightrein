import pytest

from tightrein.config import gates, project
from tightrein.config.gates import Gate
from tightrein.config.layers import ConfigError

BASE = {"project": {"name": "sample", "repo": "/tmp/sample", "mainBranch": "main"}}


def test_every_gate_defaults_to_the_user(tmp_path):
    config = project.parse(BASE, tmp_path / "project.yaml")
    assert all(row["decide"] == gates.USER for row in gates.table(config))
    assert {row["gate"] for row in gates.table(config) if row["mandatory"]} == {
        "high-risk-merge", "delete", "permissions-secrets", "over-task-limit", "needs-decision"}


def test_rule_gates_can_be_automatic_but_mandatory_ones_cannot(tmp_path):
    config = project.parse({**BASE, "gates": {"merge": "auto", "release-writes": "auto"}}, tmp_path / "project.yaml")
    assert gates.auto(config, Gate.MERGE) and gates.auto(config, Gate.RELEASE_WRITES)
    assert not gates.auto(config, Gate.ISSUE_APPROVE)
    with pytest.raises(ConfigError):
        project.parse({**BASE, "gates": {"delete": "auto"}}, tmp_path / "project.yaml")
