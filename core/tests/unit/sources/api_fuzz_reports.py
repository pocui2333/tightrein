"""以锁定版本 Schemathesis 4.28.0 对桩服务(tests/integration/api_fuzz_stub.py)实际运行一次得到的 NDJSON 事件流：
fixtures/api_fuzz/events.json 为其中各事件组成的数组，只去掉了通过的用例、用例的 meta 与探测阶段的事件。"""

import json
from pathlib import Path

from tightrein.sources.api_fuzz.authz.model import AuthzModel, EndpointRule

FIXTURE = Path(__file__).parent / "fixtures" / "api_fuzz" / "events.json"
RECORDED_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.c3R1Yi1zaWduYXR1cmU"
COMMIT = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"


def events():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_events(path, items=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False) for item in (events() if items is None else items)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def without(name, items=None):
    return [item for item in (events() if items is None else items) if name not in item]


def stub_model():
    """录制时使用的模型：Company 缺少 CanManageUsers 与 CanViewCompany。"""
    return AuthzModel(COMMIT, {
        ("GET", "/api/Order/{id}"): EndpointRule(("CanViewOrders",), False),
        ("POST", "/api/Order/Query"): EndpointRule(("CanViewOrders",), False),
        ("GET", "/api/User/List"): EndpointRule(("CanManageUsers",), False),
        ("GET", "/api/Company/{id}"): EndpointRule(("CanViewCompany",), False),
        ("POST", "/api/Auth/Login"): EndpointRule((), True),
    }, ("CanViewOrders", "CanManageUsers", "CanViewCompany"),
        {"Company": {"CanViewOrders": True, "CanManageUsers": False, "CanViewCompany": False}})
