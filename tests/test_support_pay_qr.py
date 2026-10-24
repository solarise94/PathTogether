# -*- coding: utf-8 -*-
"""桌面端「支付状态」弹层（R3 add-on）服务端验收。

新增面：
  1. GET /support/pay/<ref>/qr 弹层内嵌页——与 /support/pay/<ref> 同一套
     会话绑定 + 订单/配置归属闸门（foreign/unknown/畸形 → 中性页、无表单、
     **不触碰支付宝**）；form 态 CSP form-action = 当前模式完整跳转链、
     frame-ancestors 'self' + X-Frame-Options SAMEORIGIN（本页只为 /support
     弹层的同源 iframe 存在）；
  2. qr_pay_mode=4 + qrcode_width **只在** embed 表单的 biz_content 里——
     普通整页收银台表单绝不携带（同一下单接口、仅 biz 参数差异）；
  3. /support 页 frame-src：开启支付时 'self' + 当前模式三台精确 origin
     （弹层 iframe 的同源 embed 页 + 它提交的支付宝链）；关闭态不下发
     frame-src（default-src 'none' 已禁框架），其余页族 frame-ancestors
     'none' 不变；
  4. JSON 建单（弹层路径）：201 契约字段（pay_url / expires_at /
     next_idempotency_key）；next key **未绑定任何订单**——「二维码已过期 →
     重新生成」用它建**新**单；老 key 重放仍返回原单（R2-1 不回归）。

单元测试不触网：SDK 网络面 monkeypatch（沿用 test_support_payments 的基建）。
运行：cd 项目根 && python3 -m pytest tests/test_support_pay_qr.py -q
"""
import html as html_mod
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402
import app as app_mod  # noqa: E402
import support_payments as sp  # noqa: E402
from test_support_payments import (  # noqa: E402
    SANDBOX_CSP_ORIGINS,
    _backdate,
    _client,
    _csrf_token,
    _make_order,
    _own_session,
)

pytest_plugins = ["test_support_payments"]


def _form_biz_content(body):
    """从渲染后的支付表单页提取 biz_content 字段值（HTML 转义的 JSON）。"""
    m = re.search(r'name="biz_content" value="([^"]+)"', body)
    assert m is not None, "页面缺少 biz_content 表单域"
    return json.loads(html_mod.unescape(m.group(1)))


# --------------------------------------------------------------------------- #
# 1. embed 路由闸门（会话绑定 / 归属 / 功能闸）
# --------------------------------------------------------------------------- #
def test_qr_embed_unknown_and_foreign_are_neutral_without_form(_iso, monkeypatch):
    """unknown / 他方会话 / 畸形 ref → 中性页：无表单、无网关地址、无查单。"""
    rt_local = sp.get_runtime("sandbox")
    order = _make_order(rt_local)

    def _bomb(*args, **kwargs):  # 中性路径绝不触碰支付宝
        raise AssertionError("build_page_pay_form must not be called")

    monkeypatch.setattr(sp, "build_page_pay_form", _bomb)

    c_unknown = _client()
    r_unknown = c_unknown.get("/support/pay/%s/qr" % order["out_trade_no"])
    assert r_unknown.status_code == 200
    body = r_unknown.get_data(as_text=True)
    assert "pay-form" not in body
    assert "gateway.do" not in body
    assert "biz_content" not in body
    assert "没有找到对应的订单" in body

    # 畸形 ref 与 unknown 同一响应（字节级一致，不泄露存在性）
    r_malformed = _client().get("/support/pay/not-an-order/qr")
    assert r_malformed.status_code == 200
    assert r_malformed.get_data() == r_unknown.get_data()

    # 本会话订单 + 配置不匹配（他方运行时）→ 同一中性响应
    prod = sp.RuntimeConfig("production", sp.GATEWAY_PRODUCTION,
                            "2026000000000001", "2088000000000001",
                            rt_local.app_private_key, rt_local.alipay_public_key)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"production": prod})
    r_mismatch = _own_session(_client(),
                              order["out_trade_no"]).get(
        "/support/pay/%s/qr" % order["out_trade_no"])
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "sandbox")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"sandbox": rt_local})
    assert r_mismatch.status_code == 200
    assert r_mismatch.get_data() == r_unknown.get_data()


def test_qr_embed_gate_404_when_disabled(_iso_off):
    c = _client()
    assert c.get("/support/pay/sp" + "0" * 24 + "/qr").status_code == 404


