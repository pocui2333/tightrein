"""观察期按来源取，没列出的用 default。"""

from datetime import timedelta

from tightrein.release.accept.windows import window


def test_windows_by_source(kit):
    settings = kit.make_settings({"controls": {"release.accept": {"windows": {"default": "6h",
                                                                              "collect.static": "0s"}}}})
    assert window("collect.static", settings) == timedelta(0)
    # 缺省值按来源写明了运行时来源(业务告警等 24h)，改 default 只影响没列出的来源
    assert window("collect.alerts", settings) == timedelta(hours=24)
    assert window("collect.not_listed", settings) == timedelta(hours=6)
    assert window("collect.platform_errors", kit.make_settings()) == timedelta(hours=24)
