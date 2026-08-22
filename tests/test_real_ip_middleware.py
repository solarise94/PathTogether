# -*- coding: utf-8 -*-
"""_RealIPMiddleware 测试（2026-08-23 SakuraFrp PROXY protocol v2 链路）。

语义：仅当直接对端是回环（pt-edge nginx / sidecar）时采纳 XFF 最后一跳；
非回环对端（LAN 直连 18080）自带的 XFF 一律忽略，防伪造 IP 桶。
"""

import io
import sys
import types as _types

try:
    import openslide  # noqa: F401
except ImportError:
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import app as app_mod  # noqa: E402


def _run_through_middleware(remote_addr, xff=None):
    """把一次请求 environ 过中间件，返回改写后的 REMOTE_ADDR。"""
    captured = {}

    def fake_app(environ, start_response):
        captured["remote_addr"] = environ.get("REMOTE_ADDR")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    mw = app_mod._RealIPMiddleware(fake_app)
    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": "",
        "SERVER_NAME": "test", "SERVER_PORT": "80", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""), "wsgi.errors": io.StringIO(),
        "REMOTE_ADDR": remote_addr,
    }
    if xff is not None:
        environ["HTTP_X_FORWARDED_FOR"] = xff
    mw(environ, lambda *a, **k: None)
    return captured["remote_addr"]


def test_loopback_peer_adopts_xff_last_hop():
    """pt-edge 对端（127.0.0.1）+ XFF → REMOTE_ADDR 改写为最后一跳。"""
    assert _run_through_middleware("127.0.0.1", "203.0.113.7") == "203.0.113.7"
    # 追加式 XFF（18443 旧路径 $proxy_add_x_forwarded_for）取最后一跳
    assert _run_through_middleware(
        "127.0.0.1", "1.2.3.4, 192.0.2.11") == "192.0.2.11"


def test_loopback_peer_without_xff_keeps_loopback():
    """sidecar 回调等无 XFF 的回环请求不受影响。"""
    assert _run_through_middleware("127.0.0.1") == "127.0.0.1"
    assert _run_through_middleware("::1") == "::1"


def test_non_loopback_peer_xff_ignored():
    """LAN 直连 18080 的非回环对端：自带 XFF 不予理睬（防伪造）。"""
    assert _run_through_middleware(
        "192.168.3.99", "203.0.113.7") == "192.168.3.99"


def test_blank_xff_entries_ignored():
    """空 XFF / 空最后一跳不改写。"""
    assert _run_through_middleware("127.0.0.1", "") == "127.0.0.1"
    assert _run_through_middleware("127.0.0.1", "  , ") == "127.0.0.1"


def test_flask_request_remote_addr_via_test_client():
    """端到端：完整 wsgi 栈（含中间件）下 request.remote_addr 被 XFF 改写。

    Flask test client 默认对端 127.0.0.1（=pt-edge 场景）；用 before_request
    记录器抓 remote_addr，打公开路径 /login（匿名可达）触发。
    """
    from flask import request, request_started

    seen = []

    def _rec(sender, **extra):
        seen.append(request.remote_addr)

    # 用信号而非 before_request：全量跑时其它用例已发过请求，Flask 会锁
    # setup 方法（before_request 注册报 AssertionError），信号无此限制
    request_started.connect(_rec, app_mod.app)
    try:
        app_mod.app.config["TESTING"] = True
        with app_mod.app.test_client() as c:
            c.get("/login", headers={"X-Forwarded-For": "198.51.100.23"})
            assert seen[-1] == "198.51.100.23"
            # 无 XFF：保持回环（sidecar 回调场景）
            c.get("/login")
            assert seen[-1] == "127.0.0.1"
    finally:
        request_started.disconnect(_rec, app_mod.app)
