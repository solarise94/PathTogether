# -*- coding: utf-8 -*-
"""R6 评审修复面回归（2026-10-24）：后继单已支付 → 过期视图 regenerate 不再建新单。

评审原文件（一条失败回归）见 ``tests/test_support_review_r6.py``；本文件
覆盖修复实现自身的分支与不变量，全部走端点/仓储真实链路（支付宝网络面
monkeypatch，调用留痕断言；**零预期调用时一旦调用立即失败**，防真实出网）：

1. 「后继单已支付」防护（``sp.find_session_paid_successor`` + regenerate）：
   A 过期 → 重建 B → B 已在别处支付落库后，过期视图携未用 key 再
   regenerate(A) → 200 点名 paid 载荷（``out_trade_no``=B）、零支付宝调用、
   无新行、无 open 单、key 保持未绑定（同 key 重放同响应；随后同 key 不同
   金额下单仍可建新单）；更早创建的同金额 paid 订单 / 不同金额 / 不在会话
   refs / 不同运行时快照（app_id）→ 一律不算后继，旧单表照常推进；后继
   paid 与 open 同金额单并存 → paid 优先（先于 R5 复用防护）；
2. R5 防护的 settle-为-PAID 子分支：防护把命中单核实为 paid 时同样零 key
   绑定返回点名 paid 载荷（不 close、旧单不被查询、不建新单）；
3. paid 载荷契约：旧单自身的 paid 路径（本地已 paid / 核实 PAID）与状态
   端点恒为**不点名**载荷（逐键断言）。

运行：cd 项目根 && python3 -m pytest tests/test_support_r6_20261024.py -q
"""
import pg_store
import pytest

import support_payments as sp
import test_support_payments as helpers

pytest_plugins = ["test_support_payments"]


# --------------------------------------------------------------------------- #
# 桩与小助手（同 test_support_r5_20261023 的桩风格：留痕 + 意外调用即抛）
# --------------------------------------------------------------------------- #
def _stub_alipay(monkeypatch, query=None, close=None):
    """支付宝桩：query_trade/close_trade 调用留痕（``(op, ref)`` 追加）。

    参数为 None → 该操作**不允许发生**（调用即抛 AssertionError，防真实
    出网）；传 dict → 作为应答返回；传异常实例 → 抛出。"""
    ops = []

    def fake_query(runtime, ref):
        ops.append(("query", ref))
        if query is None:
            raise AssertionError("unexpected query_trade(%s)" % ref)
        if isinstance(query, BaseException):
            raise query
        return query

    def fake_close(runtime, ref):
        ops.append(("close", ref))
        if close is None:
            raise AssertionError("unexpected close_trade(%s)" % ref)
        if isinstance(close, BaseException):
            raise close
        return True

    monkeypatch.setattr(sp, "query_trade", fake_query)
    monkeypatch.setattr(sp, "close_trade", fake_close)
    return ops


def _order_count():
    conn = pg_store.connect()
    try:
        return conn.execute(
            "SELECT count(*) FROM support_orders").fetchone()[0]
    finally:
        conn.close()


def _active_refs():
    """仍可支付（created/paying）的订单号——「不留 open 单」的断言面。"""
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT out_trade_no FROM support_orders"
                        " WHERE status IN ('created','paying')"
                        " ORDER BY out_trade_no")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _aged_session_order(client, token, minutes=11, amount="6.66", key=None):
    """经端点建单（自动记入会话）→ paying → 回拨 created_at 到指定分钟。"""
    data = {"amount": amount, "csrf_token": token}
    if key:
        data["idempotency_key"] = key
    resp = client.post("/support/orders", data=data)
    assert resp.status_code == 303
    ref = resp.headers["Location"].rsplit("/", 1)[-1]
    sp.mark_paying(ref)
    helpers._backdate(ref, minutes=minutes)
    return ref


