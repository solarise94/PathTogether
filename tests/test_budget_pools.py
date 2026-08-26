# -*- coding: utf-8 -*-
"""P0-B AI 预算池隔离测试（docs §3.7 / §6.3 预算条目）。

覆盖：
  - 缺省周期池拆分：总 30 = owner 保留 10 + user 共享 15；单 user 3；
    Demo 子池为每日口径缺省 50（滚动 24h，0014 起）；缺省值可 env/常量
    覆盖（BUDGET_DEFAULT_*）；
  - owner 保留池保护：3 个以上受邀账号（每 user 3 次 ×5）合计打满 user 共享池
    后不能再消耗 owner 保留（OwnerReserveProtected / UserPoolBudgetExhausted），
    owner 仍可保留用量；
  - user 共享池独立于每 user 上限（多 user 分摊 15）；
  - Demo 用量同样计入保留池保护（保留闸按周期累计口径）；
  - Demo 每日子池滚动 24h 窗口：窗口外/已释放不计入、窗口内达上限拒
    （DemoBudgetExhausted）、usage_report 与闸同口径；
  - owner 不受保留闸约束（总量闸兜底）；
  - 邀请模板 ai_access：受邀用户默认 False，owner 显式授予后放行
    （app 层 _ai_reserve_run_budget 403 ai_access_required）；
  - owner PUT 校验：周期口径子池（user_pool+owner_reserve）之和不可超过
    总池；demo_turn_limit 为每日口径不参与该约束。

仅 RUN_PG_TESTS=1 时真跑（conftest 已起 pgserver；每用例 TRUNCATE 重置周期）。
"""
import os
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

import psycopg  # noqa: E402

import budget_store  # noqa: E402
import registration_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import platform_features  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

pytestmark = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="AI 预算池隔离需 PG（RUN_PG_TESTS=1）",
)


def _req():
    return "req_" + uuid.uuid4().hex


def _reserve(subject_type, subject_id, source="platform"):
    return budget_store.reserve_turn(_req(), subject_type, subject_id, source)


# --------------------------------------------------------------------------- #
# 缺省值与池拆分
# --------------------------------------------------------------------------- #
def test_period_pool_defaults():
    """测试期推荐默认（§3.7）：总 30 = owner 保留 10 + user 共享 15；单 user 3；
    Demo 子池为每日口径缺省 50（滚动 24h，0014 起）。"""
    p = budget_store.get_current_period()
    assert p["platform_turn_limit"] == 30
    assert p["demo_turn_limit"] == 50
    assert p["owner_reserved_turn_limit"] == 10
    assert p["user_pool_turn_limit"] == 15
    assert p["user_turn_limit"] == 3


def test_period_pool_defaults_env_overridable(monkeypatch):
    """缺省周期创建值可覆盖（BUDGET_DEFAULT_* 语义；常量在创建时读取）。"""
    monkeypatch.setattr(budget_store, "DEFAULT_USER_TURN_LIMIT", 2)
    monkeypatch.setattr(budget_store, "DEFAULT_OWNER_RESERVED_TURN_LIMIT", 7)
    monkeypatch.setattr(budget_store, "DEFAULT_USER_POOL_TURN_LIMIT", 11)
    p = budget_store.get_current_period()
    assert p["user_turn_limit"] == 2
    assert p["owner_reserved_turn_limit"] == 7
    assert p["user_pool_turn_limit"] == 11


def test_usage_report_pool_sections():
    _reserve("user", "usr_a")
    _reserve("owner", "usr_owner")
    report = budget_store.usage_report()
    assert report["user_pool"]["total"] == 1
    assert report["user_pool"]["limit"] == 15
    assert report["owner"]["total"] == 1
    assert report["owner"]["reserved_limit"] == 10


# --------------------------------------------------------------------------- #
# owner 保留池 / user 共享池隔离
# --------------------------------------------------------------------------- #
def test_five_invited_users_cannot_consume_owner_reserve():
    """§6.3：3 个以上受邀账号也不能消耗 owner reserve。

    5 个 user 各用满单 user 3 次 = 15（user 共享池打满）；第 16 次 user 预占
    被拒（UserPoolBudgetExhausted）；owner 仍可继续用保留的额度。
    """
    for i in range(5):
        for _ in range(3):
            assert _reserve("user", "usr_p%d" % i)["state"] == "reserved"
    # 每 user 第 4 次：单 user 上限（3）
    with pytest.raises(budget_store.UserBudgetExhausted):
        _reserve("user", "usr_p0")
    # 新 user 第 1 次：user 共享池 15 已满
    with pytest.raises(budget_store.UserPoolBudgetExhausted) as ei:
        _reserve("user", "usr_p_new")
    assert ei.value.code == "user_pool_budget_exhausted"
    # owner 保留池未被侵占：owner 仍可预占（15 已用，总量 30 剩 15 ≥ 保留 10）
    for _ in range(10):
        assert _reserve("owner", "usr_owner")["state"] == "reserved"
    # owner 超出总量的部分由平台总量闸兜底（15+10=25，第 26 次起总量拒）
    for _ in range(5):
        assert _reserve("owner", "usr_owner")["state"] == "reserved"
    with pytest.raises(budget_store.PlatformBudgetExhausted):
        _reserve("owner", "usr_owner")
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 30
    assert report["user_pool"]["total"] == 15
    assert report["owner"]["total"] == 15


