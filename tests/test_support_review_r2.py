# -*- coding: utf-8 -*-
"""用户评审 R2（2026-10-21）回归测试。

来源：/mnt/ssd2/tests/support-review-r2-AyOcsm/test_review_r2_regressions.py
——除本头注外逐字复制，脚手架与断言未做任何改动。断言的是 R2 评审确定的
安全行为：
  1) 下单幂等 key 与原订单永久绑定（paid/closed 重放同 key 也返回原单）；
  2) 退款解析成功后返回原退款行（同一请求绝不再建新退款）；
  3) requested 未决行同样占用额度，查询异常即阻断且绝不盲目同号重试；
  4) CLI query/close 校验订单/配置归属，不匹配非零退出且不触碰支付宝；
  5) 并发首击的限流计数必须累加（不是都停在 1）。
"""

import threading
import test_support_payments as helpers
import support_payments as sp
import app as app_mod
import auth_limit_store
import pg_store

pytest_plugins = ["test_support_payments"]


def count_orders():
    conn = pg_store.connect()
    try:
        return conn.execute("SELECT count(*) FROM support_orders").fetchone()[0]
    finally:
        conn.close()


def test_replayed_paid_key_returns_original_order(_iso):
    client = helpers._client()
    payload = {"amount": "6.66", "csrf_token": helpers._csrf_token(client),
               "idempotency_key": "e" * 32}
    first = client.post("/support/orders", data=payload)
    ref = first.headers["Location"].rsplit("/", 1)[-1]
    sp.mark_paid_idempotent(ref, "review-paid-first")
    replay = client.post("/support/orders", data=payload)
    assert replay.status_code == 303
    assert replay.headers["Location"] == first.headers["Location"]
    assert count_orders() == 1


def test_expiring_key_replays_do_not_create_replacement_orders(_iso, rt):
    key = "f" * 32
    old = sp.create_order(rt, 666, idempotency_key=key)
    helpers._backdate(old["out_trade_no"], minutes=11)
    results = []
    for _ in range(2):
        client = helpers._client()
        helpers._own_session(client, old["out_trade_no"])
        results.append(client.post("/support/orders", data={
            "amount": "6.66", "idempotency_key": key,
            "csrf_token": helpers._csrf_token(client)}))
    assert all(r.status_code in (200, 303, 409) for r in results)
    assert count_orders() == 1


def test_refund_retry_after_confirmation_returns_original_refund(_iso, rt, monkeypatch):
    order = helpers._paid_order(rt)
    client, _ = helpers._admin_client()
    calls = []
    def refund(runtime, order, amount, request_no, reason=None):
        calls.append(request_no)
        if len(calls) == 1:
            raise sp.TradeOperationError("simulated response timeout after server accepted")
        return {"fund_change": "Y", "trade_no": "review-refund", "refund_fee": "3.00"}
    monkeypatch.setattr(sp, "refund_trade", refund)
    monkeypatch.setattr(sp, "query_refund", lambda *args: {
        "refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
        "trade_no": "review-refund"})
    path = "/api/admin/v1/support-payments/" + order["out_trade_no"] + "/refund"
    assert client.post(path, json={"amount_yuan": "3.00"}).status_code == 502
    assert client.post(path, json={"amount_yuan": "3.00"}).status_code == 200
    assert len(calls) == 1
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300


def test_requested_refund_after_crash_blocks_new_refund(_iso, rt, monkeypatch):
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    prior = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"])
    calls = []
    monkeypatch.setattr(sp, "refund_trade", lambda runtime, order, amount, number, reason=None:
                        calls.append(number) or {"fund_change": "Y", "trade_no": "review-new"})
    monkeypatch.setattr(sp, "query_refund", lambda *args: (_ for _ in ()).throw(
        AssertionError("existing requested refund is never queried")))
    response = client.post("/api/admin/v1/support-payments/" + order["out_trade_no"] + "/refund",
                           json={"amount_yuan": "3.00"})
    assert response.status_code in (409, 502)
    assert not calls
    assert sp.get_refund(prior["out_request_no"])["status"] in ("requested", "unknown")


def test_cli_query_rejects_mismatched_runtime(_iso, rt, monkeypatch):
    order = helpers._make_order(rt)
    prod = sp.RuntimeConfig("production", sp.GATEWAY_PRODUCTION,
                            "2026000000000001", "2088000000000001",
                            rt.app_private_key, rt.alipay_public_key)
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "load_runtime_config", lambda mode: prod)
    calls = []
    monkeypatch.setattr(sp, "query_trade", lambda runtime, number: calls.append(runtime.mode) or {
        "state": "PAID", "trade_status": "TRADE_SUCCESS", "trade_no": "review-wrong",
        "total_amount": "0.01"})
    assert sp.main(["query", "--out-trade-no", order["out_trade_no"]]) != 0
    assert not calls
    assert sp.get_order(order["out_trade_no"])["status"] == order["status"]


def test_concurrent_first_rate_limit_attempts_both_count(_iso):
    barrier = threading.Barrier(2)
    results, errors = [], []
    class CursorProxy:
        def __init__(self, cursor):
            self.cursor = cursor
        def execute(self, query, params):
            self.cursor.execute(query, params)
            if query.startswith("SELECT extract") and "FOR UPDATE" in query and self.cursor.rowcount == 0:
                barrier.wait(timeout=10)
            return self
        def fetchone(self):
            return self.cursor.fetchone()
        @property
        def rowcount(self):
            return self.cursor.rowcount
    def worker():
        conn = auth_limit_store._connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    results.append(auth_limit_store._record_failure(
                        CursorProxy(cur), "review_scope", "same-subject", 2, 900, 900, 1000))
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            conn.close()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors
    assert len(results) == 2
    conn = pg_store.connect()
    try:
        count, locked = conn.execute("SELECT failed_count, locked_until FROM auth_rate_limits "
                                     "WHERE scope='review_scope' AND subject_hash='same-subject'").fetchone()
        assert count == 2
        assert locked is not None
    finally:
        conn.close()