def _regen(client, ref, key):
    """regenerate 表单 POST（带合法 CSRF token 与 32 位 hex key）。"""
    return client.post("/support/orders/%s/regenerate" % ref, data={
        "idempotency_key": key,
        "csrf_token": helpers._csrf_token(client)})


#: 查单应答样本（形态对齐 sp.query_trade 的返回 dict）
Q_PENDING = {"state": "PENDING", "total_amount": "6.66",
             "trade_no": None, "trade_status": "WAIT_BUYER_PAY"}
Q_PAID_666 = {"state": "PAID", "total_amount": "6.66",
              "trade_no": "r6-paid-trade", "trade_status": "TRADE_SUCCESS"}

def _named_paid(ref):
    """regenerate 点名 paid 载荷的精确形态（自定义金额 6.66 → tier=None、
    gift=fish_treat；out_trade_no 指向实际 paid 的那张订单）。"""
    return {"status": "paid", "out_trade_no": ref,
            "amount_yuan": "6.66", "tier": None, "gift": "fish_treat"}

#: 旧单自身 paid 路径 / 状态端点的不点名载荷（逐键一致，绝无 out_trade_no）
UNNAMED_PAID = {"status": "paid", "amount_yuan": "6.66",
                "tier": None, "gift": "fish_treat"}


# --------------------------------------------------------------------------- #
# 后继单已支付：过期视图 regenerate 绝不再建新单
# --------------------------------------------------------------------------- #
def test_paid_successor_returns_named_payload_key_stays_unbound(
        _iso, rt, monkeypatch):
    """A 过期 → 重建 B → B 已支付：再 regenerate(A) → 200 点名 paid 载荷。
    零支付宝调用、无新行、无 open 单；请求 key 保持未绑定——同 key 重放同
    响应，随后同 key 不同金额下单仍可正常建新单（key 仍可用）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=16)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "1" * 32)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    assert b != a
    assert ops == [("query", a), ("close", a)]
    # B 在另一标签页支付并本地落库（已提交的事实）
    sp.mark_paid_idempotent(b, "r6-successor-paid")
    # 过期视图携全新未用 key 再来：后继防护命中
    ops2 = _stub_alipay(monkeypatch)  # 任何支付宝调用即失败
    stale = _regen(client, a, "2" * 32)
    assert stale.status_code == 200
    assert stale.get_json() == _named_paid(b)
    assert ops2 == []
    assert _order_count() == 2
    assert _active_refs() == []
    # key 未绑定（无列、无别名）：同 key 重放同一响应
    assert sp.get_order_by_idempotency_key("2" * 32) is None
    replay = _regen(client, a, "2" * 32)
    assert replay.status_code == 200
    assert replay.get_json() == _named_paid(b)
    assert _order_count() == 2
    # key 仍可用：同 key 不同金额下单 → 正常建新单（303 表单形态）
    fresh = client.post("/support/orders", data={
        "amount": "18.88", "csrf_token": token, "idempotency_key": "2" * 32})
    assert fresh.status_code == 303
    c = fresh.headers["Location"].rsplit("/", 1)[-1]
    assert c not in (a, b)
    assert sp.get_order(c)["amount_fen"] == 1888
    assert sp.get_order_by_idempotency_key("2" * 32)["out_trade_no"] == c


def test_older_paid_same_amount_is_not_successor_old_table_proceeds(
        _iso, rt, monkeypatch):
    """比旧单**更早**创建的同金额 paid 订单（更早一次独立赞助）不是后继：
    regenerate(A) 照旧走旧单表——A PENDING → 查 → 关 → 建新单 C。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    older = sp.create_order(rt, 666)
    sp.mark_paid_idempotent(older["out_trade_no"], "r6-older-paid")
    helpers._backdate(older["out_trade_no"], minutes=2)  # 严格早于 A
    a = helpers._make_order(rt)
    helpers._own_session(client, older["out_trade_no"], a["out_trade_no"])
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    r = _regen(client, a["out_trade_no"], "3" * 32)
    assert r.status_code == 201
    c = r.get_json()["out_trade_no"]
    assert c not in (older["out_trade_no"], a["out_trade_no"])
    assert sp.get_order(a["out_trade_no"])["status"] == "closed"
    assert sp.get_order(a["out_trade_no"])["closed_reason"] == "regenerate:closed"
    assert ops == [("query", a["out_trade_no"]),
                   ("close", a["out_trade_no"])]  # 旧单只查询 A，老 paid 单不被触碰
    assert sp.get_order(older["out_trade_no"])["status"] == "paid"
    assert _active_refs() == [c]
    assert sp.get_order_by_idempotency_key("3" * 32)["out_trade_no"] == c


