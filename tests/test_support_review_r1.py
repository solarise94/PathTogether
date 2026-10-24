# -*- coding: utf-8 -*-
"""用户独立评审（2026-10-21）发现问题的回归测试（R1 修订版）。

来源：``/mnt/ssd2/tests/support-review-ZVyCwI/test_review_repros.py``——用户
的离线复现脚本。R1 评审要求：复现 4/5 的 mock **就是契约**，必须原样保留
（``sp.TradeOperationError("simulated response timeout after server accepted")``
无 sub_code；``lambda *args: called.append(args)`` 返回 None），只反转断言：

1. [P1] 静态收款方式（二维码/收款链接）整体退役：赞助只走官方下单接口；
2. [P1] 订单/配置归属：sandbox 订单绝不能用 production 配置签表单/查单/
   关单/退款；
3. [P1] 退款 fail-safe 分类：只有 ``sub_code`` 在白名单
   （``sp.REFUND_DEFINITE_REJECT_SUB_CODES``）内的异常才算明确业务拒绝；
   无 sub_code 的普通 ``TradeOperationError``、``ACQ.SYSTEM_ERROR``、
   ``isp.*``/未知码、``TradeOperationUncertainError``、任何异常、非 dict
   返回 → 一律 ``unknown``（额度占用），同一 ``out_request_no`` 解决前不得
   发新退款（额度校验+建行在每订单锁内串行）；
4. [P2] 重复提交：无 key 走会话级复用（同会话同金额 → 同一订单）；带
   idempotency key 时同 key 同金额并发只建一单、同 key 不同金额拒绝、
   剩余窗口不足 5 分钟不复用；
5. [P2] 部分退款允许低于 ¥1（最小 ¥0.01；受「剩余可退」约束）。

运行：cd 项目根 && python3 -m pytest tests/test_support_review_r1.py -q
"""
import logging
import threading

import pytest

import app as app_mod
import pg_store
import support_payments as sp
import test_support_payments as helpers

pytest_plugins = ["test_support_payments"]


def _prod_runtime(rt):
    """production 运行时快照（与用户复现脚本同构：真实测试密钥对）。"""
    return sp.RuntimeConfig(
        "production", sp.GATEWAY_PRODUCTION,
        "2026000000000001", "2088000000000001",
        rt.app_private_key, rt.alipay_public_key)


def _order_count(where="", params=()):
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_orders " + where,
                        params)
            return cur.fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 静态收款方式退役
# --------------------------------------------------------------------------- #
def test_static_collection_removed_with_pay_disabled(_iso, monkeypatch):
    """支付关闭 + 残留 URL 配置：页面没有收款链接/二维码，只有说明 + 小猫。"""
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_ENABLED", False)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_URL",
                        "https://qr.alipay.com/review-placeholder",
                        raising=False)  # 属性已随退役整体删除
    page = helpers._client().get("/support").get_data(as_text=True)
    assert 'id="alipay-link"' not in page
    assert "qr.alipay.com" not in page
    assert 'id="pay-qr"' not in page
    assert 'id="tips"' not in page
    # 页面仍是完整说明页（可陪玩小猫 + 邮箱联系）
    assert 'id="cat-stage"' in page
    assert "solarise94@gmail.com" in page


# --------------------------------------------------------------------------- #
# 4. 重复提交（复现 2 的精确脚手架：无 key，两次同会话同金额 POST）
# --------------------------------------------------------------------------- #
def test_same_checkout_submission_reuses_same_order(_iso):
    client = helpers._client()
    token = helpers._csrf_token(client)
    payload = {"amount": "6.66", "csrf_token": token}
    first = client.post("/support/orders", data=payload)
    second = client.post("/support/orders", data=payload)
    assert first.status_code == second.status_code == 303
    assert first.headers["Location"] == second.headers["Location"]
    assert client.get(first.headers["Location"]).status_code == 200
    # 服务端权威：恰好一个订单（不只是两次响应一致）
    assert _order_count() == 1


def test_same_session_different_amount_creates_new_order(_iso):
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders",
                        data={"amount": "6.66", "csrf_token": token})
    second = client.post("/support/orders",
                         data={"amount": "18.88", "csrf_token": token})
    assert first.status_code == second.status_code == 303
    assert first.headers["Location"] != second.headers["Location"]
    assert _order_count() == 2


