# -*- coding: utf-8 -*-
"""R5 评审修复面回归（2026-10-23）：同一会话绝不留两张可支付订单。

评审原文件（两条失败回归）见 ``tests/test_support_review_r5.py``；本文件
覆盖修复实现自身的分支与不变量，全部走端点/仓储真实链路（支付宝网络面
monkeypatch，调用留痕断言；**零预期调用时一旦调用立即失败**，防真实出网）：

1. 规则 A（POST /support/orders）：会话复用未命中（同金额旧单剩 <5 分钟）
   时，逐张核实/关单仍在可支付窗口内的同金额未决单，再决定复用或建新——
   PENDING（查→关→建新）/ NOT_EXIST（窗口内绝不 close，原单返回：JSON 200
   reused 与 303 两条形态、key 绑定与 paid 后重放）/ 查询结果未知 / close
   失败 / UNKNOWN / 金额不符的 PAID（一律原单返回，绝不建新）/ PAID 精确
   （原单落库 paid 后返回，整页 303）/ CLOSED（verified 关单后建新）/
   剩余 ≥5 分钟（照旧复用，零调用）/ 越过窗口线（零调用直接建新，旧单
   留给 sweep）/ 不同金额（零改动零调用）/ 无 key settle 不落别名行；
2. 规则 B（POST /support/orders/<ref>/regenerate）：旧单已被本会话同金额
   重建单替换时交回替换单——B 剩余 ≥5 分钟 → 200 reused 零调用、key 绑定
   替换单；B 仍在窗口但剩 <5 分钟且 PENDING → 先关 B 再走旧单表建 C；
   B 在窗口且 NOT_EXIST（未到截止线）→ 返回 B，旧单 A 根本不被查询。

运行：cd 项目根 && python3 -m pytest tests/test_support_r5_20261023.py -q
"""
import pg_store
import pytest

import support_payments as sp
import test_support_payments as helpers

pytest_plugins = ["test_support_payments"]


# --------------------------------------------------------------------------- #
# 桩与小助手
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
    """仍可支付（created/paying）的订单号——「同一会话至多一张」的断言面。"""
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT out_trade_no FROM support_orders"
                        " WHERE status IN ('created','paying')"
                        " ORDER BY out_trade_no")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _alias_count(key=None):
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            if key is None:
                cur.execute("SELECT count(*) FROM support_order_key_aliases")
            else:
                cur.execute("SELECT count(*) FROM support_order_key_aliases"
                            " WHERE idempotency_key=%s", (key,))
            return cur.fetchone()[0]
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
Q_NOT_EXIST = {"state": "NOT_EXIST", "total_amount": None,
               "trade_no": None, "trade_status": None}
Q_UNKNOWN = {"state": "UNKNOWN", "total_amount": None,
             "trade_no": None, "trade_status": None}
Q_CLOSED = {"state": "CLOSED", "total_amount": "6.66",
            "trade_no": None, "trade_status": "TRADE_CLOSED"}
Q_PAID_666 = {"state": "PAID", "total_amount": "6.66",
              "trade_no": "r5-paid-trade", "trade_status": "TRADE_SUCCESS"}
Q_PAID_MISMATCH = {"state": "PAID", "total_amount": "18.88",
                   "trade_no": "r5-mismatch", "trade_status": "TRADE_SUCCESS"}


# --------------------------------------------------------------------------- #
# 规则 A：POST /support/orders（同金额旧单仍在可支付窗口内）
# --------------------------------------------------------------------------- #
def test_rule_a_pending_old_closed_then_new_order(_iso, monkeypatch):
    """11 分钟旧同金额单 + PENDING：先查后关（reason=create:closed）再建新单
    ——恰好剩一张可支付订单，请求 key 绑定新单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11, key="1" * 32)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "2" * 32})
    assert fresh.status_code == 303
    new_ref = fresh.headers["Location"].rsplit("/", 1)[-1]
    assert new_ref != old
    old_row = sp.get_order(old)
    assert old_row["status"] == "closed"
    assert old_row["closed_reason"] == "create:closed"
    assert ops == [("query", old), ("close", old)]
    assert _active_refs() == [new_ref]
    assert _order_count() == 2
    assert sp.get_order_by_idempotency_key("2" * 32)["out_trade_no"] == new_ref


def test_rule_a_not_exist_returns_old_json_and_form_no_close(_iso, monkeypatch):
    """NOT_EXIST（窗口内，买家扫码前关不掉）：旧单原样返回——JSON 200
    reused 与 303 表单两条形态；绝不调 close、绝无新行；请求 key 绑定旧单
    （旧单标记 paid 后同 key 重放仍返回旧单）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(monkeypatch, query=dict(Q_NOT_EXIST))
    js = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "3" * 32},
        headers={"Accept": "application/json"})
    assert js.status_code == 200
    assert js.get_json()["out_trade_no"] == old
    assert js.get_json()["reused"] is True
    assert sp.get_order_by_idempotency_key("3" * 32)["out_trade_no"] == old
    form = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "4" * 32})
    assert form.status_code == 303
    assert form.headers["Location"] == "/support/pay/%s" % old
    assert ("close", old) not in ops  # NOT_EXIST 绝不 close
    assert _active_refs() == [old]
    assert _order_count() == 1  # 无新行
    # key 与旧单永久绑定：paid 之后同 key 重放仍返回旧单
    sp.mark_paid_idempotent(old, "r5-not-exist-paid")
    replay = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "3" * 32})
    assert replay.status_code == 303
    assert replay.headers["Location"] == "/support/pay/%s" % old
    assert _order_count() == 1
    assert _active_refs() == []


