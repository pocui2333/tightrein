"""页面检查测试共用的录制结果：fixtures/results.json 是 tests/integration/test_pages_playwright.py 用 Playwright 1.63.0
实际运行一次写出的 results.ndjson，每行一个元素；原始输出目录的绝对路径换成了 {raw}，本机端口换成了 8081。"""

import io
import json
import sys
import zipfile
from pathlib import Path

# 与采集方法的测试共用 probe_world(目标、脱敏器)
sys.path.insert(0, str(Path(__file__).parents[1] / "sources"))

FIXTURE = Path(__file__).parent / "fixtures" / "results.json"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LWNvbXBhbnkifQ.c2l0ZS1zaWduYXR1cmU"
ENCODED_TOKEN = "ZXlKaGJHY2lPaUpJVXpJMU5pSjkuZXlKemRXSWlPaUowWlhOMExXTnZiWEJoYm5raWZRLmMybDBaUzF6YVdkdVlYUjFjbVU="


def results(raw_dir):
    text = FIXTURE.read_text(encoding="utf-8").replace("{raw}", str(raw_dir))
    return json.loads(text)


def write_results(raw_dir, items=None, trailing=""):
    raw_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False) for item in (results(raw_dir) if items is None else items)]
    path = raw_dir / "results.ndjson"
    path.write_text("\n".join(lines) + "\n" + trailing, encoding="utf-8")
    return path


def trace_bytes():
    """与真实 trace 同样结构的 zip：上下文参数中的本地存储、请求头、登录响应体与一张截图。"""
    buffer = io.BytesIO()
    context = {"type": "context-options", "options": {"storageState": {
        "cookies": [{"name": "sid", "value": "cookie-secret", "domain": "127.0.0.1"}],
        "origins": [{"origin": "http://127.0.0.1:8081",
                     "localStorage": [{"name": "auth", "value": ENCODED_TOKEN}]}]}}}
    network = {"type": "resource-snapshot", "snapshot": {"request": {"method": "GET", "url": "/api/Order", "headers": [
        {"name": "Authorization", "value": f"Bearer {TOKEN}"}, {"name": "Accept", "value": "*/*"}]}}}
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1-trace.trace", json.dumps(context) + "\n")
        archive.writestr("1-trace.network", json.dumps(network) + "\n")
        archive.writestr("resources/login.json", json.dumps({"data": {"token": TOKEN}}))
        archive.writestr("resources/page.html", f"<p>token {TOKEN}</p>")
        archive.writestr("screencast/1.jpeg", b"\xff\xd8\xff\xe0binary")
    return buffer.getvalue()
