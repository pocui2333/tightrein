import pytest

from tightrein.contracts import validate
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions import defaults, points
from tightrein.extensions.result import ExtensionFailure, PointResult


def test_extendable_points_match_the_project_config_schema():
    declared = validate.schema("config/project-config.schema.json")["properties"]["extensions"]["properties"]
    for point in ExtensionPoint:
        assert (declared[point.value]["$ref"] == "#/$defs/extension") is points.SPECS[point].extendable


def test_points_without_repo_match_the_request_schema():
    without_repo = validate.schema(points.REQUEST_SCHEMA)["if"]["properties"]["point"]["enum"]
    assert set(without_repo) == {point.value for point in ExtensionPoint if not points.SPECS[point].uses_repo}


def test_schema_names_exist():
    names = set(validate.names())
    for point in ExtensionPoint:
        assert {points.SPECS[point].input_schema, points.SPECS[point].output_schema} <= names
    assert {points.REQUEST_SCHEMA, points.RESPONSE_SCHEMA, points.STACK_MANIFEST_SCHEMA} <= names


@pytest.mark.parametrize("point", list(ExtensionPoint))
def test_default_result_has_no_data_and_says_why(point):
    result = defaults.result(point)
    assert result.implementation is ExtensionLayer.DEFAULT
    assert (result.output, result.failure, result.failed, result.cached) == (None, None, False, False)
    assert result.notes == (defaults.DEFAULT_NOTES[point],)


def test_default_notes_follow_the_architecture_wording():
    assert defaults.result(ExtensionPoint.SPEC_EXPORT).notes == ("未提供 spec-export 扩展",)
    assert defaults.result(ExtensionPoint.LOG_PLATFORM, ["技术栈扩展报告不适用"]).notes == (
        "未配置集中日志平台(extensions.log-platform)", "技术栈扩展报告不适用")
    assert defaults.result(ExtensionPoint.ERROR_TRACKING).notes == ("未配置错误追踪平台(extensions.error-tracking)",)
    assert defaults.result(ExtensionPoint.ALERT_SOURCE).notes == ("未配置业务告警来源(extensions.alert-source)",)
    assert defaults.result(ExtensionPoint.STATIC_TOOLS).notes == ("未配置确定性工具",)
    assert defaults.result(ExtensionPoint.LOCAL_RUN).notes == ("未提供 local-run 扩展",)


def test_result_states_and_stats_entry():
    routes = PointResult(ExtensionPoint.PAGE_ROUTES, ExtensionLayer.PROJECT, output={"routes": [], "sourceFiles": []},
                         cached=True)
    assert not routes.failed
    assert routes.stats_entry() == {"implementation": "project", "cached": True}
    failure = ExtensionFailure(ExtensionErrorCode.TOOL_MISSING, "未找到 dotnet", "安装 .NET SDK 8")
    assert PointResult(ExtensionPoint.SPEC_EXPORT, ExtensionLayer.STACK, failure=failure).failed
    assert failure.describe() == "tool-missing：未找到 dotnet；安装 .NET SDK 8"
    assert ExtensionFailure(ExtensionErrorCode.TIMEOUT, "超过 30 秒").describe() == "timeout：超过 30 秒"