def test_user_pool_shared_across_users():
    """user 共享池按全部 user 聚合（而非每 user × 上限）。"""
    budget_store.update_period_limits({
        "user_turn_limit": 10, "user_pool_turn_limit": 4})
    for i in range(4):
        assert _reserve("user", "usr_s%d" % i)["state"] == "reserved"
    # 每个 user 自身额度未满（1 < 10），但共享池已满
    with pytest.raises(budget_store.UserPoolBudgetExhausted):
        _reserve("user", "usr_s0")


def test_owner_reserve_guard_blocks_users_beyond_guard():
    """guard = platform - owner_reserve = 20：user+demo 合计越过即拒。"""
    budget_store.update_period_limits({
        "user_turn_limit": 30, "user_pool_turn_limit": 30,
        # demo 子额度放宽（每日口径），保证第 6 次 demo 先撞 guard 而非每日闸
        "demo_turn_limit": 30, "demo_max_concurrency": 10})
    for i in range(15):
        assert _reserve("user", "usr_g%d" % i)["state"] == "reserved"
    for i in range(5):
        assert _reserve("demo", "dmo_g%d" % i)["state"] == "reserved"
    # 15 user + 5 demo = 20 = guard：再有任何 user/demo 预占都侵入保留池
    with pytest.raises(budget_store.OwnerReserveProtected) as ei:
        _reserve("user", "usr_g_extra")
    assert ei.value.code == "owner_reserve_protected"
    # demo 侧同样被保留闸拦（demo 每日子额度 30 未满，先撞 guard）
    with pytest.raises(budget_store.OwnerReserveProtected):
        _reserve("demo", "dmo_g_extra")
    # owner 不受保留闸约束（总量 30 已用 20，owner 还能 10）
    for _ in range(10):
        assert _reserve("owner", "usr_owner")["state"] == "reserved"


def test_owner_reserve_guard_ignores_misconfigured_reserve():
    """reserve > platform 视为配置错误：保留闸不阻断，总量闸兜底（防语义吞没）。"""
    budget_store.update_period_limits({
        "platform_turn_limit": 2, "demo_turn_limit": 2})
    assert _reserve("demo", "dmo_m1")["state"] == "reserved"
    assert _reserve("user", "usr_m1")["state"] == "reserved"
    with pytest.raises(budget_store.PlatformBudgetExhausted):
        _reserve("user", "usr_m2")


# --------------------------------------------------------------------------- #
# Demo 每日子池：滚动 24h 窗口口径（0014 起，不再按周期累计）
# --------------------------------------------------------------------------- #
def _age_reservation(pg_uri, request_id, hours):
    """直连把流水 reserved_at 拨回 hours 小时前（构造窗口外数据）。"""
    conn = psycopg.connect(pg_uri, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ai_budget_reservations SET reserved_at = "
                "now() - (%s * interval '1 hour') WHERE request_id=%s",
                (int(hours), request_id))
    finally:
        conn.close()


def test_demo_daily_window_excludes_out_of_window_consumed(pg_uri):
    """窗口外（reserved_at 距今 >24h）的 consumed 流水不计入每日闸与报表。"""
    budget_store.update_period_limits({
        "demo_turn_limit": 1, "demo_max_concurrency": 10})
    old = _reserve("demo", "dmo_w1")
    budget_store.consume(old["request_id"], "hp_old")
    _age_reservation(pg_uri, old["request_id"], 25)
    # 窗口计数 0/1：新预占成功（旧口径周期累计=1 会把这一单误拦）
    assert _reserve("demo", "dmo_w2")["state"] == "reserved"
    # 周期累计口径仍计 2（owner 保留闸/总量闸继续用周期值）
    assert budget_store.usage_report()["by_subject_type"]["demo"]["total"] == 2


def test_demo_daily_window_excludes_released():
    """released（显式退款/过期回收）不计入每日窗口。"""
    budget_store.update_period_limits({
        "demo_turn_limit": 1, "demo_max_concurrency": 10})
    r1 = _reserve("demo", "dmo_r1")
    budget_store.release(r1["request_id"])
    assert _reserve("demo", "dmo_r2")["state"] == "reserved"
    assert budget_store.usage_report()["demo"]["total"] == 1


