# -*- coding: utf-8 -*-
"""R3/R4 评审修复面回归：幂等键「先落库再返回」+ regenerate 端点契约。

评审原文件（仅两条失败回归）见 ``tests/test_support_review_r3.py``；本文件
覆盖修复实现自身的分支与不变量，全部走端点 / 仓储真实链路（支付宝网络面
monkeypatch，调用留痕断言）：

1. 下单：会话复用命中的新 key 后绑定（support_order_key_aliases）——别名
   key 配不同金额 409、并发同 key 只绑一行且不建新单、无 key 复用不落别名、
   别名重放在订单关闭后仍返回原单；
2. 退款：未决解析的两条 resolved 路径（同号查询 REFUND_SUCCESS / 干净非
   成功应答的同号重试 fund_change=Y）与幂等键绑定**同事务**提交——解析后
   任何环节（审计/响应）中断，同 key 重放命中原行；blocked 解析绝不绑定；
   他单退款持有的键 → 409 refund_key_conflict 且零支付宝调用；
3. regenerate（POST /support/orders/<ref>/regenerate）：逐分支验收跨层
   契约——中性/paid/PAID/PENDING±close/NOT_EXIST 截止线/CLOSED/UNKNOWN/
   金额不符/运行时不匹配/键重放/坏 key/无 CSRF/限流/键金额冲突；凡契约
   写明「不可建单」的分支一律断言无支付宝调用、无新订单。

运行：cd 项目根 && python3 -m pytest tests/test_support_review_r4.py -q
"""
import threading

import pytest

import app as app_mod
import auth_limit_store
import pg_store
import support_payments as sp
import test_support_payments as helpers

pytest_plugins = ["test_support_payments"]


def _order_count():
    conn = pg_store.connect()
    try:
        return conn.execute(
            "SELECT count(*) FROM support_orders").fetchone()[0]
    finally:
        conn.close()


def _alias_count(table, key):
    conn = pg_store.connect()
    try:
        return conn.execute(
            "SELECT count(*) FROM %s WHERE idempotency_key=%%s" % table,
            (key,)).fetchone()[0]
    finally:
        conn.close()


def _support_ip_attempts():
    conn = pg_store.connect()
    try:
        return conn.execute(
            "SELECT count(*) FROM auth_rate_limits WHERE scope=%s",
            (auth_limit_store.SCOPE_SUPPORT_ORDER_IP,)).fetchone()[0]
    finally:
        conn.close()


def _regen(client, ref, key=None, **over):
    """regenerate 表单 POST（缺省带合法 CSRF token；key=None 不带键字段）。"""
    data = dict(over)
    data.setdefault("csrf_token", helpers._csrf_token(client))
    if key is not None:
        data["idempotency_key"] = key
    return client.post("/support/orders/%s/regenerate" % ref, data=data)


# --------------------------------------------------------------------------- #
# 1. 下单：会话复用命中的新 key 后绑定（support_order_key_aliases）
# --------------------------------------------------------------------------- #
def test_alias_key_with_different_amount_conflicts(_iso):
    """复用命中的 key 经别名解析：同 key 不同金额 → 409 order_conflict，
    绝不建单。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders", data={"amount": "6.66",
                                                 "csrf_token": token})
    first_ref = first.headers["Location"].rsplit("/", 1)[-1]
    # 新 key 同金额：命中会话复用 → key 后绑定（别名表）
    reused = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "a" * 32})
    assert reused.headers["Location"] == first.headers["Location"]
    assert _alias_count("support_order_key_aliases", "a" * 32) == 1
    conflict = client.post("/support/orders", data={
        "amount": "18.88", "csrf_token": token, "idempotency_key": "a" * 32},
        headers={"Accept": "application/json"})
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "order_conflict"
    assert _order_count() == 1
    assert sp.get_order(first_ref) is not None


def test_concurrent_same_key_session_reuse_binds_exactly_once(_iso, rt):
    """两个并发 POST（同会话持有被复用订单、同一新 key）：都返回同一单、
    恰好一行别名、无新订单（键锁串行 + 唯一键兜底）。"""
    order = helpers._make_order(rt)
    key = "b" * 32
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        try:
            client = helpers._own_session(helpers._client(),
                                          order["out_trade_no"])
            barrier.wait(timeout=10)
            results.append(client.post("/support/orders", data={
                "amount": "6.66",
                "csrf_token": helpers._csrf_token(client),
                "idempotency_key": key}))
        except Exception as exc:  # noqa: BLE001 — 线程内只留类型名
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 2
    assert {r.headers["Location"] for r in results} \
        == {"/support/pay/%s" % order["out_trade_no"]}
    assert _order_count() == 1
    assert _alias_count("support_order_key_aliases", key) == 1


def test_keyless_session_reuse_creates_no_alias(_iso):
    """无 key 请求的会话复用语义不变：不落任何别名行。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders", data={"amount": "6.66",
                                                 "csrf_token": token})
    second = client.post("/support/orders", data={"amount": "6.66",
                                                  "csrf_token": token})
    assert second.headers["Location"] == first.headers["Location"]
    conn = pg_store.connect()
    try:
        assert conn.execute(
            "SELECT count(*) FROM support_order_key_aliases").fetchone()[0] == 0
    finally:
        conn.close()


