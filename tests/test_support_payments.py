# -*- coding: utf-8 -*-
"""支付宝「AI 网页应用收款」赞助订单（0087，沙箱）服务端验收。

覆盖（docs/converter-support-page-design-20261021.md §支付宝 AI 付的后续扩展
+ Skill 1.7.5 完成条件）：
  1. 金额校验（预设/自定义边界/小数位/非数字/负数/>500/浮点精度——服务端权威）；
  2. CSRF（公开表单走全局闸；缺失/错误 token 一律拒绝）；
  3. 每 IP 速率闸（auth_rate_limits support_order_ip 桶，成功也计）；
  4. out_trade_no 服务端高熵生成 + 唯一性；
  5. 收银台跳转页：SDK page_execute 表单渲染、无内联脚本、CSP form-action
     仅当前模式网关精确 origin、状态机 created→paying；
  6. 回跳页不信任同步参数（伪造 TRADE_SUCCESS 查询串仍 pending；状态只来自
     本地库/服务端验签查单）；
  7. 异步通知：验签（真实 RSA 密钥对，离线）+ 业务字段校验（app_id/
     out_trade_no/total_amount/seller_id）+ 持久化幂等 + success/failure 契约；
  8. 补偿 sweep（查单→关单/补记已付，幂等）；
  9. 退款 owner-only + out_request_no 幂等 + 退款查询；
 10. 功能开关关闭 → 订单路由 404、/support 行为不变；生产模式缺配置 fail-closed；
 11. 迁移幂等；日志/响应无任何密钥材料。

单元测试不触网：SDK 客户端的网络面（execute 的 HTTP）一律 monkeypatch；
签名验签用测试内生成的真实 RSA 密钥对（真实算法，无网络）。

运行：cd 项目根 && python3 -m pytest tests/test_support_payments.py -q
"""
import base64
import datetime
import html as html_mod
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402
import app as app_mod  # noqa: E402
import auth_limit_store  # noqa: E402
import pg_store  # noqa: E402
import support_payments as sp  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

GATEWAY_ORIGIN_SANDBOX = "https://openapi-sandbox.dl.alipaydev.com"

# CSP form-action 必须覆盖下单 POST 的完整 302 跳转链（Chrome 对每一跳都执行
# form-action；host-source 不带关键字引号——'https://…' 是非法 source → 空
# source list 等价 'none'，浏览器会拦截 SDK 表单提交——e2e 实测踩过）。
SANDBOX_CSP_ORIGINS = list(sp.form_action_origins("sandbox"))
SANDBOX_CSP_FORM_ACTION = "form-action " + " ".join(SANDBOX_CSP_ORIGINS)


# --------------------------------------------------------------------------- #
# 测试用运行时配置：真实生成的 RSA 密钥（PKCS#1 私钥体 / SPKI 公钥体——与
# .alipay-sandbox.json 字段同形态），保证签名/建表单链路真实可执行、零网络。
# --------------------------------------------------------------------------- #
class _TestRuntime(sp.RuntimeConfig):
    pass


def _make_runtime():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as crsa

    key = crsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pkcs1 = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode("ascii")
    priv_body = "".join(priv_pkcs1.splitlines()[1:-1])
    pub_body = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")
    rt = sp.RuntimeConfig(
        mode="sandbox", gateway=sp.GATEWAY_SANDBOX,
        app_id="2026000000000000", seller_pid="2088000000000000",
        app_private_key=priv_body, alipay_public_key=pub_body)
    return rt, key


#: module 内共享（rt_pair module 级只生成一次）：app_id -> cryptography 私钥
_RT_KEYS = {}


@pytest.fixture(scope="module")
def rt_pair():
    rt, key = _make_runtime()
    _RT_KEYS[rt.app_id] = key
    return rt, key


@pytest.fixture()
def rt(rt_pair):
    return rt_pair[0]


@pytest.fixture()
def privkey(rt_pair):
    return rt_pair[1]


@pytest.fixture()
def _iso(tmp_path, monkeypatch, rt):
    """隔离 app + 开启赞助支付（沙箱模式、测试运行时配置）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:18861")
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_ENABLED", True)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "sandbox")
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_NOTIFY_URL", "")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"sandbox": rt})
    yield


@pytest.fixture()
def _iso_off(tmp_path, monkeypatch):
    """隔离 app + 赞助支付关闭（默认态）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histopilot.cn")
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_ENABLED", False)
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {})
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _csrf_token(client):
    """惰性触发 token 下发并取明文 cookie 值（与前端一致）。"""
    client.get("/support")
    cookie = client.get_cookie("csrf_token", domain="localhost", path="/")
    return cookie.value if cookie is not None else ""


def _post_order(client, data=None, token=True, **kw):
    data = dict(data or {})
    if token:
        data.setdefault("csrf_token", _csrf_token(client))
    return client.post("/support/orders", data=data, **kw)


def _make_order(rt, amount_fen=666, status="paying"):
    order = sp.create_order(rt, amount_fen)
    if status == "paying":
        sp.mark_paying(order["out_trade_no"])
    return sp.get_order(order["out_trade_no"])


def _own_session(client, *refs):
    """把订单号写入该 client 的 session（等价「本会话刚下过单」的签名 Cookie；
    回跳/支付页会话绑定的测试前提）。"""
    def _inject(sess):
        cur = [r for r in (sess.get("support_order_refs") or [])]
        for r in refs:
            if r not in cur:
                cur.append(r)
        sess["support_order_refs"] = cur[-10:]
    with client.session_transaction() as sess:
        _inject(sess)
    return client


def _backdate(out_trade_no, minutes=20):
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE support_orders SET created_at = now() - (%s || '"
                " minutes')::interval WHERE out_trade_no=%s",
                (minutes, out_trade_no))
        conn.commit()
    finally:
        conn.close()


def _notify_params(rt, order, key, trade_status="TRADE_SUCCESS",
                   notify_id="ntf-test-1", **over):
    """构造真实 RSA2 签名的通知参数（形态对齐支付宝异步通知）。"""
    params = {
        "app_id": rt.app_id,
        "trade_no": "2026102122001400000000000000001",
        "out_trade_no": order["out_trade_no"],
        "trade_status": trade_status,
        "total_amount": sp.fen_to_yuan(order["amount_fen"]),
        "seller_id": rt.seller_pid,
        "notify_id": notify_id,
        "notify_type": "trade_status_sync",
        "sign_type": "RSA2",
        "gmt_create": "2026-10-21 12:00:00",
        "gmt_payment": "2026-10-21 12:01:00",
    }
    params.update(over)
    return _sign_params(params, key)


def _sign_params(params, key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from alipay.aop.api.util.SignatureUtils import get_sign_content
    payload = {k: v for k, v in params.items()
               if k not in ("sign", "sign_type")}
    content = get_sign_content(payload)
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    signed = dict(params)
    signed["sign"] = base64.b64encode(sig).decode("ascii")
    return signed


# --------------------------------------------------------------------------- #
# 1. 金额校验（单元 + 路由）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,fen", [
    ("6.66", 666), ("18.88", 1888), ("18.00", 1800), ("1", 100),
    ("1.5", 150), ("50", 5000), ("50.00", 5000), ("1.00", 100),
])
def test_parse_amount_accepts_presets_and_bounds(raw, fen):
    assert sp.parse_amount_fen(raw) == fen


@pytest.mark.parametrize("raw", [
    "", None, "0", "0.99", "50.01", "68", "500", "1000", "-6.66", "+6.66", "6.666",
    "6.66.6", "abc", " 6.66", "6.66 ", "1e2", "01.00", "6.", ".5", "6,66",
    "500.000", "１２", "999999999999", "0x10", "6.6600000000001",
    "18.885",
])
def test_parse_amount_rejects_everything_else(raw):
    with pytest.raises(sp.InvalidAmount):
        sp.parse_amount_fen(raw)


def test_parse_amount_rejects_json_number_type():
    """金额通道只收字符串：JSON number（int/float/None）在类型层即拒绝。"""
    with pytest.raises(sp.InvalidAmount):
        sp.parse_amount_fen(6.66)  # type: ignore[arg-type]
    with pytest.raises(sp.InvalidAmount):
        sp.parse_amount_fen(0.1 + 0.2)  # type: ignore[arg-type]
    with pytest.raises(sp.InvalidAmount):
        sp.parse_amount_fen(None)  # type: ignore[arg-type]


def test_amount_float_precision_is_exact_decimal():
    """Decimal 精确换算：0.1+0.2 类浮点陷阱不进入金额面。"""
    assert sp.parse_amount_fen("1.1") == 110
    assert sp.parse_amount_fen("2.2") == 220
    assert sp.parse_amount_fen("6.6") == 660
    assert sp.fen_to_yuan(1888) == "18.88"
    assert sp.fen_to_yuan(5) == "0.05"


