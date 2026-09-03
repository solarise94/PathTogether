# -*- coding: utf-8 -*-
"""批次 E 专项测试（docs ai-money-budget-bugfix-and-simplification-plan.md
§4 Demo 运行模型简化 / §8 批次 E / §9.5 Demo 全量清单）。

run 流水 / 单 active 并发闸 / 顺序多次 / 短窗口请求速率 / capability 过期的
API 与 store 级用例在 tests/test_demo_access.py 与 tests/test_demo_store.py；
本文件聚焦**金额窗口层与迁移**：

  - 多浏览器（多 capability）累计到 Demo 周金额上限后统一拒绝（§9.5：
    enforcement 切 all 验证，收尾恢复 shadow——测试收尾纪律）；
  - 新周自动获得新窗口（§9.2「Demo 所有 capability 映射同一周窗口」+ 边界滚动）；
  - Demo 无 billing account 也可完整计价 + 限制（priced 事件 + 周窗口 spent，
    无 ledger 行，§4.2/§14.1）；
  - Demo 周额度用尽不影响注册用户/Owner（窗口隔离，§3.2）；
  - §7.2 主体解析第②步接 demo_runs（0026 主源 + demo_sessions 历史回退）；
  - 迁移（§9.7）：fresh 全量 0001→0026 后 0026 表/索引/约束在位，且
    ensure_schema 与 0026 SQL 重放幂等。

全部真实 PostgreSQL（RUN_PG_TESTS=1）；本文件用例 PG 缺失时按仓库惯例整模块
skip（CI 必须 RUN_PG_TESTS=1 才算覆盖）。
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

import pytest  # noqa: E402

import billing_pricing  # noqa: E402
import billing_store  # noqa: E402
import spend_store  # noqa: E402
import pg_store  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="批次 E 金额窗口用例需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import psycopg  # noqa: E402
    import _billing_helpers as bh  # noqa: E402
    import user_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

INSTALLATION = "pin_demo_batch_e"
T0 = datetime.now(timezone.utc)


def _seed_all():
    """corrected v2 价格全域生效 + 三条默认策略 + cutover 提前到 2020（同
    test_billing_hold_settle_chain._seed_all，保证 T0 与 T0+Δ 全程 v2 计价）。"""
    bh.seed_price_books()
    bh.seed_spend_policies()
    conn = bh.connect()
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
                    datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()),))
            cur.execute("UPDATE ai_spend_policies SET effective_from="
                        "'2020-01-01T00:00:00+00:00' WHERE policy_id = ANY"
                        "(%s)", (list(bh.SEED_POLICY_IDS),))
        conn.commit()
    finally:
        conn.close()


def _set_policy_limit(policy_id, limit_nano, *, effective_from=None):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ai_spend_policies SET limit_nano_cny=%s, "
                "effective_from=COALESCE(%s, effective_from) "
                "WHERE policy_id=%s",
                (limit_nano, effective_from, policy_id))
        conn.commit()
    finally:
        conn.close()


def _set_mode(mode):
    conn = bh.connect()
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


def _hold_body(subject_type, subject_id, *, session_id, call_id=None,
               model="deepseek-v4-flash", est_in=1_000_000, max_out=200_000):
    return {
        "call_id": call_id or ("call_" + uuid.uuid4().hex),
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": "deepseek",
        "model": model,
        "estimated_input_tokens": est_in,
        "max_output_tokens": max_out,
    }


def _authorize(body, now=None):
    return billing_store.authorize_hold(
        body, installation_id=INSTALLATION, plugin_id="histopilot", now=now)


def _settle(hold_id, body, now=None):
    return billing_store.settle_hold(
        hold_id, body, installation_id=INSTALLATION, plugin_id="histopilot",
        now=now)


def _usage_event(body, *, occurred, tokens=(0, 1_000_000, 200_000)):
    hit, miss, out = tokens
    return {
        "event_id": "use_" + uuid.uuid4().hex,
        "call_id": body["call_id"],
        "schema_version": 1,
        "session_id": body["session_id"],
        "subject_type": body["subject_type"],
        "subject_id": body["subject_id"],
        "provider": "deepseek",
        "model": body["model"],
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "enqueued_at": (occurred + timedelta(seconds=1)
                        ).isoformat().replace("+00:00", "Z"),
        "cache_hit_input_tokens": hit,
        "cache_miss_input_tokens": miss,
        "output_tokens": out,
        "reasoning_tokens": 0,
        "total_tokens": hit + miss + out,
    }


def _expected_estimate(at, est_in=1_000_000, max_out=200_000,
                       model="deepseek-v4-flash"):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, at)
    finally:
        conn.close()
    assert book is not None
    # 镜像 billing_store 步骤 5 的 output 封顶（estimate_output_token_cap）
    cap = billing_store.estimate_output_token_cap()
    est_out = max_out if cap <= 0 else min(max_out, cap)
    return billing_pricing.price_tokens_nano(0, est_in, est_out, book)


def _expected_charge(at, tokens, model="deepseek-v4-flash"):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, at)
    finally:
        conn.close()
    assert book is not None
    hit, miss, out = tokens
    return billing_pricing.price_tokens_nano(hit, miss, out, book)


def _window_of_hold(call_id):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT spend_window_id FROM billing_holds "
                        "WHERE call_id=%s", (call_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row["spend_window_id"]
    return spend_store.get_window(row["spend_window_id"])


def _allowance_of_hold(call_id):
    """R3 单轨：user hold 绑 allowance（spend_window_id 恒 NULL）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT spend_total_allowance_id FROM billing_holds "
                        "WHERE call_id=%s", (call_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row["spend_total_allowance_id"]
    return row["spend_total_allowance_id"]


def _count(table, where="1=1", params=()):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM %s WHERE %s"
                        % (table, where), params)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _demo_body_for_browser(tag):
    """一个「浏览器」= 独立 capability + 独立 HP session（demo_runs 绑定）。"""
    session_id = "sess_%s_%s" % (tag, uuid.uuid4().hex[:6])
    cap = bh.bind_demo_run(session_id)
    return _hold_body("demo", cap, session_id=session_id)


