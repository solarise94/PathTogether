# -*- coding: utf-8 -*-
"""PT-4：匿名 Demo 完整链路测试（docs §5/§9.1/§9.3/§12.1）。

json 模式（默认）：
  - /demo 公开 200（json 渲染 PG 前置降级页，不加载写操作脚本）；
  - /api/demo/* 一律 503 pg_backend_required（fail-closed，不退化内存计数）；
  - Demo POST 不要求登录 CSRF（capability 通道独立，docs §10.13）；
  - Demo cookie 调 /api/ai/* 等登录态端点 → 401（capability 只放行 /api/demo/*）；
  - PUBLIC_DEMO 模式下 /internal/ai/annotate 写通道 403（docs §5.4-1）。

PG 模式（RUN_PG_TESTS=1）追加（mock sidecar）：
  - capability 签发（cookie 属性）与 /api/demo/config 状态；
  - /api/demo/ai/run 全链路：security envelope（demo-readonly-v1、无 run_grant、
    session_owner 不可反推）、max_steps=周期值、consume/release 时序；
  - 双击 409 / 同 request_id 重试不双扣 / HistoPilot 400 → release；
  - SSE 重连不扣额度；capability 过期 → stream 410；
  - 移出目录 / 删除切片 → revoke 联动（旧 capability 不可再读）；
  - catalog 外 info/dzi/tile 404；Demo cookie 读普通切片 401；
  - demo / platform 子额度耗尽（429 + 稳定 code + 释放 run）；
  - legacy adapter / HistoPilot 不可达 → run 拒绝；
  - 对账：by-request 200→consume / 404→release / 5xx→顺延；
  - owner demo-catalog CRUD 与越权 403。
"""
import inspect
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ.setdefault("AI_BUDGET_RECLAIM_INTERVAL_SECONDS", "0")  # 关后台线程


import pytest  # noqa: E402
import requests as real_requests  # noqa: E402

import share_store  # noqa: E402
import user_store  # noqa: E402
import budget_store  # noqa: E402
import demo_store  # noqa: E402
import platform_features  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND, json_only  # noqa: E402
from _pt_helpers import csrf_client, isolate_app # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="Demo API 全链路需 PG capability/预算（RUN_PG_TESTS=1）",
)


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_stores(monkeypatch, tmp_path):
    """每用例：独立存储目录（ai_config/用户库全落本用例私有目录）+ adapter 缓存复位。

    通用隔离主体在 _pt_helpers.isolate_app（test-review P3-16 收敛）；其还原护栏
    会把用例内 FakeSidecar 对 app.requests 的裸赋值在 teardown 还原成真 requests，
    无需原先的防御性恢复行。
    """
    isolate_app(monkeypatch, tmp_path)
    app_mod._ADAPTER_MODE_CACHE.update(ts=0.0, mode=None)
    yield


class FakeResponse:
    def __init__(self, status_code=200, content=None, ctype="application/json",
                 headers=None):
        self.status_code = status_code
        self.content = content if content is not None else b"{}"
        if isinstance(self.content, str):
            self.content = self.content.encode("utf-8")
        elif isinstance(self.content, dict):
            self.content = json.dumps(self.content).encode("utf-8")
        self.headers = {"Content-Type": ctype}
        if headers:
            self.headers.update(headers)

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def close(self):
        pass

    def iter_content(self, chunk_size=4096):
        yield self.content


class FakeSidecar:
    """HistoPilot mock：精确 + 前缀路由；记录全部调用；可切换不可达。"""

    def __init__(self):
        self.exact = {}
        self.prefixes = []
        self.calls = []
        self.unreachable = False
        self.adapter = "plugin-contract"

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def on(self, method, path, handler, prefix=False):
        if prefix:
            self.prefixes.append((method.upper(), path, handler))
        else:
            self.exact[(method.upper(), path)] = handler

    def _install(self):
        app_mod.requests = self
        self.on("GET", "/healthz",
                lambda path, body, params, headers: FakeResponse(200, {
                    "ok": True, "adapter": self.adapter,
                    "security_contract_version": "1.0",
                    "features": ["tool-profile:demo-readonly-v1",
                                 "session:ephemeral-v1", "session-ttl:v1"]}))
        # 默认 /run：2xx SSE（= HistoPilot 已接受执行）；用 on() 覆盖换行为
        self.on("POST", "/run", lambda path, body, params, headers: _sse_ok())
        return self

    def calls_of(self, method, path):
        return [c for c in self.calls
                if c["method"] == method.upper() and c["path"] == path]

    def _dispatch(self, method, url, **kwargs):
        raw = url.split("?")[0]
        for p in ("http://", "https://"):
            if raw.startswith(p):
                raw = raw[len(p):]
        slash = raw.find("/")
        raw = raw[slash:] if slash >= 0 else "/"
        self.calls.append({
            "method": method.upper(), "path": raw,
            "body": kwargs.get("json"), "params": kwargs.get("params"),
            "headers": kwargs.get("headers"),
        })
        if self.unreachable:
            raise FakeSidecar.ConnectionError("histopilot down (test)")
        handler = self.exact.get((method.upper(), raw))
        if handler is None:
            for m, prefix, h in self.prefixes:
                if m == method.upper() and raw.startswith(prefix):
                    handler = h
                    break
        if handler is None:
            return FakeResponse(404, {"error": "no route"})
        return handler(raw, kwargs.get("json"), kwargs.get("params"),
                       kwargs.get("headers"))

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


def _sse_ok(session_id="sess-demo-1"):
    return FakeResponse(
        200, "id: 1\nevent: security_profile_applied\ndata: {}\n\n",
        ctype="text/event-stream", headers={"X-AI-Session-ID": session_id})


def _json_err(status, code):
    return FakeResponse(status, {"error": code, "code": code})


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _login(client, role, user_id):
    with client.session_transaction() as sess:
        sess["role"] = role
        sess["user_id"] = user_id
        sess["auth_user"] = "t@x.com"
        # 批次 A：手工 session 需携带与库内一致的凭据版本（docs §6.2）
        row = user_store.get_user(user_id)
        sess["auth_version"] = (row or {}).get("auth_version", 1)