@pytest.mark.parametrize("amount", ["0", "0.99", "50.01", "68", "501", "-1", "6.666", "abc"])
def test_route_rejects_invalid_amounts(_iso, amount):
    c = _client()
    r = _post_order(c, {"amount_custom": amount})
    assert r.status_code == 303
    assert "pay_error=invalid_amount" in r.headers["Location"]
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_orders")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_route_json_error_for_api_clients(_iso):
    c = _client()
    r = _post_order(c, {"amount_custom": "abc"},
                    headers={"Accept": "application/json"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_amount"


def test_route_creates_order_and_redirects_to_pay_page(_iso):
    c = _client()
    r = _post_order(c, {"amount": "18.88"})
    assert r.status_code == 303
    loc = r.headers["Location"]
    assert loc.startswith("/support/pay/sp")
    out_trade_no = loc.rsplit("/", 1)[-1]
    order = sp.get_order(out_trade_no)
    assert order is not None
    assert order["amount_fen"] == 1888
    assert order["status"] == "created"
    assert order["subject"] == "HistoPilot 项目赞助"
    assert order["mode"] == "sandbox"
    # 会话绑定：下单成功即把订单号记入本会话（回跳/支付页的查看凭据）
    with c.session_transaction() as sess:
        assert sess["support_order_refs"] == [out_trade_no]


def test_route_custom_amount_wins_over_preset(_iso):
    c = _client()
    r = _post_order(c, {"amount": "6.66", "amount_custom": "23.45"})
    out_trade_no = r.headers["Location"].rsplit("/", 1)[-1]
    assert sp.get_order(out_trade_no)["amount_fen"] == 2345


def test_out_trade_no_uniqueness_and_shape(_iso, rt):
    seen = set()
    for _ in range(50):
        order = sp.create_order(rt, 666)
        otn = order["out_trade_no"]
        assert re.fullmatch(r"[0-9a-z]{26}", otn), otn
        assert otn not in seen
        seen.add(otn)


# --------------------------------------------------------------------------- #
# 2. CSRF
# --------------------------------------------------------------------------- #
def test_order_creation_requires_csrf_token(_iso):
    c = _client()
    r = c.post("/support/orders", data={"amount": "6.66"})
    assert r.status_code == 303
    assert "pay_error=csrf" in r.headers["Location"]
    r2 = _post_order(_client(), {"amount": "6.66"}, token=True)
    assert r2.status_code == 303
    assert r2.headers["Location"].startswith("/support/pay/")


def test_order_creation_rejects_wrong_csrf_token(_iso):
    c = _client()
    r = c.post("/support/orders", data={"amount": "6.66",
                                        "csrf_token": "deadbeef"})
    assert r.status_code == 303
    assert "pay_error=csrf" in r.headers["Location"]


# --------------------------------------------------------------------------- #
# 3. 每 IP 速率闸
# --------------------------------------------------------------------------- #
def test_order_rate_limit_per_ip(_iso, monkeypatch):
    monkeypatch.setattr(auth_limit_store, "SUPPORT_ORDER_IP_ATTEMPT_LIMIT", 3)
    c = _client()
    statuses = []
    for _ in range(4):
        r = _post_order(c, {"amount": "6.66"})
        statuses.append((r.status_code, r.headers.get("Location", "")))
    # 前 3 次成功（尝试桶计数），第 4 次 429
    assert all(s == 303 and loc.startswith("/support/pay/")
               for s, loc in statuses[:3])
    assert statuses[3][0] == 303
    assert "pay_error=rate_limited" in statuses[3][1]
    rj = _post_order(_client(), {"amount": "6.66"},
                     headers={"Accept": "application/json"})
    assert rj.status_code == 429
    assert rj.get_json()["error"] == "rate_limited"


# --------------------------------------------------------------------------- #
# 4. /support 页面集成（开启/关闭）
# --------------------------------------------------------------------------- #
def test_support_page_with_pay_enabled(_iso):
    c = _client()
    body = c.get("/support").get_data(as_text=True)
    assert 'id="pay-pick"' in body
    assert 'action="/support/orders"' in body
    assert 'name="csrf_token"' in body
    # 下单幂等 key（R1 add-on）：每次渲染新随机 32 hex 隐藏域
    key_match = re.search(
        r'name="idempotency_key" value="([0-9a-f]{32})"', body)
    assert key_match is not None
    body2 = c.get("/support").get_data(as_text=True)
    key2 = re.search(r'name="idempotency_key" value="([0-9a-f]{32})"', body2)
    assert key2 is not None and key2.group(1) != key_match.group(1)
    # R2 add-on：猫咪礼物档卡（¥1.00/¥6.00/¥18.00 + 名称 + 内联 SVG 小图）
    for yuan in ("1.00", "6.00", "18.00"):
        assert 'value="%s"' % yuan in body
    for tier_name in ("一份猫粮", "猫玩具", "神秘礼物"):
        assert tier_name in body
    assert "gift-svg-bowl" in body
    assert "gift-svg-mouse" in body
    assert "gift-svg-modem" in body
    assert 'id="amount-custom"' in body
    assert "用支付宝支持" in body
    # CSP：表单 POST 本站 → form-action 'self'
    csp = c.get("/support").headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp


def test_support_page_flag_off_unchanged(_iso_off):
    c = _client()
    r = c.get("/support")
    body = r.get_data(as_text=True)
    assert r.headers["Content-Security-Policy"].endswith(
        "form-action 'none'; frame-ancestors 'none'")
    assert 'id="pay-pick"' not in body
    assert "/support/orders" not in body
    # 订单族路由一律 404（功能不存在）
    assert c.post("/support/orders", data={}).status_code == 404
    assert c.get("/support/pay/sp" + "0" * 24).status_code == 404
    assert c.get("/support/return").status_code == 404
    assert c.post("/support/alipay/notify", data={}).status_code in (404,)


# --------------------------------------------------------------------------- #
# 5. 收银台跳转页
# --------------------------------------------------------------------------- #
def test_pay_page_renders_sdk_form_without_inline_script(_iso):
    c = _client()
    order = _make_order(sp.get_runtime("sandbox"))
    _own_session(c, order["out_trade_no"])
    r = c.get("/support/pay/%s" % order["out_trade_no"])
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    # CSP：form-action 放开当前模式跳转链（网关+中转+收银台）
    csp = r.headers["Content-Security-Policy"]
    assert SANDBOX_CSP_FORM_ACTION in csp
    assert "'unsafe-inline'" not in csp
    # SDK 表单存在（签名后的 POST 表单，官方布局：公共参数在 action 查询串），
    # 且无内联脚本/内联样式
    assert ('class="pay-form" name="punchout_form" accept-charset="utf-8"'
            ' method="post"') in body
    assert ('action="%s?' % sp.GATEWAY_SANDBOX) in body
    action_attr = re.search(r'action="([^"]+)"', body).group(1)
    query = dict(parse_qsl(urlsplit(action_attr).query,
                           keep_blank_values=True))
    # 防乱码闸（GBK 回退实测）：charset 必须留在 action 查询
    assert query.get("charset") == "utf-8"
    assert "sign" in query and "app_id" in query and "timestamp" in query
    assert 'name="biz_content"' in body
    assert "&quot;out_trade_no&quot;:&quot;%s&quot;" % order["out_trade_no"] \
        in body
    assert "&quot;total_amount&quot;:&quot;6.66&quot;" in body
    assert "&quot;product_code&quot;:&quot;FAST_INSTANT_TRADE_PAY&quot;" in body
    # 无内联 <script>（外链脚本除外）；无内联 style
    for m in re.finditer(r"<script\b([^>]*)>", body):
        assert "src=" in m.group(1), "pay 页不允许内联 <script>"
    assert 'style="display:none"' not in body
    assert 'src="/static/support/support_pay.js' in body
    # 金额/订单号回显来自本地订单行
    assert "¥6.66" in body
    # 状态机：created → paying
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


def test_pay_page_signs_with_configured_key(_iso):
    """签名用配置私钥真实生成（离线重建签名内容验证）。

    SDK 官方布局：公共参数（含签名）在 **action 查询串**，body 只有
    biz_content；签名覆盖「全部公共参数 + biz_content」并集，按同一参数集
    重建并验签。CSP form-action 不比较查询串——原始查询串（含裸 ``+``）可过
    Chrome 提交（2026-10-21 沙箱浏览器实测，见 sanitize_page_form docstring）；
    charset 必须留在查询串（网关按它解码 body，缺省回退 GBK 乱码）。"""
    import html as html_mod

    from alipay.aop.api.util.SignatureUtils import get_sign_content
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    body = _own_session(_client(), order["out_trade_no"]).get(
        "/support/pay/%s" % order["out_trade_no"]).get_data(as_text=True)
    action = re.search(r'action="([^"]+)"', body).group(1)
    assert action.startswith(sp.GATEWAY_SANDBOX + "?")  # 官方布局：带查询串
    query = dict(parse_qsl(urlsplit(action).query, keep_blank_values=True))
    body_fields = dict(re.findall(
        r'<input type="hidden" name="([^"]+)" value="([^"]*)">', body))
    assert "sign" in query
    assert set(body_fields) == {"biz_content"}  # body 只承载业务参数
    assert set(query) >= {"timestamp", "app_id", "method", "charset",
                          "format", "version", "sign_type", "return_url"}
    assert query["charset"] == "utf-8"
    payload = dict(query)
    payload.update({k: html_mod.unescape(v)
                    for k, v in body_fields.items()})
    signed_content = get_sign_content(
        {k: v for k, v in payload.items() if k != "sign"})
    key = _RT_KEYS[rt.app_id]
    # 不抛异常即验签通过（真实 RSA2）
    key.public_key().verify(base64.b64decode(query["sign"]),
                            signed_content.encode("utf-8"),
                            padding.PKCS1v15(), hashes.SHA256())
    biz_json = json.loads(html_mod.unescape(body_fields["biz_content"]))
    assert biz_json["out_trade_no"] == order["out_trade_no"]
    assert biz_json["total_amount"] == "6.66"
    assert biz_json["product_code"] == "FAST_INSTANT_TRADE_PAY"
    assert biz_json["subject"] == "HistoPilot 项目赞助"  # UTF-8 原文（防乱码）
    # 绝对超时：created_at + 15m（Asia/Shanghai），不再下发相对 timeout_express
    assert "timeout_express" not in biz_json
    expected = (order["created_at"] + datetime.timedelta(minutes=15)) \
        .astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    assert biz_json["time_expire"] == expected


def test_sanitize_rejects_form_without_charset_in_query(_iso, monkeypatch):
    """防乱码闸：action 查询缺 charset=utf-8 的 SDK 表单拒绝渲染
    （fail-closed，绝不退回 GBK 乱码形态）。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)

    def no_charset_page_execute(self, request, http_method="POST"):
        form = _orig_page_execute(self, request, http_method)
        return form.replace("charset=utf-8&", "").replace("&charset=utf-8", "")

    from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
    _orig_page_execute = DefaultAlipayClient.page_execute
    monkeypatch.setattr(DefaultAlipayClient, "page_execute",
                        no_charset_page_execute)
    with pytest.raises(sp.TradeOperationError):
        sp.build_page_pay_form(rt, order, "http://127.0.0.1:18861/return")


def test_pay_page_unknown_malformed_or_foreign_ref_neutral(_iso, monkeypatch):
    """会话绑定（AI 收款回跳状态展示契约）：非本会话订单号（未知/他方/形态
    非法）一律中性 200，不建表单、不查单、互相无差别（不泄露存在性）。"""
    rt = sp.get_runtime("sandbox")
    foreign = _make_order(rt)  # 存在，但不属于这个 client 的会话
    query_calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: query_calls.append(otn) or {})
    c = _client()
    r_unknown = c.get("/support/pay/sp000000000000000000000000")
    r_malformed = c.get("/support/pay/nope")
    r_foreign = c.get("/support/pay/%s" % foreign["out_trade_no"])
    for r in (r_unknown, r_malformed, r_foreign):
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "没有找到对应的订单" in body
        assert "pay-form" not in body  # 不建表单
        assert foreign["out_trade_no"] not in body
    # 未知 vs 他方：同一响应（字节级一致 → 不泄露存在性）
    assert r_unknown.get_data() == r_foreign.get_data()
    assert query_calls == []  # 支付页中性态不触发服务端查单
    # 功能关闭时仍 404（功能不存在）
    # （关闭态由 test_support_page_flag_off_unchanged 覆盖）


def test_return_page_foreign_or_unknown_identical_and_no_query(
        _iso, monkeypatch):
    """回跳页会话绑定：他方订单与未知订单号同一中性响应（字节一致），
    且**不触发** trade.query（查单是外部副作用，只允许发生在本会话订单上）。"""
    rt = sp.get_runtime("sandbox")
    foreign = _make_order(rt)  # 存在但不属于这个 client
    query_calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: query_calls.append(otn) or {
                            "state": "PAID", "total_amount": "6.66",
                            "trade_no": "x", "trade_status": "TRADE_SUCCESS"})
    c = _client()
    r_unknown = c.get("/support/return?out_trade_no=%s" % ("sp" + "0" * 24))
    r_foreign = c.get("/support/return?out_trade_no=%s"
                      % foreign["out_trade_no"])
    assert r_unknown.status_code == r_foreign.status_code == 200
    assert r_unknown.get_data() == r_foreign.get_data()
    assert "没有找到对应的订单" in r_unknown.get_data(as_text=True)
    assert query_calls == []  # 他方/未知订单绝不触发服务端查单
    assert sp.get_order(foreign["out_trade_no"])["status"] == "paying"


def test_session_order_refs_bounded(_iso):
    """下单把订单号记入 session 且**有界**（最近 10 个）：会话 Cookie 不膨胀。"""
    c = _client()
    r1 = _post_order(c, {"amount": "6.66"})
    ref1 = r1.headers["Location"].rsplit("/", 1)[-1]
    with c.session_transaction() as sess:
        refs = sess["support_order_refs"]
        assert refs == [ref1]
        # 预填 9 个旧号（凑满 10），再下一单必须淘汰最旧的
        sess["support_order_refs"] = ["sp" + format(i, "024x")
                                      for i in range(9)] + [ref1]
    # 不同金额：不复用（幂等复用只对同金额生效）→ 产生新订单号
    r2 = _post_order(c, {"amount": "18.88"})
    ref2 = r2.headers["Location"].rsplit("/", 1)[-1]
    assert ref2 != ref1
    with c.session_transaction() as sess:
        refs = sess["support_order_refs"]
        assert len(refs) == 10
        assert refs[-1] == ref2
        assert ref1 in refs and "sp" + format(0, "024x") not in refs  # 最旧被淘汰


def test_pay_page_paid_redirects_closed_renders_closed(_iso):
    rt = sp.get_runtime("sandbox")
    c = _client()
    order = _make_order(rt)
    _own_session(c, order["out_trade_no"])
    sp.mark_paid_idempotent(order["out_trade_no"], "trd_1")
    r = c.get("/support/pay/%s" % order["out_trade_no"])
    assert r.status_code == 303
    assert r.headers["Location"].startswith("/support/return?out_trade_no=")

    order2 = _make_order(rt)
    _own_session(c, order2["out_trade_no"])
    sp.mark_closed_idempotent(order2["out_trade_no"], "sweep:expired")
    r2 = c.get("/support/pay/%s" % order2["out_trade_no"])
    body = r2.get_data(as_text=True)
    assert r2.status_code == 200
    assert "pay-form" not in body  # 关闭态不再渲染支付表单
    assert r2.headers["Content-Security-Policy"].endswith(
        "form-action 'none'; frame-ancestors 'none'")


# --------------------------------------------------------------------------- #
# 6. 回跳页不信任同步参数
# --------------------------------------------------------------------------- #
def _mock_query(monkeypatch, state="PENDING", amount=None, trade_no=None):
    calls = []

    def fake_query(runtime, out_trade_no):
        calls.append(out_trade_no)
        return {"state": state,
                "total_amount": amount,
                "trade_no": trade_no,
                "trade_status": {"PAID": "TRADE_SUCCESS",
                                 "PENDING": "WAIT_BUYER_PAY",
                                 "CLOSED": "TRADE_CLOSED",
                                 "NOT_EXIST": None,
                                 "UNKNOWN": None}.get(state)}

    monkeypatch.setattr(sp, "query_trade", fake_query)
    return calls


def test_return_page_ignores_forged_success_params(_iso, monkeypatch):
    """伪造 trade_status=TRADE_SUCCESS 的同步查询串不能产生已支付状态
    （本会话订单：状态只来自本地库/服务端验签查单）。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    _mock_query(monkeypatch, state="PENDING")
    r = c.get(
        "/support/return?out_trade_no=%s&trade_status=TRADE_SUCCESS"
        "&total_amount=6.66&trade_no=FAKE123&sign=FORGED"
        % order["out_trade_no"])
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "正在确认支付结果" in body
    assert "谢谢你的支持" not in body
    # 伪造参数绝不回显
    assert "FAKE123" not in body and "FORGED" not in body
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


def test_return_page_confirms_paid_via_server_query(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    _mock_query(monkeypatch, state="PAID", amount="6.66",
                trade_no="20261021real")
    r = c.get(
        "/support/return?out_trade_no=%s&trade_status=WAIT_BUYER_PAY"
        % order["out_trade_no"])
    body = r.get_data(as_text=True)
    assert "已收到你的支持" in body
    assert "¥6.66" in body
    assert order["out_trade_no"] in body  # 凭据凭条带订单 ref
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    assert row["trade_no"] == "20261021real"
    # 平台边界：不展示赞助名单/累计统计
    assert "赞助名单" not in body and "累计金额" not in body


def test_return_page_amount_mismatch_stays_pending(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt, amount_fen=1888)
    _mock_query(monkeypatch, state="PAID", amount="6.66")  # 金额不一致
    r = _own_session(_client(), order["out_trade_no"]).get(
        "/support/return?out_trade_no=%s" % order["out_trade_no"])
    assert "正在确认支付结果" in r.get_data(as_text=True)
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


def test_return_page_query_failure_is_pending_not_500(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])

    def boom(runtime, otn):
        raise sp.TradeOperationError("gateway down")

    monkeypatch.setattr(sp, "query_trade", boom)
    r = c.get("/support/return?out_trade_no=%s" % order["out_trade_no"])
    assert r.status_code == 200
    assert "正在确认支付结果" in r.get_data(as_text=True)


def test_return_page_states_closed_and_neutral(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    sp.mark_closed_idempotent(order["out_trade_no"], "sweep:expired")
    # 已关单也会服务端查单核实（closed→paid 恢复路径）；查得非 PAID 维持关闭态
    _mock_query(monkeypatch, state="NOT_EXIST")
    r = c.get("/support/return?out_trade_no=%s" % order["out_trade_no"])
    assert "订单未完成" in r.get_data(as_text=True)
    assert sp.get_order(order["out_trade_no"])["status"] == "closed"

    # 缺参 / 未知订单：安全中性态（200，不 404/500、不泄露存在性）
    r2 = _client().get("/support/return")
    assert r2.status_code == 200
    assert "没有找到对应的订单" in r2.get_data(as_text=True)
    r3 = _client().get("/support/return?out_trade_no=%s"
                       % ("sp" + "9" * 24))
    assert r3.status_code == 200
    assert "没有找到对应的订单" in r3.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 7. 异步通知
# --------------------------------------------------------------------------- #
def _post_notify(c, params):
    return c.post("/support/alipay/notify", data=params)


def test_notify_valid_signed_marks_paid(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    r = _post_notify(_client(), _notify_params(rt, order, privkey))
    assert r.status_code == 200
    assert r.get_data(as_text=True) == "success"
    assert r.mimetype == "text/plain"
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events")
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_notify_duplicate_is_idempotent(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey)
    c = _client()
    assert _post_notify(c, params).get_data(as_text=True) == "success"
    # 重推（同 notify_id）→ success 且不重复变更
    assert _post_notify(c, params).get_data(as_text=True) == "success"
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT paid_at FROM support_orders"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            paid_at = cur.fetchone()[0]
    finally:
        conn.close()
    # 幂等：paid_at 未被第二次覆盖（仍为首次时间戳）
    assert paid_at is not None


def test_notify_bad_signature_rejected(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey)
    params["total_amount"] = "0.01"  # 签名后篡改
    r = _post_notify(_client(), params)
    assert r.get_data(as_text=True) == "failure"
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


def test_notify_missing_or_garbage_sign_rejected(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _client()
    params = _notify_params(rt, order, privkey)
    del params["sign"]
    assert _post_notify(c, params).get_data(as_text=True) == "failure"
    params2 = _notify_params(rt, order, privkey, notify_id="ntf-x2")
    params2["sign"] = "!!!not-base64!!!"
    assert _post_notify(c, params2).get_data(as_text=True) == "failure"
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


@pytest.mark.parametrize("field,value", [
    ("app_id", "2026999900009999"),          # app_id 不匹配本地订单快照
    ("seller_id", "2088000000009999"),       # seller 与运行时配置不符
    ("total_amount", "7.77"),                # 金额不符
])
def test_notify_business_field_mismatch_rejected(_iso, privkey, field, value):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey, **{field: value})
    r = _post_notify(_client(), params)
    assert r.get_data(as_text=True) == "failure"
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"
    # 失败不落事件表
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_notify_unknown_order_rejected(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey,
                            out_trade_no="sp" + "f" * 24)
    assert _post_notify(_client(), params).get_data(as_text=True) == "failure"


def test_notify_wait_buyer_pay_and_closed_states(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    c = _client()
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey,
                            trade_status="WAIT_BUYER_PAY",
                            notify_id="ntf-wait")
    assert _post_notify(c, params).get_data(as_text=True) == "success"
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"

    order2 = _make_order(rt)
    params2 = _notify_params(rt, order2, privkey,
                             trade_status="TRADE_CLOSED",
                             notify_id="ntf-closed")
    assert _post_notify(c, params2).get_data(as_text=True) == "success"
    assert sp.get_order(order2["out_trade_no"])["status"] == "closed"


def test_notify_refund_event_never_marks_paid(_iso, privkey):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    sp.mark_paid_idempotent(order["out_trade_no"], "trd_r")
    params = _notify_params(rt, order, privkey, notify_id="ntf-refund",
                            out_biz_no="rf_20261021",
                            gmt_refund="2026-10-21 13:00:00",
                            refund_fee="6.66")
    r = _post_notify(_client(), params)
    assert r.get_data(as_text=True) == "success"  # 事件已记录、不再重推
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"  # 不因退款通知改变状态（退款单独记账）


def test_notify_verified_paid_recovers_closed_order(_iso, privkey, caplog):
    """本地已关单后收到 verified 实付通知（验签 + app_id/seller/金额全匹配）：
    closed → paid 恢复，记 error 级 ``paid after local close`` 告警
    （只含订单号，无参数/密钥）。真实收款不可丢。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    sp.mark_closed_idempotent(order["out_trade_no"], "sweep:expired")
    closed_at = sp.get_order(order["out_trade_no"])["closed_at"]
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="support_payments"):
        params = _notify_params(rt, order, privkey, notify_id="ntf-recover",
                                trade_no="20261021late-pay")
        r = _post_notify(_client(), params)
    assert r.get_data(as_text=True) == "success"
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    assert row["trade_no"] == "20261021late-pay"
    assert row["paid_at"] is not None and row["closed_at"] == closed_at
    alerts = [rec for rec in caplog.records
              if "paid after local close" in rec.getMessage()]
    assert alerts, "缺少 paid-after-close 告警"
    assert alerts[0].levelno == logging.ERROR
    msg = alerts[0].getMessage()
    # 告警绝不携带通知参数值/密钥材料
    assert params["sign"] not in msg
    assert "6.66" not in msg and "TRADE_SUCCESS" not in msg
    assert rt.app_private_key not in msg and rt.alipay_public_key not in msg


def test_notify_trade_closed_never_downgrades_paid(_iso, privkey):
    """TRADE_CLOSED 通知绝不把已支付行降级（paid 是吸收态）。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    assert sp.mark_paid_idempotent(order["out_trade_no"], "trd_keep") is True
    row = sp.get_order(order["out_trade_no"])
    snapshot = (row["status"], row["trade_no"], row["paid_at"])
    params = _notify_params(rt, order, privkey, trade_status="TRADE_CLOSED",
                            notify_id="ntf-late-close")
    assert _post_notify(_client(), params).get_data(as_text=True) == "success"
    row2 = sp.get_order(order["out_trade_no"])
    assert (row2["status"], row2["trade_no"], row2["paid_at"]) == snapshot
    # mark_paid/marked_closed 直调同样不得降级
    assert sp.mark_closed_idempotent(order["out_trade_no"],
                                     "sweep:expired") is False
    row3 = sp.get_order(order["out_trade_no"])
    assert (row3["status"], row3["trade_no"], row3["paid_at"]) == snapshot


def test_notify_second_paid_event_for_paid_order_no_change(_iso, privkey):
    """已支付订单收到第二笔不同的实付通知（不同 notify_id）：success、
    状态/trade_no/paid_at 零变更，两笔事件都留痕。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _client()
    p1 = _notify_params(rt, order, privkey, notify_id="ntf-first")
    assert _post_notify(c, p1).get_data(as_text=True) == "success"
    row = sp.get_order(order["out_trade_no"])
    snapshot = (row["status"], row["trade_no"], row["paid_at"])
    p2 = _notify_params(rt, order, privkey, notify_id="ntf-second",
                        gmt_payment="2026-10-21 12:09:00")
    assert _post_notify(c, p2).get_data(as_text=True) == "success"
    row2 = sp.get_order(order["out_trade_no"])
    assert (row2["status"], row2["trade_no"], row2["paid_at"]) == snapshot
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_notify_amount_normalization_exact(_iso, privkey):
    """6.6 / 6.60 与本地 660 分精确等值；形态非法（3 位小数）拒绝。"""
    rt = sp.get_runtime("sandbox")
    c = _client()
    order = _make_order(rt, amount_fen=660)
    params = _notify_params(rt, order, privkey, total_amount="6.60",
                            notify_id="ntf-norm1")
    assert _post_notify(c, params).get_data(as_text=True) == "success"
    assert sp.get_order(order["out_trade_no"])["status"] == "paid"

    order2 = _make_order(rt, amount_fen=660)
    params2 = _notify_params(rt, order2, privkey, total_amount="6.660",
                             notify_id="ntf-norm2")
    assert _post_notify(c, params2).get_data(as_text=True) == "failure"
    assert sp.get_order(order2["out_trade_no"])["status"] == "paying"


def test_notify_never_echoes_or_logs_secrets(_iso, privkey, caplog):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    caplog.clear()
    with caplog.at_level("DEBUG"):
        params = _notify_params(rt, order, privkey)
        params["total_amount"] = "9.99"
        _post_notify(_client(), params)
    secrets_material = (rt.app_private_key, rt.alipay_public_key)
    blob = "\n".join(str(r.getMessage()) for r in caplog.records)
    for secret in secrets_material:
        assert secret not in blob
    # 签名值不进日志（脱敏）
    assert params["sign"] not in blob


# --------------------------------------------------------------------------- #
# 8. 补偿 sweep + 迁移幂等
# --------------------------------------------------------------------------- #
def test_execute_accepts_unwrapped_response_envelope(_iso, monkeypatch):
    """SDK execute()（验签开启）返回**已剥信封**响应体（3.7.1411 实测）——
    ``_execute`` 必须同时兼容包裹键形态与裸形态；业务失败抛带 sub_code 的
    TradeOperationError。"""
    rt = sp.get_runtime("sandbox")

    class _FakeClient:
        def __init__(self, payload):
            self._payload = payload

        def execute(self, request):
            return json.dumps(self._payload)

    from alipay.aop.api.request.AlipayTradeQueryRequest import \
        AlipayTradeQueryRequest

    # 形态 A：裸响应（真实形态）
    monkeypatch.setattr(sp, "build_client",
                        lambda cfg: _FakeClient(
                            {"code": "10000", "trade_status": "WAIT_BUYER_PAY",
                             "total_amount": "6.66", "trade_no": "trd_x"}))
    info = sp.query_trade(rt, "sp" + "1" * 24)
    assert info["state"] == "PENDING"

    # 形态 B：业务失败（裸响应，code!=10000）→ sub_code 进异常
    monkeypatch.setattr(sp, "build_client",
                        lambda cfg: _FakeClient(
                            {"msg": "Business Failed", "code": "40004",
                             "sub_code": "ACQ.TRADE_NOT_EXIST",
                             "sub_msg": "交易不存在"}))
    info = sp.query_trade(rt, "sp" + "2" * 24)
    assert info["state"] == "NOT_EXIST"

    # 形态 C：包裹键（历史/文档形态）同样兼容
    monkeypatch.setattr(sp, "build_client",
                        lambda cfg: _FakeClient(
                            {"alipay_trade_query_response": {
                                "code": "10000",
                                "trade_status": "TRADE_SUCCESS",
                                "total_amount": "6.66"}}))
    info = sp.query_trade(rt, "sp" + "3" * 24)
    assert info["state"] == "PAID"


def test_sweep_closes_expired_and_records_paid(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    c = _client()
    # 未支付过期单：查单 PENDING → close → 本地关单
    o1 = _make_order(rt)
    _backdate(o1["out_trade_no"])
    # 已支付但通知未达：查单 PAID → 补记 paid（补偿路径）
    o2 = _make_order(rt)
    _backdate(o2["out_trade_no"])
    # 新鲜单（< 16min）：不扫
    o3 = _make_order(rt)

    def fake_query(runtime, otn):
        if otn == o1["out_trade_no"]:
            return {"state": "PENDING", "total_amount": "6.66",
                    "trade_no": None, "trade_status": "WAIT_BUYER_PAY"}
        return {"state": "PAID", "total_amount": "6.66",
                "trade_no": "20261021comp", "trade_status": "TRADE_SUCCESS"}

    closed_calls = []

    def fake_close(runtime, otn):
        closed_calls.append(otn)
        return True

    monkeypatch.setattr(sp, "query_trade", fake_query)
    monkeypatch.setattr(sp, "close_trade", fake_close)
    stats = sp.sweep(min_age_minutes=16)
    assert stats["scanned"] == 2
    assert stats["closed"] == 1
    assert stats["paid"] == 1
    assert closed_calls == [o1["out_trade_no"]]
    assert sp.get_order(o1["out_trade_no"])["status"] == "closed"
    row2 = sp.get_order(o2["out_trade_no"])
    assert row2["status"] == "paid" and row2["trade_no"] == "20261021comp"
    assert sp.get_order(o3["out_trade_no"])["status"] == "paying"
    # 幂等：再跑一遍只重扫「closed 且从未 paid」的 o1（补偿恢复扫描集）；
    # 查得非 PAID → 跳过、无重复关单/补记；paid 的 o2 不再扫
    stats2 = sp.sweep(min_age_minutes=16)
    assert stats2["scanned"] == 1
    assert stats2["skipped"] == 1
    assert stats2["closed"] == 0 and stats2["paid"] == 0


def test_sweep_trade_not_exist_closes_without_close_call(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    o = _make_order(rt)
    _backdate(o["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: {
                            "state": "NOT_EXIST", "total_amount": None,
                            "trade_no": None, "trade_status": None})
    called = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, otn: called.append(otn) or True)
    stats = sp.sweep(min_age_minutes=16)
    assert stats["closed"] == 1
    assert called == []  # 交易不存在：无需调 close，直接本地关单
    assert sp.get_order(o["out_trade_no"])["status"] == "closed"


def test_sweep_paid_amount_mismatch_skipped(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    o = _make_order(rt, amount_fen=1888)
    _backdate(o["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: {
                            "state": "PAID", "total_amount": "6.66",
                            "trade_no": "x", "trade_status": "TRADE_SUCCESS"})
    stats = sp.sweep(min_age_minutes=16)
    assert stats["skipped"] == 1 and stats["paid"] == 0
    assert sp.get_order(o["out_trade_no"])["status"] == "paying"


def test_migration_idempotent(pg_uri):
    """0087 幂等：ensure_schema 重复执行 + 迁移 SQL 重放均不报错。"""
    for _ in range(2):
        conn = psycopg.connect(pg_uri)
        try:
            pg_store.ensure_schema(conn)
        finally:
            conn.close()
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "0087_support_payments.sql").read_text(encoding="utf-8")
    conn = psycopg.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # 迁移文件自身幂等（IF NOT EXISTS）
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 9. 退款（owner-only Admin API）
# --------------------------------------------------------------------------- #
def _setup_owner_and_user():
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    user = user_store.create_user(
        "user0@x.com", "userpass00-abcdef", role="user", display_name="User")
    return owner, user


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _admin_client():
    """登录 owner 且自动带 X-CSRF-Token 的 client（与前端 apiFetch 一致）。"""
    from _pt_helpers import csrf_client
    owner, _u = _setup_owner_and_user()
    c = csrf_client(_client())
    return _login(c, owner), owner


def _paid_order(rt):
    order = _make_order(rt)
    sp.mark_paid_idempotent(order["out_trade_no"], "trd_refund_1")
    return order


def test_refund_requires_owner(_iso):
    rt = sp.get_runtime("sandbox")
    order = _paid_order(rt)
    c = _client()
    anon = c.post("/api/admin/v1/support-payments/%s/refund"
                  % order["out_trade_no"], json={"amount_yuan": "6.66"})
    assert anon.status_code == 401

    _owner, user = _setup_owner_and_user()
    from _pt_helpers import csrf_client
    c_user = _login(csrf_client(_client()), user)
    r_user = c_user.post("/api/admin/v1/support-payments/%s/refund"
                         % order["out_trade_no"], json={"amount_yuan": "6.66"})
    assert r_user.status_code == 403


def test_refund_success_and_audit(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _paid_order(rt)
    c, _owner = _admin_client()

    def fake_refund(runtime, o, amount_fen, orn, reason=None):
        return {"fund_change": "Y", "refund_fee": sp.fen_to_yuan(amount_fen),
                "trade_no": "trd_refund_1"}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    r = c.post("/api/admin/v1/support-payments/%s/refund"
               % order["out_trade_no"], json={"amount_yuan": "6.66",
                                              "refund_reason": "测试退款"})
    assert r.status_code == 200
    refund = r.get_json()["refund"]
    assert refund["status"] == "refunded"
    assert refund["refund_amount_yuan"] == "6.66"
    assert re.fullmatch(r"[0-9a-z]{26}", refund["out_request_no"])
    # 审计
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_events WHERE action=%s",
                        ("support_payment.refund",))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_refund_only_paid_orders_and_cumulative_cap(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    c, _owner = _admin_client()
    unpaid = _make_order(rt)
    r = c.post("/api/admin/v1/support-payments/%s/refund"
               % unpaid["out_trade_no"], json={"amount_yuan": "1.00"})
    assert r.status_code == 409

    order = _paid_order(rt)  # 6.66

    def fake_refund(runtime, o, amount_fen, orn, reason=None):
        return {"fund_change": "Y", "refund_fee": sp.fen_to_yuan(amount_fen),
                "trade_no": "trd"}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    ok = c.post("/api/admin/v1/support-payments/%s/refund"
                % order["out_trade_no"], json={"amount_yuan": "6.00"})
    assert ok.status_code == 200
    over = c.post("/api/admin/v1/support-payments/%s/refund"
                  % order["out_trade_no"], json={"amount_yuan": "1.00"})
    assert over.status_code == 409
    assert over.get_json()["error"]["code"] == "refund_exceeds_paid"


def test_refund_retry_reuses_out_request_no(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _paid_order(rt)
    c, _owner = _admin_client()

    calls = []

    def fake_refund(runtime, o, amount_fen, orn, reason=None):
        calls.append((orn, amount_fen))
        return {"fund_change": "N", "refund_fee": None, "trade_no": "trd"}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    # R2-3：操作员显式传 out_request_no = 直接同号重试受理（不做查询前置
    # 门槛）。首受理 fund_change=N → unknown；显式重试复用同号同金额。
    r1 = c.post("/api/admin/v1/support-payments/%s/refund"
                % order["out_trade_no"], json={"amount_yuan": "3.00"})
    orn = r1.get_json()["refund"]["out_request_no"]
    assert r1.get_json()["refund"]["status"] == "unknown"  # fund_change=N
    # 同号显式重试：同一 out_request_no、同一金额再次受理
    r2 = c.post("/api/admin/v1/support-payments/%s/refund"
                % order["out_trade_no"],
                json={"amount_yuan": "3.00", "out_request_no": orn})
    assert r2.status_code == 200
    assert r2.get_json()["refund"]["status"] == "unknown"  # 仍 N → 待确认
    # 同号不同金额 → 拒绝（契约：防重复退款）
    r3 = c.post("/api/admin/v1/support-payments/%s/refund"
                % order["out_trade_no"],
                json={"amount_yuan": "4.00", "out_request_no": orn})
    assert r3.status_code == 409
    # 受理只对同一 out_request_no 发生（不产生新请求号）
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == orn
    assert calls[0][1] == calls[1][1] == 300
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_refunds"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 1  # 不产生新退款行
    finally:
        conn.close()


def test_refund_query_confirms_success(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _paid_order(rt)
    c, _owner = _admin_client()

    def fake_refund(runtime, o, amount_fen, orn, reason=None):
        return {"fund_change": "N", "refund_fee": None, "trade_no": None}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    monkeypatch.setattr(sp, "query_refund", lambda runtime, o, orn: {
        "refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
        "trade_no": "trd"})
    r1 = c.post("/api/admin/v1/support-payments/%s/refund"
                % order["out_trade_no"], json={"amount_yuan": "3.00"})
    orn = r1.get_json()["refund"]["out_request_no"]
    r2 = c.post("/api/admin/v1/support-payments/%s/refund-query"
                % order["out_trade_no"], json={"out_request_no": orn})
    assert r2.status_code == 200
    assert r2.get_json()["refund"]["status"] == "refunded"
    assert r2.get_json()["alipay_refund_status"] == "REFUND_SUCCESS"


# --------------------------------------------------------------------------- #
# 10. 生产模式 fail-closed
# --------------------------------------------------------------------------- #
def test_production_mode_without_config_fails_closed(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_ENABLED", True)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {})
    for name in ("SUPPORT_ALIPAY_PAY_APP_ID",
                 "SUPPORT_ALIPAY_PAY_SELLER_PID",
                 "SUPPORT_ALIPAY_PAY_APP_PRIVATE_KEY_FILE",
                 "SUPPORT_ALIPAY_PAY_ALIPAY_PUBLIC_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    c = _client()
    # 配置缺失 → 支付功能整体不可用（404；绝不回退沙箱/示例配置）
    assert c.post("/support/orders", data={"amount": "6.66"}).status_code == 404
    assert c.get("/support/return").status_code == 404
    with pytest.raises(sp.SupportPayConfigError):
        sp.load_runtime_config("production")
    # sweep 同样拒绝
    with pytest.raises(sp.SupportPayConfigError):
        sp.get_runtime("production")


def test_production_mode_with_partial_config_still_closed(
        tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_ENABLED", True)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {})
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_APP_ID", "2026000000000001")
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_SELLER_PID", "2088000000000001")
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_APP_PRIVATE_KEY_FILE",
                       "/nonexistent/app_private_key.pem")
    monkeypatch.delenv("SUPPORT_ALIPAY_PAY_ALIPAY_PUBLIC_KEY_FILE",
                       raising=False)
    with pytest.raises(sp.SupportPayConfigError):
        sp.load_runtime_config("production")


# --------------------------------------------------------------------------- #
# 11. 密钥材料不出现在响应/日志
# --------------------------------------------------------------------------- #
def test_key_material_never_in_responses_or_logs(_iso, privkey, caplog):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    caplog.clear()
    with caplog.at_level("DEBUG"):
        page1 = c.get(
            "/support/pay/%s" % order["out_trade_no"]).get_data(as_text=True)
        params = _notify_params(rt, order, privkey)
        params["total_amount"] = "1.23"
        notify_resp = _post_notify(c, params)  # 验签失败路径
        page2 = c.get(
            "/support/return?out_trade_no=%s"
            % order["out_trade_no"]).get_data(as_text=True)
        app_mod.app.logger.error("support pay form build failed: RuntimeError")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (rt.app_private_key, rt.alipay_public_key, params["sign"]):
        assert secret not in blob
        assert secret not in page1
        assert secret not in page2
        assert secret not in notify_resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 12. 通知路径精确豁免（不放松其他校验面）
# --------------------------------------------------------------------------- #
def test_csrf_exemption_is_precise_to_notify_path(_iso):
    assert app_mod._csrf_exempt_path("/support/alipay/notify") is True
    assert app_mod._csrf_exempt_path("/support/orders") is False
    assert app_mod._csrf_exempt_path("/support/pay/x") is False
    assert app_mod._csrf_exempt_path("/support/return") is False
    assert app_mod._csrf_exempt_path("/support/alipay/notify/extra") is False


# --------------------------------------------------------------------------- #
# 12b. 会话提醒卡（R2 B1）+ 状态轮询端点（R2 B2）
# --------------------------------------------------------------------------- #
def test_support_notice_card_pending_and_paid(_iso):
    """会话内有未决订单 → 「待确认」卡（金额+查看结果链接）；24h 内 paid →
    「查看致谢凭条」卡；无会话 refs / 他方订单 → 无卡（不泄露）。"""
    rt = sp.get_runtime("sandbox")
    c = _client()
    # 无会话 refs：无卡
    assert "pay-notice" not in _client().get("/support").get_data(as_text=True)

    order = _make_order(rt)
    _own_session(c, order["out_trade_no"])
    body = c.get("/support").get_data(as_text=True)
    assert "pay-notice" in body
    assert "你有一笔支持待确认" in body
    assert ("/support/return?out_trade_no=%s" % order["out_trade_no"]) in body

    # 支付后 → 凭条卡
    sp.mark_paid_idempotent(order["out_trade_no"], "trd-notice")
    body2 = c.get("/support").get_data(as_text=True)
    assert "查看致谢凭条" in body2
    assert "你有一笔支持待确认" not in body2

    # 他方订单（新 client，无该 ref 的会话）：无卡
    assert "pay-notice" not in _client().get("/support").get_data(as_text=True)


def test_status_endpoint_own_foreign_unknown_identical(_iso, monkeypatch):
    """状态端点会话绑定：他方/未知/畸形 ref 一致返回 neutral（字节级一致），
    且不触发服务端查单。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: calls.append(otn)
                        or {"state": "PAID", "total_amount": "6.66",
                            "trade_no": "x", "trade_status": "TRADE_SUCCESS"})
    foreign = _client().get(  # 全新 client：会话里没有这个 ref
        "/support/orders/%s/status" % order["out_trade_no"])
    unknown = _client().get("/support/orders/%s/status" % ("sp" + "0" * 24))
    malformed = _client().get("/support/orders/nope/status")
    for r in (foreign, unknown, malformed):
        assert r.status_code == 200
        assert r.get_json() == {"status": "neutral"}
    assert foreign.get_data() == unknown.get_data()
    assert calls == []  # 他方/未知订单绝不触发查单


def test_status_endpoint_throttles_query_within_three_seconds(
        _iso, monkeypatch):
    """3s 节流：两次紧邻的状态轮询只触发一次服务端 trade.query。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: calls.append(otn)
                        or {"state": "PENDING", "total_amount": "6.66",
                            "trade_no": None, "trade_status": "WAIT_BUYER_PAY"})
    r1 = c.get("/support/orders/%s/status" % order["out_trade_no"])
    r2 = c.get("/support/orders/%s/status" % order["out_trade_no"])
    assert r1.get_json() == {"status": "paying", "confirmed": True,
                             "expired": False}
    assert r2.get_json() == {"status": "paying", "confirmed": True,
                             "expired": False}
    assert len(calls) == 1  # 第二次命中 3s 节流，不再查询


def test_status_endpoint_paid_transition_requires_exact_amount(
        _iso, monkeypatch):
    """轮询中 verified PAID（金额精确匹配）→ 落库 paid 并返回 paid + 礼物
    字段；金额不符 → 保持 paying（等待 sweep/notify）。"""
    rt = sp.get_runtime("sandbox")
    c = _client()

    o1 = _make_order(rt)
    _own_session(c, o1["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: {"state": "PAID",
                                              "total_amount": "6.66",
                                              "trade_no": "trd-ok",
                                              "trade_status": "TRADE_SUCCESS"})
    r = c.get("/support/orders/%s/status" % o1["out_trade_no"])
    data = r.get_json()
    assert data["status"] == "paid"
    assert data["amount_yuan"] == "6.66"
    assert data["tier"] is None          # 666 非档位金额
    assert data["gift"] == "fish_treat"  # 自定义金额 → 小鱼干
    row = sp.get_order(o1["out_trade_no"])
    assert row["status"] == "paid" and row["trade_no"] == "trd-ok"

    o2 = _make_order(rt, amount_fen=1888)
    _own_session(c, o2["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: {"state": "PAID",
                                              "total_amount": "6.66",
                                              "trade_no": "trd-bad",
                                              "trade_status": "TRADE_SUCCESS"})
    r2 = c.get("/support/orders/%s/status" % o2["out_trade_no"])
    assert r2.get_json() == {"status": "paying", "confirmed": False,
                             "expired": False}  # 金额不符不认账
    assert sp.get_order(o2["out_trade_no"])["status"] == "paying"


def test_status_endpoint_paid_payload_per_tier(_iso):
    """paid 载荷的 tier/gift 只由本地订单行 amount_fen 推导：三档礼物 + 自定义
    → fish_treat；非 paid 响应绝无额外字段。"""
    rt = sp.get_runtime("sandbox")
    for fen, tier, gift in ((100, "cat_food", "cat_food"),
                            (600, "cat_toy", "cat_toy"),
                            (1800, "mystery", "mystery"),
                            (1888, None, "fish_treat")):
        order = _make_order(rt, amount_fen=fen)
        sp.mark_paid_idempotent(order["out_trade_no"], "trd-payload-%s" % fen)
        c = _own_session(_client(), order["out_trade_no"])
        data = c.get(
            "/support/orders/%s/status" % order["out_trade_no"]).get_json()
        assert data == {"status": "paid",
                        "amount_yuan": sp.fen_to_yuan(fen),
                        "tier": tier,
                        "gift": gift}, (fen, data)


def test_status_endpoint_non_paid_payload_has_no_gift_fields(_iso):
    """非 paid（paying）响应保持最小契约：status + confirmed/expired 两个
    查询置信度键（2026-10-23 评审 P2），绝无礼物/凭条字段；neutral 同样
    无额外字段。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    assert c.get(
        "/support/orders/%s/status" % order["out_trade_no"]).get_json() \
        == {"status": "paying", "confirmed": False, "expired": False}
    assert _client().get(
        "/support/orders/%s/status" % ("sp" + "0" * 24)).get_json() \
        == {"status": "neutral"}


def test_collectible_mapping_helper():
    """档位礼物与档位同名；非档位金额（含自定义）→ fish_treat（服务端权威）。"""
    assert sp.gift_collectible_for_amount(100) == "cat_food"
    assert sp.gift_collectible_for_amount(600) == "cat_toy"
    assert sp.gift_collectible_for_amount(1800) == "mystery"
    for other in (666, 1888, 6666, 101, 5000, 599, 601, 0):
        assert sp.gift_collectible_for_amount(other) == "fish_treat", other


def test_status_endpoint_runtime_mismatch_no_query(_iso, rt, monkeypatch):
    """订单/配置归属不匹配 → 不查单，返回本地状态。"""
    order = _make_order(rt)
    prod = sp.RuntimeConfig("production", sp.GATEWAY_PRODUCTION,
                            "2026000000000001", "2088000000000001",
                            rt.app_private_key, rt.alipay_public_key)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"production": prod})
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: calls.append(otn)
                        or {"state": "PAID", "total_amount": "6.66",
                            "trade_no": "x", "trade_status": "TRADE_SUCCESS"})
    c = _client()
    _own_session(c, order["out_trade_no"])
    r = c.get("/support/orders/%s/status" % order["out_trade_no"])
    assert r.status_code == 200
    assert r.get_json() == {"status": "paying", "confirmed": False,
                            "expired": False}
    assert calls == []


