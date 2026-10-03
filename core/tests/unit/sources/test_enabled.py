from tightrein.domain.enums import ExtensionPoint
from tightrein.sources.enabled import disabled


def test_every_method_needs_its_configuration(make_config):
    nothing = disabled(make_config(), lambda point: False)
    assert set(nothing) == {"platform-errors", "access-log", "alerts", "project-probe", "api-fuzz"}
    assert nothing["api-fuzz"] == "没有配置接口描述(extensions.spec-export)"
    configured = {ExtensionPoint.ERROR_TRACKING, ExtensionPoint.LOG_PLATFORM, ExtensionPoint.ALERT_SOURCE,
                  ExtensionPoint.SPEC_EXPORT}
    config = make_config(sources={"access-log": {"query": '{job="nginx"}'},
                                  "project-probes": [{"name": "a", "command": ["x"], "every": "1h"}]})
    assert disabled(config, configured.__contains__) == {}
    assert disabled(make_config(target=None), configured.__contains__)["api-fuzz"] == "没有配置 target.baseUrl"