def test_expired_order_not_reused(_iso):
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders",
                        data={"amount": "6.66", "csrf_token": token})
    helpers._backdate(
        first.headers["Location"].rsplit("/", 1)[-1], minutes=16)
    second = client.post("/support/orders",
                         data={"amount": "6.66", "csrf_token": token})
    assert second.status_code == 303
    assert first.headers["Location"] != second.headers["Location"]


# --------------------------------------------------------------------------- #
# 4-add-on：服务端 idempotency key（真双击 = 并发同 key）
# --------------------------------------------------------------------------- #
def test_concurrent_same_key_creates_exactly_one_order(_iso):
    """两个线程同 key 同金额并发 POST（起跑线后同时发车，真实 PG）：
    恰好一个订单行、同一 Location。"""
    key = "a" * 32
    prepared = []
    for _ in range(2):
        c = helpers._client()
        token = helpers._csrf_token(c)
        prepared.append((c, token))
    results = []
    barrier = threading.Barrier(2)

    def worker(client, token):
        barrier.wait()
        results.append(client.post(
            "/support/orders",
            data={"amount": "6.66", "idempotency_key": key,
                  "csrf_token": token}))

    threads = [threading.Thread(target=worker, args=pair)
               for pair in prepared]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2
    assert all(r.status_code == 303 for r in results), \
        [r.status_code for r in results]
    assert len({r.headers["Location"] for r in results}) == 1
    assert _order_count() == 1
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_orders"
                        " WHERE idempotency_key=%s", (key,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_same_key_different_amount_rejected_and_creates_nothing(_iso):
    client = helpers._client()
    key = "b" * 32
    first = client.post("/support/orders",
                        data={"amount": "6.66", "idempotency_key": key,
                              "csrf_token": helpers._csrf_token(client)})
    assert first.status_code == 303
    second = client.post("/support/orders",
                         data={"amount": "18.88", "idempotency_key": key,
                               "csrf_token": helpers._csrf_token(client)},
                         headers={"Accept": "application/json"})
    assert second.status_code == 409
    # /support/orders 的 JSON 错误形态是 {"error": "<code>"}
    assert second.get_json()["error"] == "order_conflict"
    assert _order_count() == 1  # 没有建任何新单


def test_order_with_less_than_five_minutes_left_not_reused(_iso, monkeypatch):
    """剩余窗口不足 5 分钟：**无 key 的会话回退路径**新建（绝不落在即将过期
    的收银台上）。带 key 的请求不受此限——key 与原订单永久绑定（R2-1），
    无论剩余窗口一律 303 回原订单支付页。"""
    client = helpers._client()
    key = "c" * 32
    token = helpers._csrf_token(client)
    first = client.post("/support/orders",
                        data={"amount": "6.66", "idempotency_key": key,
                              "csrf_token": token})
    ref1 = first.headers["Location"].rsplit("/", 1)[-1]
    helpers._backdate(ref1, minutes=11)  # 距 time_expire 只剩 4 分钟
    second = client.post("/support/orders",
                         data={"amount": "6.66", "idempotency_key": key,
                               "csrf_token": token})
    # R2-1：key 永久绑定原订单——即使是快过期的单也返回原单（绝不换新）
    assert second.status_code == 303
    assert second.headers["Location"] == first.headers["Location"]
    assert _order_count() == 1

    # 会话路径（无 key）同样遵守窗口：剩 6 分钟仍复用；剩 4 分钟新建
    client2 = helpers._client()
    token2 = helpers._csrf_token(client2)
    a = client2.post("/support/orders",
                     data={"amount": "6.66", "csrf_token": token2})
    helpers._backdate(a.headers["Location"].rsplit("/", 1)[-1], minutes=9)
    b = client2.post("/support/orders",
                     data={"amount": "6.66", "csrf_token": token2})
    assert b.headers["Location"] == a.headers["Location"]
    helpers._backdate(a.headers["Location"].rsplit("/", 1)[-1], minutes=11)
    # 脚手架增补（R5 规则 A）：剩 4 分钟的同金额旧单仍在可支付窗口内，下单
    # 会先核实/关单（query PENDING + close 成功）再建新单——真实 SDK 会出网，
    # 必须打桩；断言未做任何改动。
    monkeypatch.setattr(sp, "query_trade", lambda runtime, ref: {
        "state": "PENDING", "total_amount": "6.66",
        "trade_no": None, "trade_status": "WAIT_BUYER_PAY"})
    monkeypatch.setattr(sp, "close_trade", lambda runtime, ref: True)
    c_ = client2.post("/support/orders",
                      data={"amount": "6.66", "csrf_token": token2})
    assert c_.headers["Location"] != a.headers["Location"]


# --------------------------------------------------------------------------- #
# 2. 订单/配置归属
# --------------------------------------------------------------------------- #
def test_sandbox_order_cannot_be_signed_for_production(_iso, rt, monkeypatch):
    order = helpers._make_order(rt)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE",
                        {"production": _prod_runtime(rt)})
    client = helpers._client()
    helpers._own_session(client, order["out_trade_no"])
    page = client.get("/support/pay/" + order["out_trade_no"])
    body = page.get_data(as_text=True)
    # 中性页（200）：不建表单、不出现生产网关地址，订单未被改动
    assert page.status_code == 200
    assert sp.GATEWAY_PRODUCTION not in body
    assert "pay-form" not in body
    assert sp.get_order(order["out_trade_no"])["mode"] == "sandbox"


