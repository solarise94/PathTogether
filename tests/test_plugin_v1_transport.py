# -*- coding: utf-8 -*-
"""Stage 4-2：二进制 region transport + 像素预算 + 速率限制测试。

覆盖（仅作用于 /api/plugin/v1 通道，/internal/ai/* 与主站不受影响）：
  - 内容协商：Accept: application/octet-stream（与 ?format=binary）→ raw JPEG
    bytes + 元数据头；缺省（无 Accept）保持 JSON base64 兼容；
  - 二进制头齐全（Content-SHA256/X-Asset-Revision/X-Region-Bbox/X-Region-Out/
    X-Region-Magnification/X-Region-Encoder）；二进制 body 与 JSON base64 解码
    后字节逐字节一致（同参数两请求 sha256 相同）；
  - 像素预算闸 1（单请求上限）：超限 → 429 rate_limited（retryable=true）+
    Retry-After，且**在读盘/解码前拒绝**（slide_cache.borrow_pair /
    _read_region_b64 未被调用）；
  - 像素预算闸 2（滑窗预算）：耗尽 → 429 + Retry-After；
  - 并发闸（进程级信号量）：超载 → 429，且在读盘前拒绝；
  - 速率限制（v1 全能力端点 token bucket）：超限 → 429 + Retry-After；
  - 红线：AUTH_ENABLED=False 内网模式 + legacy /internal/ai/region 通道零影响
    （即便插件像素预算耗尽，legacy 通道仍正常出 base64）。

json / pg 双后端通用（RUN_PG_TESTS=1 时 conftest 已切 postgres）。
运行：cd 项目根 && python3 -m pytest tests/test_plugin_v1_transport.py -q
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
import share_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# 隔离 + 引导（同 test_plugin_api 的形态）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False（内网 json 模式不变量）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # 每用例重置三道闸门为全新实例（小预算由具体用例再覆盖），避免跨用例状态
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW", app_mod._SlidingPixelWindow(
        app_mod._PLUGIN_REGION_PIXEL_BUDGET_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER", app_mod._PluginRateLimiter(
        app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_CONCURRENCY_SEM",
                        threading.BoundedSemaphore(app_mod._PLUGIN_REGION_MAX_CONCURRENT))
    yield


def _bootstrap():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _file_secret():
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _token_for(inst):
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"], "secret": _file_secret()})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


_FAKE_ENTRY = {"pool": None, "sem": None}


def _slide_read_mocks(osr=None, mpp=0.5):
    """slide 读路径 mock 栈（dimensions + mpp），返回 (get_slide, borrow, metadata)。"""
    pair = {"osr": osr or mock.Mock(dimensions=(1000, 2000))}
    return (mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY),
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)),
            mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": mpp}))


def _fake_region(jpeg_bytes, width=1568, height=1568):
    return {
        "image_base64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "mime": "image/jpeg", "width": width, "height": height,
        "src": {"x": 0, "y": 0, "w": 100, "h": 100}, "magnification": 20.0,
        "read_level": 1,  # W0 契约：实际解码金字塔层（mock 与真实返回同形）
    }


def _assert_envelope(r, status, code, retryable=None):
    assert r.status_code == status, "got %s body=%r" % (r.status_code, r.get_data(as_text=True))
    body = r.get_json() or {}
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert err["code"] == code, "code=%r full=%r" % (err.get("code"), err)
    assert isinstance(err["retryable"], bool)
    if retryable is not None:
        assert err["retryable"] is retryable
    return err


# --------------------------------------------------------------------------- #
# 1. 二进制 region transport —— 内容协商
# --------------------------------------------------------------------------- #
def test_region_binary_via_accept_header():
    inst = _bootstrap()
    slide = _touch_slide()
    jpeg = b"\xff\xd8binary-jpeg-payload-octet\xff\xd9"
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers={**_bearer(_token_for(inst)), "Accept": "application/octet-stream"},
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"] == "application/octet-stream"
    # body 即 raw JPEG bytes
    assert r.data == jpeg
    sha = hashlib.sha256(jpeg).hexdigest()
    assert r.headers["Content-SHA256"] == sha
    # 元数据头齐全
    assert r.headers["X-Asset-Revision"]
    assert json.loads(r.headers["X-Region-Bbox"]) == {"x": 0, "y": 0, "w": 100, "h": 100}
    assert json.loads(r.headers["X-Region-Out"]) == {"outW": 1568, "outH": 1568}
    assert json.loads(r.headers["X-Region-Magnification"]) == 20.0
    # W0 契约：二进制路径以响应头回传实际解码层（int）
    assert json.loads(r.headers["X-Region-Read-Level"]) == 1
    enc = json.loads(r.headers["X-Region-Encoder"])
    assert enc["id"] == "pillow" and enc["resize"] == "LANCZOS" and enc["jpeg_quality"] == 85


def test_region_binary_via_query_format():
    inst = _bootstrap()
    slide = _touch_slide()
    jpeg = b"\xff\xd8binary-via-query\xff\xd9"
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions?format=binary" % slide,
            headers=_bearer(_token_for(inst)),
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert r.data == jpeg


def test_region_default_no_accept_is_json_base64():
    inst = _bootstrap()
    slide = _touch_slide()
    jpeg = b"\xff\xd8json-default-payload\xff\xd9"
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers=_bearer(_token_for(inst)),  # 无 Accept
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    assert r.status_code == 200, r.get_json()
    assert r.headers["Content-Type"] == "application/json"
    body = r.get_json()
    assert "image_base64" in body
    assert base64.b64decode(body["image_base64"]) == jpeg
    # W0 契约：JSON 路径携带 read_level 且为 int
    assert isinstance(body["read_level"], int) and body["read_level"] == 1


def test_region_binary_bytes_identical_to_json_base64():
    """同参数：二进制 body 与 JSON base64 解码后字节一致（Content-SHA256 相同）。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    jpeg = b"\xff\xd8parity-payload-12345\xff\xd9"
    expected_sha = hashlib.sha256(jpeg).hexdigest()

    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        rb = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers={**_bearer(token), "Accept": "application/octet-stream"},
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    assert rb.status_code == 200
    assert rb.headers["Content-SHA256"] == expected_sha

    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        rj = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers=_bearer(token),
            json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    assert rj.status_code == 200

    binary_sha = hashlib.sha256(rb.data).hexdigest()
    json_sha = hashlib.sha256(base64.b64decode(rj.get_json()["image_base64"])).hexdigest()
    assert binary_sha == json_sha == expected_sha


