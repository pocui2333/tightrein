"""集成测试共用的夹具：校验通过的最小 project.yaml。集成测试需要真实的外部工具，工具缺失时各测试模块自行跳过。"""

import pytest

from tightrein.config import project

PROJECT = {
    "project": {"name": "demo", "repo": "/tmp/demo", "mainBranch": "main"},
    "target": {"baseUrl": "http://127.0.0.1", "healthcheck": "/"},
    "accounts": {"roles": {"Company": {"keychain": "tightrein.demo.company"}},
                 "login": {"endpoint": "/api/Auth/Login", "bodyTemplate": {"userName": "{account}",
                                                                           "password": "{password}"},
                           "tokenPath": "data.token"}},
    "stages": {},
    "evaluation": {"judge": {"runner": "claude"}, "budgetUsd": 1},
    "thresholds": {"suppressionDays": {"value": 30, "min": 7, "max": 90},
                   "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}}},
}


@pytest.fixture
def make_config(tmp_path):
    def build(**changes):
        return project.parse({**PROJECT, **changes}, tmp_path / "project.yaml")

    return build