def test_sandbox_order_not_queried_for_production_on_return(_iso, rt,
                                                            monkeypatch):
    order = helpers._make_order(rt)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE",
                        {"production": _prod_runtime(rt)})
    queried = []

    def fake_query(runtime, otn):
        queried.append(otn)
        return {"state": "PAID", "total_amount": "6.66",
                "trade_no": "x", "trade_status": "TRADE_SUCCESS"}

    monkeypatch.setattr(sp, "query_trade", fake_query)
    client = helpers._client()
    helpers._own_session(client, order["out_trade_no"])
    r = client.get("/support/return?out_trade_no=" + order["out_trade_no"])
    assert r.status_code == 200
    assert "没有找到对应的订单" in r.get_data(as_text=True)
    assert queried == []  # 归属不匹配：不做服务端查单
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"


def test_sweep_skips_config_mismatch(_iso, rt, monkeypatch, caplog):
    order = helpers._make_order(rt)
    helpers._backdate(order["out_trade_no"])
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE",
                        {"production": _prod_runtime(rt)})
    queried, closed = [], []
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, otn: queried.append(otn)
                        or {"state": "PENDING", "total_amount": "6.66",
                            "trade_no": None, "trade_status": "WAIT_BUYER_PAY"})
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, otn: closed.append(otn) or True)
    with caplog.at_level(logging.WARNING, logger="support_payments"):
        stats = sp.sweep(min_age_minutes=16)
    assert stats["config_mismatch"] == 1
    assert stats["scanned"] == 0 and stats["closed"] == 0 and stats["paid"] == 0
    assert queried == [] and closed == []  # 不触碰支付宝侧、不关单
    assert sp.get_order(order["out_trade_no"])["status"] == "paying"
    # 日志只带订单号（无参数/密钥）
    logs = [rec.getMessage() for rec in caplog.records
            if "config mismatch" in rec.getMessage()]
    assert logs and order["out_trade_no"] in logs[0]