def test_alias_replay_after_close_returns_original_order(_iso):
    """别名 key 与原单永久绑定：订单关闭后同 key 重放仍返回原单，绝不
    另建新单（R2-1 语义经别名同样成立）。"""
    client = helpers._client()
    token = helpers._csrf_token(client)
    first = client.post("/support/orders", data={"amount": "6.66",
                                                 "csrf_token": token})
    ref = first.headers["Location"].rsplit("/", 1)[-1]
    client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "c" * 32})
    sp.mark_closed_idempotent(ref, "sweep:expired")
    replay = client.post("/support/orders", data={
        "amount": "6.66", "csrf_token": token, "idempotency_key": "c" * 32})
    assert replay.headers["Location"] == "/support/pay/%s" % ref
    assert _order_count() == 1


# --------------------------------------------------------------------------- #
# 2. 退款：未决解析与幂等键绑定同事务（blocked 绝不绑定）
# --------------------------------------------------------------------------- #
def test_resolution_via_same_number_retry_binds_key(_iso, rt, monkeypatch):
    """resolved 路径之二（干净非成功查询应答 → 同号重试 fund_change=Y）：
    返回原行且键已绑定——同 key 重放直接命中原行，refund_trade 全程只此
    一次（累计 300 分，绝不重复退款）。"""
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"])
    refund_calls, query_calls = [], []

    def fake_query(runtime, o, number):
        query_calls.append(number)
        return {"refund_status": None, "refund_amount": "3.00",
                "trade_no": None}  # 干净、可解析的非成功应答

    def fake_retry(runtime, o, amount, number, reason=None):
        refund_calls.append(number)
        return {"fund_change": "Y", "trade_no": "r4-retry-ok"}

    monkeypatch.setattr(sp, "query_refund", fake_query)
    monkeypatch.setattr(sp, "refund_trade", fake_retry)
    path = "/api/admin/v1/support-payments/%s/refund" % order["out_trade_no"]
    payload = {"amount_yuan": "3.00", "idempotency_key": "r4-retry-key-1"}
    first = client.post(path, json=payload)
    assert first.status_code == 200
    assert first.get_json()["refund"]["out_request_no"] \
        == old["out_request_no"]
    assert first.get_json()["refund"]["status"] == "refunded"
    assert refund_calls == [old["out_request_no"]]
    # 重放：同 key 直接命中原行，零支付宝调用
    monkeypatch.setattr(sp, "query_refund",
                        lambda *a: query_calls.append("replay-q") or {})
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *a: refund_calls.append("replay-r") or {})
    replay = client.post(path, json=payload)
    assert replay.status_code == 200
    assert replay.get_json()["refund"]["out_request_no"] \
        == old["out_request_no"]
    assert query_calls == [old["out_request_no"]]  # 无 replay-q
    assert refund_calls == [old["out_request_no"]]  # 无 replay-r
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
    assert _alias_count("support_refund_key_aliases", "r4-retry-key-1") == 1