def test_status_endpoint_rate_limited_per_ip(_iso, rt, monkeypatch):
    """每 IP 限流：超过限额 → 429 slow_down（auth_limit_store 模式）。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    monkeypatch.setattr(auth_limit_store, "SUPPORT_STATUS_IP_ATTEMPT_LIMIT", 2)
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: {"state": "PENDING",
                                              "total_amount": "6.66",
                                              "trade_no": None,
                                              "trade_status": "WAIT_BUYER_PAY"})
    assert c.get("/support/orders/%s/status"
                 % order["out_trade_no"]).status_code == 200
    assert c.get("/support/orders/%s/status"
                 % order["out_trade_no"]).status_code == 200
    r3 = c.get("/support/orders/%s/status" % order["out_trade_no"])
    assert r3.status_code == 429
    assert r3.get_json() == {"status": "slow_down"}


def test_return_page_pending_has_polling_attrs(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    body = c.get("/support/return?out_trade_no=%s"
                 % order["out_trade_no"]).get_data(as_text=True)
    assert 'data-return-pending' in body
    assert ('data-status-url="/support/orders/%s/status"'
            % order["out_trade_no"]) in body


def test_return_page_paid_shows_receipt_with_verified_data(
        _iso, monkeypatch):
    """已支付回跳页：凭条展示核实金额 + 订单号 + 用途说明 + 重播按钮，
    并带平滑打印动画类。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    _mock_query(monkeypatch, state="PAID", amount="6.66",
                trade_no="20261021real")
    body = c.get("/support/return?out_trade_no=%s"
                 % order["out_trade_no"]).get_data(as_text=True)
    assert "receipt-live" in body
    assert "receipt-replay" in body
    assert "¥6.66" in body and order["out_trade_no"] in body
    assert "你的支持将用于 HistoPilot 的服务器与日常维护。" in body
    assert "不代表付款记录" not in body