# --------------------------------------------------------------------------- #
# 2. 像素预算闸 1：单请求上限（必须在读盘/解码前拒绝）
# --------------------------------------------------------------------------- #
def test_region_single_request_pixel_limit_429_before_disk(monkeypatch):
    inst = _bootstrap()
    slide = _touch_slide()
    # 单请求上限压到极小：bbox 10×10、out 100×100 → pixels=max(100,10000)=10000
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 1000)

    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    read_region = mock.patch.object(app_mod, "_read_region_b64")
    get_slide = mock.patch.object(app_mod, "_get_slide")
    with get_slide as g, borrow as b, read_region as rr:
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers=_bearer(_token_for(inst)),
            json={"x": 0, "y": 0, "w": 10, "h": 10, "out_w": 100, "out_h": 100})
    err = _assert_envelope(r, 429, "rate_limited", retryable=True)
    assert err["details"]["reason"] == "single_request_pixels"
    assert "Retry-After" in r.headers and int(r.headers["Retry-After"]) >= 1
    # 读盘/解码路径零调用（slide_cache.borrow_pair / _read_region_b64 / _get_slide）
    assert b.call_count == 0
    assert rr.call_count == 0
    assert g.call_count == 0


# --------------------------------------------------------------------------- #
# 3. 像素预算闸 2：滑窗预算耗尽
# --------------------------------------------------------------------------- #
def test_region_pixel_window_budget_exhausted_429(monkeypatch):
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    # pixels=max(100,10000)=10000；预算 15000 → 第 1 次计入(ok)，第 2 次超限
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW", app_mod._SlidingPixelWindow(15000))
    # 单请求上限放大，避免误触闸 1
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 10 ** 9)

    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(b"x")):
        r1 = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide, headers=_bearer(token),
            json={"x": 0, "y": 0, "w": 10, "h": 10, "out_w": 100, "out_h": 100})
    assert r1.status_code == 200, r1.get_data(as_text=True)

    # 第 2 次：窗口已计 10000，再计 10000 → 20000 > 15000 → 429（读盘前）
    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    with borrow as b:
        r2 = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide, headers=_bearer(token),
            json={"x": 0, "y": 0, "w": 10, "h": 10, "out_w": 100, "out_h": 100})
    err = _assert_envelope(r2, 429, "rate_limited", retryable=True)
    assert err["details"]["reason"] == "pixel_budget"
    assert int(r2.headers["Retry-After"]) >= 1
    assert b.call_count == 0  # 超限在读盘前


