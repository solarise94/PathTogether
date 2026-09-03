# -*- coding: utf-8 -*-
"""Batch B：注册 user 一次性总额度存储层测试（docs
review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §Batch B / §3.1 /
§7.3；R3 Wave1-Money 起单轨）。

覆盖（全部需真实 PG；json 模式整文件 skip——额度语义无 json 后端）：
  - 0029/0032 迁移：fresh 全量含 0029+0032、幂等；0032 删除
    user_spend_target/registration_open 旧行并物化 defaults；billing_holds
    CHECK 接受 legacy 双 NULL、拒绝双目标；ai_spend_total_allowances
    subject/source CHECK；
  - 原子性：建号/邀请兑换同事务建 allowance（注入失败整体回滚：用户不创建、
    邀请不消费、user_acquisition 零新增、不建 user_override）；
  - 旧 monthly 邀请面值形参已物理删除（R3 Wave2-Compat；传入即
    TypeError，0033 亦 DROP 列）；退役 source/campaign/cohort 参数
    忽略不写库；
  - 原子授权：并发 2 hold 竞同一 user 最后额度只有满足不等式的成功
    （多连接 + Barrier，禁 mock；不超卖）；settle/release/expire 投影准确；
    X<已用时拒绝且 overage 正确、新调用 fail-closed；
  - 跨月：allowance 不重建不归零（无窗口语义——手动推进 at 验证）；
  - denial events：savepoint 回滚后仍提交、(call_id, reason) 去重；
  - hold 目标矩阵：user hold 恒 allowance、demo/owner 恒 window；
  - CAS：set_user_total_limit / set_total_default / compare_and_set_setting
    409 语义；restore-default 不清 spent；
  - reconcile_total_allowances 报 drift 不修账；
  - admin_users_spend_summaries 单轨互斥形态（user 恒 total——缺行稳定
    error；owner 恒 window）；
  - admin_demo_spend_stats 只读聚合（前后业务表行数不变）；
  - cutover 脚本（R3 Wave1-Money 瘦身为纯迁移：无 target CAS、无
    rollback-plan）：preflight（维护闸已开、默认总额度不可解析、物化名单
    无策略、allowance/窗口限额不一致——逐一硬失败）/ apply（open hold
    中止保持维护态、无当前窗口 user 的零消费窗物化+审计、remaining 逐
    user 守恒、提交后自动关闸）。

运行：RUN_PG_TESTS=1 python3 -m pytest tests/test_spend_total_allowances.py -q
"""
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import pytest  # noqa: E402

import billing_pricing  # noqa: E402
import billing_store  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402
import user_store_pg  # noqa: E402

from pg_compat import BACKEND  # noqa: E402

if BACKEND == "postgres":
    import psycopg  # noqa: E402
    import _billing_helpers as bh  # noqa: E402

UTC = timezone.utc
INSTALLATION = "pin_total_allowance_test"


# --------------------------------------------------------------------------- #
# 公共基建（镜像 test_billing_hold_settle_chain 的种子口径）
# --------------------------------------------------------------------------- #
def _conn():
    import pg_store
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


def _seed_all(monkeypatch_ttl=None):
    """价格书（corrected v2 全域）+ 策略 + cutover 提前 + 0029 种子。"""
    bh.seed_price_books()
    bh.seed_spend_policies()
    bh.seed_spend_settings()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE billing_price_books SET effective_from="
                "'2020-01-01T00:00:00+00:00' WHERE price_book_id = ANY (%s)",
                (list(bh.CORRECTED_BOOK_IDS),))
            cur.execute(
                "UPDATE billing_price_books SET status='retired' "
                "WHERE price_book_id = ANY (%s)",
                (list(bh.LEGACY_BOOK_IDS),))
            cur.execute(
                "INSERT INTO platform_settings (key, value, updated_at, "
                "updated_by) VALUES ('pricing_v2_cutover_at', %s, now(), "
                "'pytest') ON CONFLICT (key) DO UPDATE SET "
                "value=EXCLUDED.value",
                (psycopg.types.json.Jsonb(
                    datetime(2020, 1, 1, tzinfo=UTC).timestamp()),))
            cur.execute("UPDATE ai_spend_policies SET effective_from="
                        "'2020-01-01T00:00:00+00:00' WHERE policy_id = ANY"
                        "(%s)", (list(bh.SEED_POLICY_IDS),))
        conn.commit()
    finally:
        conn.close()


def _set_mode(mode):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_settings (key, value, updated_at, "
                "updated_by) VALUES ('spend_enforcement_mode', %s, now(), "
                "'pytest') ON CONFLICT (key) DO UPDATE SET "
                "value=EXCLUDED.value",
                (psycopg.types.json.Jsonb(mode),))
        conn.commit()
    finally:
        conn.close()


def _sql_one(sql, params=()):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    finally:
        conn.close()
    return rows


def _exec(sql, params=()):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _allowance_row(user_id):
    rows = _sql_one("SELECT * FROM ai_spend_total_allowances "
                    "WHERE subject_id=%s", (user_id,))
    return rows[0] if rows else None


def _hold_row(**kw):
    col = "call_id" if kw.get("call_id") else "hold_id"
    rows = _sql_one("SELECT * FROM billing_holds WHERE %s=%%s" % col,
                    (kw.get("call_id") or kw.get("hold_id"),))
    return rows[0] if rows else None


def _count(table, where="1=1", params=()):
    rows = _sql_one("SELECT count(*)::int AS n FROM %s WHERE %s"
                    % (table, where), params)
    return int(rows[0]["n"])