def test_demo_daily_window_limit_enforced():
    """窗口内计数达到 demo_turn_limit（单日上限）→ DemoBudgetExhausted。"""
    budget_store.update_period_limits({
        "demo_turn_limit": 1, "demo_max_concurrency": 10})
    r1 = _reserve("demo", "dmo_l1")
    budget_store.consume(r1["request_id"], "hp_l1")
    with pytest.raises(budget_store.DemoBudgetExhausted) as ei:
        _reserve("demo", "dmo_l2")
    assert ei.value.code == "demo_budget_exhausted"
    assert ei.value.context["limit"] == 1
    assert ei.value.context["used"] == 1
    assert "每日" in str(ei.value) and "滚动 24" in str(ei.value)


def test_usage_report_demo_total_same_window_as_gate(pg_uri):
    """usage_report.demo.total 与每日闸同口径：窗口外不计、窗口内计。"""
    budget_store.update_period_limits({"demo_turn_limit": 5})
    r_in = _reserve("demo", "dmo_u1")
    budget_store.consume(r_in["request_id"], "hp_u1")
    r_out = _reserve("demo", "dmo_u2")
    budget_store.consume(r_out["request_id"], "hp_u2")
    _age_reservation(pg_uri, r_out["request_id"], 24 + 1)
    report = budget_store.usage_report()
    assert report["demo"]["total"] == 1  # 窗口外不计（与闸同口径）
    assert report["demo"]["limit"] == 5
    # 周期累计口径（by_subject_type）不受窗口影响，仍计 2
    assert report["by_subject_type"]["demo"]["total"] == 2


# --------------------------------------------------------------------------- #
# 邀请 ai_access 模板 + owner 授予（§3.7）
# --------------------------------------------------------------------------- #
OWNER_SEQ = {"n": 0}

#: 用例级 owner 缓存（0015 部分唯一索引后 PG 上不可重复 create(role=owner)；
#: conftest 每用例 TRUNCATE，跨用例自动重建）
_OWNER_CACHE = {"uid": None}


def _mk_owner():
    """取/建本用例的唯一 owner。

    批次 A store 线在 PG 上加了 users_single_enabled_owner_key：第二个
    enabled owner 会被索引拦下。本模块用例只消费 owner 的 user_id/role
    语义，用例内多次调用共享同一 owner 即可。
    """
    if _OWNER_CACHE["uid"]:
        row = user_store.get_user(_OWNER_CACHE["uid"])
        if row is not None:
            return row
    owners = user_store.list_enabled_owners()
    if owners:
        _OWNER_CACHE["uid"] = owners[0]["user_id"]
        return owners[0]
    OWNER_SEQ["n"] += 1
    owner = user_store.create_bootstrap_owner(
        "bp-owner-%d@x.com" % OWNER_SEQ["n"], "ownerpass123456")
    _OWNER_CACHE["uid"] = owner["user_id"]
    return owner