# --------------------------------------------------------------------------- #
# 2. embed 表单：qr 参数只在 embed；CSP frame-ancestors 'self'
# --------------------------------------------------------------------------- #
def test_qr_embed_form_has_qr_params_and_pay_page_does_not(_iso):
    rt_local = sp.get_runtime("sandbox")
    order = _make_order(rt_local)
    c = _own_session(_client(), order["out_trade_no"])

    embed = c.get("/support/pay/%s/qr" % order["out_trade_no"])
    assert embed.status_code == 200
    embed_biz = _form_biz_content(embed.get_data(as_text=True))
    assert embed_biz.get("qr_pay_mode") == sp.QR_PAY_MODE_EMBED == "4"
    assert embed_biz.get("qrcode_width") == sp.QRCODE_WIDTH_EMBED == "200"
    assert embed_biz.get("out_trade_no") == order["out_trade_no"]

    normal = c.get("/support/pay/%s" % order["out_trade_no"])
    assert normal.status_code == 200
    normal_biz = _form_biz_content(normal.get_data(as_text=True))
    assert "qr_pay_mode" not in normal_biz
    assert "qrcode_width" not in normal_biz


def test_qr_embed_csp_chain_and_frame_ancestors(_iso):
    rt_local = sp.get_runtime("sandbox")
    order = _make_order(rt_local)
    c = _own_session(_client(), order["out_trade_no"])
    r = c.get("/support/pay/%s/qr" % order["out_trade_no"])
    csp = r.headers["Content-Security-Policy"]
    assert "form-action " + " ".join(SANDBOX_CSP_ORIGINS) in csp
    assert "frame-ancestors 'self'" in csp
    assert "frame-ancestors 'none'" not in csp
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "excashier.alipay.com" not in csp  # 生产主机绝不混入沙箱表
    # 中性态同样是 embed 族（frame-ancestors 'self'，无表单）
    r_neutral = _client().get("/support/pay/sp" + "0" * 24 + "/qr")
    csp_neutral = r_neutral.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp_neutral
    assert "form-action 'none'" in csp_neutral


def test_qr_embed_closed_state_no_form(_iso):
    rt_local = sp.get_runtime("sandbox")
    order = _make_order(rt_local)
    sp.mark_closed_idempotent(order["out_trade_no"], "sweep:expired")
    r = _own_session(_client(), order["out_trade_no"]).get(
        "/support/pay/%s/qr" % order["out_trade_no"])
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "pay-form" not in body
    assert "订单已关闭" in body


def test_qr_embed_paid_redirects_to_return(_iso):
    rt_local = sp.get_runtime("sandbox")
    order = _make_order(rt_local)
    sp.mark_paid_idempotent(order["out_trade_no"], "qr-e2e-trade")
    r = _own_session(_client(), order["out_trade_no"]).get(
        "/support/pay/%s/qr" % order["out_trade_no"])
    assert r.status_code == 303
    assert r.headers["Location"].startswith(
        "/support/return?out_trade_no=")


def test_qr_embed_marks_paying(_iso):
    rt_local = sp.get_runtime("sandbox")
    order = sp.create_order(rt_local, 600)  # status=created
    c = _own_session(_client(), order["out_trade_no"])
    assert c.get("/support/pay/%s/qr" % order["out_trade_no"]).status_code == 200
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


# --------------------------------------------------------------------------- #
# 3. /support 页 frame-src（开启/关闭）
# --------------------------------------------------------------------------- #
def test_support_page_frame_src_lists_chain_when_enabled(_iso, monkeypatch):
    # /support/return 的 verified 查询不触网（本测试只看响应头）
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, otn: {"state": "NOT_EXIST", "total_amount": None,
                              "trade_no": None, "trade_status": None})
    c = _client()
    r = c.get("/support")
    csp = r.headers["Content-Security-Policy"]
    m = re.search(r"frame-src ([^;]+)", csp)
    assert m is not None, "开启支付时 /support 必须下发 frame-src"
    sources = m.group(1).split()
    assert sources == ["'self'"] + list(SANDBOX_CSP_ORIGINS)
    assert "excashier.alipay.com" not in csp  # 不混入生产链
    # 其余支持页族不下发 frame-src（default-src 'none' 已禁框架）
    order = _make_order(sp.get_runtime("sandbox"))
    for path in ("/support/pay/%s" % order["out_trade_no"],
                 "/support/return?out_trade_no=%s" % order["out_trade_no"]):
        rr = _own_session(_client(), order["out_trade_no"]).get(path)
        assert "frame-src" not in rr.headers["Content-Security-Policy"]
        assert rr.headers["X-Frame-Options"] == "DENY"


