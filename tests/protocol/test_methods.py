from pathlib import Path

import pytest
import yaml

import tightrein
from tightrein.collect.common.source import SourceMisconfigured
from tightrein.protocol import methods
from tightrein.settings.load import Layer, Settings

LOGS = "tightrein.collect.platform_errors.log_platform"
TRACKING = "tightrein.collect.platform_errors.error_tracking"


def settings(section, sites):
    return Settings([Layer("test", None, {"controls": {"*": {}, "collect.platform_errors": section}})], None, sites)


def test_methods_are_found_by_name_only():
    assert methods.exists(LOGS, "loki") and methods.exists(TRACKING, "sentry")
    assert not methods.exists(LOGS, "sentry") and not methods.exists(LOGS, "../loki")
    with pytest.raises(SourceMisconfigured, match="方法名"):
        methods.load(LOGS, "Loki;rm")
    with pytest.raises(SourceMisconfigured, match="没有这个方法"):
        methods.load(LOGS, "elastic")
    assert methods.load(LOGS, "loki").manifest["site"] == "loki"


def test_configure_merges_sites_settings_and_secrets():
    method = methods.load(LOGS, "loki")
    configured = methods.configure(method, settings=settings({"loki": {"retentionDays": 30, "pageSize": 100}},
                                                             {"loki": {"url": "https://logs.example.test"}}),
                                   source="collect.platform_errors", secrets={"loki.token": "t"})
    assert configured.options == {"url": "https://logs.example.test", "retentionDays": 30, "pageSize": 100}
    assert configured.token == "t"


def test_configure_lists_every_problem_and_required_secrets():
    loki = methods.load(LOGS, "loki")
    with pytest.raises(SourceMisconfigured) as caught:
        methods.configure(loki, settings=settings({"loki": {"pageSize": 0}}, {}), source="collect.platform_errors",
                          secrets={})
    assert "url" in caught.value.message and "retentionDays" in caught.value.message
    sentry = methods.load(TRACKING, "sentry")
    complete = settings({"sentry": {"projects": [], "environment": None, "retentionDays": 90, "limit": 10,
                                    "breadcrumbs": 2}},
                        {"sentry": {"url": "https://sentry.io", "organization": "acme"}})
    with pytest.raises(SourceMisconfigured, match="sentry.token"):
        methods.configure(sentry, settings=complete, source="collect.platform_errors", secrets={})


def test_every_method_manifest_names_itself_and_its_directory():
    root = Path(tightrein.__file__).parent
    manifests = [path for path in root.rglob("*.yaml") if path.with_suffix(".py").exists()]
    assert len(manifests) >= 8
    for path in manifests:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert manifest["name"] == path.stem, path
        assert manifest["kind"] == path.parent.name, path
        assert "optionsSchema" in manifest and "summary" in manifest, path