def test_rule_a_query_uncertain_returns_old_no_close(_iso, monkeypatch):
    """查单结果未知（网关超时）：旧单返回（结果未知绝不关单/建新）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(
        monkeypatch,
        query=sp.TradeOperationUncertainError("simulated gateway timeout"))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "5" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    assert ops == [("query", old)]
    assert sp.get_order(old)["status"] == "paying"
    assert _active_refs() == [old]
    assert _order_count() == 1


def test_rule_a_close_failure_returns_old(_iso, monkeypatch):
    """PENDING + close 抛 TradeOperationError（关不掉）：旧单返回（可能仍
    可支付），不建新单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(
        monkeypatch, query=dict(Q_PENDING),
        close=sp.TradeOperationError("simulated close refusal"))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "6" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    assert ops == [("query", old), ("close", old)]
    assert sp.get_order(old)["status"] == "paying"
    assert _active_refs() == [old]
    assert _order_count() == 1


def test_rule_a_unknown_returns_old(_iso, monkeypatch):
    """UNKNOWN：结果未知 → 旧单返回，不关单、不建新。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(monkeypatch, query=dict(Q_UNKNOWN))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "7" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    assert ops == [("query", old)]
    assert sp.get_order(old)["status"] == "paying"
    assert _order_count() == 1


def test_rule_a_paid_amount_mismatch_returns_old(_iso, monkeypatch):
    """PAID 但金额不符：不认账 → 旧单保持未决原样返回，无新行、不 close。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PAID_MISMATCH))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "8" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    assert ops == [("query", old)]
    row = sp.get_order(old)
    assert row["status"] == "paying" and row["trade_no"] is None
    assert _order_count() == 1


def test_rule_a_paid_exact_returns_old_paid_full_page_303(_iso, monkeypatch):
    """PAID 且金额精确：旧单落库 paid 后返回——整页 303 到旧单支付页（支付
    页再转发收据），绝无新行、绝不 close。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PAID_666))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "9" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    row = sp.get_order(old)
    assert row["status"] == "paid" and row["trade_no"] == "r5-paid-trade"
    assert ops == [("query", old)]
    assert _order_count() == 1


def test_rule_a_closed_creates_new_order(_iso, monkeypatch):
    """CLOSED（支付宝侧已关）：verified 推进本地关单（create:alipay_closed，
    零额外 close 调用）→ 建新单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11, key="a" * 32)
    ops = _stub_alipay(monkeypatch, query=dict(Q_CLOSED))
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "b" * 32})
    assert fresh.status_code == 303
    new_ref = fresh.headers["Location"].rsplit("/", 1)[-1]
    assert new_ref != old
    old_row = sp.get_order(old)
    assert old_row["status"] == "closed"
    assert old_row["closed_reason"] == "create:alipay_closed"
    assert ops == [("query", old)]
    assert _active_refs() == [new_ref]


def test_rule_a_reusable_window_reuses_with_zero_alipay(_iso, monkeypatch):
    """9 分钟旧（剩 6 分钟 ≥ ORDER_REUSE_MIN_REMAINING_SECONDS）：与既有
    会话复用同一条边界——复用旧单，零支付宝调用。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=9)
    _stub_alipay(monkeypatch)  # 任何支付宝调用即失败
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "c" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] == "/support/pay/%s" % old
    assert _order_count() == 1
    assert _active_refs() == [old]
    assert sp.get_order_by_idempotency_key("c" * 32)["out_trade_no"] == old


def test_rule_a_expired_beyond_window_creates_without_alipay(_iso, monkeypatch):
    """16 分钟旧（越过 15m+10s 可支付窗口线）：不查询、不关单（留给补偿
    sweep）、不阻塞——直接建新单，零支付宝调用（R1
    test_expired_order_not_reused 同一契约）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=16)
    _stub_alipay(monkeypatch)
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "d" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] != "/support/pay/%s" % old
    assert sp.get_order(old)["status"] == "paying"  # 原样留给 sweep
    assert _order_count() == 2


