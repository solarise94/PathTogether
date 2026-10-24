# -*- coding: utf-8 -*-
"""用户评审 R3（2026-10-23）回归测试。

来源：/mnt/ssd2/tests/support-review-r3-iBYw3U/test_review_r3_regressions.py
——除本头注外逐字复制，脚手架与断言未做任何改动。断言的是评审确定的
安全行为：
  1) 下单会话复用命中（顺序重复提交兜底）必须把请求携带的新 checkout key
     后绑定到被复用订单——该单支付后同 key 重放仍返回原单，绝不另建第二个
     可支付订单；
  2) 未决退款解析确认成功必须把请求携带的新重试键同事务绑定到原退款行——
     同键重放直接命中原行，绝不产生第二个 out_request_no 重复退款。
"""

import test_support_payments as helpers
import support_payments as sp
import pg_store

pytest_plugins = ["test_support_payments"]


def count_orders():
    conn = pg_store.connect()
    try:
        return conn.execute("SELECT count(*) FROM support_orders").fetchone()[0]
    finally:
        conn.close()


def test_session_reuse_keeps_new_checkout_key_bound_after_payment(_iso):
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token,
        "idempotency_key": "1" * 32})
    payload = {"amount": "6.66", "csrf_token": token,
               "idempotency_key": "2" * 32}
    reused = client.post("/support/orders", data=payload)
    assert reused.status_code == 303
    before_replay = count_orders()
    ref = reused.headers["Location"].rsplit("/", 1)[-1]
    sp.mark_paid_idempotent(ref, "r3-paid-reused-order")
    replay = client.post("/support/orders", data=payload)
    count = count_orders()
    assert replay.headers["Location"] == reused.headers["Location"], (
        "same checkout key changed order after payment", first.headers["Location"],
        reused.headers["Location"], replay.headers["Location"], count)
    assert count == before_replay


def test_resolving_pending_refund_binds_new_retry_key(_iso, rt, monkeypatch):
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"])
    sp.update_refund_status(old["out_request_no"], "unknown")
    monkeypatch.setattr(sp, "query_refund", lambda *args: {
        "refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
        "trade_no": "r3-confirmed-refund"})
    calls = []
    monkeypatch.setattr(sp, "refund_trade", lambda *args: calls.append(args[3]) or {
        "fund_change": "Y", "trade_no": "r3-extra-refund"})
    path = "/api/admin/v1/support-payments/" + order["out_trade_no"] + "/refund"
    payload = {"amount_yuan": "3.00", "idempotency_key": "r3-recover-retry-key"}
    recovered = client.post(path, json=payload)
    assert recovered.status_code == 200
    assert recovered.get_json()["refund"]["out_request_no"] == old["out_request_no"]
    replay = client.post(path, json=payload)
    assert replay.status_code == 200
    assert replay.get_json()["refund"]["out_request_no"] == old["out_request_no"], (
        "same refund key issued a second refund", calls,
        sp.sum_refunded_fen(order["out_trade_no"]))
    assert calls == []
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