# --------------------------------------------------------------------------- #
# 12c. 猫咪礼物档（R2 add-on）：服务端按精确金额推导档位
# --------------------------------------------------------------------------- #
def test_gift_tier_mapping_exact_amounts_only():
    assert sp.gift_tier_for_amount(100) == "cat_food"
    assert sp.gift_tier_for_amount(600) == "cat_toy"
    assert sp.gift_tier_for_amount(1800) == "mystery"
    for other in (666, 1888, 6666, 101, 5000, 599, 601, 1799, 6799, 0):
        assert sp.gift_tier_for_amount(other) is None, other


def test_support_page_renders_tier_cards_with_names_and_prices(_iso):
    body = _client().get("/support").get_data(as_text=True)
    assert "pay-tier" in body
    for yuan, name in (("1.00", "一份猫粮"), ("6.00", "猫玩具"),
                       ("18.00", "神秘礼物")):
        assert 'value="%s"' % yuan in body
        assert name in body


# --------------------------------------------------------------------------- #
# 12d. 喂猫互动（R4 add-on）：/support 猫卡礼物只来自本会话已支付订单
# --------------------------------------------------------------------------- #
def test_support_page_cat_gifts_only_for_session_paid_orders(_iso):
    """礼物本体 + 托盘按钮只渲染**本会话** status='paid' 的订单（服务端
    权威）：未决/已关闭/他方会话一律不渲染；默认 hidden（JS 只揭示未喂过
    的）；舞台本体 aria-hidden（真正控件是托盘按钮）。"""
    rt = sp.get_runtime("sandbox")
    c = _client()

    # 无会话订单：无礼物元素（托盘容器为空）
    body = c.get("/support").get_data(as_text=True)
    assert 'class="cat-gift ' not in body
    assert "data-gift=" not in body

    o_pending = sp.create_order(rt, 100)
    sp.mark_paying(o_pending["out_trade_no"])              # 未决：不算
    o_toy = sp.create_order(rt, 600)
    sp.mark_paid_idempotent(o_toy["out_trade_no"], "trd-gift1")
    o_closed = sp.create_order(rt, 1800)
    sp.mark_closed_idempotent(o_closed["out_trade_no"], "sweep:expired")
    o_fish = sp.create_order(rt, 1888)                     # 自定义 → 小鱼干
    sp.mark_paid_idempotent(o_fish["out_trade_no"], "trd-gift2")
    _own_session(c, o_pending["out_trade_no"], o_toy["out_trade_no"],
                 o_closed["out_trade_no"], o_fish["out_trade_no"])
    body2 = c.get("/support").get_data(as_text=True)
    assert 'data-gift="cat_toy"' in body2
    assert 'data-gift="fish_treat"' in body2
    assert 'data-gift="cat_food"' not in body2      # 未决不算
    assert 'data-gift="mystery"' not in body2       # 关闭不算
    # 礼物元素（本体 + 托盘按钮）只挂已支付订单的 ref
    assert body2.count('data-ref="%s"' % o_toy["out_trade_no"]) == 2
    assert body2.count('data-ref="%s"' % o_fish["out_trade_no"]) == 2
    assert 'data-ref="%s"' % o_pending["out_trade_no"] not in body2
    assert 'data-ref="%s"' % o_closed["out_trade_no"] not in body2
    # 本体 hidden + aria-hidden（在 role=button 舞台内）；托盘是真按钮
    assert re.search(
        r'class="cat-gift cat-gift-cat_toy" data-ref="%s"'
        r'\s+data-gift="cat_toy"\s+aria-hidden="true" hidden'
        % o_toy["out_trade_no"], body2)
    assert re.search(
        r'<button type="button" class="btn btn-secondary cat-gift-feed"\s+'
        r'data-ref="%s"\s+data-gift="fish_treat" hidden'
        % o_fish["out_trade_no"], body2)
    assert "陪小猫玩小老鼠" in body2 and "喂小猫吃小鱼干" in body2
    assert "喂小猫吃猫粮" not in body2 and "把神秘礼物交给小猫" not in body2
    # 猫卡 section 有 id（回跳页领取卡锚点）
    assert 'id="cat-card"' in body2

    # 他方会话（新 client，无该 ref）：无礼物
    body3 = _client().get("/support").get_data(as_text=True)
    assert "data-gift=" not in body3