def test_rule_a_different_amount_untouched_zero_alipay(_iso, monkeypatch):
    """不同金额：完全不变——零支付宝调用、旧单不动、直接建新单（R1
    test_same_session_different_amount_creates_new_order 契约）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11, amount="6.66")
    _stub_alipay(monkeypatch)
    fresh = client.post("/support/orders", data={
        "amount": "18.88", "csrf_token": token, "idempotency_key": "e" * 32})
    assert fresh.status_code == 303
    assert fresh.headers["Location"] != "/support/pay/%s" % old
    assert sp.get_order(old)["status"] == "paying"
    assert _order_count() == 2


def test_rule_a_keyless_settle_creates_no_alias(_iso, monkeypatch):
    """无 key 请求走 settle 路径（PENDING → 关单 → 新单）：不落任何别名行
    （与既有会话复用的无 key 语义一致）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    old = _aged_session_order(client, token, minutes=11)  # 建单即无 key
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    fresh = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token})
    assert fresh.status_code == 303
    new_ref = fresh.headers["Location"].rsplit("/", 1)[-1]
    assert new_ref != old
    assert sp.get_order(old)["closed_reason"] == "create:closed"
    assert _alias_count() == 0
    assert _active_refs() == [new_ref]


# --------------------------------------------------------------------------- #
# 规则 B：regenerate（旧单已被本会话同金额重建单替换）
# --------------------------------------------------------------------------- #
def test_rule_b_stale_regenerate_returns_replacement_zero_alipay(
        _iso, monkeypatch):
    """A 过期被 B 替换后，过期视图再 regenerate(A)：交回 B（200 reused），
    零支付宝调用；请求 key 绑定 B（同 key 重放 → B）；恰好一张可支付单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=16, key="a" * 32)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "b" * 32)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    assert b != a
    assert ops == [("query", a), ("close", a)]  # 首次：旧单表正常关 A 建 B
    # B 已存在：过期视图的 regenerate 绝不再建第三张（零支付宝调用）
    ops2 = _stub_alipay(monkeypatch)
    stale = _regen(client, a, "c" * 32)
    assert stale.status_code == 200
    assert stale.get_json()["out_trade_no"] == b
    assert stale.get_json()["reused"] is True
    assert ops2 == []
    assert sp.get_order_by_idempotency_key("c" * 32)["out_trade_no"] == b
    replay = _regen(client, a, "c" * 32)
    assert replay.status_code == 200
    assert replay.get_json()["out_trade_no"] == b
    assert _active_refs() == [b]
    assert _order_count() == 2


def test_rule_b_replacement_pending_closed_then_old_table(_iso, monkeypatch):
    """替换单 B 仍在窗口但剩 <5 分钟且 PENDING：先核实关掉 B，旧单表再处理
    A（同样关掉）→ 建新单 C；恰好一张可支付订单（先新后旧的结算顺序）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=12, key="d" * 32)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "e" * 32)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    helpers._backdate(b, minutes=11)  # B 老化到剩 4 分钟（仍在可支付窗口）
    second = _regen(client, a, "f" * 32)
    assert second.status_code == 201
    c = second.get_json()["out_trade_no"]
    assert c not in (a, b)
    assert sp.get_order(a)["status"] == "closed"
    assert sp.get_order(b)["status"] == "closed"
    assert _active_refs() == [c]
    # 规则 B 先结算较新的 B（query+close），随后旧单表处理 A（query+close）
    assert ops == [("query", a), ("close", a),
                   ("query", b), ("close", b),
                   ("query", a), ("close", a)]
    assert sp.get_order_by_idempotency_key("f" * 32)["out_trade_no"] == c


def test_rule_b_replacement_not_exist_returns_b_old_not_queried(
        _iso, monkeypatch):
    """替换单 B 在窗口内 + NOT_EXIST（未到截止线，买家仍可能扫码）：规则 B
    返回 B（open），旧单 A 根本不被查询、不被触碰、无新单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    a = _aged_session_order(client, token, minutes=12, key="a1" * 16)
    ops = _stub_alipay(monkeypatch, query=dict(Q_PENDING), close=True)
    first = _regen(client, a, "b2" * 16)
    assert first.status_code == 201
    b = first.get_json()["out_trade_no"]
    helpers._backdate(b, minutes=11)
    ops2 = _stub_alipay(monkeypatch, query=dict(Q_NOT_EXIST))
    second = _regen(client, a, "c3" * 16)
    assert second.status_code == 200
    assert second.get_json()["out_trade_no"] == b
    assert second.get_json()["reused"] is True
    assert ops2 == [("query", b)]  # 本次 regenerate 只查 B，A 不再被查询
    # A 已在首建替换单时按旧单表关单；本次过期视图重放不触碰任何旧单
    assert sp.get_order(a)["status"] == "closed"
    # B 经 regenerate JSON 建单（未打开支付页）保持 created，未到截止线
    # 绝不本地关单
    assert sp.get_order(b)["status"] == "created"
    assert _active_refs() == [b]
    assert _order_count() == 2