# --------------------------------------------------------------------------- #
# 4. 并发闸（进程级信号量）
# --------------------------------------------------------------------------- #
def test_region_concurrency_gate_429_before_disk(monkeypatch):
    inst = _bootstrap()
    slide = _touch_slide()
    # 单槽信号量，并预先占满（模拟并发已满）
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_CONCURRENCY_SEM", sem)
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 10 ** 9)

    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    try:
        with borrow as b:
            r = _client().post(
                "/api/plugin/v1/slides/%s/regions" % slide,
                headers={**_bearer(_token_for(inst)), "Accept": "application/octet-stream"},
                json={"bbox": {"x": 0, "y": 0, "w": 100, "h": 100}, "max_long_edge": 1568})
    finally:
        sem.release()
    err = _assert_envelope(r, 429, "rate_limited", retryable=True)
    assert err["details"]["reason"] == "concurrency"
    assert int(r.headers["Retry-After"]) >= 1
    assert b.call_count == 0  # 并发闸在读盘前


# --------------------------------------------------------------------------- #
# 5. 速率限制（v1 全能力端点 token bucket）
# --------------------------------------------------------------------------- #
def test_rate_limit_token_bucket_429_on_second_call(monkeypatch):
    inst = _bootstrap()
    slide = _touch_slide()
    # 每分钟 1 次（容量 1）→ 第 1 次 ok，第 2 次 429
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER", app_mod._PluginRateLimiter(1))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3:
        r1 = _client().get("/api/plugin/v1/slides/%s" % slide,
                           headers=_bearer(_token_for(inst)))
    assert r1.status_code == 200, r1.get_json()
    # 第 2 次（slide-info，证明限流覆盖非 region 端点）→ 429
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3:
        r2 = _client().get("/api/plugin/v1/slides/%s" % slide,
                           headers=_bearer(_token_for(inst)))
    err = _assert_envelope(r2, 429, "rate_limited", retryable=True)
    assert err["details"]["reason"] == "rate_limit"
    assert int(r2.headers["Retry-After"]) >= 1


# --------------------------------------------------------------------------- #
# 6. 红线：AUTH_ENABLED=False + legacy /internal/ai/* 通道零影响
# --------------------------------------------------------------------------- #
def test_legacy_internal_region_unaffected_by_plugin_gates(monkeypatch):
    inst = _bootstrap()  # noqa: F841 — 仅占位引导
    slide = _touch_slide()
    # 把插件像素预算/速率桶压到极小，验证 legacy 通道不被波及
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW", app_mod._SlidingPixelWindow(1))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER", app_mod._PluginRateLimiter(1))
    # 先耗尽插件像素预算（plugin region → 429）
    plugin_r = _client().post(
        "/api/plugin/v1/slides/%s/regions" % slide,
        headers=_bearer(_token_for(inst)),
        json={"x": 0, "y": 0, "w": 10, "h": 10, "out_w": 100, "out_h": 100})
    assert plugin_r.status_code == 429

    jpeg = b"\xff\xd8legacy-base64-payload\xff\xd9"
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, \
            mock.patch.object(app_mod, "_read_region_b64", return_value=_fake_region(jpeg)):
        r = _client().post(
            "/internal/ai/region",
            headers={"X-AI-Internal-Token": app_mod.AI_INTERNAL_TOKEN},
            json={"slide": slide, "x": 0, "y": 0, "w": 100, "h": 100, "max_long_edge": 1568})
    # legacy 通道仍正常出 base64（无二进制协商、无预算/限流闸）
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"] == "application/json"
    body = r.get_json()
    assert "image_base64" in body and "content_sha256" not in body
    assert base64.b64decode(body["image_base64"]) == jpeg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