def test_refund_config_mismatch_409_without_alipay_call(_iso, rt, monkeypatch):
    order = helpers._paid_order(rt)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE",
                        {"production": _prod_runtime(rt)})
    client, _owner = helpers._admin_client()
    calls = []
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *args, **kw: calls.append(args))
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    response = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "order_runtime_mismatch"
    assert not calls  # 不发起任何支付宝调用
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_refunds")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. 不确定退款 ≠ 业务失败（复现 4/5 的精确脚手架，只反转断言）
# --------------------------------------------------------------------------- #
def test_uncertain_refund_marks_unknown_and_reuses_request_no(
        _iso, rt, monkeypatch):
    """复现 4 原样 mock（普通 TradeOperationError、无 sub_code——用户脚本
    的 ``lost_response`` 一字不改）：结果未知 → 行 unknown、额度占用 300；
    第二次请求只做同号解析，不生成新请求号、不建新行。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    seen_requests = []

    def lost_response(runtime, order, amount, request_no, reason=None):
        seen_requests.append(request_no)
        raise sp.TradeOperationError(
            "simulated response timeout after server accepted")

    monkeypatch.setattr(sp, "refund_trade", lost_response)
    # 同号退款查询：走一次仍无成功结论（解析步骤的配套脚手架）
    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order, request_no: {"refund_status": None,
                                            "refund_amount": None,
                                            "trade_no": None})
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    first = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert first.status_code == 502
    assert sp.get_refund(seen_requests[0])["status"] == "unknown"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300  # 占用不释放

    second = client.post(endpoint, json={"amount_yuan": "3.00"})
    # 未决未解决：阻塞新退款（409）；同号解析（查询+同号重试）——
    # 不生成新请求号、不建新行
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "refund_unresolved"
    assert len(seen_requests) == 2
    assert len(set(seen_requests)) == 1  # 同一 out_request_no
    assert sp.get_refund(seen_requests[0])["status"] == "unknown"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_refunds"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 1  # 没有新行
    finally:
        conn.close()


def test_uncertain_refund_resolved_via_same_request_no_query(
        _iso, rt, monkeypatch):
    """同号退款查询确认成功 → unknown 落 refunded，之后可正常发起新退款。
    （mock 同复现 4：普通 TradeOperationError，无 sub_code。）"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    seen_requests = []

    def lost_response(runtime, order, amount, request_no, reason=None):
        seen_requests.append(request_no)
        raise sp.TradeOperationError(
            "simulated response timeout after server accepted")

    monkeypatch.setattr(sp, "refund_trade", lost_response)

    def query_refund(runtime, order, request_no):
        assert request_no == seen_requests[0]  # 同号确认
        return {"refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
                "trade_no": "trd"}

    monkeypatch.setattr(sp, "query_refund", query_refund)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    first = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert first.status_code == 502
    assert sp.get_refund(seen_requests[0])["status"] == "unknown"

    second = client.post(endpoint, json={"amount_yuan": "3.00"})
    # R2-2：解析确认成功 → 直接返回该原始退款行（200、refunded）并停止——
    # 绝不在解析成功的同一请求里再发起新退款
    assert second.status_code == 200
    assert second.get_json()["refund"]["status"] == "refunded"
    assert second.get_json()["refund"]["out_request_no"] == seen_requests[0]
    assert len(seen_requests) == 1  # 未再发起第二次受理
    assert sp.get_refund(seen_requests[0])["status"] == "refunded"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300


