import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tightrein.collect.common.source import SourceInvalid, SourceUnavailable
from tightrein.protocol.http import HttpRequest, HttpResponse, Platform, UrllibTransport, auth_headers

TOKEN = "platform-token-0123456789abcdef"


class Echo(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        status = 200 if body.get("ok") else 500
        payload = json.dumps({"echo": self.headers.get("X-Probe")}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def server(monkeypatch):
    # 没给代理表时按进程环境变量发送：本机设了 http_proxy 而没设 no_proxy 时，访问本地测试服务器会被送去代理
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_urllib_transport_returns_status_and_body_for_errors_too(server):
    transport = UrllibTransport()
    good = transport(HttpRequest("POST", f"{server}/x", 5, {"X-Probe": "a"}, b'{"ok": true}'))
    bad = transport(HttpRequest("POST", f"{server}/x", 5, {"X-Probe": "b"}, b'{"ok": false}'))
    assert (good.status, json.loads(good.body)) == (200, {"echo": "a"}) and good.ok
    assert (bad.status, json.loads(bad.body)) == (500, {"echo": "b"}) and not bad.ok
    assert good.elapsed_ms >= 0


def test_urllib_transport_reports_connection_errors():
    response = UrllibTransport()(HttpRequest("GET", "http://127.0.0.1:9/none", 2))
    assert response.status is None and response.error


def test_urllib_transport_uses_the_given_proxy_table(server):
    proxied = UrllibTransport(proxies={"http": server, "no": "localhost"})
    response = proxied(HttpRequest("POST", "http://far.example.invalid/x", 5, {"X-Probe": "p"}, b'{"ok": true}'))
    assert (response.status, json.loads(response.body)) == (200, {"echo": "p"})
    direct = UrllibTransport(proxies={"http": "http://127.0.0.1:9", "no": "127.0.0.1,localhost"})
    assert direct(HttpRequest("POST", f"{server}/x", 5, {"X-Probe": "d"}, b'{"ok": true}')).status == 200
    assert UrllibTransport().opener(server) is None


def test_auth_headers():
    assert auth_headers(None) == {}
    assert auth_headers("t") == {"Authorization": "Bearer t"}
    assert auth_headers("t", "12345") == {"Authorization": "Basic MTIzNDU6dA=="}


def test_platform_errors_never_carry_the_token():
    sent = []

    def refusing(request: HttpRequest) -> HttpResponse:
        sent.append(request)
        return HttpResponse(401, b"{}")

    platform = Platform("Sentry", refusing, 5, auth_headers(TOKEN))
    with pytest.raises(SourceUnavailable) as caught:
        platform.get("https://sentry.example.test/api", {"q": "x"})
    assert "401" in caught.value.message and TOKEN not in caught.value.message
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}" and sent[0].url.endswith("?q=x")
    unreachable = Platform("Loki", lambda request: HttpResponse(None, error="URLError: refused"), 5)
    with pytest.raises(SourceUnavailable, match="refused"):
        unreachable.get("https://loki.example.test")
    with pytest.raises(SourceInvalid):
        Platform("Loki", lambda request: HttpResponse(200, b"<html>"), 5).get_json("https://loki.example.test")