def test_support_page_no_frame_src_when_disabled(_iso_off):
    r = _client().get("/support")
    csp = r.headers["Content-Security-Policy"]
    assert "frame-src" not in csp
    assert csp.endswith("form-action 'none'; frame-ancestors 'none'")


# --------------------------------------------------------------------------- #
# 4. JSON 建单契约（弹层路径）
# --------------------------------------------------------------------------- #
def test_json_create_payload_contract(_iso):
    c = _client()
    r = c.post("/support/orders", data={
        "amount": "6.00", "csrf_token": _csrf_token(c),
        "idempotency_key": "a" * 32,
    }, headers={"Accept": "application/json"})
    assert r.status_code == 201
    data = r.get_json()
    assert data["out_trade_no"].startswith("sp")
    assert data["pay_url"] == "/support/pay/%s" % data["out_trade_no"]
    assert data["reused"] is False
    assert data["amount_yuan"] == "6.00"
    assert data["tier"] == "cat_food"
    # expires_at：ISO 8601 UTC，等于 created_at + 15m（沙箱表单同一截止线）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        data["expires_at"])
    order = sp.get_order(data["out_trade_no"])
    deadline = sp.order_time_expire(order["created_at"])
    from zoneinfo import ZoneInfo
    import datetime
    parsed = datetime.datetime.strptime(
        data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    assert parsed.astimezone(ZoneInfo("Asia/Shanghai"))\
        .strftime("%Y-%m-%d %H:%M:%S") == deadline
    # next_idempotency_key：32 hex、且**未绑定任何订单**
    assert re.fullmatch(r"[0-9a-f]{32}", data["next_idempotency_key"])
    assert sp.get_order_by_idempotency_key(data["next_idempotency_key"]) is None


def test_next_key_creates_fresh_order_while_old_key_replays_original(
        _iso, monkeypatch):
    """「二维码已过期 → 重新生成」语义：过线原单不再被会话复用（R1），next key
    建**新**单；老 key 重放仍回原单（R2-1 永久绑定不回归）。"""
    c = _client()
    first = c.post("/support/orders", data={
        "amount": "6.00", "csrf_token": _csrf_token(c),
        "idempotency_key": "b" * 32,
    }, headers={"Accept": "application/json"})
    assert first.status_code == 201
    data = first.get_json()
    # 原单回退到复用窗口之外（>10min，复用要求剩余 ≥5m）：等价「已过期」
    _backdate(data["out_trade_no"], minutes=11)
    # 脚手架增补（R5 规则 A）：11 分钟旧的原单仍在可支付窗口内，新 key 下单
    # 会先核实/关单旧单（query PENDING + close 成功）再建新单——真实 SDK 会
    # 出网，必须打桩；断言未做任何改动。
    monkeypatch.setattr(sp, "query_trade", lambda runtime, ref: {
        "state": "PENDING", "total_amount": "6.00",
        "trade_no": None, "trade_status": "WAIT_BUYER_PAY"})
    monkeypatch.setattr(sp, "close_trade", lambda runtime, ref: True)
    second = c.post("/support/orders", data={
        "amount": "6.00", "csrf_token": _csrf_token(c),
        "idempotency_key": data["next_idempotency_key"],
    }, headers={"Accept": "application/json"})
    assert second.status_code == 201
    assert second.get_json()["out_trade_no"] != data["out_trade_no"]
    # 老 key 重放：返回原单（200 reused），绝不新建
    replay = c.post("/support/orders", data={
        "amount": "6.00", "csrf_token": _csrf_token(c),
        "idempotency_key": "b" * 32,
    }, headers={"Accept": "application/json"})
    assert replay.status_code == 200
    assert replay.get_json()["out_trade_no"] == data["out_trade_no"]
    assert replay.get_json()["reused"] is True


def test_json_error_responses_unchanged(_iso):
    c = _client()
    r = c.post("/support/orders", data={
        "amount": "abc", "csrf_token": _csrf_token(c)},
        headers={"Accept": "application/json"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "invalid_amount"}
