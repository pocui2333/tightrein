import json

from tightrein.collect.access_log import parse

FIELDS = {"method": "req.method", "route": "req.path", "status": "status", "durationMs": "ms"}


def line(method, route, status, duration):
    return json.dumps({"req": {"method": method, "path": route}, "status": status, "ms": duration})


def test_parse_json_and_pattern_lines():
    found, unparsed = parse.parse([line("get", "/api/orders?page=2", 200, 12), "not json", "", "[1]"], FIELDS, None)
    assert found == [parse.Request("GET", "/api/orders", 200, 12.0)] and unparsed == 2
    pattern = r'"(?P<method>\w+) (?P<route>\S+) HTTP/1\.1" (?P<status>\d+) (?P<durationMs>\d+)'
    found, unparsed = parse.parse(['1.2.3.4 - "POST /api/login HTTP/1.1" 500 87', "garbage"], {}, pattern)
    assert found == [parse.Request("POST", "/api/login", 500, 87.0)] and unparsed == 1


def test_requests_need_method_route_and_status_but_not_duration():
    assert parse.request_of({"method": "get", "route": "/a", "status": "204", "durationMs": None}) == parse.Request(
        "GET", "/a", 204, None)
    assert parse.request_of({"method": "GET", "route": "/a", "status": None}) is None
    assert parse.request_of({"method": "GET", "route": "", "status": 200}) is None
    assert parse.request_of({"method": "GET", "route": "/a", "status": "x"}) is None
    assert parse.Request("GET", "/a", 200, None).endpoint == "GET /a"