def _make_user(role="owner"):
    return user_store.create_user(
        "u-%s@x.com" % uuid.uuid4().hex[:8], "password1password1", role=role)


def _touch(name="demo1.svs"):
    # 经 app_mod.UPLOAD_DIR 写入（其它测试模块可能改写过该常量）
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_platform():
    app_mod._save_ai_config({
        "base_url": "http://platform.example/v1",
        "api_key": "sk-platform-123456", "model": "gpt-p"})


def _enable_demo_period():
    budget_store.update_period_limits({"demo_enabled": True})


def _catalog_add(name):
    """直连 store 造目录条目（绕开 admin API），返回 slide_id。"""
    share_store.set_slide_meta(name)
    slide_id = share_store.get_slide_id(name)
    demo_store.catalog_add(slide_id, added_by="owner-test")
    return slide_id


def _demo_usage_total():
    return budget_store.usage_report()["demo"]["total"]


def _platform_usage_total():
    return budget_store.usage_report()["platform"]["total"]


def _fresh_capability(fake):
    """新浏览器：发 config 拿 capability cookie，返回 client。"""
    client = _client()
    r = client.get("/api/demo/config")
    assert r.status_code == 200, r.get_json()
    return client


def _pg_conn():
    import psycopg
    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# json 后端：fail-closed 与通道隔离