def test_original_key_and_resolved_key_both_return_original_row(
        _iso, rt, monkeypatch):
    """原行自带 key K0（建行期列），经新 key K 解析确认成功（别名）——
    K0 与 K 之后都返回原行，重放零支付宝调用。"""
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"],
                               idempotency_key="r4-k0-original-key")
    sp.update_refund_status(old["out_request_no"], "unknown")
    calls = []
    monkeypatch.setattr(sp, "query_refund", lambda *a: calls.append("q") or {
        "refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
        "trade_no": "r4-k1-confirm"})
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *a: calls.append("r") or {"fund_change": "Y"})
    path = "/api/admin/v1/support-payments/%s/refund" % order["out_trade_no"]
    resolved = client.post(path, json={"amount_yuan": "3.00",
                                       "idempotency_key": "r4-k1-resolve"})
    assert resolved.status_code == 200
    assert resolved.get_json()["refund"]["out_request_no"] \
        == old["out_request_no"]
    assert calls == ["q"]  # 查询确认即解决，无重试受理
    for key in ("r4-k0-original-key", "r4-k1-resolve"):
        calls.clear()
        replay = client.post(path, json={"amount_yuan": "3.00",
                                         "idempotency_key": key})
        assert replay.status_code == 200
        assert replay.get_json()["refund"]["out_request_no"] \
            == old["out_request_no"]
        assert calls == []  # 重放零支付宝调用
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300
    assert _alias_count("support_refund_key_aliases", "r4-k1-resolve") == 1


def test_key_binding_survives_crash_right_after_resolution(
        _iso, rt, monkeypatch):
    """解析成功后、响应构造前进程死掉（_audit 抛出）：键绑定已随 refunded
    置位**同事务**提交——重放同 key 命中原行，绝无第二笔退款。"""
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"])
    sp.update_refund_status(old["out_request_no"], "unknown")
    monkeypatch.setattr(sp, "query_refund", lambda *a: {
        "refund_status": "REFUND_SUCCESS", "refund_amount": "3.00",
        "trade_no": "r4-crash-confirm"})
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *a: {"fund_change": "Y"})

    class AuditBoom(RuntimeError):
        pass

    def boom(*a, **kw):
        raise AuditBoom("simulated audit sink down")

    monkeypatch.setattr(app_mod, "_audit", boom)
    path = "/api/admin/v1/support-payments/%s/refund" % order["out_trade_no"]
    payload = {"amount_yuan": "3.00", "idempotency_key": "r4-crash-key-1"}
    with pytest.raises(AuditBoom):
        client.post(path, json=payload)
    # 崩溃后：退款事实与键绑定都已提交
    assert sp.get_refund(old["out_request_no"])["status"] == "refunded"
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(sp, "query_refund",
                        lambda *a: calls.append("q") or {})
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *a: calls.append("r") or {})
    replay = client.post(path, json=payload)
    assert replay.status_code == 200
    assert replay.get_json()["refund"]["out_request_no"] \
        == old["out_request_no"]
    assert calls == []  # 重放零支付宝调用（键命中原行）
    assert sp.sum_refunded_fen(order["out_trade_no"]) == 300


def test_blocked_resolution_does_not_bind_key(_iso, rt, monkeypatch):
    """查询抛异常 → 409 refund_unresolved：解析没有发生，键必须保持未用
    （不落列也不落别名），绝不盲目同号重试受理。"""
    order = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order["out_trade_no"], 300, owner["user_id"])
    calls = []

    def boom(*a):
        calls.append("q")
        raise RuntimeError("simulated refund query failure")

    monkeypatch.setattr(sp, "query_refund", boom)
    monkeypatch.setattr(sp, "refund_trade", lambda *a: calls.append("r") or {})
    path = "/api/admin/v1/support-payments/%s/refund" % order["out_trade_no"]
    payload = {"amount_yuan": "3.00", "idempotency_key": "r4-blocked-key"}
    r = client.post(path, json=payload)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "refund_unresolved"
    assert calls == ["q"]  # 绝不 refund_trade
    assert sp.get_refund(old["out_request_no"])["status"] == "requested"
    assert sp.get_refund_by_idempotency_key("r4-blocked-key") is None
    assert _alias_count("support_refund_key_aliases", "r4-blocked-key") == 0


def test_refund_key_bound_to_other_order_conflicts_without_alipay(
        _iso, rt, monkeypatch):
    """键已绑定订单 X 的退款行（别名）再用到订单 Y → 409
    refund_key_conflict，零支付宝调用；同 key 回 X 仍返回原行。"""
    order_x = helpers._paid_order(rt)
    order_y = helpers._paid_order(rt)
    client, owner = helpers._admin_client()
    old = sp.create_refund_row(order_x["out_trade_no"], 100, owner["user_id"])
    # 经别名路径绑定（解析命中后绑定的形态）
    assert sp.mark_refunded_and_bind_key(old["out_request_no"],
                                         "r4-x-owned-key") is True
    calls = []
    monkeypatch.setattr(sp, "query_refund",
                        lambda *a: calls.append("q") or {})
    monkeypatch.setattr(sp, "refund_trade",
                        lambda *a: calls.append("r") or {})
    path_y = "/api/admin/v1/support-payments/%s/refund" \
        % order_y["out_trade_no"]
    r = client.post(path_y, json={"amount_yuan": "1.00",
                                  "idempotency_key": "r4-x-owned-key"})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "refund_key_conflict"
    assert calls == []  # 零支付宝调用
    path_x = "/api/admin/v1/support-payments/%s/refund" \
        % order_x["out_trade_no"]
    replay = client.post(path_x, json={"amount_yuan": "1.00",
                                       "idempotency_key": "r4-x-owned-key"})
    assert replay.status_code == 200
    assert replay.get_json()["refund"]["out_request_no"] \
        == old["out_request_no"]
    assert calls == []