def _redeem_new_user(login_id, ai_access=False):
    """走真实邀请兑换创建一个 ai_access 模板可控的注册用户。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(
        owner["user_id"], login_id=login_id, ai_access=ai_access)
    out = registration_store.redeem_invite(inv["token"], login_id,
                                           "userpass1234567")
    return out["user"], inv


def test_invited_user_default_ai_access_false():
    u, _inv = _redeem_new_user("noai@x.com", ai_access=False)
    assert u["ai_access"] is False
    row = user_store.get_user(u["user_id"])
    assert row["ai_access"] is False


def test_invited_user_ai_access_template_true():
    u, _inv = _redeem_new_user("withai@x.com", ai_access=True)
    assert u["ai_access"] is True


def test_owner_legacy_created_user_keeps_ai_access():
    """owner 线下创建（/api/admin/users 路径）的存量用户保持 ai_access=True。"""
    u = user_store.create_user("legacy@x.com", "userpass1234567", role="user")
    assert user_store.get_user(u["user_id"])["ai_access"] is True


def test_ai_access_gate_blocks_and_grants(monkeypatch):
    """_ai_reserve_run_budget：ai_access=false → 403 ai_access_required；
    owner 授予（user_store_pg.set_user_ai_access）后恢复预占。"""
    import user_store_pg
    denied_user, _inv = _redeem_new_user("gate@x.com", ai_access=False)
    ok_user, _inv2 = _redeem_new_user("gate-ok@x.com", ai_access=True)
    # 平台凭据可用（绕过真实平台配置读取）
    monkeypatch.setattr(
        app_mod, "_resolve_ai_credentials",
        lambda ctx: ("platform", {"base_url": "http://127.0.0.1:9/v1",
                                  "api_key": "k", "model": "m"}))
    with app_mod.app.test_request_context():
        rid = _req()
        resv, err = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": denied_user["user_id"]}, rid)
        assert resv is None
        assert err is not None and err[1] == 403
        resp_body = err[0].get_json()
        assert resp_body["code"] == "ai_access_required"
    # owner 授予后放行（重新预占新 request_id）
    granted = user_store_pg.set_user_ai_access(denied_user["user_id"], True)
    assert granted["ai_access"] is True
    resv2, err2 = app_mod._ai_reserve_run_budget(
        {"role": "user", "user_id": denied_user["user_id"]}, _req())
    assert err2 is None and resv2["state"] == "reserved"
    # 授予前 ai_access=true 的受邀用户不受影响
    resv3, err3 = app_mod._ai_reserve_run_budget(
        {"role": "user", "user_id": ok_user["user_id"]}, _req())
    assert err3 is None and resv3["state"] == "reserved"
    # owner 不经过 ai_access 闸
    owner = _mk_owner()
    resv4, err4 = app_mod._ai_reserve_run_budget(
        {"role": "owner", "user_id": owner["user_id"]}, _req())
    assert err4 is None and resv4["state"] == "reserved"


def test_ai_access_gate_read_error_fails_closed(monkeypatch):
    """P0-B review 修复：ai_access 用户行读取异常 → fail-closed 503
    （ai_access=false 的用户不能因一次读库抖动获得平台 AI 访问）。"""
    u, _inv = _redeem_new_user("gate-err@x.com", ai_access=True)
    monkeypatch.setattr(
        app_mod, "_resolve_ai_credentials",
        lambda ctx: ("platform", {"base_url": "http://127.0.0.1:9/v1",
                                  "api_key": "k", "model": "m"}))

    def _boom(_uid):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(user_store, "get_user", _boom)
    with app_mod.app.test_request_context():
        resv, err = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": u["user_id"]}, _req())
        assert resv is None
        assert err is not None and err[1] == 503
        assert err[0].get_json()["code"] == "ai_access_check_unavailable"


def test_ai_access_admin_api(monkeypatch):
    """POST /api/admin/users/<id>/ai-access：owner-only + CSRF + 持久化。"""
    from _pt_helpers import csrf_client
    owner = _mk_owner()
    u, _inv = _redeem_new_user("api-ai@x.com", ai_access=False)
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.post("/api/admin/users/%s/ai-access" % u["user_id"],
                    json={"enabled": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ai_access"] is True
    assert user_store.get_user(u["user_id"])["ai_access"] is True
    # 非 owner 403
    c2 = csrf_client(app_mod.app.test_client())
    with c2.session_transaction() as s:
        s.update({"auth_user": "g", "user_id": u["user_id"], "role": "user",
                  "auth_version": u.get("auth_version", 1)})
    r2 = c2.post("/api/admin/users/%s/ai-access" % u["user_id"],
                 json={"enabled": False})
    assert r2.status_code == 403


def test_budget_put_validates_pool_sum():
    """owner PUT：周期口径子池（user_pool+owner_reserve）之和不可超过
    platform；demo_turn_limit 为每日滚动 24h 口径，不参与该约束。"""
    from _pt_helpers import csrf_client
    owner = _mk_owner()
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.put("/api/admin/settings/ai-budget", json={
        "owner_reserved_turn_limit": 25, "user_pool_turn_limit": 15})
    assert r.status_code == 400
    assert "不可超过" in r.get_json()["error"]
    # demo 每日口径与周期总量不再可比：demo_turn_limit > platform 允许保存
    r_demo = client.put("/api/admin/settings/ai-budget", json={
        "demo_turn_limit": 40})
    assert r_demo.status_code == 200, r_demo.get_data(as_text=True)
    assert r_demo.get_json()["limits"]["demo_turn_limit"] == 40
    r2 = client.put("/api/admin/settings/ai-budget", json={
        "owner_reserved_turn_limit": 12, "user_pool_turn_limit": 13})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    limits = r2.get_json()["limits"]
    assert limits["owner_reserved_turn_limit"] == 12
    assert limits["user_pool_turn_limit"] == 13
    # 未重提交的 demo_turn_limit 沿用现值（40），不参与周期加和判定
    assert limits["demo_turn_limit"] == 40


def test_concurrent_user_pool_exact_limit():
    """并发 25 线程 ×（每 user 1 次）：user 共享池 15 恰好成功 15，无超扣。"""
    n = 25

    def worker(i):
        try:
            budget_store.reserve_turn(_req(), "user", "usr_cc%d" % i, "platform")
            return "ok"
        except budget_store.BudgetError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(worker, range(n)))
    assert results.count("ok") == 15
    assert all(r in ("ok", "user_pool_budget_exhausted") for r in results), results
    assert budget_store.usage_report()["user_pool"]["total"] == 15


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