def test_paid_after_old_but_different_amount_is_not_successor(
        _iso, rt, monkeypatch):
    """旧单之后创建但**金额不同**的 paid 订单不是后继：旧单表照常推进。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = helpers._make_order(rt)
    other = sp.create_order(rt, 1888)  # 晚于 A、同会话、不同金额
    sp.mark_paid_idempotent(other["out_trade_no"], "r6-other-amount")
    helpers._own_session(client, a["out_trade_no"], other["out_trade_no"])
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    r = _regen(client, a["out_trade_no"], "4" * 32)
    assert r.status_code == 201
    c = r.get_json()["out_trade_no"]
    assert c not in (a["out_trade_no"], other["out_trade_no"])
    assert sp.get_order(a["out_trade_no"])["status"] == "closed"
    assert ops == [("query", a["out_trade_no"]),
                   ("close", a["out_trade_no"])]
    assert sp.get_order(other["out_trade_no"])["status"] == "paid"  # 未被触碰


def test_paid_successor_outside_session_refs_not_considered(
        _iso, rt, monkeypatch):
    """同金额 paid 后继存在但**不在本会话 refs**：不考虑（会话边界），
    旧单表照常推进。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = helpers._make_order(rt)
    hidden = sp.create_order(rt, 666)  # 晚于 A、已支付、但不在本会话
    sp.mark_paid_idempotent(hidden["out_trade_no"], "r6-hidden-paid")
    helpers._own_session(client, a["out_trade_no"])
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    r = _regen(client, a["out_trade_no"], "5" * 32)
    assert r.status_code == 201
    c = r.get_json()["out_trade_no"]
    assert c not in (a["out_trade_no"], hidden["out_trade_no"])
    assert sp.get_order(a["out_trade_no"])["status"] == "closed"
    assert ops == [("query", a["out_trade_no"]),
                   ("close", a["out_trade_no"])]
    assert hidden["out_trade_no"] not in [ref for op, ref in ops]


def test_paid_successor_different_runtime_not_considered(
        _iso, rt, monkeypatch):
    """同金额 paid 后继属于**另一运行时快照**（不同 app_id）：不是后继，
    旧单表照常推进。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = helpers._make_order(rt)
    rt2 = sp.RuntimeConfig("sandbox", sp.GATEWAY_SANDBOX,
                           "2026111100000000", "2088111100000000",
                           rt.app_private_key, rt.alipay_public_key)
    foreign = sp.create_order(rt2, 666)  # 晚于 A、同会话、不同 app_id
    sp.mark_paid_idempotent(foreign["out_trade_no"], "r6-foreign-runtime")
    helpers._own_session(client, a["out_trade_no"], foreign["out_trade_no"])
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    r = _regen(client, a["out_trade_no"], "6" * 32)
    assert r.status_code == 201
    c = r.get_json()["out_trade_no"]
    assert c not in (a["out_trade_no"], foreign["out_trade_no"])
    assert sp.get_order(a["out_trade_no"])["status"] == "closed"
    assert ops == [("query", a["out_trade_no"]),
                   ("close", a["out_trade_no"])]
    assert sp.get_order(foreign["out_trade_no"])["status"] == "paid"


def test_paid_successor_wins_over_open_same_amount_order(_iso, rt, monkeypatch):
    """后继 paid 单与同金额 open 单并存：paid 优先（防护先于 R5 复用）——
    零支付宝调用、请求 key 不绑定、open 单原样保留、无新行。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=16)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "7" * 32)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    sp.mark_paid_idempotent(b, "r6-paid-wins")
    # 另一张同金额 open 单（本会话、剩余 ≥5min，R5 防护会复用它）
    open_ref = helpers._make_order(rt)
    helpers._own_session(client, open_ref["out_trade_no"])
    ops2 = _stub_alipay(monkeypatch)  # 任何支付宝调用即失败
    stale = _regen(client, a, "8" * 32)
    assert stale.status_code == 200
    assert stale.get_json() == _named_paid(b)
    assert ops2 == []
    assert sp.get_order_by_idempotency_key("8" * 32) is None
    assert sp.get_order(open_ref["out_trade_no"])["status"] == "paying"
    assert _order_count() == 3