def test_support_page_cat_gifts_max_three_newest_first(_iso):
    """有界展示：会话内 4 份已支付 → 只渲染最新 3 份（按 paid_at 降序）。"""
    rt = sp.get_runtime("sandbox")
    c = _client()
    refs = []
    for i, fen in enumerate((100, 600, 1800, 1888)):
        o = sp.create_order(rt, fen)
        sp.mark_paid_idempotent(o["out_trade_no"], "trd-max-%s" % i)
        refs.append(o["out_trade_no"])
    _own_session(c, *refs)
    body = c.get("/support").get_data(as_text=True)
    rendered = set(re.findall(r'data-ref="(sp[0-9a-z]{24})"', body))
    assert rendered == set(refs[1:])          # 最旧的 100 被裁掉
    assert refs[0] not in rendered


def test_client_tier_field_is_never_trusted(_iso):
    """档位只由服务端 amount_fen 推导：提交多余 tier 字段一律无视。"""
    c = _client()
    r = _post_order(c, {"amount": "6.66", "tier": "cat_toy"})
    ref = r.headers["Location"].rsplit("/", 1)[-1]
    row = sp.get_order(ref)
    assert row["amount_fen"] == 666  # 不是档位金额
    assert row["subject"] == "HistoPilot 项目赞助"