def test_discordant_repeat_request_is_unknown_blocked_and_alerted(
        _iso, rt, monkeypatch, caplog):
    """ACQ.DISCORDANT_REPEAT_REQUEST（同号下已有参数不同的请求——先前请求
    可能已被受理）不是业务拒绝：行 unknown、额度占用、同号阻塞，并记只含
    订单号的告警。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    seen = []

    def discordant(runtime, order, amount, request_no, reason=None):
        seen.append(request_no)
        exc = sp.TradeOperationError("inconsistent repeat request")
        exc.sub_code = "ACQ.DISCORDANT_REPEAT_REQUEST"
        raise exc

    monkeypatch.setattr(sp, "refund_trade", discordant)
    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order, request_no: {"refund_status": None,
                                            "refund_amount": None,
                                            "trade_no": None})
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    with caplog.at_level(logging.WARNING, logger="support_payments"):
        first = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert first.status_code == 502
    assert first.get_json()["error"]["code"] == "refund_uncertain"
    assert sp.get_refund(seen[0])["status"] == "unknown"  # 不是 failed
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300  # 额度仍占用
    # 告警：只含订单号（无参数/请求号/密钥）
    alerts = [rec.getMessage() for rec in caplog.records
              if "discordant repeat request" in rec.getMessage()]
    assert alerts and order["out_trade_no"] in alerts[0]
    assert seen[0] not in alerts[0]

    second = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert second.status_code == 409  # 未决阻塞
    assert second.get_json()["error"]["code"] == "refund_unresolved"
    assert len(seen) == 2 and len(set(seen)) == 1  # 同号解析，不产生新号
    assert sp.get_refund(seen[0])["status"] == "unknown"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_refunds"
                        " WHERE out_trade_no=%s", (order["out_trade_no"],))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_failed_refund_explicit_retry_respects_released_budget(
        _iso, rt, monkeypatch):
    """R2 复核：failed 行的额度已释放，此后其他退款可能用掉预算——显式重试
    该行必须重跑累计校验：超支 → 409 refund_exceeds_paid 且不发起任何
    支付宝调用。"""
    order = helpers._paid_order(rt)  # 实付 6.66
    client, _owner = helpers._admin_client()
    calls = []

    def refund(runtime, order, amount, request_no, reason=None):
        calls.append((request_no, amount))
        if len(calls) == 1:
            exc = sp.TradeOperationError("refund refused by alipay")
            exc.sub_code = "ACQ.REFUND_AMT_NOT_EQUAL_TOTAL"  # 白名单明确拒绝
            raise exc
        return {"fund_change": "Y", "trade_no": "trd", "refund_fee": None}

    monkeypatch.setattr(sp, "refund_trade", refund)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    # 1) 3.00 明确拒绝 → failed、额度释放
    r1 = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert r1.status_code == 502
    failed_no = calls[0][0]
    assert sp.get_refund(failed_no)["status"] == "failed"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 0
    # 2) 新退款 6.66：正常受理（预算 666 全部用掉）
    r2 = client.post(endpoint, json={"amount_yuan": "6.66"})
    assert r2.status_code == 200
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 666
    # 3) 显式重试 failed 的 3.00：366? 不——666 已占用，+300 超支 → 409，
    #    且不发起任何支付宝调用
    r3 = client.post(endpoint,
                     json={"amount_yuan": "3.00",
                           "out_request_no": failed_no})
    assert r3.status_code == 409
    assert r3.get_json()["error"]["code"] == "refund_exceeds_paid"
    assert len(calls) == 2  # 没有为这次重试发起受理
    assert sp.get_refund(failed_no)["status"] == "failed"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 666


def test_failed_refund_explicit_retry_with_headroom_proceeds_same_number(
        _iso, rt, monkeypatch):
    """failed 行显式重试：剩余额度足够时按原号重新受理并成功。"""
    order = helpers._paid_order(rt)  # 实付 6.66
    client, _owner = helpers._admin_client()
    calls = []

    def refund(runtime, order, amount, request_no, reason=None):
        calls.append((request_no, amount))
        if len(calls) == 1:
            exc = sp.TradeOperationError("refund refused by alipay")
            exc.sub_code = "ACQ.REFUND_AMT_NOT_EQUAL_TOTAL"
            raise exc
        return {"fund_change": "Y", "trade_no": "trd", "refund_fee": None}

    monkeypatch.setattr(sp, "refund_trade", refund)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    r1 = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert r1.status_code == 502
    failed_no = calls[0][0]
    assert sp.get_refund(failed_no)["status"] == "failed"

    # 预算未动（sum 0）：0 + 300 ≤ 666 → 同号重试受理成功
    r2 = client.post(endpoint,
                     json={"amount_yuan": "3.00",
                           "out_request_no": failed_no})
    assert r2.status_code == 200
    assert r2.get_json()["refund"]["status"] == "refunded"
    assert r2.get_json()["refund"]["out_request_no"] == failed_no
    assert len(calls) == 2
    assert calls[1][0] == failed_no and calls[1][1] == 300  # 同号同金额
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300


def test_service_error_sub_code_is_unknown_not_failed(_iso, rt, monkeypatch):
    """ACQ.SYSTEM_ERROR（系统错误）不是业务拒绝 → unknown，额度占用。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    seen = []

    def system_error(runtime, order, amount, request_no, reason=None):
        seen.append(request_no)
        exc = sp.TradeOperationError("system busy")
        exc.sub_code = "ACQ.SYSTEM_ERROR"
        raise exc

    monkeypatch.setattr(sp, "refund_trade", system_error)
    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order, request_no: {"refund_status": None,
                                            "refund_amount": None,
                                            "trade_no": None})
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    first = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert first.status_code == 502
    assert first.get_json()["error"]["code"] == "refund_uncertain"
    assert sp.get_refund(seen[0])["status"] == "unknown"
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
    second = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert second.status_code == 409  # 未决阻塞，不产生新号
    assert len(set(seen)) == 1


