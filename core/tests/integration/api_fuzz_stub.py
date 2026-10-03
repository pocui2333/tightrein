"""api-fuzz 集成测试与报告夹具录制用的桩服务：几个带已知缺陷的接口与它们的 OpenAPI 描述。

- POST /api/Auth/Login：userName 与 password 正确时返回 {"data": {"token": ...}}；
- GET /api/Order/{id}：id 为负数时返回 500；
- POST /api/Order/Query：pageSize 小于 1 时返回 500(未处理的参数校验)；
- GET /api/Order/List：返回的 name 为数字，与描述不符；
- GET /api/Report/Export：1.2 秒后才响应；
- GET /api/User/List：任何登录用户都返回 200(越权)；
- GET /api/Company/{id}：一律返回未声明的 403；
- 未声明的方法返回 405 与 Allow 请求头。
用法：python api_fuzz_stub.py <端口>，端口为 0 时自动选择，启动后在标准输出打印实际端口。
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.c3R1Yi1zaWduYXR1cmU"
ACCOUNT = "test-company"
PASSWORD = "stub-password"
JSON_BODY = {"application/json": {"schema": {"type": "object"}}}


def ok(schema=None):
    content = {"application/json": {"schema": schema}} if schema else JSON_BODY
    return {"200": {"description": "ok", "content": content}}


SPEC = {
    "openapi": "3.0.1",
    "info": {"title": "stub", "version": "V1"},
    "paths": {
        "/api/Auth/Login": {"post": {
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["userName", "password"],
                "properties": {"userName": {"type": "string"}, "password": {"type": "string"}}}}}},
            "responses": {**ok(), "401": {"description": "bad credentials"}}}},
        "/api/Order/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {**ok({"type": "object", "properties": {"id": {"type": "integer"}}}),
                          "400": {"description": "bad id"}}}},
        "/api/Order/Query": {"post": {
            "parameters": [{"name": "company", "in": "query", "required": False, "schema": {"type": "string"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["pageSize"],
                "properties": {"pageSize": {"type": "integer"}, "keyword": {"type": "string"}}}}}},
            "responses": {**ok(), "400": {"description": "bad request"}}}},
        "/api/Order/List": {"get": {"responses": ok(
            {"type": "array", "items": {"type": "object", "required": ["name"],
                                        "properties": {"name": {"type": "string"}}}})}},
        "/api/Report/Export": {"get": {"responses": ok()}},
        "/api/User/List": {"get": {"responses": ok({"type": "array"})}},
        "/api/Company/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": ok()}},
    },
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, allow=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        if allow is not None:
            self.send_header("Allow", allow)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return None

    def route(self):
        return self.path.split("?", 1)[0]

    def do_GET(self):
        path = self.route()
        if path == "/openapi.json":
            return self.send(200, SPEC)
        if path.startswith("/api/Order/") and path != "/api/Order/List":
            try:
                number = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self.send(400, {"error": "bad id"})
            if number < 0:
                return self.send(500, {"error": "Sequence contains no elements"})
            return self.send(200, {"id": number})
        if path == "/api/Order/List":
            return self.send(200, [{"name": 1}])
        if path == "/api/Report/Export":
            time.sleep(1.2)
            return self.send(200, {})
        if path == "/api/User/List":
            return self.send(200, [])
        if path.startswith("/api/Company/"):
            return self.send(403, {"error": "forbidden"})
        return self.send(404, {})

    def do_POST(self):
        path = self.route()
        body = self.body()
        if path == "/api/Auth/Login":
            if isinstance(body, dict) and body.get("userName") == ACCOUNT and body.get("password") == PASSWORD:
                return self.send(200, {"data": {"token": TOKEN}})
            return self.send(401, {"error": "bad credentials"})
        if path == "/api/Order/Query":
            if not isinstance(body, dict) or not isinstance(body.get("pageSize"), int):
                return self.send(400, {"error": "bad request"})
            if body["pageSize"] < 1:
                return self.send(500, {"error": "Value cannot be negative or zero. (Parameter 'pageSize')"})
            return self.send(200, {"rows": []})
        return self.send(405, {}, "GET")

    def not_allowed(self):
        self.body()
        self.send(405, {}, "POST" if self.route() in ("/api/Auth/Login", "/api/Order/Query") else "GET")

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.not_allowed
        raise AttributeError(name)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