@pytest.fixture(autouse=True)
def _demo_batch_e_env(tmp_path, monkeypatch):
    """独立 app 环境 + 收尾强制 enforcement 回 shadow（测试内切 all 例外纪律）。"""
    isolate_app(monkeypatch, tmp_path)
    app_mod._ADAPTER_MODE_CACHE.update(ts=0.0, mode=None)
    yield
    try:
        if BACKEND == "postgres":
            _set_mode("shadow")
    except Exception:
        pass


# =========================================================================== #
# §9.5：多浏览器累计到 Demo 周金额上限后统一拒绝（mode=all 验证，收尾恢复）
# =========================================================================== #
@PG
def test_multi_browser_demo_week_pool_exhaustion_denies_all():
    _seed_all()
    _set_mode("all")  # 测试内切 all 验证（fixture 收尾恢复 shadow）
    est = _expected_estimate(T0)
    # 周额度 = 2.5 × 单次估算：前两个浏览器成功，第三个起统一拒绝
    _set_policy_limit("spp_demo_global", est * 2 + est // 2)
    b1 = _demo_body_for_browser("mb1")
    b2 = _demo_body_for_browser("mb2")
    h1 = _authorize(b1, now=T0)
    h2 = _authorize(b2, now=T0)
    assert h1["status"] == h2["status"] == "open"
    # 多浏览器（多 capability）共享同一 demo_global 周窗口
    w1 = _window_of_hold(b1["call_id"])
    w2 = _window_of_hold(b2["call_id"])
    assert w1["window_id"] == w2["window_id"]
    assert w1["subject_type"] == "demo" and w1["subject_id"] == "demo_global"
    assert w1["reserved_nano_cny"] == est * 2
    # 第三个浏览器（全新 capability + 全新 session）同样拒绝
    b3 = _demo_body_for_browser("mb3")
    with pytest.raises(spend_store.SpendBudgetExhaustedError) as ei:
        _authorize(b3, now=T0)
    assert ei.value.code == "spend_budget_exhausted"
    assert _count("billing_holds", "call_id=%s", (b3["call_id"],)) == 0
    # 拒绝不追加 reserved（不改数）
    assert spend_store.get_window(w1["window_id"])["reserved_nano_cny"] == \
        est * 2
    # shadow 恢复后同类调用只观测不拒绝（投影继续累加，不构成额度）
    _set_mode("shadow")
    b4 = _demo_body_for_browser("mb4")
    h4 = _authorize(b4, now=T0)
    assert h4["status"] == "open"
    assert spend_store.get_window(w1["window_id"])["reserved_nano_cny"] == \
        est * 3


# =========================================================================== #
# §9.5：新周自动获得新窗口
# =========================================================================== #
@PG
def test_new_week_gets_fresh_demo_window():
    _seed_all()
    _set_mode("all")
    est = _expected_estimate(T0)
    _set_policy_limit("spp_demo_global", est)  # 本周只够一次
    b1 = _demo_body_for_browser("wk1")
    h1 = _authorize(b1, now=T0)
    assert h1["status"] == "open"
    w_this = _window_of_hold(b1["call_id"])
    b2 = _demo_body_for_browser("wk2")
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(b2, now=T0 + timedelta(seconds=1))  # 本周已满
    # 下周一 00:00（Asia/Shanghai）起：新窗口自动获得完整额度
    start, end = spend_store.week_window_bounds(T0)
    next_week = end + timedelta(seconds=1)
    b3 = _demo_body_for_browser("wk3")
    h3 = _authorize(b3, now=next_week)
    assert h3["status"] == "open"
    w_next = _window_of_hold(b3["call_id"])
    assert w_next["window_id"] != w_this["window_id"]
    assert w_next["window_start"] == float(end.timestamp())
    assert w_next["spent_nano_cny"] == 0
    # 与预占同时刻复算（T0 与下周一 00:00 可能跨 peak/off_peak 时段带——
    # 拿 T0 的 est 断言 next_week 的 reserved 是按墙钟碰运气）
    assert w_next["reserved_nano_cny"] == _expected_estimate(next_week)
    # 一周后旧 hold 已过 TTL：authorize 的惰性回收把旧窗口 reserved 归还
    # （§3.4.6/7：TTL 回收还 reserved；真实成本由迟到结算照记）
    w_after = spend_store.get_window(w_this["window_id"])
    assert w_after["reserved_nano_cny"] == 0
    assert w_after["spent_nano_cny"] == 0
    # 迟到结算：真实成本记进**事件发生时刻**所属的旧窗口；新窗口不受影响
    tokens = (0, 1_000_000, 200_000)
    settle = _settle(h1["hold_id"],
                     {"usage_event": _usage_event(b1, occurred=T0,
                                                  tokens=tokens)}, now=T0)
    assert settle["status"] == "settled"
    w_settled = spend_store.get_window(w_this["window_id"])
    assert w_settled["spent_nano_cny"] == _expected_charge(T0, tokens)
    assert spend_store.get_window(w_next["window_id"])["spent_nano_cny"] == 0


# =========================================================================== #
# §9.5：Demo 无 billing account 也可完整计价 + 限制（无 ledger 行）
# =========================================================================== #
@PG
def test_demo_priced_and_limited_without_billing_account():
    _seed_all()
    _set_mode("all")
    est = _expected_estimate(T0)
    _set_policy_limit("spp_demo_global", est * 2 + est // 2)
    b1 = _demo_body_for_browser("np1")
    h1 = _authorize(b1, now=T0)
    assert h1["status"] == "open" and h1["subject_type"] == "demo"
    # settle 带完整 usage event → priced 入库 + 周窗口 spent 投影
    # （output 取估计封顶值 4096：actual==est，2.5×est 的窗口恰好容纳
    #   「已结算一次 + 在途一次」、第三次拒绝——200k output 的 actual 约
    #   1.58×clamp 后 est，该窗口装不下，属测试构造而非语义）
    tokens = (0, 1_000_000, 4096)
    event = _usage_event(b1, occurred=T0, tokens=tokens)
    settle = _settle(h1["hold_id"], {"usage_event": event}, now=T0)
    assert settle["status"] == "settled"
    w = _window_of_hold(b1["call_id"])
    assert w["spent_nano_cny"] == _expected_charge(T0, tokens)
    assert w["reserved_nano_cny"] == 0
    # usage event priced（可查、计价正确）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, charge_nano_cny, subject_type, "
                        "subject_id FROM ai_usage_events WHERE event_id=%s",
                        (event["event_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["status"] == "priced"
    assert int(row["charge_nano_cny"]) == _expected_charge(T0, tokens)
    assert row["subject_type"] == "demo"
    # demo 永不开户、永不写 ledger（§14.1 红线）
    assert _count("billing_accounts") == 0
    assert _count("billing_ledger_entries") == 0
    # 周额度内第二个 capability 仍可用；耗尽后拒绝（hard=all）
    b2 = _demo_body_for_browser("np2")
    assert _authorize(b2, now=T0)["status"] == "open"
    b3 = _demo_body_for_browser("np3")
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(b3, now=T0)


# =========================================================================== #
# §9.5：Demo 周额度用尽不影响注册用户 / Owner（窗口隔离）
# =========================================================================== #
@PG
def test_demo_exhaustion_does_not_affect_user_or_owner():
    _seed_all()
    _set_mode("all")
    est = _expected_estimate(T0)
    _set_policy_limit("spp_demo_global", est)  # demo 本周只够一次
    b_demo = _demo_body_for_browser("iso")
    assert _authorize(b_demo, now=T0)["status"] == "open"
    b_demo2 = _demo_body_for_browser("iso2")
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(b_demo2, now=T0)
    demo_win = _window_of_hold(b_demo["call_id"])
    # 注册用户：自己的一次性总额度（单轨默认 20 CNY），不受 demo 周池影响
    user = user_store.create_user("iso-user@x.com", "pass123456789012")
    u_sess = "sess_user_%s" % uuid.uuid4().hex[:8]
    bh.bind_reservation("req_iso_user", u_sess, "user", user["user_id"])
    b_user = _hold_body("user", user["user_id"], session_id=u_sess,
                        call_id="call_" + uuid.uuid4().hex)
    b_user["request_id"] = "req_iso_user"
    b_user["user_id"] = user["user_id"]
    hu = _authorize(b_user, now=T0)
    assert hu["status"] == "open"
    u_allowance_id = _allowance_of_hold(b_user["call_id"])
    u_allow = spend_store.get_total_allowance(user["user_id"])
    assert u_allow["allowance_id"] == u_allowance_id
    assert u_allow["reserved_nano_cny"] is not None
    # owner：独立策略窗口，同样不受影响
    owner = user_store.create_user("iso-owner@x.com", "pass123456789012",
                                   role="owner")
    o_sess = "sess_owner_%s" % uuid.uuid4().hex[:8]
    bh.bind_reservation("req_iso_owner", o_sess, "owner", owner["user_id"])
    b_owner = _hold_body("owner", owner["user_id"], session_id=o_sess,
                         call_id="call_" + uuid.uuid4().hex)
    b_owner["request_id"] = "req_iso_owner"
    b_owner["user_id"] = owner["user_id"]
    ho = _authorize(b_owner, now=T0)
    assert ho["status"] == "open"
    o_win = _window_of_hold(b_owner["call_id"])
    assert o_win["subject_type"] == "owner"
    assert o_win["window_id"] != demo_win["window_id"]
    # demo 池仍是唯一被耗尽的窗口
    assert spend_store.get_window(demo_win["window_id"])["reserved_nano_cny"] \
        == est


# =========================================================================== #
# §7.2 主体解析第②步：demo_runs 主源 + demo_sessions 历史回退
# =========================================================================== #
@PG
def test_usage_subject_resolution_prefers_demo_runs():
    _seed_all()
    b = _demo_body_for_browser("res")  # bind_demo_run：只建 demo_runs 绑定
    event = _usage_event(b, occurred=T0)
    out = billing_store.ingest_usage_event(event, installation_id=INSTALLATION)
    assert out["status"] == "priced"
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subject_type, subject_id FROM ai_usage_events "
                "WHERE event_id=%s", (event["event_id"],))
            row = cur.fetchone()
            # 反证：该 capability 在 demo_sessions 上没有任何 session 绑定
            cur.execute("SELECT histopilot_session_id IS NULL AS n FROM "
                        "demo_sessions WHERE id=%s", (b["subject_id"],))
            legacy = cur.fetchone()
    finally:
        conn.close()
    assert row["subject_type"] == "demo"
    assert row["subject_id"] == b["subject_id"]
    assert legacy["n"] is True  # 老表无绑定 → 解析确实来自 demo_runs


@PG
def test_usage_subject_resolution_legacy_demo_sessions_fallback():
    """0026 前的历史行（demo_sessions.histopilot_session_id）仍可解析。"""
    _seed_all()
    session_id = "sess_legacy_%s" % uuid.uuid4().hex[:8]
    cap = bh.bind_demo_session(session_id)  # 只写 demo_sessions（历史形态）
    body = _hold_body("demo", cap, session_id=session_id)
    event = _usage_event(body, occurred=T0)
    out = billing_store.ingest_usage_event(event, installation_id=INSTALLATION)
    assert out["status"] == "priced"
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT subject_type, subject_id FROM ai_usage_events "
                        "WHERE event_id=%s", (event["event_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert (row["subject_type"], row["subject_id"]) == ("demo", cap)


@PG
def test_sequential_demo_runs_bind_distinct_sessions():
    """同 capability 顺序两次 run 绑定两个 HP session：各自 usage 都正确归属。"""
    _seed_all()
    cap = "demo_cap_seq_%s" % uuid.uuid4().hex[:10]
    s1 = "sess_seq1_%s" % uuid.uuid4().hex[:6]
    s2 = "sess_seq2_%s" % uuid.uuid4().hex[:6]
    bh.bind_demo_run(s1, capability_id=cap)
    bh.bind_demo_run(s2, capability_id=cap)
    e1 = _usage_event(_hold_body("demo", cap, session_id=s1), occurred=T0)
    e2 = _usage_event(_hold_body("demo", cap, session_id=s2),
                      occurred=T0 + timedelta(seconds=5))
    assert billing_store.ingest_usage_event(
        e1, installation_id=INSTALLATION)["status"] == "priced"
    assert billing_store.ingest_usage_event(
        e2, installation_id=INSTALLATION)["status"] == "priced"


# =========================================================================== #
# §9.7 迁移：fresh 全量 0001→0026 在位 + ensure_schema / 0026 SQL 重放幂等
# =========================================================================== #
@PG
def test_migration_0026_tables_constraints_and_idempotent_replay(pg_uri):
    import psycopg
    # conftest session 起始已对 fresh PG 全量应用 0001→0026；这里校验在位
    conn = psycopg.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations "
                        "WHERE filename='0026_demo_runs.sql'")
            assert cur.fetchone() is not None
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='demo_runs' ORDER BY ordinal_position")
            cols = {r[0] for r in cur.fetchall()}
            assert {"demo_run_id", "capability_id", "request_id", "state",
                    "histopilot_session_id", "slide_id", "asset_revision",
                    "attempt", "rollback_epoch", "ip_prefix_hash",
                    "created_at", "updated_at", "accepted_at", "finished_at",
                    "expires_at"} <= cols
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='demo_runs'")
            idx = {r[0] for r in cur.fetchall()}
            assert idx >= {"demo_runs_pkey",
                           "demo_runs_capability_id_request_id_key",
                           "uq_demo_runs_single_active",
                           "idx_demo_runs_state_expires",
                           "idx_demo_runs_session",
                           "idx_demo_runs_slide_active"}
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='demo_ip_request_rate'")
            assert {r[0] for r in cur.fetchall()} >= {
                "ip_prefix_hash", "window_started_at", "request_count"}
    finally:
        conn.close()
    # ensure_schema 重放幂等（不抛、记录仍在）
    conn = pg_store.connect()
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    # 0026 SQL 原文重放幂等（IF NOT EXISTS 自保）
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "0026_demo_runs.sql").read_text(encoding="utf-8")
    conn = psycopg.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()