def test_definite_allowlisted_rejection_marks_failed_and_releases(
        _iso, rt, monkeypatch):
    """白名单内的明确业务拒绝（官方错误码表 ACQ.REFUND_AMT_NOT_EQUAL_TOTAL）
    → failed、额度释放、允许新号重试。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    seen_requests = []

    def definite_rejection(runtime, order, amount, request_no, reason=None):
        seen_requests.append(request_no)
        exc = sp.TradeOperationError("refund refused by alipay")
        exc.sub_code = "ACQ.REFUND_AMT_NOT_EQUAL_TOTAL"
        raise exc

    monkeypatch.setattr(sp, "refund_trade", definite_rejection)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    first = client.post(endpoint, json={"amount_yuan": "3.00"})
    assert first.status_code == 502
    assert first.get_json()["error"]["code"] == "refund_failed"
    assert sp.get_refund(seen_requests[0])["status"] == "failed"
    # 明确业务失败 → 释放额度
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 0
    second = client.post(endpoint, json={"amount_yuan": "3.00"})
    # failed 不阻塞：允许用新请求号重试
    assert second.status_code == 502
    assert len(seen_requests) == 2
    assert seen_requests[0] != seen_requests[1]


# --------------------------------------------------------------------------- #
# 5. 低于 ¥1 的部分退款（复现 5 的精确脚手架：lambda 返回 None）
# --------------------------------------------------------------------------- #
def test_sub_yuan_refund_accepted(_iso, rt, monkeypatch):
    """复现 5 原样 mock（``lambda *args: called.append(args)``，返回 None）：
    ¥0.66 部分退款必须被受理（≠400），退款行落 unknown（非 dict 返回 = 结果
    未知，额度占用），无 500、行不得停留在 requested。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()
    called = []
    monkeypatch.setattr(sp, "refund_trade", lambda *args: called.append(args))
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    response = client.post(endpoint, json={"amount_yuan": "0.66"})
    assert response.status_code != 400
    assert response.status_code != 500  # 不再因 None 结果崩溃
    assert called  # 确实发起了退款受理
    request_no = called[0][3]  # args = (runtime, order, amount_fen, request_no, reason)
    row = sp.get_refund(request_no)
    assert row is not None
    assert row["status"] == "unknown"  # 非 dict 返回 = 结果未知（不是 requested）
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 66  # 额度占用
    assert response.get_json()["error"]["code"] == "refund_uncertain"


def test_sub_yuan_refund_fund_change_y_refunded(_iso, rt, monkeypatch):
    """有正常应答时，¥0.66 部分退款走 refunded。"""
    order = helpers._paid_order(rt)
    client, _owner = helpers._admin_client()

    def fake_refund(runtime, order, amount, request_no, reason=None):
        return {"fund_change": "Y", "refund_fee": "0.66", "trade_no": "trd"}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    response = client.post(endpoint, json={"amount_yuan": "0.66"})
    assert response.status_code == 200
    assert response.get_json()["refund"]["status"] == "refunded"
    assert response.get_json()["refund"]["refund_amount_yuan"] == "0.66"


def test_sub_yuan_refund_respects_remaining_cap(_iso, rt, monkeypatch):
    order = helpers._paid_order(rt)  # 实付 6.66
    client, _owner = helpers._admin_client()

    def fake_refund(runtime, order, amount, request_no, reason=None):
        return {"fund_change": "Y", "refund_fee": None, "trade_no": "trd"}

    monkeypatch.setattr(sp, "refund_trade", fake_refund)
    endpoint = ("/api/admin/v1/support-payments/"
                + order["out_trade_no"] + "/refund")
    r1 = client.post(endpoint, json={"amount_yuan": "6.00"})
    assert r1.status_code == 200
    r2 = client.post(endpoint, json={"amount_yuan": "0.66"})
    assert r2.status_code == 200  # 600 + 66 = 666 分，恰好退满
    r3 = client.post(endpoint, json={"amount_yuan": "0.01"})
    assert r3.status_code == 409
    assert r3.get_json()["error"]["code"] == "refund_exceeds_paid"
    # 单位/形态边界：0、三位小数、非字符串一律拒绝
    for bad in ("0", "0.001", "0.00"):
        r = client.post(endpoint, json={"amount_yuan": bad})
        assert r.status_code == 400, bad
    r_num = client.post(endpoint, json={"amount_yuan": 0.66})
    assert r_num.status_code == 400