def _user(login):
    """直接插入 user 行（绕开建号组合原语）。

    review R2 起 PG role=user 的 user_store.create_user 委托
    create_user_with_total_allowance：total 目标下会自动按默认建 allowance
    行，与本文件「手工建行/设定特定 spent/opening 基线」的夹具语义冲突
    （唯一约束 SpendTotalAllowanceExistsError）。本文件测的是 allowance
    机制本身；开通串行化/维护闸/自动建行由 test_provisioning_lock.py 与
    test_user_creation_spend_target.py 覆盖。
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            row = user_store_pg._insert_user_tx(
                cur, user_store_pg._user_id(),
                user_store_pg._normalize_login_id(login), login,
                "pass123456789012", user_store_pg.ROLE_USER, time.time())
        conn.commit()
        return user_store_pg._public(row)
    finally:
        conn.close()


def _mk_owner():
    return user_store.create_bootstrap_owner("total-owner@x.com",
                                             "ownerpass123456")


def _ids():
    hex32 = uuid.uuid4().hex
    return ("call_" + hex32, "sess_" + uuid.uuid4().hex[:16],
            "req_" + uuid.uuid4().hex[:16])


def _hold_body(subject_type, subject_id, *, session_id, request_id=None,
               call_id=None, model="deepseek-v4-flash", est_in=1_000_000,
               max_out=200_000, user_id=None):
    call_id = call_id or ("call_" + uuid.uuid4().hex)
    body = {
        "call_id": call_id,
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": "deepseek",
        "model": model,
        "estimated_input_tokens": est_in,
        "max_output_tokens": max_out,
    }
    if request_id is not None:
        body["request_id"] = request_id
    if user_id is not None:
        body["user_id"] = user_id
    return body


def _bound_user_hold(user, subject_type="user", model="deepseek-v4-flash"):
    call_id, session_id, request_id = _ids()
    bh.bind_reservation(request_id, session_id, subject_type,
                        user["user_id"])
    return _hold_body(subject_type, user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id, model=model,
                      user_id=user["user_id"])


def _authorize(body, now=None):
    return billing_store.authorize_hold(
        body, installation_id=INSTALLATION, plugin_id="histopilot", now=now)


def _settle(hold_id, body, now=None):
    return billing_store.settle_hold(
        hold_id, body, installation_id=INSTALLATION, plugin_id="histopilot",
        now=now)


def _expected_estimate(at, est_in=1_000_000, max_out=200_000,
                       model="deepseek-v4-flash"):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, at)
    finally:
        conn.close()
    assert book is not None
    cap = billing_store.estimate_output_token_cap()
    est_out = max_out if cap <= 0 else min(max_out, cap)
    return billing_pricing.price_tokens_nano(0, est_in, est_out, book)


def _usage_event(call_id, session_id, subject_type, subject_id, *, occurred,
                 tokens=(0, 1_000_000, 200_000), user_id=None, event_id=None):
    hit, miss, out = tokens
    event = {
        "event_id": event_id or ("use_" + uuid.uuid4().hex),
        "call_id": call_id,
        "schema_version": 1,
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "enqueued_at": (occurred + timedelta(seconds=1)
                        ).isoformat().replace("+00:00", "Z"),
        "cache_hit_input_tokens": hit,
        "cache_miss_input_tokens": miss,
        "output_tokens": out,
        "reasoning_tokens": 0,
        "total_tokens": hit + miss + out,
    }
    if user_id is not None:
        event["user_id"] = user_id
    return event


def _insert_priced_event(cur, hex32, subject_type, subject_id, occurred_at,
                         charge_nano):
    """直接写 priced usage event（对账/统计断言用；绕过绑定链）。"""
    cur.execute(
        "INSERT INTO ai_usage_events (event_id, call_id, payload_hash, "
        "schema_version, session_id, subject_type, subject_id, provider, "
        "model, cache_hit_input_tokens, cache_miss_input_tokens, "
        "output_tokens, reasoning_tokens, total_tokens, occurred_at, "
        "enqueued_at, received_at, status, provider_price_book_id, "
        "charge_price_book_id, provider_cost_nano_cny, charge_nano_cny) "
        "VALUES (%s,%s,%s,1,'sess_rc',%s,%s,'deepseek','deepseek-v4-flash',"
        "100,1000,200,0,1300,%s,%s,%s,'priced',%s,%s,%s,%s)",
        ("use_" + hex32, "call_" + hex32, "0" * 64, subject_type, subject_id,
         occurred_at, occurred_at, occurred_at, bh.CORRECTED_BOOK_IDS[0],
         bh.CORRECTED_BOOK_IDS[1], charge_nano, charge_nano))


# =========================================================================== #
# 1. 0029/0032 迁移：种子 / CHECK / 幂等
# =========================================================================== #
def test_0029_0032_seeds_present_and_idempotent():
    _seed_all()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                        (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
            assert cur.fetchone()["value"] is False
            # 0032：user_spend_target / registration_open 旧行删除
            cur.execute("SELECT count(*)::int AS n FROM platform_settings "
                        "WHERE key IN ('user_spend_target', "
                        "'registration_open')")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()
    # 整文件重放幂等：种子不重复、CHECK 重复添加被判存跳过
    bh.seed_spend_settings()
    assert _count("platform_settings",
                  "key='ai_dispatch_maintenance'") == 1
    assert _count("platform_settings", "key='user_spend_target'") == 0


def test_fresh_migration_includes_0029_and_0032():
    pytest.importorskip("pgserver")
    import tempfile
    import pg_store
    data_dir = tempfile.mkdtemp(prefix="m0029-fresh-")
    srv = pytest.importorskip("pgserver").get_server(data_dir)
    try:
        conn = psycopg.connect(srv.get_uri())
        try:
            files = pg_store.ensure_schema(conn)
            pg_store.ensure_schema(conn)  # 幂等重跑
            assert "0029_user_total_allowances_and_denials.sql" in files
            assert "0032_user_total_allowance_single_track.sql" in files
            conn.row_factory = psycopg.rows.dict_row
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM schema_migrations "
                            "WHERE filename="
                            "'0029_user_total_allowances_and_denials.sql'")
                assert cur.fetchone()["n"] == 1
                # 0032：target 行被删除（0029 seed 后 0032 DELETE）
                cur.execute("SELECT count(*)::int AS n FROM platform_settings "
                            "WHERE key='user_spend_target'")
                assert cur.fetchone()["n"] == 0
                # 0032：user_default 策略面值物化为 defaults 权威行（20 CNY）
                cur.execute("SELECT default_limit_nano_cny FROM "
                            "ai_spend_total_defaults WHERE singleton='global'")
                assert int(cur.fetchone()["default_limit_nano_cny"]) \
                    == 20 * 10 ** 9
                cur.execute("SELECT column_name FROM information_schema"
                            ".columns WHERE table_name='registration_invites'")
                cols = {r["column_name"] for r in cur.fetchall()}
                assert "total_limit_nano_cny" in cols
        finally:
            conn.close()
    finally:
        srv.cleanup()


def test_billing_holds_target_mutex_check():
    """0029 CHECK：legacy 双 NULL 接受；双目标拒绝；subject CHECK 拒非 user
    写入 ai_spend_total_allowances。"""
    _seed_all()
    hex_a, hex_b, hex_c = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    # legacy：两目标皆 NULL（0024 前历史行形态）→ 接受
    _exec(
        "INSERT INTO billing_holds (hold_id, call_id, subject_type, "
        "subject_id, installation_id, session_id, model, estimated_nano_cny, "
        "status, expires_at) VALUES (%s,%s,'user','usr_legacy','i','s','m',"
        "100,'open', now() + interval '1h')",
        ("hold_legacy_" + hex_a[:16], "call_" + hex_a))
    # 双目标 → CheckViolation
    _exec("INSERT INTO ai_spend_total_allowances (allowance_id, subject_id, "
          "limit_nano_cny, source) VALUES (%s,'usr_dual',1000,'invite')",
          ("sta_" + hex_b[:20],))
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(
            "INSERT INTO billing_holds (hold_id, call_id, subject_type, "
            "subject_id, installation_id, session_id, model, status, "
            "expires_at, spend_window_id, spend_total_allowance_id) VALUES "
            "(%s,%s,'user','usr_dual','i','s','m','open', now() + interval "
            "'1h', 'spw_x', %s)",
            ("hold_dual_" + hex_c[:16], "call_" + hex_c, "sta_" + hex_b[:20]))
    # owner/demo 主体写总额度表 → subject CHECK 拒绝（store 断言 + CHECK
    # 双保险的 CHECK 半边；显式传 subject_type 才触发）
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec("INSERT INTO ai_spend_total_allowances (allowance_id, "
              "subject_type, subject_id, limit_nano_cny, source) VALUES "
              "(%s,'owner','usr_owner_x',1000,'invite')",
              ("sta_" + hex_a[:20],))
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec("INSERT INTO ai_spend_total_allowances (allowance_id, "
              "subject_type, subject_id, limit_nano_cny, source) VALUES "
              "(%s,'demo','demo_global',1000,'invite')",
              ("sta_" + hex_a[:20],))
    # 非法 source 拒绝
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec("INSERT INTO ai_spend_total_allowances (allowance_id, "
              "subject_id, limit_nano_cny, source) VALUES (%s,'usr_srcline',"
              "1000,'cron')", ("sta_" + hex_c[:20],))


# =========================================================================== #
# 2. 建号/兑换同事务建 allowance（原子性 + 退役语义）
# =========================================================================== #
def test_create_user_total_allowance_atomic_and_no_override():
    _seed_all()
    owner = _mk_owner()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "created@x.com", "pass123456789012", total_limit_nano_cny=30 * 10 ** 9,
        actor_user_id=owner["user_id"])
    assert allowance["subject_id"] == user["user_id"]
    assert allowance["limit_nano_cny"] == 30 * 10 ** 9
    assert allowance["source"] == "admin_create"
    # 不建 user_override（月额度覆盖退役）
    resolved = spend_store.resolve_policy("user", user["user_id"])
    assert resolved["scope_type"] == "user_default"
    assert _count("ai_spend_policies", "scope_type='user_override'") == 0
    # 注入失败 → 用户不创建（同事务整体回滚）
    orig = spend_store.create_user_total_allowance_tx

    def boom(*_a, **_k):
        raise RuntimeError("allowance down")
    spend_store.create_user_total_allowance_tx = boom
    try:
        with pytest.raises(RuntimeError):
            user_store_pg.create_user_with_total_allowance(
                "rollback@x.com", "pass123456789012",
                total_limit_nano_cny=10 ** 9)
    finally:
        spend_store.create_user_total_allowance_tx = orig
    assert user_store.get_user_by_login_id("rollback@x.com") is None
    # 兼容壳 create_user_with_spend_override 已随单轨删除（旧 wire 名退役）
    assert not hasattr(user_store_pg, "create_user_with_spend_override")


def test_redeem_invite_creates_allowance_atomically():
    _seed_all()
    owner = _mk_owner()
    inv = registration_store.create_invite(
        owner["user_id"], login_id="redeem@x.com", ai_access=True,
        total_limit_nano_cny=15 * 10 ** 9)
    out = registration_store.redeem_invite(inv["token"], "redeem@x.com",
                                           "pass123456789012")
    uid = out["user"]["user_id"]
    assert out["total_allowance"]["limit_nano_cny"] == 15 * 10 ** 9
    assert out["total_allowance"]["source"] == "invite"
    assert _allowance_row(uid) is not None
    # 退役写路径：user_acquisition 零新增、不建 override
    assert _count("user_acquisition") == 0
    assert _count("ai_spend_policies", "scope_type='user_override'") == 0
    # 注入失败 → 邀请不消费、用户不创建、无 allowance 残留
    inv2 = registration_store.create_invite(
        owner["user_id"], login_id="rb@x.com",
        total_limit_nano_cny=10 ** 9)
    orig = spend_store.create_user_total_allowance_tx

    def boom(*_a, **_k):
        raise RuntimeError("allowance down")
    spend_store.create_user_total_allowance_tx = boom
    try:
        with pytest.raises(RuntimeError):
            registration_store.redeem_invite(inv2["token"], "rb@x.com",
                                             "pass123456789012")
    finally:
        spend_store.create_user_total_allowance_tx = orig
    assert user_store.get_user_by_login_id("rb@x.com") is None
    row = registration_store.get_invite(inv2["invite_id"])
    assert row["consumed_at"] is None and row["use_count"] == 0
    assert _count("ai_spend_total_allowances") == 1


def test_invite_retired_fields_ignored_and_legacy_monthly_becomes_total():
    _seed_all()
    owner = _mk_owner()
    # 退役参数：兼容接受但不校验不写库（app.py wave 2 才改调用方）
    inv = registration_store.create_invite(
        owner["user_id"], login_id="retired@x.com", cohort="c1",
        source_code="Not A Slug!", campaign_id="no-such-campaign")
    assert inv["source_code"] in ("", None)
    assert inv["campaign_id"] in ("", None)
    assert inv["cohort"] in ("", None)
    row = _sql_one("SELECT * FROM registration_invites WHERE invite_id=%s",
                   (inv["invite_id"],))[0]
    assert not row["source_code"] and row["campaign_id"] is None \
        and not row["cohort"]
    # create audit 不再携带 source/campaign/cohort/acq 字段
    audits = _sql_one("SELECT detail FROM audit_events WHERE action="
                      "'registration.invite_create' AND target_id=%s",
                      (inv["invite_id"],))
    assert audits
    assert not ({"source_code", "campaign_id", "cohort", "acq",
                 "campaign_bound"} & set(audits[0]["detail"]))
    # R3 Wave2-Compat：旧 monthly 形参已物理删除（传入即 TypeError；
    # 0033 亦 DROP 列，total_limit_nano_cny 为唯一金额模板面）
    with pytest.raises(TypeError):
        registration_store.create_invite(
            owner["user_id"], login_id="legacy@x.com",
            monthly_limit_nano_cny=7 * 10 ** 9)
    with pytest.raises(TypeError):
        registration_store.create_invite(
            owner["user_id"], login_id="amb@x.com",
            monthly_limit_nano_cny=10 ** 9, total_limit_nano_cny=10 ** 9)


# =========================================================================== #
# 3. 原子授权与结算（§Batch B 原子授权与结算 1-2）
# =========================================================================== #
def test_concurrent_two_holds_only_one_crosses_total_limit():
    """两个并发 hold 竞同一 user 最后额度：只有满足不等式的一个成功（不超卖）。"""
    _seed_all()
    _set_mode("registered")
    user = _user("race-total@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 0, source="invite")  # 先建行再改限额
        conn.commit()
    finally:
        conn.close()
    est = _expected_estimate(datetime.now(UTC))
    limit = est + est // 2  # 1.5×est：第二个必拒
    _exec("UPDATE ai_spend_total_allowances SET limit_nano_cny=%s "
          "WHERE subject_id=%s", (limit, user["user_id"]))
    b1 = _bound_user_hold(user)
    b2 = _bound_user_hold(user)
    barrier = threading.Barrier(2)
    results = []

    def worker(body):
        barrier.wait()
        try:
            results.append(("ok", _authorize(body)))
        except spend_store.SpendBudgetExhaustedError as exc:
            results.append(("denied", exc))

    threads = [threading.Thread(target=worker, args=(b,)) for b in (b1, b2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(k for k, _ in results) == ["denied", "ok"]
    row = _allowance_row(user["user_id"])
    assert int(row["reserved_nano_cny"]) == est        # 只有赢家预占
    assert int(row["spent_nano_cny"]) == 0
    assert int(row["version"]) >= 2
    winners = [b for b in (b1, b2) if _hold_row(call_id=b["call_id"])]
    assert len(winners) == 1


def test_hold_target_matrix_user_total_demo_owner_window():
    """新 user hold 仅 allowance；demo/owner hold 仅 window。"""
    _seed_all()
    _set_mode("registered")
    user = _user("matrix@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 12, source="invite")
        conn.commit()
    finally:
        conn.close()
    body = _bound_user_hold(user)
    r = _authorize(body)
    assert r["status"] == "open"
    row = _hold_row(call_id=body["call_id"])
    assert row["spend_total_allowance_id"] == \
        _allowance_row(user["user_id"])["allowance_id"]
    assert row["spend_window_id"] is None
    # demo：周窗口（不读 user_spend_target）
    _, session_id, _ = _ids()
    cap = bh.bind_demo_session(session_id)
    dbody = _hold_body("demo", cap, session_id=session_id)
    dr = _authorize(dbody)
    drow = _hold_row(call_id=dbody["call_id"])
    assert drow["spend_window_id"] is not None
    assert drow["spend_total_allowance_id"] is None
    assert dr["enforcement_mode"] == "registered"  # demo 观测（等 all）
    # owner：月窗口
    owner_user = _user("owner-matrix@x.com")
    _exec("UPDATE users SET role='owner' WHERE user_id=%s",
          (owner_user["user_id"],))
    obody = _bound_user_hold(owner_user, subject_type="owner")
    _authorize(obody)
    orow = _hold_row(call_id=obody["call_id"])
    assert orow["spend_window_id"] is not None
    assert orow["spend_total_allowance_id"] is None


def _settle_allowance_flow(user, est):
    """authorize → settle 真实 usage：spent=actual、reserved=0。"""
    body = _bound_user_hold(user)
    hold = _authorize(body, now=datetime.now(UTC))
    assert hold["status"] == "open"
    occurred = datetime.now(UTC)
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=occurred,
                         user_id=user["user_id"])
    out = _settle(hold["hold_id"], {"usage_event": event},
                  now=occurred + timedelta(seconds=5))
    assert out["status"] == "settled"
    return out, event


def test_settle_release_expire_projection_accurate():
    _seed_all()
    _set_mode("registered")
    user = _user("flow@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 12, source="invite")
        conn.commit()
    finally:
        conn.close()
    est = _expected_estimate(datetime.now(UTC))
    # release：reserved 归还
    body = _bound_user_hold(user)
    hold = _authorize(body)
    assert int(_allowance_row(user["user_id"])["reserved_nano_cny"]) == est
    released = _settle(hold["hold_id"], None)
    assert released["status"] == "released"
    row = _allowance_row(user["user_id"])
    assert int(row["reserved_nano_cny"]) == 0
    assert int(row["spent_nano_cny"]) == 0
    # settle：actual 记 spent（含 actual>estimated overage）
    out, event = _settle_allowance_flow(user, est)
    actual = out["actual_nano_cny"]
    row = _allowance_row(user["user_id"])
    assert int(row["spent_nano_cny"]) == actual
    assert int(row["reserved_nano_cny"]) == 0
    # settle 重放幂等：spent 不变
    _settle(out["hold_id"], {"usage_event": event})
    assert int(_allowance_row(user["user_id"])["spent_nano_cny"]) == actual
    # expire：TTL 过期后下一次 authorize 惰性回收并归还 reserved
    t_call, t_sess, t_req = _ids()
    bh.bind_reservation(t_req, t_sess, "user", user["user_id"])
    trigger = _hold_body("user", user["user_id"], session_id=t_sess,
                         request_id=t_req, call_id=t_call)
    body2 = _bound_user_hold(user)
    hold2 = _authorize(body2, now=datetime.now(UTC))
    assert int(_allowance_row(user["user_id"])["reserved_nano_cny"]) == est
    later = datetime.now(UTC) + timedelta(seconds=600)
    _authorize(trigger, now=later)  # 触发惰性回收
    assert _hold_row(hold_id=hold2["hold_id"])["status"] == "expired"
    # 过期 hold 的 reserved 已归还；trigger 自身的新预占仍在（est）
    assert int(_allowance_row(user["user_id"])["reserved_nano_cny"]) == est
    # 迟到的合法 usage（expired hold）：真实成本仍记 spent
    event2 = _usage_event(body2["call_id"], body2["session_id"], "user",
                          user["user_id"], occurred=later,
                          user_id=user["user_id"])
    out2 = _settle(hold2["hold_id"], {"usage_event": event2}, now=later)
    assert out2["status"] == "settled"
    assert int(_allowance_row(
        user["user_id"])["spent_nano_cny"]) == actual + out2["actual_nano_cny"]


def test_lower_limit_than_spent_denies_with_overage_fail_closed():
    """X < 已用：remaining=0、overage 明示；后续授权全部拒绝且不改数。"""
    _seed_all()
    _set_mode("registered")
    user = _user("overage@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 12, source="invite")
        conn.commit()
    finally:
        conn.close()
    est = _expected_estimate(datetime.now(UTC))
    out, _event = _settle_allowance_flow(user, est)
    actual = out["actual_nano_cny"]
    row = _allowance_row(user["user_id"])
    ver = int(row["version"])
    # X 压到低于已用
    lowered = spend_store.set_user_total_limit(
        user["user_id"], actual // 2, ver, actor_user_id="pytest")
    assert lowered["limit_nano_cny"] == actual // 2
    assert int(lowered["spent_nano_cny"]) == actual   # spent 不动
    summary = spend_store.admin_users_spend_summaries(
        [("user", user["user_id"])])[user["user_id"]]
    assert summary["total"]["remaining_nano"] == 0
    assert summary["total"]["overage_nano"] == actual - actual // 2
    # 新调用 fail-closed：稳定拒绝、hold 不落行、数字不变
    body = _bound_user_hold(user)
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(body)
    assert _hold_row(call_id=body["call_id"]) is None
    row = _allowance_row(user["user_id"])
    assert int(row["spent_nano_cny"]) == actual
    assert int(row["reserved_nano_cny"]) == 0


def test_allowance_not_rebuilt_or_reset_across_month():
    """跨月：allowance 不重建、不归零（无窗口语义；手动推进 at 验证）。"""
    _seed_all()
    user = _user("nomonth@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            allowance = spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 42 * 10 ** 9, source="invite")
        conn.commit()
    finally:
        conn.close()
    spend_store.total_allowance_reserve(allowance["allowance_id"], 5 * 10 ** 9)
    before = _allowance_row(user["user_id"])
    # 推进到下月（get_or_create_window 对 user 不再被调用；直接验证无新窗）
    next_month = datetime.now(UTC) + timedelta(days=45)
    spend_store.get_or_create_window("demo", "cap_x", at=next_month)
    after = _allowance_row(user["user_id"])
    assert after["allowance_id"] == before["allowance_id"]
    assert int(after["limit_nano_cny"]) == 42 * 10 ** 9
    assert int(after["reserved_nano_cny"]) == 5 * 10 ** 9
    assert int(after["version"]) == int(before["version"])  # 未被任何轮换触碰
    assert _count("ai_spend_windows",
                  "subject_type='user' AND subject_id=%s",
                  (user["user_id"],)) == 0  # user 无窗口行


# =========================================================================== #
# 4. denial events（savepoint 回滚后提交 + 去重）
# =========================================================================== #
def test_denial_events_committed_after_savepoint_and_deduped():
    _seed_all()
    _set_mode("registered")
    user = _user("denial@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 1000, source="invite")  # 1000 nano
        conn.commit()
    finally:
        conn.close()
    est = _expected_estimate(datetime.now(UTC))
    body = _bound_user_hold(user)
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(body)
    # hold 不落行；denial event 已提交（外层事务提交而非随 savepoint 回滚）
    assert _hold_row(call_id=body["call_id"]) is None
    events = _sql_one("SELECT * FROM ai_spend_denial_events WHERE call_id=%s",
                      (body["call_id"],))
    assert len(events) == 1
    assert events[0]["reason"] == "spend_budget_exhausted"
    assert events[0]["subject_type"] == "user"
    assert int(events[0]["estimated_nano_cny"]) == est
    # 同 call_id 重试（同 reason）→ 去重仍一条
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(body)
    assert _count("ai_spend_denial_events", "call_id=%s",
                  (body["call_id"],)) == 1
    # pricing_unavailable：estimated NULL
    user2 = _user("denial2@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user2["user_id"], 10 ** 12, source="invite")
        conn.commit()
    finally:
        conn.close()
    body2 = _bound_user_hold(user2, model="deepseek-v4-unknown")
    with pytest.raises(billing_store.HoldPricingUnavailableError):
        _authorize(body2)
    ev2 = _sql_one("SELECT * FROM ai_spend_denial_events WHERE call_id=%s",
                   (body2["call_id"],))
    assert len(ev2) == 1 and ev2[0]["reason"] == "pricing_unavailable"
    assert ev2[0]["estimated_nano_cny"] is None


# =========================================================================== #
# 5. CAS / restore-default / reconcile / summaries / demo stats / settings
# =========================================================================== #
def test_cas_409_semantics():
    _seed_all()
    user = _user("cas@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            allowance = spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 9, source="invite")
        conn.commit()
    finally:
        conn.close()
    # 绝对值 CAS：未命中 409；命中只改 limit、不清 spent/reserved
    spend_store.total_allowance_reserve(allowance["allowance_id"], 10 ** 8)
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.set_user_total_limit(user["user_id"], 2 * 10 ** 9, 1)
    out = spend_store.set_user_total_limit(
        user["user_id"], 2 * 10 ** 9, 2, actor_user_id="pytest")
    assert out["limit_nano_cny"] == 2 * 10 ** 9
    assert int(out["reserved_nano_cny"]) == 10 ** 8
    # set_total_default：CAS 未命中（旧版本重放）409；命中只改面值+version+1
    # （conftest/seed 基线已有 v1 行——「首写 INSERT」分支由缺行用例覆盖）
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.set_total_default(5 * 10 ** 9, 2)
    d1 = spend_store.set_total_default(5 * 10 ** 9, 1, updated_by="pytest")
    assert d1["default_limit_nano_cny"] == 5 * 10 ** 9
    d2 = spend_store.set_total_default(
        6 * 10 ** 9, int(d1["version"]), updated_by="pytest")
    assert int(d2["version"]) == int(d1["version"]) + 1
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.set_total_default(7 * 10 ** 9, int(d1["version"]))
    assert spend_store.get_total_default()[
        "default_limit_nano_cny"] == 6 * 10 ** 9
    # compare_and_set_setting：未命中 409、命中写入（以维护闸键为例——
    # user_spend_target 键已随单轨删除，不再存在可 CAS 的 target）
    with pytest.raises(settings_store.SettingsVersionConflictError):
        settings_store.compare_and_set_setting(
            settings_store.AI_DISPATCH_MAINTENANCE_KEY, True, False)
    settings_store.compare_and_set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, False, True,
        updated_by="pytest")
    assert settings_store.get_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY) is True


def test_restore_default_keeps_spent_and_reserved():
    _seed_all()
    user = _user("restore@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 11, source="invite")
        conn.commit()
    finally:
        conn.close()
    spend_store.set_total_default(9 * 10 ** 9, 1, updated_by="pytest")
    spend_store.total_allowance_reserve(
        _allowance_row(user["user_id"])["allowance_id"], 10 ** 8)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.total_allowance_add_spent_tx(
                cur, _allowance_row(user["user_id"])["allowance_id"],
                3 * 10 ** 9)
        conn.commit()
    finally:
        conn.close()
    row = _allowance_row(user["user_id"])
    out = spend_store.restore_user_total_default(
        user["user_id"], int(row["version"]), actor_user_id="pytest")
    assert out["limit_nano_cny"] == 9 * 10 ** 9   # 改为当时默认 X
    assert int(out["spent_nano_cny"]) == 3 * 10 ** 9    # 绝不清零
    assert int(out["reserved_nano_cny"]) == 10 ** 8


def test_reconcile_total_allowances_reports_drift_only():
    _seed_all()
    user = _user("recon@x.com")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 12, source="cutover",
                opening_spent_nano=500, cutover_at=datetime.now(UTC),
                source_window_id="spw_src", source_window_version=3)
        conn.commit()
    finally:
        conn.close()
    aid = _allowance_row(user["user_id"])["allowance_id"]
    cutover_at = _sql_one("SELECT cutover_at FROM ai_spend_total_allowances "
                          "WHERE allowance_id=%s", (aid,))[0]["cutover_at"]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            _insert_priced_event(cur, "a" * 32, "user", user["user_id"],
                                 cutover_at + timedelta(minutes=5), 700)
        conn.commit()
    finally:
        conn.close()
    result = spend_store.reconcile_total_allowances()
    item = next(i for i in result["items"] if i["subject_id"] ==
                user["user_id"])
    assert item["expected_spent_nano"] == 500 + 700  # opening + cutover 后
    assert item["actual_spent_nano"] == 500
    assert item["matches"] is False
    assert result["drift_allowances"] >= 1
    # 修正投影后 → 无 drift（只报告不修账的补集验证）
    _exec("UPDATE ai_spend_total_allowances SET spent_nano_cny=1200 "
          "WHERE allowance_id=%s", (aid,))
    item = next(i for i in spend_store.reconcile_total_allowances()["items"]
                if i["subject_id"] == user["user_id"])
    assert item["matches"] is True


def test_admin_summaries_single_track_forms():
    """§4.3 互斥形态（R3 Wave1-Money 单轨，纯角色驱动）：

    - user → **恒** total 形态；缺行 → 稳定
      ``error=spend_total_allowance_missing``（数据损坏如实上报）；
    - owner → 恒 window 形态（每月窗口语义不变）；
    - total/window 两形态键互斥；``spend_target`` 键为纯展示标注。"""
    _seed_all()
    user = _user("summary@x.com")       # 有 allowance 行
    ghost = _user("ghost@x.com")        # 无 allowance 行（数据损坏形态）
    owner = _user("summary-owner@x.com")
    _exec("UPDATE users SET role='owner' WHERE user_id=%s",
          (owner["user_id"],))
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, user["user_id"], 10 ** 10, source="invite")
        conn.commit()
    finally:
        conn.close()
    summaries = spend_store.admin_users_spend_summaries(
        [("user", user["user_id"]), ("user", ghost["user_id"]),
         ("owner", owner["user_id"])])
    total = summaries[user["user_id"]]
    assert "total" in total and "window" not in total  # 互斥
    assert total["spend_target"] == "total_allowance"  # 纯展示标注
    assert total["total"]["total_limit_nano_cny"] == 10 ** 10
    assert total["total"]["remaining_nano"] == 10 ** 10
    assert total["total"]["overage_nano"] == 0
    assert total["total"]["source"] == "invite"
    assert summaries[ghost["user_id"]].get("error") == \
        "spend_total_allowance_missing"
    assert "total" not in summaries[ghost["user_id"]]
    assert "window" not in summaries[ghost["user_id"]]
    win = summaries[owner["user_id"]]
    assert "window" in win and "total" not in win  # 互斥
    assert win["spend_target"] == "window"
    # 重复解析稳定（无窗口翻转分支——即使策略后来被禁用，user 形态不变）
    _exec("UPDATE ai_spend_policies SET enabled=false "
          "WHERE scope_type='user_default'")
    again = spend_store.admin_users_spend_summaries(
        [("user", user["user_id"]), ("user", ghost["user_id"])])
    assert "total" in again[user["user_id"]]
    assert again[ghost["user_id"]].get("error") == \
        "spend_total_allowance_missing"


def test_admin_demo_spend_stats_readonly_aggregates():
    _seed_all()
    at = datetime.now(UTC)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            spend_store.get_or_create_window("demo", "demo_global", at=at)
            _insert_priced_event(cur, "b" * 32, "demo", "demo_cap_a",
                                 at - timedelta(minutes=10), 1234)
            _insert_priced_event(cur, "c" * 32, "demo", "demo_cap_b",
                                 at - timedelta(minutes=5), 0)
            cur.execute(
                "INSERT INTO ai_spend_denial_events (denial_id, call_id, "
                "subject_type, subject_id, reason, estimated_nano_cny, "
                "occurred_at) VALUES ('den_t1', %s, 'demo', 'demo_cap_a', "
                "'spend_budget_exhausted', 999, %s)",
                ("call_" + uuid.uuid4().hex, at - timedelta(minutes=8)))
        conn.commit()
    finally:
        conn.close()
    before = {t: _count(t) for t in
              ("ai_usage_events", "billing_holds", "ai_spend_windows",
               "ai_spend_total_allowances", "ai_spend_denial_events",
               "registration_invites", "users")}
    stats = spend_store.admin_demo_spend_stats("current", at=at)
    after = {t: _count(t) for t in before}
    assert before == after  # 只读：接口前后任何业务表行数不变
    assert stats["window"] == "current"
    assert stats["priced_calls"] == 2
    assert stats["charge_nano_cny"] == 1234
    assert stats["holds"]["authorized"] == 0
    assert stats["denials_total"] == 1
    assert stats["denials"][0]["reason"] == "spend_budget_exhausted"
    assert stats["db_unavailable_denials_included"] is False
    assert "数据库整体不可用类拒绝不在 DB 聚合内" in stats["note"]
    # previous：只读既有行（上一周无行 → 虚拟全 0 + 策略面值）
    prev = spend_store.admin_demo_spend_stats("previous", at=at)
    assert prev["window"] == "previous"
    assert prev["spent_nano_cny"] == 0
    assert prev["limit_nano_cny"] > 0
    # 非法 window 拒绝
    with pytest.raises(spend_store.InvalidSpendRequestError):
        spend_store.admin_demo_spend_stats("week13", at=at)


def test_admin_spend_settings_values_split():
    _seed_all()
    values = spend_store.admin_spend_settings_values()
    assert values["demo_weekly_limit_nano_cny"] == 50 * 10 ** 9
    assert values["owner_monthly_limit_nano_cny"] == 1000 * 10 ** 9
    # conftest/seed 基线已物化 defaults 行（0032 同款 20 CNY）→ 权威来源
    assert values["user_default_total_limit_nano_cny"] == 20 * 10 ** 9
    assert values["user_default_total_limit_source"] == "total_defaults"
    # 单轨无回退（wave 2 收口）：defaults 行被手工删除 = 数据损坏，
    # 展示面同源同靶地报「未配置」（全 None），不再回退 user_default 策略面值
    _exec("DELETE FROM ai_spend_total_defaults")
    values = spend_store.admin_spend_settings_values()
    assert values["user_default_total_limit_nano_cny"] is None
    assert values["user_default_total_limit_source"] is None
    assert values["user_default_total_limit_version"] is None
    assert values["user_default_total_policy_id"] is None
    # 首个写入者以 expected_version=1 重建 defaults 行
    spend_store.set_total_default(30 * 10 ** 9, 1, updated_by="pytest")
    values = spend_store.admin_spend_settings_values()
    assert values["user_default_total_limit_nano_cny"] == 30 * 10 ** 9
    assert values["user_default_total_limit_source"] == "total_defaults"


# =========================================================================== #
# 6. cutover 脚本（R3 Wave1-Money 瘦身：preflight / apply，纯迁移）
# =========================================================================== #
def _mk_user_with_window(login, *, limit=None, spent=0, reserved=0):
    """建 user + 当前月窗口（limit 可调）；返回 user dict。

    单轨：建号组合原语同时建 allowance 行（defaults 面值 20 CNY，与
    0023 user_default 窗口快照同值——apply 的 existing-allowance 分支
    限额一致校验通过，回填 opening/spent）。"""
    user = user_store.create_user(login, "pass123456789012")
    win = spend_store.get_or_create_window("user", user["user_id"])
    if limit is not None or spent or reserved:
        _exec("UPDATE ai_spend_windows SET limit_nano_snapshot=%s, "
              "spent_nano_cny=%s, reserved_nano_cny=%s WHERE window_id=%s",
              (limit if limit is not None else win["limit_nano_snapshot"],
               spent, reserved, win["window_id"]))
    return user


def _load_cutover_script():
    """按真实脚本文件加载 cutover 模块（真 PG、真 _die/sys.exit 语义）。"""
    import importlib.util
    script_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts",
        "cutover_user_total_allowances.py")
    spec = importlib.util.spec_from_file_location(
        "cutover_user_total_allowances", script_path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def _seed_priced_usage_with_ledger(user_id, occurred_at, charge_nano, *,
                                   account_id=None):
    """priced usage event + 配套 ``'usage:'||event_id`` usage_debit ledger
    （镜像 ingest 补账：rollback 前置双扣检测要求每个 priced event 恰一条
    usage ledger）。返回 account_id（同 user 多笔复用同一账户）。"""
    hex32 = uuid.uuid4().hex
    if account_id is None:
        account_id = billing_store.create_billing_account(
            user_id, actor="pytest")["account_id"]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            _insert_priced_event(cur, hex32, "user", user_id, occurred_at,
                                 charge_nano)
            cur.execute(
                "INSERT INTO billing_ledger_entries (entry_id, account_id, "
                "event_id, kind, amount_nano_cny, idempotency_key, reason, "
                "metadata) VALUES (%s,%s,'use_'||%s,'usage_debit',%s,"
                "'usage:'||'use_'||%s,'pytest-synthetic','{}'::jsonb)",
                ("ble_" + uuid.uuid4().hex[:20], account_id, hex32,
                 -int(charge_nano), hex32))
        conn.commit()
    finally:
        conn.close()
    return account_id


def test_cutover_preflight_and_apply():
    script = _load_cutover_script()
    _seed_all()
    u1 = _mk_user_with_window("cut1@x.com", limit=20 * 10 ** 9)
    u2 = _mk_user_with_window("cut2@x.com", limit=20 * 10 ** 9)
    actor = "usr_owner_cutover"

    # preflight：干净 → 通过
    conn = script._connect()
    try:
        report = script.preflight(conn, actor=actor)
    finally:
        conn.close()
    assert report["ok"] is True
    assert len(report["per_user"]) == 2

    # 给 u1 造「有真实依据」的消费（priced event + 配套 usage ledger，窗口
    # spent 同步刷新 → drift 恒 0；open hold 用于验证中止路径）
    now = datetime.now(UTC)
    _seed_priced_usage_with_ledger(u1["user_id"], now, 3 * 10 ** 9)
    _exec("UPDATE ai_spend_windows SET spent_nano_cny=%s "
          "WHERE subject_id=%s", (3 * 10 ** 9, u1["user_id"]))

    # 中止路径：open hold → apply 中止并保持维护态
    body = _bound_user_hold(u1)
    _authorize(body)  # shadow：照常落行 + 窗口预占
    assert _hold_row(call_id=body["call_id"])["status"] == "open"
    conn = script._connect()
    try:
        with pytest.raises(SystemExit):
            script.apply_cutover(conn, actor=actor)
    finally:
        conn.close()
    assert settings_store.get_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY) is True  # 保持维护态
    # user_spend_target 键已随 0032 删除（无 flag 可切；中止只保持维护态）
    assert settings_store.get_setting("user_spend_target") is None
    # 清场：把 hold 置 released 并归还窗口预占（模拟在途结束）；运维显式
    # CAS 关闸后重跑 apply（apply 严格要求 false→true）
    _exec("UPDATE billing_holds SET status='released' WHERE call_id=%s",
          (body["call_id"],))
    # 单轨：user hold 的预占在 allowance 行上（按 hold 目标快照归还）
    _exec("UPDATE ai_spend_total_allowances a SET reserved_nano_cny=0 "
          "FROM billing_holds h WHERE h.call_id=%s AND "
          "h.spend_total_allowance_id=a.allowance_id", (body["call_id"],))
    settings_store.compare_and_set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, True, False,
        updated_by="pytest")

    # apply：成功切换（remaining 逐 user 守恒）
    conn = script._connect()
    try:
        report = script.apply_cutover(conn, actor=actor)
    finally:
        conn.close()
    assert settings_store.get_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY) is False  # 自动关闸
    assert spend_store.get_total_default()[
        "default_limit_nano_cny"] == 20 * 10 ** 9  # user_default 面值固化
    for uid, before_rem in ((u1["user_id"], 20 * 10 ** 9 - 3 * 10 ** 9),
                            (u2["user_id"], 20 * 10 ** 9)):
        row = _allowance_row(uid)
        assert row is not None
        assert row["source"] == "cutover"
        assert row["source_window_id"] is not None
        assert int(row["opening_spent_nano_cny"]) == \
            int(row["spent_nano_cny"])
        remaining = (int(row["limit_nano_cny"]) - int(row["spent_nano_cny"])
                     - int(row["reserved_nano_cny"]))
        assert remaining == before_rem  # nano-CNY 精确守恒
    per = {p["user_id"]: p for p in report["per_user"]}
    assert per[u1["user_id"]]["remaining_before_nano"] == \
        per[u1["user_id"]]["remaining_after_nano"]
    # 旧窗口冻结不删
    assert _count("ai_spend_windows", "subject_id=%s",
                  (u1["user_id"],)) == 1




def test_preflight_flags_maintenance_active(capsys):
    """R2-F3：维护闸已开（ai_dispatch_maintenance=true）→ apply 的闸 CAS
    false→true 必然失败，preflight 报 maintenance_active 硬失败。"""
    script = _load_cutover_script()
    _seed_all()
    _mk_user_with_window("pf-maint@x.com", limit=20 * 10 ** 9)
    settings_store.compare_and_set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, False, True,
        updated_by="pytest")
    conn = script._connect()
    try:
        with pytest.raises(SystemExit) as exc_info:
            script.preflight(conn, actor="usr_owner_pf")
    finally:
        conn.close()
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "preflight 通过" not in out
    report = json.loads(out)
    assert report["ok"] is False
    assert report["problems"] == [{"problem": "maintenance_active",
                                   "current": True}]
    assert report["ai_dispatch_maintenance"] is True


def test_preflight_flags_total_default_unresolvable(capsys):
    """R2-F3：默认总额度不可解析（单轨后 = defaults 表缺行；策略无关——
    兼容回退已删）→ apply 会 _die「无可用默认总额度」，preflight 报
    total_default_unresolvable 硬失败。"""
    script = _load_cutover_script()
    _seed_all()
    _mk_user_with_window("pf-nodefault@x.com", limit=20 * 10 ** 9)
    _exec("DELETE FROM ai_spend_total_defaults")
    conn = script._connect()
    try:
        with pytest.raises(SystemExit) as exc_info:
            script.preflight(conn, actor="usr_owner_pf")
    finally:
        conn.close()
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "preflight 通过" not in out
    report = json.loads(out)
    assert report["ok"] is False
    assert report["problems"] == [{"problem": "total_default_unresolvable"}]
    assert report["total_default_nano_cny"] is None
    assert report["total_default_source"] is None
    assert report["total_default_version"] is None


def test_preflight_flags_allowance_window_limit_mismatch(capsys):
    """F5b：已有 allowance 行限额与当前窗口快照不一致 → preflight 即报
    allowance_limit_mismatch 并非零退出（不等到 apply 在维护窗内才中止）。"""
    script = _load_cutover_script()
    _seed_all()
    user = _mk_user_with_window("pf-mismatch@x.com", limit=20 * 10 ** 9)
    # 单轨：建号已自动建 allowance（20 CNY）——CAS 提额到 25 CNY 构造不一致
    spend_store.set_user_total_limit(user["user_id"], 25 * 10 ** 9, 1,
                                     actor_user_id="pytest")
    conn = script._connect()
    try:
        with pytest.raises(SystemExit) as exc_info:
            script.preflight(conn, actor="usr_owner_pf")
    finally:
        conn.close()
    assert exc_info.value.code != 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert {"problem": "allowance_limit_mismatch",
            "user_id": user["user_id"],
            "allowance_limit_nano": 25 * 10 ** 9,
            "window_limit_nano": 20 * 10 ** 9} in report["problems"]
    per = next(p for p in report["per_user"]
               if p["user_id"] == user["user_id"])
    assert per["existing_allowance_limit_nano_cny"] == 25 * 10 ** 9


def test_cutover_materializes_windowless_user_preflight_and_apply():
    """R2-F1 owner 方向 #3：window 模式建号无 X 的 user 无任何 spend 行
    （无窗口/allowance/override）——不再被 current_window_missing 阻断：
    preflight ok 且标 materialize；apply 在迁移事务内先按有效策略物化
    零消费窗口（写审计）再走正常迁移：allowance limit=策略面值、
    opening=spent=0、reconcile_total_allowances 无 drift。"""
    script = _load_cutover_script()
    _seed_all()  # 维护闸关、user_default 20 CNY 生效、defaults 已物化
    # 单轨：建号不传 X 走 defaults 解析恒建 allowance（20 CNY），但无窗口行
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "mat@x.com", "pass123456789012", actor_user_id="usr_owner_mat")
    assert allowance is not None
    assert int(allowance["limit_nano_cny"]) == 20 * 10 ** 9
    uid = user["user_id"]
    assert _count("ai_spend_windows", "subject_id=%s", (uid,)) == 0
    assert _count("ai_spend_policies", "scope_type='user_override'") == 0

    # preflight：ok=True、windows_to_materialize=1、条目标 materialize
    conn = script._connect()
    try:
        report = script.preflight(conn, actor="usr_owner_mat")
    finally:
        conn.close()
    assert report["ok"] is True
    assert report["windows_to_materialize"] == 1
    assert report["problems"] == []
    per = next(p for p in report["per_user"] if p["user_id"] == uid)
    assert per["window_materialize_required"] is True
    assert per["window_id"] is None
    assert per["remaining_nano"] is None

    # apply：物化零消费窗 → 正常迁移（remaining 守恒）
    conn = script._connect()
    try:
        report = script.apply_cutover(conn, actor="usr_owner_mat")
    finally:
        conn.close()
    assert settings_store.get_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY) is False
    assert report["windows_materialized"] == [uid]
    row = _allowance_row(uid)
    assert row is not None
    assert int(row["limit_nano_cny"]) == 20 * 10 ** 9  # user_default 面值
    assert int(row["opening_spent_nano_cny"]) == 0
    assert int(row["spent_nano_cny"]) == 0
    assert int(row["reserved_nano_cny"]) == 0
    assert row["source"] == "cutover"
    # 物化出的窗口行：恰好一个、零消费、快照=策略面值
    wins = _sql_one(
        "SELECT * FROM ai_spend_windows WHERE subject_type='user' "
        "AND subject_id=%s", (uid,))
    assert len(wins) == 1
    assert wins[0]["status"] == "open"
    assert int(wins[0]["spent_nano_cny"]) == 0
    assert int(wins[0]["reserved_nano_cny"]) == 0
    assert int(wins[0]["limit_nano_snapshot"]) == 20 * 10 ** 9
    # 审计留痕：spend.cutover_window_materialize（窗口字段齐备）
    audits = _sql_one(
        "SELECT detail FROM audit_events WHERE action="
        "'spend.cutover_window_materialize' AND target_id=%s",
        (wins[0]["window_id"],))
    assert audits
    detail = audits[0]["detail"]
    assert detail["user_id"] == uid
    assert detail["window_id"] == wins[0]["window_id"]
    assert detail["policy_id"] == "spp_user_default"
    assert int(detail["policy_version"]) == int(wins[0]["policy_version"])
    assert int(detail["limit_nano_snapshot"]) == 20 * 10 ** 9
    # reconcile 无 drift（opening=spent=0 且 cutover 后无 usage）
    recon = spend_store.reconcile_total_allowances()
    assert [i for i in recon["items"] if not i["matches"]] == []


def test_cutover_preflight_flags_window_materialize_no_policy(capsys):
    """R2-F1 物化前置：无当前窗口且无 override、user_default 也禁用 →
    物化不了零消费窗，preflight 报 window_materialize_no_policy 硬失败
    （apply 的对应 _die 分支兜底，不在此重复验证）。"""
    script = _load_cutover_script()
    _seed_all()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "matnp@x.com", "pass123456789012")
    assert allowance is not None  # 单轨：defaults 解析恒建行
    _exec("UPDATE ai_spend_policies SET enabled=false "
          "WHERE scope_type='user_default'")
    conn = script._connect()
    try:
        with pytest.raises(SystemExit) as exc_info:
            script.preflight(conn, actor="usr_owner_mat")
    finally:
        conn.close()
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "preflight 通过" not in out
    report = json.loads(out)
    assert report["ok"] is False
    assert report["windows_to_materialize"] == 1
    assert {"problem": "window_materialize_no_policy",
            "user_id": user["user_id"]} in report["problems"]



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