# --------------------------------------------------------------------------- #
# 3. regenerate：逐分支验收（契约见 app.support_order_regenerate docstring）
# --------------------------------------------------------------------------- #
def test_regen_neutral_for_foreign_or_unknown_ref_no_side_effects(
        _iso, rt, monkeypatch):
    """ref 畸形/未知/他方 → 200 neutral（与未知响应一致）：不查单、不建单、
    不记限流（order IP 桶零写入）。"""
    foreign = helpers._make_order(rt)
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda *a: calls.append("q") or {})
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: calls.append("c") or True)
    stranger = helpers._client()
    r_unknown = _regen(stranger, "sp" + "0" * 24, key="d" * 32)
    r_malformed = _regen(stranger, "nope", key="d" * 32)
    r_foreign = _regen(helpers._client(), foreign["out_trade_no"],
                       key="d" * 32)
    for r in (r_unknown, r_malformed, r_foreign):
        assert r.status_code == 200
        assert r.get_json() == {"status": "neutral"}
    assert calls == []  # 零支付宝调用
    assert _order_count() == 1  # 无新订单（仅既有的 foreign）
    assert _support_ip_attempts() == 0  # 中性路径不记限流


def test_regen_local_paid_returns_paid_payload_no_alipay(_iso, rt,
                                                         monkeypatch):
    """本地已 paid：200 paid 载荷（与状态端点同一构造），零支付宝调用。"""
    order = helpers._paid_order(rt)
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    monkeypatch.setattr(sp, "query_trade",
                        lambda *a: pytest.fail("local paid must not query"))
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: pytest.fail("local paid must not close"))
    r = _regen(c, order["out_trade_no"], key="e" * 32)
    assert r.status_code == 200
    assert r.get_json() == {"status": "paid", "amount_yuan": "6.66",
                            "tier": None, "gift": "fish_treat"}
    assert _order_count() == 1


def test_regen_query_paid_exact_marks_paid_creates_nothing(_iso, rt,
                                                           monkeypatch):
    """查得 PAID 且金额精确匹配：落库 paid 并返回 paid 载荷——绝不基于
    已付款重建新单，也绝不调 close。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PAID", amount="6.66",
                        trade_no="r4-regen-paid")
    close_calls = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: close_calls.append(a) or True)
    r = _regen(c, order["out_trade_no"], key="f" * 32)
    assert r.status_code == 200
    assert r.get_json()["status"] == "paid"
    row = sp.get_order(order["out_trade_no"])
    assert row["status"] == "paid" and row["trade_no"] == "r4-regen-paid"
    assert close_calls == []
    assert _order_count() == 1


def test_regen_pending_close_ok_creates_new_order_same_amount(
        _iso, rt, monkeypatch):
    """PENDING + close 成功：旧单关单（reason=regenerate:closed）→ 以旧单
    金额建新单（201，reused=False），会话拥有新 ref；请求金额字段被忽略。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PENDING")
    close_calls = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, ref: close_calls.append(ref) or True)
    r = _regen(c, old["out_trade_no"], key="11" * 16, amount="500.00")
    assert r.status_code == 201
    data = r.get_json()
    new_ref = data["out_trade_no"]
    assert new_ref != old["out_trade_no"]
    assert data["reused"] is False
    assert data["amount_yuan"] == "6.66"  # 复制旧单金额，忽略请求金额
    assert data["pay_url"] == "/support/pay/%s" % new_ref
    with c.session_transaction() as sess:
        assert new_ref in sess["support_order_refs"]
        assert old["out_trade_no"] in sess["support_order_refs"]
    old_row = sp.get_order(old["out_trade_no"])
    assert old_row["status"] == "closed"
    assert old_row["closed_reason"] == "regenerate:closed"
    new_row = sp.get_order(new_ref)
    assert new_row["amount_fen"] == old_row["amount_fen"] == 666
    assert new_row["idempotency_key"] == "11" * 16
    assert close_calls == [old["out_trade_no"]]
    assert _order_count() == 2