# --------------------------------------------------------------------------- #
# R5 防护子分支：命中单在本请求内被核实为 PAID
# --------------------------------------------------------------------------- #
def test_r5_guard_settled_as_paid_returns_named_payload_no_bind(
        _iso, rt, monkeypatch):
    """B 仍在窗口但剩 <5 分钟且本地 open，查得 PAID（金额精确）→ 200 点名
    paid 载荷（B）：key 不绑定、绝不 close、旧单 A 不被查询、无新行。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=12, key="9" * 32)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "a" * 32)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    helpers._backdate(b, minutes=11)  # B 剩 ~4 分钟：不可复用、仍在窗口
    ops2 = _stub_alipay(monkeypatch, query=dict(Q_PAID_666))
    second = _regen(client, a, "b" * 32)
    assert second.status_code == 200
    assert second.get_json() == _named_paid(b)
    assert ops2 == [("query", b)]  # 只查 B：无 close、A 不被查询
    assert sp.get_order(b)["status"] == "paid"
    assert sp.get_order_by_idempotency_key("b" * 32) is None
    assert _order_count() == 2
    assert _active_refs() == []


# --------------------------------------------------------------------------- #
# paid 载荷契约：旧单自身 paid 路径与状态端点恒不点名
# --------------------------------------------------------------------------- #
def test_old_order_own_paid_paths_stay_unnamed(_iso, rt, monkeypatch):
    """本地已 paid / 核实 PAID（旧单本身）→ 不点名 paid 载荷（与状态端点
    逐键一致，绝无 out_trade_no）。"""
    client = helpers._client()
    # 路径 1：本地已 paid（零支付宝调用）
    o1 = helpers._make_order(rt)
    sp.mark_paid_idempotent(o1["out_trade_no"], "r6-local-paid")
    helpers._own_session(client, o1["out_trade_no"])
    _stub_alipay(monkeypatch)  # 任何支付宝调用即失败
    r1 = _regen(client, o1["out_trade_no"], "c" * 32)
    assert r1.status_code == 200
    assert r1.get_json() == UNNAMED_PAID
    # 路径 2：核实 PAID（金额精确）落库后返回同一不点名载荷
    o2 = helpers._make_order(rt)
    helpers._own_session(client, o2["out_trade_no"])
    ops = _stub_alipay(monkeypatch, query=dict(Q_PAID_666))
    r2 = _regen(client, o2["out_trade_no"], "d" * 32)
    assert r2.status_code == 200
    assert r2.get_json() == UNNAMED_PAID
    assert ops == [("query", o2["out_trade_no"])]
    assert sp.get_order(o2["out_trade_no"])["status"] == "paid"
    assert _order_count() == 2  # 两条路径都绝不建新单


def test_status_endpoint_paid_payload_stays_unnamed(_iso, rt):
    """状态端点 paid 载荷恒不点名（逐键断言，无 out_trade_no）。"""
    order = helpers._make_order(rt)
    sp.mark_paid_idempotent(order["out_trade_no"], "r6-status-paid")
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    data = c.get(
        "/support/orders/%s/status" % order["out_trade_no"]).get_json()
    assert data == UNNAMED_PAID