def test_return_page_pickup_card_by_server_derived_gift(_iso, monkeypatch):
    """回跳页已支付：100 → cat_food 领取卡（去喂小猫）+ 凭条礼物行；
    非档位金额（666）→ fish_treat（一条小鱼干）同样有领取卡与礼物行。
    礼物动画舞台已退役（喂猫互动移到 /support 猫卡）。"""
    rt = sp.get_runtime("sandbox")
    c = _client()

    o1 = _make_order(rt, amount_fen=100)
    _own_session(c, o1["out_trade_no"])
    _mock_query(monkeypatch, state="PAID", amount="1.00",
                trade_no="trd-tier1")
    body1 = c.get("/support/return?out_trade_no=%s" % o1["out_trade_no"]) \
        .get_data(as_text=True)
    assert "gift-pickup" in body1
    assert "gift-svg-bowl" in body1
    assert "一份猫粮已经放到小猫身边啦" in body1
    assert 'href="/support#cat-card"' in body1
    assert "去喂小猫" in body1
    assert "这份心意" in body1 and "一份猫粮" in body1
    # 礼物动画舞台不再出现在回跳页（喂猫互动移到 /support 猫卡）
    assert "gift-stage" not in body1

    o2 = _make_order(rt, amount_fen=666)
    _own_session(c, o2["out_trade_no"])
    _mock_query(monkeypatch, state="PAID", amount="6.66",
                trade_no="trd-tier2")
    body2 = c.get("/support/return?out_trade_no=%s" % o2["out_trade_no"]) \
        .get_data(as_text=True)
    # 自定义/非档位金额：礼物是小鱼干（凭条行 + 领取卡）
    assert "一条小鱼干" in body2
    assert "一条小鱼干已经放到小猫身边啦" in body2
    assert "gift-svg-fish" in body2


