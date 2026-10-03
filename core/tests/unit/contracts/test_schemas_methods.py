import pytest
from contract_samples import changed, paths, without


MANIFEST = "extension/method-manifest.schema.json"

METHOD = {
    "name": "loki",
    "point": "log-platform",
    "summary": "在 Grafana Loki 上查询日志原文",
    "applicability": "日志已送入 Grafana Loki 时",
    "options": {"pageSize": 1000},
    "optionsSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
    "tools": [{"tool": "semgrep", "minVersion": None, "install": "pipx install semgrep"}],
}
DOC = {"prerequisites": ["只读令牌存进钥匙串"], "output": "日志原文片段", "limitations": ["不支持指标查询"],
       "example": "extensions:\n  log-platform: {use: core/loki}\n"}
STACK_METHOD = changed(without(METHOD, "options", "tools"), name="console", point="log-parse",
                       command=["{python}", "run.py"])

VALID = [METHOD, STACK_METHOD, without(METHOD, "options", "tools"), changed(METHOD, doc=DOC)]

INVALID = [
    (without(METHOD, "summary"), "$"),
    (changed(METHOD, name="Loki_Query"), "$.name"),
    (changed(METHOD, point="log-source"), "$.point"),
    (changed(METHOD, doc=without(DOC, "example")), "$.doc"),
    (changed(METHOD, doc=changed(DOC, prerequisites="令牌")), "$.doc.prerequisites"),
    (changed(METHOD, point="log-reader"), "$.point"),
    (changed(METHOD, applicability=""), "$.applicability"),
    (changed(METHOD, command=[]), "$.command"),
    (changed(METHOD, optionsSchema="loki.schema.json"), "$.optionsSchema"),
    (changed(METHOD, tools=[{"minVersion": "1.0"}]), "$.tools[0]"),
    (changed(METHOD, extra=True), "$"),
]


@pytest.mark.parametrize("instance", VALID)
def test_valid_samples(instance):
    assert paths(MANIFEST, instance) == set()


@pytest.mark.parametrize("instance,path", INVALID)
def test_invalid_samples(instance, path):
    assert path in paths(MANIFEST, instance)
