# -*- coding: utf-8 -*-
"""billing/usage-ingest 测试共用基建（PR2）。

- ``load_event``：读 tests/fixtures/usage_events/ 样例（深拷贝，用例可改写）；
- ``seed_price_books``：幂等重放 migrations/0018_billing.sql——迁移文件是
  DeepSeek 2026-08-28 价格种子的唯一权威来源（conftest 每用例 TRUNCATE 会
  清掉迁移期种子，需要种子的用例显式调用本函数重建）；
- ``bind_reservation`` / ``bind_demo_session``：直接写权威绑定行
  （ai_budget_reservations / demo_sessions），构造 §7.2 四步解析的前置态。

仅 PG 后端可用（RUN_PG_TESTS=1；json 模式下调用方自行 skip）。
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
USAGE_DIR = REPO_ROOT / "tests" / "fixtures" / "usage_events"
BILLING_DIR = REPO_ROOT / "tests" / "fixtures" / "billing"
_MIGRATION_0018 = REPO_ROOT / "migrations" / "0018_billing.sql"


def load_event(name):
    """读样例事件（深拷贝 dict，用例可安全改写）。"""
    return json.loads((USAGE_DIR / name).read_text(encoding="utf-8"))


def load_price_snapshot():
    return json.loads(
        (BILLING_DIR / "deepseek_price_snapshot_2026-08-28.json")
        .read_text(encoding="utf-8"))


def load_time_band_cases():
    return json.loads(
        (BILLING_DIR / "time_band_cases.json").read_text(encoding="utf-8"))


def connect():
    """dict_row PG 连接（与 store 模块同风格）。"""
    import psycopg
    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def seed_price_books(conn=None):
    """幂等重放 0018 迁移（IF NOT EXISTS/ON CONFLICT DO NOTHING）→ 重建
    2026-08-28 DeepSeek 种子价格书（provider_cost + customer_charge）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_0018.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        if own:
            conn.close()


#: 种子书起点（快照日 Asia/Shanghai 00:00 = UTC 前一日 16:00）
SEED_EFFECTIVE_FROM = "2026-08-27T16:00:00+00:00"


def seed_early_price_books():
    """补一对覆盖 [1990-01-01, 种子起点) 的 active 书（与种子区间不相交）。

    种子书 effective_from 固定为 2026-08-27T16:00Z；测试里平移到
    ``now - 1h`` 的 occurred_at 在时钟贴近该日期时会落进种子起点之前，
    找不到价格 → no_active_price_book（非确定）。本对书与种子区间半开
    不相交（可无 supersede 直接激活），保证任何过去时刻都有价可查；
    值与种子一致（夹具 nano 值），不影响金额断言（金额断言用注入 now
    的确定性用例，此处只为 priced 状态稳定）。
    """
    import billing_store
    from datetime import datetime
    snap = load_price_snapshot()
    rates = [{
        "provider": "deepseek", "model": model, "time_band": band,
        "cache_hit_nano_per_million": values["cache_hit_nano_per_million"],
        "cache_miss_nano_per_million": values["cache_miss_nano_per_million"],
        "output_nano_per_million": values["output_nano_per_million"],
    } for model, bands in snap["models"].items()
        for band, values in bands.items()]
    eff_to = datetime.fromisoformat(SEED_EFFECTIVE_FROM)
    for kind in ("provider_cost", "customer_charge"):
        book = billing_store.create_price_book(
            kind, rates, datetime(1990, 1, 1, tzinfo=timezone.utc),
            eff_to, source_url="test-early-coverage", created_by="pytest",
            price_book_id="pb_test_early_%s" % kind)
        activated = billing_store.activate_price_book(
            book["price_book_id"], actor="pytest")
        assert activated["status"] == "active"


def seed_price_books_with_history():
    """种子书 + 早期覆盖书（端到端测试默认：任何过去 occurred_at 均可计价）。"""
    seed_price_books()
    seed_early_price_books()


def bind_reservation(request_id, session_id, subject_type, subject_id,
                     state="consumed", credential_source="platform", conn=None):
    """直接插一条 ai_budget_reservations（consumed 默认带 histopilot_session_id，
    复现 app.py _ai_budget_lifecycle.on_accepted → budget_store.consume 的终态）。"""
    import pg_store
    import budget_store
    own = conn is None
    if own:
        conn = connect()
    try:
        with pg_store.transaction(conn) as c:
            period = budget_store.get_or_create_current_period(c)
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_budget_reservations "
                    "(request_id, period_id, subject_type, subject_id, "
                    " credential_source, state, reserved_at, "
                    " reservation_expires_at, histopilot_session_id, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s, now(), now() + interval '1 hour', "
                    " %s, now()) ON CONFLICT (request_id) DO UPDATE SET "
                    "state=EXCLUDED.state, "
                    "histopilot_session_id=EXCLUDED.histopilot_session_id, "
                    "updated_at=now()",
                    (request_id, period["id"], subject_type, subject_id,
                     credential_source, state,
                     session_id if state == "consumed" else None))
    finally:
        if own:
            conn.close()


def bind_demo_session(histopilot_session_id, capability_id=None,
                      run_state="consumed", conn=None):
    """直接插一条 demo_sessions（histopilot_session_id 绑定，恢复 demo 主体）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        cap = capability_id or ("demo_cap_" + uuid.uuid4().hex[:20])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO demo_sessions "
                "(id, token_hash, created_at, expires_at, run_state, "
                " consumed_at, histopilot_session_id) "
                "VALUES (%s,%s, now(), now() + interval '1 day', %s, now(), %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "histopilot_session_id=EXCLUDED.histopilot_session_id, "
                "run_state=EXCLUDED.run_state",
                (cap, "tok_" + uuid.uuid4().hex, run_state,
                 histopilot_session_id))
        conn.commit()
        return cap
    finally:
        if own:
            conn.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