def test_return_page_mystery_tier_pickup_card(_iso, monkeypatch):
    rt = sp.get_runtime("sandbox")
    o = _make_order(rt, amount_fen=1800)
    c = _own_session(_client(), o["out_trade_no"])
    _mock_query(monkeypatch, state="PAID", amount="18.00",
                trade_no="trd-tier3")
    body = c.get("/support/return?out_trade_no=%s" % o["out_trade_no"]) \
        .get_data(as_text=True)
    assert "一个神秘礼物已经放到小猫身边啦" in body
    assert "gift-svg-modem" in body
    # 机架/猫动画舞台只属于 /support 的喂猫互动，回跳页不再渲染
    assert "gift-svg-rack" not in body
    assert "cat-svg" not in body


# --------------------------------------------------------------------------- #
# 13. 资金正确性回归：closed→paid 恢复 / 原子通知 / 绝对超时 / CSP 跳转链
# --------------------------------------------------------------------------- #
def test_sdk_model_has_time_expire_field():
    """安装源码核实：AlipayTradePagePayModel 存在可写的 time_expire 字段
    （page.pay 绝对超时载体；timeout_express 是另一字段，不再使用）。"""
    from alipay.aop.api.domain.AlipayTradePagePayModel import \
        AlipayTradePagePayModel

    model = AlipayTradePagePayModel()
    assert hasattr(model, "time_expire")
    model.time_expire = "2026-10-21 12:00:00"
    assert model.time_expire == "2026-10-21 12:00:00"


