"""平台来源测试共用的假扩展客户端：按扩展点给出预设结果，记录调用。"""

from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions.result import ExtensionFailure, PointResult


def ok(point, output):
    return PointResult(point, ExtensionLayer.CORE, output=output)


def broken(point, message="平台返回 503"):
    return PointResult(point, ExtensionLayer.CORE,
                       failure=ExtensionFailure(ExtensionErrorCode.SOURCE_UNAVAILABLE, message))


def chunk(stream, lines, modified="2026-10-05T02:59:00Z"):
    text = "".join(line + "\n" for line in lines)
    return {"stream": stream, "text": text, "startPosition": 0, "endPosition": len(text.encode("utf-8")),
            "modifiedAt": modified}


class PlatformClient:
    def __init__(self, configured=(), **results):
        self.points = set(configured)
        self.results = results
        self.calls = []

    def configured(self, point):
        return point in self.points

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        return self.results[name]

    def error_tracking(self, since, until):
        return self._answer("error_tracking", since, until)

    def log_platform(self, query, since, until, limit):
        return self._answer("log_platform", query, since, until, limit)

    def log_parse(self, chunks, state):
        return self._answer("log_parse", chunks, state)

    def alert_source(self):
        return self._answer("alert_source")


ERROR_TRACKING = ExtensionPoint.ERROR_TRACKING
LOG_PLATFORM = ExtensionPoint.LOG_PLATFORM
LOG_PARSE = ExtensionPoint.LOG_PARSE
ALERT_SOURCE = ExtensionPoint.ALERT_SOURCE