def test_regen_pending_close_error_creates_nothing(_iso, rt, monkeypatch):
    """PENDING + close 抛 TradeOperationError：200 unconfirmed，什么都不建
    （旧单可能仍可支付）。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PENDING")

    def boom(runtime, ref):
        raise sp.TradeOperationError("simulated close failure")

    monkeypatch.setattr(sp, "close_trade", boom)
    r = _regen(c, old["out_trade_no"], key="12" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert _order_count() == 1
    assert sp.get_order(old["out_trade_no"])["status"] == "paying"


def test_regen_query_failure_creates_nothing(_iso, rt, monkeypatch):
    """服务端查单失败（网关超时）：200 unconfirmed，不关单、不建单。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])

    def boom(runtime, ref):
        raise sp.TradeOperationUncertainError("simulated gateway timeout")

    monkeypatch.setattr(sp, "query_trade", boom)
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: pytest.fail("must not close"))
    r = _regen(c, old["out_trade_no"], key="13" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert _order_count() == 1
    assert sp.get_order(old["out_trade_no"])["status"] == "paying"


def test_regen_not_exist_past_deadline_closes_locally_then_creates(
        _iso, rt, monkeypatch):
    """NOT_EXIST 且越过 created_at + ORDER_TIMEOUT + 宽限：本地关单（不调
    close，同 sweep 约定）后建新单（201）。"""
    old = helpers._make_order(rt)
    helpers._backdate(old["out_trade_no"], minutes=20)  # > 15m + 10s 截止线
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="NOT_EXIST")
    close_calls = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, ref: close_calls.append(ref) or True)
    r = _regen(c, old["out_trade_no"], key="14" * 16)
    assert r.status_code == 201
    assert close_calls == []  # NOT_EXIST 不调 close
    old_row = sp.get_order(old["out_trade_no"])
    assert old_row["status"] == "closed"
    assert old_row["closed_reason"] == "regenerate:not_exist"
    assert sp.get_order(r.get_json()["out_trade_no"])[
        "amount_fen"] == old_row["amount_fen"]
    assert _order_count() == 2


def test_regen_not_exist_before_deadline_waits(_iso, rt, monkeypatch):
    """NOT_EXIST 但未到截止线：买家仍可能提交 → 200 unconfirmed，不关单、
    不建单。"""
    old = helpers._make_order(rt)  # 新鲜单，远未到 15m+10s
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="NOT_EXIST")
    close_calls = []
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, ref: close_calls.append(ref) or True)
    r = _regen(c, old["out_trade_no"], key="15" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert close_calls == []
    assert _order_count() == 1
    assert sp.get_order(old["out_trade_no"])["status"] == "paying"


def test_regen_alipay_closed_creates_new_order(_iso, rt, monkeypatch):
    """查得 CLOSED（支付宝侧已关）：经 verified 推进本地关单后建新单。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="CLOSED")
    r = _regen(c, old["out_trade_no"], key="16" * 16)
    assert r.status_code == 201
    assert r.get_json()["reused"] is False
    old_row = sp.get_order(old["out_trade_no"])
    assert old_row["status"] == "closed"
    assert old_row["closed_reason"] == "regenerate:alipay_closed"
    assert _order_count() == 2


def test_regen_unknown_creates_nothing(_iso, rt, monkeypatch):
    """UNKNOWN：结果未知 → 200 unconfirmed，不建单。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="UNKNOWN")
    r = _regen(c, old["out_trade_no"], key="17" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert _order_count() == 1
    assert sp.get_order(old["out_trade_no"])["status"] == "paying"


def test_regen_paid_amount_mismatch_creates_nothing(_iso, rt, monkeypatch):
    """PAID 但金额不符：不认账（不复核成 paid）→ 200 unconfirmed，
    旧单保持未决、不建新单。"""
    old = helpers._make_order(rt, amount_fen=1888)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PAID", amount="6.66",
                        trade_no="r4-mismatch")
    r = _regen(c, old["out_trade_no"], key="18" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert _order_count() == 1
    row = sp.get_order(old["out_trade_no"])
    assert row["status"] == "paying" and row["trade_no"] is None


def test_regen_runtime_mismatch_no_query_no_creation(_iso, rt, monkeypatch):
    """订单/配置归属不匹配：200 unconfirmed，不触碰支付宝、不建单。"""
    old = helpers._make_order(rt)
    prod = sp.RuntimeConfig("production", sp.GATEWAY_PRODUCTION,
                            "2026000000000001", "2088000000000001",
                            rt.app_private_key, rt.alipay_public_key)
    monkeypatch.setattr(app_mod, "SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"production": prod})
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda *a: calls.append("q") or {})
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    r = _regen(c, old["out_trade_no"], key="19" * 16)
    assert r.status_code == 200
    assert r.get_json() == {"status": "unconfirmed"}
    assert calls == []
    assert _order_count() == 1


def test_regen_key_replay_returns_same_order_no_alipay(_iso, rt,
                                                       monkeypatch):
    """同 key 重放：键已绑定新建订单 → 200 reused 返回同一单，零支付宝
    调用、无新订单（键与订单永久绑定经 regenerate 同样成立）。"""
    old = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    helpers._mock_query(monkeypatch, state="PENDING")
    monkeypatch.setattr(sp, "close_trade", lambda runtime, ref: True)
    first = _regen(c, old["out_trade_no"], key="20" * 16)
    assert first.status_code == 201
    new_ref = first.get_json()["out_trade_no"]
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda *a: calls.append("q") or {})
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: calls.append("c") or True)
    replay = _regen(c, old["out_trade_no"], key="20" * 16)
    assert replay.status_code == 200
    assert replay.get_json()["out_trade_no"] == new_ref
    assert replay.get_json()["reused"] is True
    assert calls == []  # 重放零支付宝调用
    assert _order_count() == 2