def test_order_time_expire_is_absolute_shanghai():
    """time_expire = created_at + 15m，Asia/Shanghai，yyyy-MM-dd HH:mm:ss。"""
    created = datetime.datetime(2026, 10, 21, 4, 0, 0,
                                tzinfo=datetime.timezone.utc)  # 北京 12:00
    assert sp.order_time_expire(created) == "2026-10-21 12:15:00"
    # naive 输入按 UTC 解释（防御）
    assert sp.order_time_expire(
        datetime.datetime(2026, 10, 21, 4, 0, 0)) == "2026-10-21 12:15:00"


def test_form_action_origins_per_mode():
    """沙箱/生产跳转链 origin 列表各自独立、精确无通配；未知模式报错。"""
    assert sp.form_action_origins("sandbox") == (
        "https://openapi-sandbox.dl.alipaydev.com",
        "https://unitradeprod-sandbox.dl.alipaydev.com",
        "https://excashier-sandbox.dl.alipaydev.com")
    assert sp.form_action_origins("production") == (
        "https://openapi.alipay.com",
        "https://unitradeprod.alipay.com",
        "https://excashier.alipay.com")
    with pytest.raises(sp.SupportPayConfigError):
        sp.form_action_origins("other")


def test_pay_page_csp_lists_full_redirect_chain(_iso):
    """支付页 CSP form-action = 当前模式完整跳转链（网关+中转+收银台），
    不混入另一模式的主机。"""
    order = _make_order(sp.get_runtime("sandbox"))
    csp = _own_session(_client(), order["out_trade_no"]).get(
        "/support/pay/%s" % order["out_trade_no"]
    ).headers["Content-Security-Policy"]
    m = re.search(r"form-action ([^;]+);", csp)
    assert m is not None
    assert m.group(1).split() == SANDBOX_CSP_ORIGINS
    assert "excashier.alipay.com" not in csp
    assert "unitradeprod.alipay.com " not in csp + " "


def test_notify_atomic_rollback_allows_retry(_iso, privkey, monkeypatch):
    """事件插入后状态推进失败 → 整体回滚（事件不留半截记录）→ failure；
    支付宝重试（同一事件重放）成功落库为 paid。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    params = _notify_params(rt, order, privkey, notify_id="ntf-atomic")
    real_transition = sp._apply_paid_transition

    def flaky(cur, otn, trade_no):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(sp, "_apply_paid_transition", flaky)
    r = _post_notify(_client(), params)
    assert r.get_data(as_text=True) == "failure"
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 0  # 事件随事务回滚
    finally:
        conn.close()
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"

    monkeypatch.setattr(sp, "_apply_paid_transition", real_transition)
    r2 = _post_notify(_client(), params)  # 同一通知重放（同一事件键）
    assert r2.get_data(as_text=True) == "success"
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    assert row["trade_no"] == params["trade_no"]
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_notify_events"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 1  # 重放只落一次事件
    finally:
        conn.close()


def test_sweep_does_not_close_before_time_expire_deadline(_iso, monkeypatch):
    """NOT_EXIST 且未到 time_expire+余量（15m+120s）：sweep 绝不提前关单；
    过线后才允许本地关单。"""
    rt = sp.get_runtime("sandbox")
    o = _make_order(rt)
    _backdate(o["out_trade_no"], minutes=16)  # > min_age(16m) 但 < 17m 截止线
    monkeypatch.setattr(sp, "query_trade", lambda runtime, otn: {
        "state": "NOT_EXIST", "total_amount": None,
        "trade_no": None, "trade_status": None})
    closed_calls = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, otn: closed_calls.append(otn) or True)
    stats = sp.sweep(min_age_minutes=16)
    assert stats["closed"] == 0 and stats["skipped"] == 1
    assert closed_calls == []
    assert sp.get_order(o["out_trade_no"])["status"] == "paying"

    _backdate(o["out_trade_no"], minutes=20)  # 越过 17m 截止线
    stats2 = sp.sweep(min_age_minutes=16)
    assert stats2["closed"] == 1
    assert sp.get_order(o["out_trade_no"])["status"] == "closed"


def test_sweep_recovers_locally_closed_paid_order(_iso, monkeypatch, caplog):
    """本地关单后实付（通知从未到达）：sweep 查得 PAID → closed→paid 恢复
    + error 级告警；trade_no/paid_at 落库。"""
    rt = sp.get_runtime("sandbox")
    o = _make_order(rt)
    sp.mark_closed_idempotent(o["out_trade_no"], "sweep:expired")
    _backdate(o["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade", lambda runtime, otn: {
        "state": "PAID", "total_amount": "6.66",
        "trade_no": "20261021sweep-pay", "trade_status": "TRADE_SUCCESS"})
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="support_payments"):
        stats = sp.sweep(min_age_minutes=16)
    assert stats["paid"] == 1
    row = sp.get_order(o["out_trade_no"])
    assert row["status"] == "paid"
    assert row["trade_no"] == "20261021sweep-pay"
    assert row["paid_at"] is not None
    assert any("paid after local close" in rec.getMessage()
               for rec in caplog.records)


def test_return_page_recovers_paid_after_local_close(_iso, monkeypatch):
    """本地已关单 + 服务端查单实付：回跳页直接展示收据并落库 paid
    （重读订单行），同步参数仍不参与判定。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    c = _own_session(_client(), order["out_trade_no"])
    sp.mark_closed_idempotent(order["out_trade_no"], "sweep:expired")
    _mock_query(monkeypatch, state="PAID", amount="6.66",
                trade_no="20261021ret")
    r = c.get(
        "/support/return?out_trade_no=%s&trade_status=TRADE_SUCCESS"
        % order["out_trade_no"])
    body = r.get_data(as_text=True)
    assert "已收到你的支持" in body
    assert "¥6.66" in body
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid"
    assert row["trade_no"] == "20261021ret"


def test_mark_paid_twice_is_noop(_iso):
    """mark_paid_idempotent 幂等：第二次调用 False，trade_no/paid_at 不变。"""
    rt = sp.get_runtime("sandbox")
    order = _make_order(rt)
    assert sp.mark_paid_idempotent(order["out_trade_no"], "t1") is True
    row = sp.get_order(order["out_trade_no"])
    snapshot = (row["status"], row["trade_no"], row["paid_at"])
    assert sp.mark_paid_idempotent(order["out_trade_no"], "t2") is False
    row2 = sp.get_order(order["out_trade_no"])
    assert (row2["status"], row2["trade_no"], row2["paid_at"]) == snapshot
