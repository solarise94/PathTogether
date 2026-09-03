# -*- coding: utf-8 -*-
"""review R2-F2 锁有效性证明测试：用户开通（建号/兑换）与 cutover 串行化
+ 维护闸暂停注册/建号（仅 RUN_PG_TESTS=1）。

钉死的契约（spend_store shim 区段）：

- ``USER_PROVISIONING_ADVISORY_LOCK_KEY``：建号/兑换事务内取 **xact 级**
  ``pg_advisory_xact_lock``；cutover apply/rollback 脚本持**会话级**
  ``pg_advisory_lock``（同键）全程——两侧互斥串行。本文件用「conn A 持
  会话锁 → 建号子线程阻塞 → 解锁后完成」证明互斥真实生效（lock_timeout
  15s 生效、0.5s 时点未建成）；
- ``is_dispatch_maintenance_tx``：cutover 维护闸开启（或读取异常，
  fail-closed）→ 建号/兑换抛 ``ProvisioningMaintenanceError``
  （code=ai_dispatch_maintenance），用户行/invite 均零副作用；关闸恢复；
- app.py v1 建号端点：维护中稳定 503 ai_dispatch_maintenance（与 AI
  dispatch 同款）。

运行：RUN_PG_TESTS=1 python3 -m pytest tests/test_provisioning_lock.py -q
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
os.environ["ADMIN_PASSWORD"] = ""
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402
import user_store_pg  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="advisory lock / 维护闸矩阵需真实 PG（RUN_PG_TESTS=1）",
)

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402

_PW = "password-123456"
_OWNER_PW = "ownerpass123456"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例：目录隔离 + 单轨种子（策略 + 闸=false + defaults 默认额度行）。

    platform_settings / ai_spend_total_defaults 在 conftest 的 _BUSINESS_TABLES
    TRUNCATE 清单内，闸值与默认额度不会跨用例串扰（每用例先 TRUNCATE 再由
    本 fixture 重播种；conftest 亦会恢复一条 20 CNY defaults 基线行）。"""
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    app_mod.app.config["TESTING"] = True
    if BACKEND == "postgres":
        bh.seed_spend_policies()
        bh.seed_spend_settings()
    yield


def _mk_owner(login="lock-owner@x.com"):
    return user_store.create_user(login, _OWNER_PW, role="owner")


def _set_gate(expected, value):
    """CAS 切维护闸（生产同款路径）；返回写入值。"""
    return settings_store.compare_and_set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, expected, value,
        updated_by="pytest")