# --------------------------------------------------------------------------- #
@json_only  # PG 下 /demo 渲染 Viewer（capability/Viewer 测试另见 pg_only 用例）
def test_demo_page_public_json_backend():
    """/demo 公开 200；json 渲染降级页（不加载 demo.js 写入口，不靠 CSS 隐藏）。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "PostgreSQL" in body
    assert "仅用于研究、教学和软件演示" in body
    assert 'href="/login"' in body
    # 降级页不加载 Viewer/AI 脚本（无写操作入口可言）
    assert "demo.js" not in body and "app.js" not in body


@json_only  # PG 下 Demo API 正常开放（全链路见 pg_only 用例）
def test_json_backend_demo_api_fail_closed():
    """json/dual：全部 /api/demo/* 一律 503 pg_backend_required（docs §4.3）。"""
    client = _client()
    checks = [
        ("GET", "/api/demo/config", None),
        ("GET", "/api/demo/slides", None),
        ("GET", "/api/demo/slides/sld_x/info", None),
        ("GET", "/api/demo/slides/sld_x.dzi", None),
        ("GET", "/api/demo/slides/sld_x_files/0/0_0.jpeg", None),
        ("GET", "/api/demo/ai/session/s1/stream", None),
        ("GET", "/api/demo/ai/session/s1", None),
        ("POST", "/api/demo/ai/run", {"slide_id": "sld_x"}),
    ]
    for method, path, body in checks:
        r = client.open(path, method=method, json=body)
        assert r.status_code == 503, (method, path, r.status_code)
        assert (r.get_json() or {}).get("code") == "pg_backend_required", path


@json_only  # 锁 json 语义：无 CSRF 头也到达视图（PG 下视图正常处理）
def test_demo_post_does_not_require_login_csrf():
    """Demo POST 用 capability 通道：不带登录 CSRF 也应到达视图（json 下 503
    pg_backend_required，而非 400 csrf_required）。"""
    client = app_mod.app.test_client()  # 裸 client：不注入 X-CSRF-Token
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    r = client.post("/api/demo/ai/run", json={"slide_id": "sld_x"})
    assert r.status_code == 503
    assert (r.get_json() or {}).get("code") == "pg_backend_required"


def test_demo_cookie_cannot_call_normal_api():
    """伪造/真实 Demo cookie 调 /api/ai/*、/api/slide/* → 401（capability 只放行
    /api/demo/*，不进入登录 session 语义）。"""
    client = app_mod.app.test_client()
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client.set_cookie("demo_capability", "forged-token", domain="localhost")
    assert client.post("/api/ai/run", json={"slide": "a.svs"}).status_code == 401
    assert client.get("/api/slide/a.svs/info").status_code == 401
    assert client.get("/api/slides").status_code == 401


@json_only  # PG 下由 by-request 反查对账测试覆盖写通道语义
def test_public_demo_env_disables_internal_annotate(monkeypatch):
    """PUBLIC_DEMO 模式（env）：internal 写通道 403（docs §5.4-1）。"""
    _touch("a.svs")
    client = _client()
    tok = app_mod.AI_INTERNAL_TOKEN
    monkeypatch.setenv("PUBLIC_DEMO_ENABLED", "1")
    r = client.post("/internal/ai/annotate",
                    json={"slide": "a.svs", "label": "x", "x": 0, "y": 0,
                          "side_px": 10},
                    headers={"X-AI-Internal-Token": tok})
    assert r.status_code == 403
    assert (r.get_json() or {}).get("code") == "demo_write_channel_disabled"
    # 未开启时不走该闸（继续走参数校验 → 400）
    monkeypatch.setenv("PUBLIC_DEMO_ENABLED", "0")
    r2 = client.post("/internal/ai/annotate",
                     json={"slide": "a.svs", "label": "x", "x": 0, "y": 0,
                           "side_px": 10},
                     headers={"X-AI-Internal-Token": tok})
    assert r2.status_code != 403


# --------------------------------------------------------------------------- #
# PG：capability / config / 目录
# --------------------------------------------------------------------------- #
@pg_only
def test_capability_issuance_and_config():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    assert 'Set-Cookie' in r.headers and "demo_capability=" in r.headers["Set-Cookie"]
    cookie_header = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie_header and "SameSite=Lax" in cookie_header
    body = r.get_data(as_text=True)
    assert "demo.js" in body  # PG+开启：渲染 Viewer
    assert "app-mode.js" in body and "viewer-core.js" in body
    assert 'id="app-header"' in body
    assert 'id="demo-admin-bar"' not in body
    assert 'id="upload-btn"' not in body
    assert 'src="/static/app.js' not in body
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["demo_enabled"] is True
    assert cfg["adapter_mode"] == "plugin-contract"
    assert cfg["ai_available"] is True
    assert cfg["run_state"] == "available"
    assert cfg["task_max_steps"] == budget_store.DEFAULT_DEMO_TASK_MAX_STEPS
    assert cfg["task_max_chars"] == 300
    assert cfg["budget"]["demo_limit"] == budget_store.DEFAULT_DEMO_TURN_LIMIT
    assert cfg["per_browser_limit"] == 1
    assert cfg["per_browser_used"] == 0
    assert cfg["per_browser_remaining"] == 1
    # 未带 capability：slides 端点 401（签发只在 /demo 与 /config）
    client2 = _client()
    assert client2.get("/api/demo/slides").status_code == 401
    # 伪造 cookie → 410
    client2.set_cookie("demo_capability", "not-a-real-token", domain="localhost")
    assert client2.get("/api/demo/slides").status_code == 410


@pg_only
def test_demo_disabled_blocks_anonymous_slides_and_viewer():
    """demo_enabled=false：不渲染 Viewer、不签发 cookie，切片/瓦片 403。"""
    _setup_platform()
    FakeSidecar()._install()
    _catalog_add(_touch("closed.svs"))
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "demo.js" not in body
    assert "demo.closed" in body or "当前未开放" in body
    assert "demo_capability=" not in (r.headers.get("Set-Cookie") or "")
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["demo_enabled"] is False
    assert client.get("/api/demo/slides").status_code == 403
    assert (client.get("/api/demo/slides").get_json() or {}).get("code") == \
        "demo_disabled"


@pg_only
def test_demo_slides_listing_and_catalog_outside_rejected(monkeypatch):
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    inside = _catalog_add(_touch("in-demo.svs"))
    outside_name = _touch("private.svs")
    share_store.set_slide_meta(outside_name)  # 有 slides 行但不在目录
    outside_id = share_store.get_slide_id(outside_name)
    client = _client()
    client.get("/api/demo/config")
    data = client.get("/api/demo/slides").get_json()
    ids = [s["slide_id"] for s in data["slides"]]
    assert ids == [inside]
    # catalog 外 / 未知 slide_id：info / dzi / tile 一律 404
    for sid in (outside_id, "sld_unknown"):
        assert client.get("/api/demo/slides/%s/info" % sid).status_code == 404
        assert client.get("/api/demo/slides/%s.dzi" % sid).status_code == 404
        assert client.get(
            "/api/demo/slides/%s_files/0/0_0.jpeg" % sid).status_code == 404
    # 目录内 dzi：瓦片 URL 指向 demo 端点（同样过 allowlist）
    class _FakeCtx:
        def __init__(self, value):
            self._v = value

        def __enter__(self):
            return self._v

        def __exit__(self, *exc):
            return False

    fake_dz = type("DZ", (), {"level_dimensions": [(100, 100)]})()
    monkeypatch.setattr(app_mod.slide_cache, "borrow_pair",
                        lambda entry: _FakeCtx({"dz": fake_dz}))
    xml = client.get("/api/demo/slides/%s.dzi" % inside).get_data(as_text=True)
    assert 'Url="/api/demo/slides/%s_files/"' % inside in xml
    # 目录内 tile：命中缓存路径（monkeypatch 免开真实切片）
    monkeypatch.setattr(app_mod, "_tile_cache_get", lambda key: b"JPEGBYTES")
    tr = client.get("/api/demo/slides/%s_files/0/0_0.jpeg" % inside)
    assert tr.status_code == 200 and tr.data == b"JPEGBYTES"


@pg_only
def test_demo_cookie_cannot_read_normal_slides_pg():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("private2.svs")
    client = _client()
    client.get("/api/demo/config")
    assert client.get("/api/slide/%s/info" % name).status_code == 401
    assert client.get("/api/slides").status_code == 401


# --------------------------------------------------------------------------- #
# PG：/api/demo/ai/run 全链路
# --------------------------------------------------------------------------- #
@pg_only
def test_demo_run_full_flow_and_security_envelope():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run1.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    r = client.post("/api/demo/ai/run",
                    json={"slide_id": slide_id, "task": "看一眼",
                          "request_id": rid})
    assert r.status_code == 200
    assert r.headers.get("X-AI-Session-ID") == "sess-demo-1"
    runs = fake.calls_of("POST", "/run")
    assert len(runs) == 1
    body = runs[0]["body"]
    # security envelope（docs §5.4）：只读 profile / ephemeral / TTL / 同 request_id
    sec = body["security"]
    assert sec["security_contract_version"] == "1.0"
    assert sec["tool_profile"] == "demo-readonly-v1"
    assert sorted(sec["required_features"]) == sorted([
        "tool-profile:demo-readonly-v1", "session:ephemeral-v1",
        "session-ttl:v1"])
    assert sec["session_ttl_seconds"] == 86400
    assert sec["request_id"] == rid == body["request_id"]
    assert "create_annotation" not in json.dumps(body)
    cfg = body["config"]
    assert cfg["max_steps"] == budget_store.DEFAULT_DEMO_TASK_MAX_STEPS == 20
    # session_owner = "demo_" + token_hash 前 16 位（不可反推、非 IP/明文）
    assert cfg["session_owner"].startswith("demo_")
    assert len(cfg["session_owner"]) == len("demo_") + 16
    assert "run_grant" not in cfg  # 只读 run 不发 grant（docs §5.4-5）
    # consume：run_state=consumed + 双记账
    after = client.get("/api/demo/config").get_json()
    assert after["run_state"] == "consumed"
    assert after["histopilot_session_id"] == "sess-demo-1"
    assert after["per_browser_used"] == 1
    assert after["per_browser_remaining"] == 0
    assert _demo_usage_total() == 1
    assert _platform_usage_total() == 1
    # 再跑一次（新动作）→ 409 已使用
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 409
    assert (r2.get_json() or {}).get("code") == "demo_run_already_used"
    assert _demo_usage_total() == 1  # 未双扣


@pg_only
def test_demo_run_double_click_only_one_succeeds():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run2.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    # 双击：同 request_id 两次（第二次 CAS 冲突 → 409；预算只预占一次）
    r1 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    r2 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert {r1.status_code, r2.status_code} == {200, 409}
    assert len(fake.calls_of("POST", "/run")) == 1
    assert _demo_usage_total() == 1


@pg_only
def test_demo_run_retry_same_request_id_no_double_charge():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run3.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    # 第一次：HistoPilot 400（模拟未知/缺失 security 拒绝）→ 全额回滚
    fake.on("POST", "/run", lambda p, b, q, h: _json_err(
        400, "security_envelope_invalid"))
    r1 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert r1.status_code == 400
    assert client.get("/api/demo/config").get_json()["run_state"] == "available"
    report = budget_store.usage_report()
    assert report["demo"]["reserved"] == 0 and report["demo"]["accepted"] == 0
    # 同 request_id 重试成功 → 只计 1 次
    fake.on("POST", "/run", lambda p, b, q, h: _sse_ok("sess-retry"))
    r2 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert r2.status_code == 200
    assert _demo_usage_total() == 1
    assert client.get("/api/demo/config").get_json()["run_state"] == "consumed"


@pg_only
def test_demo_run_rejected_releases_reservation():
    """HistoPilot 4xx（非 security 场景）同样 release：run 可再来、预算归零。"""
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run4.svs"))
    client = _client()
    client.get("/api/demo/config")
    fake.on("POST", "/run", lambda p, b, q, h: _json_err(409, "session_conflict"))
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 409
    assert client.get("/api/demo/config").get_json()["run_state"] == "available"
    assert _demo_usage_total() == 0


@pg_only
def test_demo_sse_reconnect_no_extra_charge():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run5.svs"))
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    assert _demo_usage_total() == 1
    fake.on("GET", "/session/", lambda p, b, q, h: FakeResponse(
        200, "id: 2\nevent: agent_finished\ndata: {}\n\n",
        ctype="text/event-stream"), prefix=True)
    for after_seq in ("0", "1"):
        r = client.get("/api/demo/ai/session/sess-demo-1/stream?after_seq=%s"
                       % after_seq)
        assert r.status_code == 200
    assert _demo_usage_total() == 1  # 重连不扣额度
    # 重挂只读：没有再 POST /run
    assert len(fake.calls_of("POST", "/run")) == 1
    # 读别人的 session id → 403（不泄露存在性）
    assert client.get(
        "/api/demo/ai/session/sess-other/stream").status_code == 403


@pg_only
def test_capability_expired_stream_410():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run6.svs"))
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE demo_sessions SET expires_at="
                        " now() - interval '1 hour'")
        conn.commit()
    finally:
        conn.close()
    r = client.get("/api/demo/ai/session/sess-demo-1/stream")
    assert r.status_code == 410
    assert (r.get_json() or {}).get("code") == "capability_expired"


@pg_only
def test_slide_removed_from_catalog_revokes_capability():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("run7.svs")
    slide_id = _catalog_add(name)
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    owner = _make_user("owner")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    r = admin.delete("/api/admin/demo-catalog?slide=" + name)
    assert r.status_code == 200
    body = r.get_json()
    assert body["expired_capabilities"] >= 1
    # 旧 capability：不能读该 slide（info/dzi 410），stream 410
    assert client.get("/api/demo/slides/%s/info" % slide_id).status_code == 410
    assert client.get(
        "/api/demo/ai/session/sess-demo-1/stream").status_code == 410
    # 目录已空
    client3 = _client()
    client3.get("/api/demo/config")
    assert client3.get("/api/demo/slides").get_json()["slides"] == []


@pg_only
def test_slide_delete_revokes_demo():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("run8.svs")
    slide_id = _catalog_add(name)
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    owner = _make_user("owner")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    assert admin.delete("/api/slide/" + name).status_code == 200
    assert demo_store.catalog_get(slide_id) is None  # 目录条目一并清理
    assert client.get("/api/demo/slides/%s/info" % slide_id).status_code == 410
    assert client.get(
        "/api/demo/ai/session/sess-demo-1/stream").status_code == 410


@pg_only
def test_demo_budget_exhausted_releases_run():
    _enable_demo_period()
    budget_store.update_period_limits({"demo_turn_limit": 1})
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run9.svs"))
    c1 = _fresh_capability(fake)
    assert c1.post("/api/demo/ai/run", json={"slide_id": slide_id}).status_code == 200
    assert _demo_usage_total() == 1  # 1/1 已满
    c2 = _fresh_capability(fake)  # 新浏览器（独立 capability）
    r = c2.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 429
    assert (r.get_json() or {}).get("code") == "demo_budget_exhausted"
    # 超限回滚：新浏览器 run 回 available、未占预算
    assert c2.get("/api/demo/config").get_json()["run_state"] == "available"
    assert _demo_usage_total() == 1
    assert len(fake.calls_of("POST", "/run")) == 1


@pg_only
def test_platform_budget_exhausted():
    _enable_demo_period()
    budget_store.update_period_limits({"platform_turn_limit": 1})
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run10.svs"))
    c1 = _fresh_capability(fake)
    assert c1.post("/api/demo/ai/run", json={"slide_id": slide_id}).status_code == 200
    c2 = _fresh_capability(fake)
    r = c2.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 429
    assert (r.get_json() or {}).get("code") == "platform_ai_budget_exhausted"
    assert c2.get("/api/demo/config").get_json()["run_state"] == "available"


@pg_only
def test_legacy_adapter_and_unreachable_fail_closed():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run11.svs"))
    client = _client()
    client.get("/api/demo/config")
    # legacy adapter → run 拒绝 + config 标记
    fake.adapter = "legacy"
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 503
    assert (r.get_json() or {}).get("code") == "histopilot_legacy_adapter"
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["ai_available"] is False
    assert cfg["ai_unavailable_code"] == "histopilot_legacy_adapter"
    # 探测失败（不可达）→ run 拒绝
    fake.adapter = "plugin-contract"
    fake.unreachable = True
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 503
    assert (r2.get_json() or {}).get("code") == "histopilot_unreachable"
    assert fake.calls_of("POST", "/run") == []  # 未转发
    # 两种拒绝都不扣额度
    assert _demo_usage_total() == 0
    assert client.get("/api/demo/config").get_json()["run_state"] == "available"


@pg_only
def test_task_too_long_rejected():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run12.svs"))
    client = _client()
    client.get("/api/demo/config")
    r = client.post("/api/demo/ai/run",
                    json={"slide_id": slide_id, "task": "字" * 301})
    assert r.status_code == 400
    assert (r.get_json() or {}).get("code") == "task_too_long"
    assert client.get("/api/demo/config").get_json()["run_state"] == "available"


@pg_only
def test_run_disabled_when_demo_off():
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run13.svs"))
    client = _client()
    client.get("/api/demo/config")
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 403
    assert (r.get_json() or {}).get("code") == "demo_disabled"


# --------------------------------------------------------------------------- #
# PG：确认式对账（by-request 反查 → consume / release / 顺延）
# --------------------------------------------------------------------------- #
def _reserve_pending_run(slide_id):
    """模拟 worker 崩溃：store 层直接预占（不触发 HistoPilot），返回
    (capability_dict, request_id)。"""
    cap = demo_store.create_capability(
        "dcp_" + uuid.uuid4().hex[:8], "hash_" + uuid.uuid4().hex[:12])
    rid = "req_" + uuid.uuid4().hex[:12]
    run = demo_store.reserve_run(cap["id"], rid, slide_id, "rev-stub")
    assert run is not None
    budget_store.reserve_turn(rid, "demo", cap["id"], "platform")
    return cap, rid


def _expire_everything():
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE demo_sessions SET "
                        "reservation_expires_at = now() - interval '1 hour' "
                        "WHERE run_state='reserved'")
            cur.execute("UPDATE ai_budget_reservations SET "
                        "reservation_expires_at = now() - interval '1 hour' "
                        "WHERE state='reserved'")
        conn.commit()
    finally:
        conn.close()


@pg_only
def test_reconcile_found_consumes_missing_releases_unavailable_extends():
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("rec1.svs"))
    verdicts = {}

    def by_request_handler(path, body, params, headers):
        rid = path[len("/session/by-request/"):]
        v = verdicts.get(rid, "missing")
        if v == "found":
            return FakeResponse(200, {"session": {
                "id": "sess-rec-" + rid[-4:],
                "security_profile_applied": True,
                "accepted_at": time.time(),
            }})
        if v == "missing":
            return FakeResponse(404, {"error": "该 request_id 没有对应会话",
                                      "code": "not_found"})
        return FakeResponse(500, {"error": "store unavailable",
                                  "code": "store_unavailable"})

    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/", by_request_handler, prefix=True)
    fake._install()

    # a) 200 → consume（防误退款）
    cap_a, rid_a = _reserve_pending_run(slide_id)
    verdicts[rid_a] = "found"
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    assert {e["action"] for e in summary["demo"] if e["id"] == cap_a["id"]} == \
        {"consumed"}
    assert demo_store.get_session(cap_a["id"])["run_state"] == "consumed"
    assert budget_store.get_reservation(rid_a)["state"] == "consumed"

    # b) 404 not_found → release（run 回 available、预算 released）
    cap_b, rid_b = _reserve_pending_run(slide_id)
    verdicts[rid_b] = "missing"
    _expire_everything()
    app_mod.reconcile_expired_reservations()
    assert demo_store.get_session(cap_b["id"])["run_state"] == "available"
    assert budget_store.get_reservation(rid_b)["state"] == "released"

    # c) 5xx → 不释放，顺延（仍 reserved，且过期时间被推回未来）
    cap_c, rid_c = _reserve_pending_run(slide_id)
    verdicts[rid_c] = "unavailable"
    _expire_everything()
    app_mod.reconcile_expired_reservations()
    row = demo_store.get_session(cap_c["id"])
    assert row["run_state"] == "reserved"
    assert row["reservation_expires_at"] > time.time()
    resv = budget_store.get_reservation(rid_c)
    assert resv["state"] == "reserved"
    assert resv["reservation_expires_at"] > time.time()
    # 顺延后的项下一轮对账不再被当作过期（除非再次到期）
    summary2 = app_mod.reconcile_expired_reservations()
    assert not [e for e in summary2["demo"] if e["id"] == cap_c["id"]]


@pg_only
def test_reconcile_consume_failed_extends_instead_of_blind_release(monkeypatch):
    """HistoPilot 已接受但 consume 失败：顺延，不得留给盲回收退款。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-fail.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-kept",
                                  "security_profile_applied": True,
                                  "accepted_at": 1}}), prefix=True)
    fake._install()
    cap, rid = _reserve_pending_run(slide_id)
    _expire_everything()

    def _boom(*_a, **_k):
        raise RuntimeError("consume exploded")

    monkeypatch.setattr(budget_store, "consume", _boom)
    monkeypatch.setattr(demo_store, "consume_run", _boom)
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"] if e["id"] == cap["id"]}
    budget_actions = {e["action"] for e in summary["budget"] if e["request_id"] == rid}
    assert demo_actions == {"consume_failed_extended"}
    assert budget_actions == {"consume_failed_extended"}
    assert demo_store.get_session(cap["id"])["run_state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    assert budget_store.get_reservation(rid)["reservation_expires_at"] > time.time()
    # 后台线程不得再盲 reclaim（源码守卫）
    loop_src = inspect.getsource(app_mod._start_budget_reclaim_thread)
    assert "reclaim_expired_reservations()" not in loop_src.split("def _loop")[1]


@pg_only
def test_reconcile_found_without_accepted_extends():
    """session 已创建但尚未接受（安全确认前崩溃）→ 顺延，不得 consume/release。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-pending.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-pending",
                                  "security_profile_applied": False,
                                  "accepted_at": None}}), prefix=True)
    fake._install()
    cap, rid = _reserve_pending_run(slide_id)
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"] if e["id"] == cap["id"]}
    budget_actions = {e["action"] for e in summary["budget"] if e["request_id"] == rid}
    assert demo_actions == {"pending_extended"}
    assert budget_actions == {"pending_extended"}
    assert demo_store.get_session(cap["id"])["run_state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    assert budget_store.get_reservation(rid)["reservation_expires_at"] > time.time()


@pg_only
def test_reconcile_abandoned_releases():
    """启动恢复标记 abandoned 的未接受动作：对账释放，不得 consume/顺延。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-abandon.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-abandon",
                                  "security_profile_applied": False,
                                  "accepted_at": None,
                                  "abandoned": True,
                                  "abandoned_at": time.time()}}), prefix=True)
    fake._install()
    cap, rid = _reserve_pending_run(slide_id)
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"] if e["id"] == cap["id"]}
    budget_actions = {e["action"] for e in summary["budget"] if e["request_id"] == rid}
    assert demo_actions == {"released"}
    assert budget_actions == {"released"}
    assert demo_store.get_session(cap["id"])["run_state"] == "available"
    assert budget_store.get_reservation(rid)["state"] == "released"


@pg_only
def test_reconcile_abandoned_stale_attempt_does_not_release_newer_try():
    """abandoned 确认退款后重新预占才换代；旧 attempt 对账不得退新尝试。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-toc.svs"))
    cap, rid = _reserve_pending_run(slide_id)
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {
                    "id": "sess-toc",
                    "security_profile_applied": False,
                    "accepted_at": None,
                    "abandoned": True,
                    "abandoned_at": time.time(),
                }}), prefix=True)
    fake._install()
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"] if e["id"] == cap["id"]}
    assert demo_actions == {"released"}
    assert demo_store.get_session(cap["id"])["run_state"] == "available"
    assert budget_store.get_reservation(rid)["state"] == "released"
    # 确认放弃后显式换代
    again_demo = demo_store.reserve_run(cap["id"], rid, slide_id, "rev-stub")
    again_budget = budget_store.reserve_turn(rid, "demo", cap["id"], "platform")
    assert again_demo["attempt"] == 2
    assert again_budget["attempt"] == 2
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run(cap["id"], expected_attempt=1, expected_request_id=rid)
    with pytest.raises(budget_store.ReservationAttemptConflict):
        budget_store.release(rid, expected_attempt=1)
    assert demo_store.get_session(cap["id"])["run_state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"


@pg_only
def test_catalog_remove_reconciles_reservations_not_blind_release():
    """目录撤销：found+已接受 → consume；missing → release；未接受 → 顺延。"""
    _setup_platform()
    budget_store.update_period_limits({"demo_max_concurrency": 8})
    slide_id = _catalog_add(_touch("rev-cat.svs"))
    cap_ok, rid_ok = _reserve_pending_run(slide_id)
    cap_miss, rid_miss = _reserve_pending_run(slide_id)
    cap_pend, rid_pend = _reserve_pending_run(slide_id)
    verdicts = {
        rid_ok: "accepted",
        rid_miss: "missing",
        rid_pend: "pending",
    }

    def by_request_handler(path, body, params, headers):
        rid = path[len("/session/by-request/"):]
        v = verdicts.get(rid, "missing")
        if v == "accepted":
            return FakeResponse(200, {"session": {
                "id": "sess-ok", "security_profile_applied": True,
                "accepted_at": time.time()}})
        if v == "pending":
            return FakeResponse(200, {"session": {
                "id": "sess-pend", "security_profile_applied": False}})
        return FakeResponse(404, {"error": "该 request_id 没有对应会话",
                                  "code": "not_found"})

    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/", by_request_handler, prefix=True)
    fake._install()
    revoke = demo_store.revoke_by_slide(slide_id)
    released = app_mod._release_budget_for_terminated_runs(
        revoke.get("terminated_runs"))
    assert rid_miss in released
    assert rid_ok not in released
    assert rid_pend not in released
    assert budget_store.get_reservation(rid_ok)["state"] == "consumed"
    assert budget_store.get_reservation(rid_miss)["state"] == "released"
    assert budget_store.get_reservation(rid_pend)["state"] == "reserved"
    assert demo_store.get_session(cap_ok["id"])["run_state"] == "consumed"
    assert demo_store.get_session(cap_miss["id"])["run_state"] == "available"
    assert demo_store.get_session(cap_pend["id"])["run_state"] == "reserved"


@pg_only
def test_demo_per_browser_limit_two_runs_via_ui_config():
    """demo_per_browser_limit=2：第一次 consumed 后 config 仍有剩余，可再跑。"""
    _enable_demo_period()
    budget_store.update_period_limits({"demo_per_browser_limit": 2})
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("twice.svs"))
    client = _client()
    client.get("/api/demo/config")
    r1 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r1.status_code == 200, r1.get_json()
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["run_state"] == "consumed"
    assert cfg["per_browser_limit"] == 2
    assert cfg["per_browser_used"] == 1
    assert cfg["per_browser_remaining"] == 1
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 200, r2.get_json()
    cfg2 = client.get("/api/demo/config").get_json()
    assert cfg2["per_browser_used"] == 2
    assert cfg2["per_browser_remaining"] == 0
    r3 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r3.status_code == 409
    assert (r3.get_json() or {}).get("code") == "demo_run_already_used"


@pg_only
def test_demo_ip_run_limit_blocks_cookie_rotation(monkeypatch):
    """清 cookie 换 capability 不能从同一 IP 前缀耗尽 Demo 子额度。"""
    monkeypatch.setenv("DEMO_IP_RUN_LIMIT", "1")
    _enable_demo_period()
    budget_store.update_period_limits({"demo_turn_limit": 5})
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("ip-run.svs"))
    c1 = _fresh_capability(fake)
    r1 = c1.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.113.10"})
    assert r1.status_code == 200, r1.get_json()
    c2 = _fresh_capability(fake)
    r2 = c2.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.113.200"})
    assert r2.status_code == 429, r2.get_json()
    body = r2.get_json() or {}
    assert body.get("code") == "demo_ip_rate_limited"
    assert r2.headers.get("Retry-After")
    assert c2.get("/api/demo/config").get_json()["run_state"] == "available"
    assert _demo_usage_total() == 1
    assert len(fake.calls_of("POST", "/run")) == 1
    # 不同 /24 不受该桶限制
    c3 = _fresh_capability(fake)
    r3 = c3.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.114.1"})
    assert r3.status_code == 200, r3.get_json()
    # 同 request_id 重放不因已计数被挡
    rid = "req_replay_ip"
    c4 = _fresh_capability(fake)
    r4 = c4.post("/api/demo/ai/run",
                 json={"slide_id": slide_id, "request_id": rid},
                 environ_base={"REMOTE_ADDR": "198.51.100.9"})
    assert r4.status_code == 200, r4.get_json()
    r4b = c4.post("/api/demo/ai/run",
                  json={"slide_id": slide_id, "request_id": rid},
                  environ_base={"REMOTE_ADDR": "198.51.100.9"})
    # 同 ID 重放跳过 IP 闸；capability 已 consumed 故 reserve 返回 409，不得 429
    assert r4b.status_code == 409, r4b.get_json()
    assert (r4b.get_json() or {}).get("code") == "demo_run_already_used"


# --------------------------------------------------------------------------- #
# PG：owner demo-catalog 管理
# --------------------------------------------------------------------------- #
@pg_only
def test_admin_demo_catalog_crud_and_access_control():
    _setup_platform()
    FakeSidecar()._install()
    owner = _make_user("owner")
    user = _make_user("user")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    # 空
    assert admin.get("/api/admin/demo-catalog").get_json()["slides"] == []
    # user 越权 403；匿名 401（走 /api/ 认证闸）
    uc = _client()
    _login(uc, "user", user["user_id"])
    assert uc.get("/api/admin/demo-catalog").status_code == 403
    assert _client().get("/api/admin/demo-catalog").status_code == 401
    # PUT：不存在文件 404；正常加入
    assert admin.put("/api/admin/demo-catalog",
                     json={"slide": "ghost.svs"}).status_code == 404
    name = _touch("cat1.svs")
    r = admin.put("/api/admin/demo-catalog", json={
        "slide": name, "display_name": "教学示例", "sort_order": 1,
        "is_default": True})
    assert r.status_code == 200
    entry = r.get_json()
    assert entry["display_name"] == "教学示例" and entry["is_default"] is True
    assert entry["slide_id"].startswith("sld_")
    # 列表可见
    listed = admin.get("/api/admin/demo-catalog").get_json()["slides"]
    assert [s["name"] for s in listed] == [name]
    # DELETE：不存在的 404；正常移除（含 revoke 联动）
    assert admin.delete(
        "/api/admin/demo-catalog?slide=ghost2.svs").status_code == 404
    r2 = admin.delete("/api/admin/demo-catalog?slide=" + name)
    assert r2.status_code == 200
    assert admin.get("/api/admin/demo-catalog").get_json()["slides"] == []


@pg_only
def test_public_demo_env_json_refuses_startup(monkeypatch):
    """（已有启动闸回归）PUBLIC_DEMO_ENABLED=1 + 非 PG → SystemExit。"""
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "json")
    with pytest.raises(SystemExit):
        app_mod._check_public_demo_backend_or_exit(
            {"PUBLIC_DEMO_ENABLED": "1"})


def test_demo_js_event_reset_uses_snapshot_not_second_stream():
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    assert "rebuildFromSnapshot" in text
    # C2 起 demo.js 硬依赖 HP_API（app-mode.js demoAdapter）：
    # 会话/流端点 URL 在 adapter 侧，demo.js 只经 demoApi() 调用
    assert "demoApi().aiSession" in text
    mode_js = (Path(__file__).resolve().parent.parent / "static" / "app-mode.js") \
        .read_text(encoding="utf-8")
    assert "/api/demo/ai/session/" in mode_js
    assert "demo.ai.reset.fail" in text
    # event_reset 分支必须走 snapshot，不得再开 after_seq=0 的第二条流
    idx = text.find('type === "event_reset"')
    assert idx >= 0
    chunk = text[idx:idx + 800]
    assert "rebuildFromSnapshot" in chunk
    assert "reconnectStream" not in chunk
    assert "pendingEvents" in text
    assert "rebuilding" in text
    # 重建期间必须缓存原流，snapshot 完成后再回放
    assert "state.pendingEvents.push" in text
    assert "state.rebuilding = false" in text
    # 管理员用量栏已移出 Demo 产品界面（诊断在正式版 AI 预算区）
    assert "initAdminBar" not in text
    assert "/api/admin/settings/ai-budget/reset" not in text
    # 每浏览器多次配额：consumed 且仍有剩余时不得永久禁用按钮
    assert "per_browser_remaining" in text
    assert "remaining <= 0" in text
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.ai.run.available.n":') == 2
    assert i18n.count('"demo.ai.run.ip.limited":') == 2
    assert "demo_ip_rate_limited" in text


def test_demo_html_owner_admin_reset_bar_not_in_product_ui():
    """黑色 Admin 用量栏不得进入普通 Demo；owner 诊断留在正式版 AI 预算区。"""
    root = Path(__file__).resolve().parent.parent
    demo = (root / "templates" / "demo.html").read_text(encoding="utf-8")
    shell = (root / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    assert 'id="demo-admin-bar"' not in demo
    assert 'id="demo-admin-reset"' not in demo
    assert 'id="demo-admin-usage"' not in demo
    assert 'id="demo-admin-bar"' not in shell
    assert 'id="aibudget-reset-btn"' in shell


def test_demo_uses_shared_shell_not_separate_product():
    """Demo 是 demo/readonly 运行模式：共享外壳 + adapter，不加载正式 app.js。"""
    root = Path(__file__).resolve().parent.parent
    demo = (root / "templates" / "demo.html").read_text(encoding="utf-8")
    index = (root / "templates" / "index.html").read_text(encoding="utf-8")
    shell = (root / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    mode_js = (root / "static" / "app-mode.js").read_text(encoding="utf-8")
    viewer_js = (root / "static" / "viewer-core.js").read_text(encoding="utf-8")
    assert '{% include "_app_shell.html" %}' in demo
    assert '{% include "_app_shell.html" %}' in index
    assert 'src="/static/app-mode.js' in demo
    assert 'src="/static/viewer-core.js' in demo
    assert 'src="/static/demo.js' in demo
    assert 'src="/static/app.js' not in demo
    assert 'id="app-header"' in shell
    assert 'data-i18n="demo.badge"' in shell
    assert 'id="demo-quota-chip"' in shell
    assert "HP_API" in mode_js and "/api/demo/" in mode_js
    assert "HP_ViewerCore" in viewer_js
    caps = app_mod._app_capabilities("demo")
    assert caps["mode"] == "demo"
    assert caps["upload"] is False
    assert caps["annotate"] is False
    assert caps["share"] is False
    assert caps["ai_config"] is False
    assert caps["ai_branch"] is False
    assert caps["readonly_badge"] is True
    assert caps["login_cta"] is True
    official = app_mod._app_capabilities("official")
    assert official["upload"] is True
    assert official["readonly_badge"] is False


def test_demo_available_template_hides_write_ops():
    """服务端按 capabilities 不渲染写入口（不只靠 CSS 隐藏）。"""
    from flask import render_template
    with app_mod.app.app_context():
        html = render_template(
            "demo.html",
            demo_available=True,
            demo_enabled=True,
            app_mode="demo",
            capabilities=app_mod._app_capabilities("demo"),
            histopilot_ui_enabled=False,
            adapter_mode="plugin-contract",
        )
    assert 'id="app-header"' in html
    assert 'data-i18n="demo.badge"' in html
    assert 'id="demo-quota-chip"' in html
    assert 'id="ai-run-btn"' in html
    assert 'id="ai-config-wrap"' not in html
    assert 'id="ai-continue-btn"' not in html
    assert 'id="upload-btn"' not in html
    assert 'id="save-anno-btn"' not in html
    assert 'id="share-create-btn"' not in html
    assert 'id="demo-admin-bar"' not in html
    assert 'src="/static/demo.js' in html
    assert 'src="/static/app.js' not in html
    assert "/plugins/histopilot/" not in html


def test_demo_landing_login_cta_switches_for_logged_in_users(monkeypatch):
    """/demo 已登录用户 CTA 切换「打开完整版」，匿名保持登录/注册（docs B2）。

    仅模板渲染读登录态：/api/demo/* 面不读 identity 的 capability 设计不变。
    """
    # 路由层：logged_in 来自 session["auth_user"]
    captured = {}
    real_render = app_mod.render_template

    def fake_render(template, **ctx):
        captured.update(ctx)
        return real_render(template, **ctx)

    monkeypatch.setattr(app_mod, "render_template", fake_render)
    client = _client()
    client.get("/demo")
    assert captured.get("logged_in") is False
    with client.session_transaction() as sess:
        sess["auth_user"] = "u@x.com"
    client.get("/demo")
    assert captured.get("logged_in") is True

    # 模板层：login_cta 分支按 logged_in 切换 CTA
    from flask import render_template
    caps = app_mod._app_capabilities("demo")
    common = dict(
        demo_available=True, demo_enabled=True, app_mode="demo",
        capabilities=caps, histopilot_ui_enabled=False,
        adapter_mode="plugin-contract",
    )
    with app_mod.app.app_context():
        anon = render_template("demo.html", logged_in=False, **common)
        logged = render_template("demo.html", logged_in=True, **common)
    assert 'data-i18n="demo.login"' in anon
    assert 'data-i18n="demo.register"' in anon
    assert 'data-i18n="demo.open.full"' not in anon
    assert 'data-i18n="demo.open.full"' in logged
    assert 'href="/"' in logged
    assert 'data-i18n="demo.login"' not in logged
    # i18n 双语键齐全
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.open.full":') == 2


def test_official_template_keeps_write_ops_and_budget_diag():
    from flask import render_template
    with app_mod.app.app_context():
        html = render_template(
            "index.html",
            app_mode="official",
            capabilities=app_mod._app_capabilities("official"),
            histopilot_ui_enabled=True,
            sample_plugin_enabled=False,
            sample_plugin_permissions=[],
        )
    assert 'id="upload-btn"' in html
    assert 'id="ai-config-wrap"' in html
    assert 'id="aibudget-reset-btn"' in html
    assert 'src="/static/app.js' in html
    assert 'data-i18n="demo.badge"' not in html


def test_demo_js_finish_run_does_not_auto_reconnect_terminal_session():
    """终态 finishRun 刷新 config 时禁止自动重连，避免 agent_finished 循环。"""
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    idx = text.find("function finishRun")
    assert idx >= 0
    chunk = text[idx:idx + 350]
    assert "loadConfig({ restore: false })" in chunk
    assert "closeActiveStream" in chunk
    assert "reconnectStream" not in chunk
    # 页面恢复与配额刷新分流
    assert "loadConfig({ restore: true })" in text
    assert "sessionAttached" in text
    assert "opts.restore" in text or "opts && opts.restore" in text


def test_demo_js_text_delta_and_paused_are_terminal():
    """Demo 必须展示 text_delta，且 agent_paused 结束本轮不得重连。"""
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    assert 'type === "text_delta"' in text
    assert "appendLiveText" in text
    idx = text.find('type === "agent_paused"')
    assert idx >= 0
    chunk = text[idx:idx + 700]
    assert "state.terminal = true" in chunk
    assert "finishRun()" in chunk
    assert "reconnectStream" not in chunk
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.ai.ended":') == 2
    assert i18n.count('"demo.ai.snapshot":') == 2