def test_regen_bad_key_invalid_request(_iso, rt):
    """key 缺失/畸形（非 32 位小写 hex）→ 400 invalid_request，不建单。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    for bad_key in (None, "", "short", "A" * 32, "g" * 32,
                    "0" * 31 + "g0"):  # 大写/非 hex/长度不符一律拒绝
        r = _regen(c, order["out_trade_no"], key=bad_key)
        assert r.status_code == 400, bad_key
        assert r.get_json() == {"error": "invalid_request"}, bad_key
    assert _order_count() == 1


def test_regen_requires_csrf(_iso, rt):
    """无 CSRF token：全局 CSRF 闸 400 csrf_required（不进业务）。"""
    order = helpers._make_order(rt)
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    r = c.post("/support/orders/%s/regenerate" % order["out_trade_no"],
               data={"idempotency_key": "21" * 16})
    assert r.status_code == 400
    assert r.get_json() == {"error": "csrf_required"}
    assert _order_count() == 1


def test_regen_rate_limited_429_with_retry_after(_iso, rt, monkeypatch):
    """订单 IP 桶限流：超限 → 429 rate_limited + Retry-After。"""
    order = helpers._paid_order(rt)
    c = helpers._own_session(helpers._client(), order["out_trade_no"])
    monkeypatch.setattr(auth_limit_store,
                        "SUPPORT_ORDER_IP_ATTEMPT_LIMIT", 1)
    first = _regen(c, order["out_trade_no"], key="22" * 16)
    assert first.status_code == 200  # 本地 paid：200（尝试额度同时记账）
    second = _regen(c, order["out_trade_no"], key="23" * 16)
    assert second.status_code == 429
    assert second.get_json() == {"error": "rate_limited"}
    assert second.headers.get("Retry-After")


def test_regen_bound_key_with_different_amount_conflicts(_iso, rt,
                                                         monkeypatch):
    """key 已绑定其他金额的订单：409 order_conflict，绝不返回也不复用
    （防串单），无新订单、零支付宝调用。"""
    old = helpers._make_order(rt)  # 666
    other = sp.create_order(rt, 1888, idempotency_key="24" * 16)
    c = helpers._own_session(helpers._client(), old["out_trade_no"])
    calls = []
    monkeypatch.setattr(sp, "query_trade",
                        lambda *a: calls.append("q") or {})
    monkeypatch.setattr(sp, "close_trade",
                        lambda *a: calls.append("c") or True)
    r = _regen(c, old["out_trade_no"], key="24" * 16)
    assert r.status_code == 409
    assert r.get_json() == {"error": "order_conflict"}
    assert calls == []
    assert _order_count() == 2  # old + other，无新订单
    assert sp.get_order(other["out_trade_no"]) is not None