# --------------------------------------------------------------------------- #
# 1. 串行化证明：cutover 会话锁阻塞建号，解锁后放行
# --------------------------------------------------------------------------- #
@PG
def test_cutover_session_lock_blocks_provisioning_until_unlock(pg_uri):
    """conn A 持会话级 pg_advisory_lock(开通键) → 子线程建号阻塞（0.5s 时点
    仍 blocked、用户行未落地）；A 解锁 → 子线程完成且结果正确（单轨恒建
    allowance 行）。锁释放走 finally（断言失败也不残留会话锁）。"""
    _mk_owner()
    conn = psycopg.connect(pg_uri, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)",
                        (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
        outcome = {}

        def _create():
            try:
                outcome["result"] = \
                    user_store_pg.create_user_with_total_allowance(
                        "locked@x.com", _PW)
            except Exception as exc:  # 传播回主线程断言
                outcome["error"] = exc

        th = threading.Thread(target=_create, name="prov-blocked")
        th.start()
        time.sleep(0.5)
        assert th.is_alive(), "子线程应仍阻塞在用户开通锁上（未建成）"
        assert user_store.get_user_by_login_id("locked@x.com") is None, \
            "阻塞期间不得产生半创建用户行"
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)",
                        (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
            assert cur.fetchone()[0] is True
        th.join(timeout=30)
        assert not th.is_alive(), "解锁后建号应完成（30s 足够，lock_timeout=15s）"
        assert "error" not in outcome, outcome.get("error")
        user, allowance = outcome["result"]
        assert user["login_id"] == "locked@x.com"
        # 单轨：无 X 建号走 defaults 解析（fixture 种子 = 20 CNY）恒建行
        assert allowance is not None
        assert spend_store.get_total_allowance(user["user_id"]) is not None
        assert int(allowance["limit_nano_cny"]) == 20 * 10 ** 9
    finally:
        # 收尾清理：关连接即释放会话级锁（xact 锁随各事务早已终结）
        conn.close()


@PG
def test_lock_helper_maps_timeout_to_provisioning_maintenance(pg_uri):
    """锁等待超时（lock_timeout）映射为 ProvisioningMaintenanceError：
    conn A 持会话锁不释放 → 子线程建号在 15s 上限处报错而非无限挂起。"""
    _mk_owner()
    conn = psycopg.connect(pg_uri, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)",
                        (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
        outcome = {}

        def _create():
            try:
                user_store_pg.create_user_with_total_allowance(
                    "timeout@x.com", _PW)
            except Exception as exc:
                outcome["error"] = exc

        th = threading.Thread(target=_create, name="prov-timeout")
        th.start()
        t0 = time.time()
        th.join(timeout=60)
        elapsed = time.time() - t0
        assert not th.is_alive()
        err = outcome.get("error")
        assert isinstance(err, spend_store.ProvisioningMaintenanceError), err
        assert 14.0 <= elapsed <= 45.0, \
            "应在 lock_timeout=15s 量级拒绝（实测 %.1fs）" % elapsed
        assert user_store.get_user_by_login_id("timeout@x.com") is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. 维护闸：开闸暂停建号/兑换（零副作用），关闸恢复；缺键按开闸
# --------------------------------------------------------------------------- #
@PG
def test_maintenance_gate_blocks_create_and_redeem_then_recovers():
    """开闸（CAS false→true，生产同款路径）：建号与兑换各抛
    ProvisioningMaintenanceError；用户未建、invite 未消费（关闸后仍可用）。
    关闸恢复：建号/兑换照常成功。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(
        owner["user_id"], login_id="gated@x.com")
    assert _set_gate(False, True) is True
    with pytest.raises(spend_store.ProvisioningMaintenanceError):
        user_store_pg.create_user_with_total_allowance(
            "gated@x.com", _PW)
    with pytest.raises(spend_store.ProvisioningMaintenanceError):
        registration_store.redeem_invite(inv["token"], "gated@x.com", _PW)
    # 零副作用：用户行不存在、邀请未消费
    assert user_store.get_user_by_login_id("gated@x.com") is None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["consumed_at"] is None and row["use_count"] == 0
    # 关闸恢复：invite 仍可兑换（成功路径单轨恒建 allowance 行）。
    # compare_and_set_setting 返回**写入后的值**——关闸写入 False，未抛
    # SettingsVersionConflictError 即 CAS 命中
    _set_gate(True, False)
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "aftergate@x.com", _PW)
    assert user["login_id"] == "aftergate@x.com" and allowance is not None
    out = registration_store.redeem_invite(inv["token"], "gated@x.com", _PW)
    assert out["user"]["login_id"] == "gated@x.com"
    assert out["total_allowance"] is not None
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 1


@PG
def test_missing_maintenance_setting_treated_open():
    """缺键按开闸（与 app 层 _ai_dispatch_maintenance_active 同口径；0029
    种子保证生产恒有行，缺键时 cutover apply 的闸 CAS 会失败、advisory
    锁仍互斥）→ 建号正常进行；**读异常**才 fail-closed（shim 语义）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM platform_settings WHERE key=%s",
                        (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
        conn.commit()
    finally:
        conn.close()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "nokey@x.com", _PW)
    assert user["login_id"] == "nokey@x.com"


# --------------------------------------------------------------------------- #
# 3. 端点层：维护中 POST /api/admin/v1/users → 503 ai_dispatch_maintenance
# --------------------------------------------------------------------------- #
@PG
def test_v1_users_create_returns_503_while_gate_on():
    """开闸时 v1 建号端点稳定 503（code=ai_dispatch_maintenance，与 AI
    dispatch 同款文案口径），且不产生半创建用户。"""
    owner = _mk_owner("ep-owner@x.com")
    assert _set_gate(False, True) is True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s.update({"auth_user": owner["login_id"],
                  "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = c.post("/api/admin/v1/users",
               json={"login_id": "epgated@x.com", "password": _PW})
    assert r.status_code == 503, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error"]["code"] == "ai_dispatch_maintenance"
    assert user_store.get_user_by_login_id("epgated@x.com") is None
    # 关闸后同一端点恢复 200（CAS 返回写入值 False；未抛冲突即命中）
    _set_gate(True, False)
    r2 = c.post("/api/admin/v1/users",
                json={"login_id": "epafter@x.com", "password": _PW})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert user_store.get_user_by_login_id("epafter@x.com") is not None
