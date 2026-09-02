# -*- coding: utf-8 -*-
"""billing/usage-ingest 测试共用基建（PR2）。

- ``load_event``：读 tests/fixtures/usage_events/ 样例（深拷贝，用例可改写）；
- ``seed_price_books``：幂等重放 migrations/0018_billing.sql +
  migrations/0022_billing_price_unit_fix.sql——迁移文件是 DeepSeek
  2026-08-28 价格种子与批次 A 单位修复（corrected v2 书 + legacy 收口）的
  唯一权威来源（conftest 每用例 TRUNCATE 会清掉迁移期种子，需要种子的
  用例显式调用本函数重建）；
- ``seed_legacy_price_books_only``：只重放 0018（单位修复**前**的错误状态，
  供 cutover/legacy 用例构造历史区间）；
- ``bind_reservation`` / ``bind_run_binding`` / ``bind_demo_session`` /
  ``bind_demo_run``：直接写权威绑定行（ai_budget_reservations ①legacy 回退 /
  ai_run_bindings ①主源（批次 F）/ demo_runs+demo_sessions ②），构造 §7.2
  解析矩阵的前置态。

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
_MIGRATION_0022 = REPO_ROOT / "migrations" / "0022_billing_price_unit_fix.sql"
_MIGRATION_0023 = REPO_ROOT / "migrations" / "0023_spend_policies_windows.sql"
_MIGRATION_0029 = REPO_ROOT / "migrations" / "0029_user_total_allowances_and_denials.sql"

#: 0023 种子策略（占位默认额度：demo 周池 50 CNY / 用户默认月 20 CNY /
#: owner 月 1000 CNY；面值是 owner 待决策的占位默认，后台可改）
SEED_POLICY_IDS = ("spp_demo_global", "spp_user_default", "spp_owner")

#: 0018 种子书（错误 legacy 换算 CNY×1000；批次 A 起在 cutover 收口保留）
LEGACY_BOOK_IDS = (
    "pb_deepseek_provider_cost_20260828",
    "pb_deepseek_customer_charge_20260828",
)
#: 0022 corrected v2 书（正确换算 CNY×1e9；cutover 起生效）
CORRECTED_BOOK_IDS = (
    "pb_deepseek_provider_cost_v2_corrected",
    "pb_deepseek_customer_charge_v2_corrected",
)


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


def _replay(conn, path):
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def seed_price_books(conn=None):
    """幂等重放 0018 + 0022（IF NOT EXISTS/ON CONFLICT/守卫 UPDATE）→ 重建
    「legacy 书（已收口）+ corrected v2 书（当前生效）」的完整价格史。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        _replay(conn, _MIGRATION_0018)
        _replay(conn, _MIGRATION_0022)
    finally:
        if own:
            conn.close()


def seed_legacy_price_books_only(conn=None):
    """只重放 0018：单位修复**前**的错误价格书（active、区间开放）。

    仅用于 cutover/legacy 语义用例（旧事件定价、历史 rate 不被改写）；
    普通用例请用 :func:`seed_price_books`（当前生效价 = corrected v2）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        _replay(conn, _MIGRATION_0018)
    finally:
        if own:
            conn.close()


def seed_spend_policies(conn=None):
    """幂等重放 0023：重建三条默认策略 + spend_enforcement_mode=shadow。

    conftest 每用例 TRUNCATE 会清掉迁移期种子；需要策略解析/窗口的用例显式
    调用本函数（迁移文件是种子与 shadow 开关的唯一权威来源）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        _replay(conn, _MIGRATION_0023)
    finally:
        if own:
            conn.close()


def seed_spend_settings(conn=None):
    """幂等重放 0029 种子（Batch B）：user_spend_target="window" +
    ai_dispatch_maintenance=false（ai_spend_total_allowances/denial_events/
    total_defaults 表结构无种子——面值由 cutover 写入）。

    conftest 每用例 TRUNCATE platform_settings 会清掉这两个键；需要显式
    target/维护闸状态的用例调用本函数（迁移文件是种子的唯一权威来源）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        _replay(conn, _MIGRATION_0029)
    finally:
        if own:
            conn.close()


def set_user_spend_target(target, conn=None):
    """【测试专用】直接写 platform_settings.user_spend_target。

    生产路径只能走 settings_store.compare_and_set_setting（CAS；cutover
    脚本），绝不允许无版本的 last-write-wins——本辅助绕过 CAS 仅用于测试
    固定前置态（conftest TRUNCATE 后 seed 缺失时也用它快速置 target）。
    """
    import psycopg
    import pg_store
    if target not in ("window", "total_allowance"):
        raise ValueError("target 需为 'window'|'total_allowance'")
    own = conn is None
    if own:
        conn = connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO platform_settings (key, value, updated_at, "
                    "updated_by) VALUES ('user_spend_target', %s, now(), "
                    "'pytest') ON CONFLICT (key) DO UPDATE SET "
                    "value=EXCLUDED.value, updated_at=now()",
                    (psycopg.types.json.Jsonb(target),))
    finally:
        if own:
            conn.close()


def pricing_cutover(conn=None):
    """读取 0022 写入的 pricing_v2_cutover_at（datetime，UTC）。

    未迁移（无标志）时返回 None。测试用它取确定性 cutover，再构造
    cutover 前/后的事件时间，避免依赖真实时钟与种子时刻的相对快慢。
    """
    own = conn is None
    if own:
        conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM platform_settings "
                "WHERE key='pricing_v2_cutover_at'")
            row = cur.fetchone()
    finally:
        if own:
            conn.close()
    if row is None:
        return None
    return datetime.fromtimestamp(float(row["value"]), tz=timezone.utc)


#: 种子书起点（快照日 Asia/Shanghai 00:00 = UTC 前一日 16:00）
SEED_EFFECTIVE_FROM = "2026-08-27T16:00:00+00:00"


def seed_early_price_books():
    """补一对覆盖 [1990-01-01, 种子起点) 的 active 书（与种子区间不相交）。

    种子书 effective_from 固定为 2026-08-27T16:00Z；测试里平移到
    ``now - 1h`` 的 occurred_at 在时钟贴近该日期时会落进种子起点之前，
    找不到价格 → no_active_price_book（非确定）。本对书与种子区间半开
    不相交（可无 supersede 直接激活），保证任何过去时刻都有价可查；
    值与 corrected 夹具一致，不影响金额断言（金额断言用注入 now 的
    确定性用例，此处只为 priced 状态稳定）。
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


def bind_run_binding(request_id, session_id, subject_type, subject_id,
                     conn=None):
    """直接插一条 ai_run_bindings（0027 金额时代 owner/user run 绑定，复现
    app.py _ai_budget_lifecycle.on_accepted → budget_store.record_run_binding
    的终态；批次 F 硬闸主体的解析第①步主源）。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_run_bindings "
                "(request_id, subject_type, subject_id, histopilot_session_id) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (request_id) DO UPDATE SET "
                "histopilot_session_id=EXCLUDED.histopilot_session_id",
                (request_id, subject_type, subject_id, session_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def bind_demo_session(histopilot_session_id, capability_id=None,
                      run_state="consumed", conn=None):
    """直接插一条 demo_sessions（histopilot_session_id 绑定，恢复 demo 主体）。

    0026 前的**历史行形态**（resolver 第②步回退源）；新模型的绑定请用
    :func:`bind_demo_run`（批次 E：capability 与 run 分离）。"""
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


def bind_demo_run(histopilot_session_id, capability_id=None, conn=None):
    """0026 模型：插 demo_sessions（capability）+ demo_runs（accepted 流水，
    绑定 histopilot_session_id）——resolver 第②步的主绑定源（批次 E）。

    返回 capability id（= 权威 subject_id）。同一 capability 可先后绑定多个
    HP session（顺序多次 run 各绑各的）：复用同一 capability_id 再次调用时，
    上一条 active 流水先转 finished（终态后才能再开，模拟顺序体验）。"""
    import pg_store
    own = conn is None
    if own:
        conn = connect()
    try:
        cap = capability_id or ("demo_cap_" + uuid.uuid4().hex[:20])
        rid = "req_" + uuid.uuid4().hex[:16]
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO demo_sessions "
                    "(id, token_hash, created_at, expires_at) "
                    "VALUES (%s,%s, now(), now() + interval '1 day') "
                    "ON CONFLICT (id) DO NOTHING",
                    (cap, "tok_" + uuid.uuid4().hex))
                # 顺序语义：同 capability 上一个 active run 先终态化
                cur.execute(
                    "UPDATE demo_runs SET state='finished', "
                    "finished_at=now(), updated_at=now() "
                    "WHERE capability_id=%s AND state IN "
                    "('reserved', 'accepted')", (cap,))
                cur.execute(
                    "INSERT INTO demo_runs "
                    "(demo_run_id, capability_id, request_id, state, "
                    " histopilot_session_id, slide_id, accepted_at, "
                    " expires_at) "
                    "VALUES (%s,%s,%s,'accepted',%s,'sld_stub', now(), "
                    " now() + interval '1 hour')",
                    ("dmr_" + uuid.uuid4().hex[:20], cap, rid,
                     histopilot_session_id))
    finally:
        if own:
            conn.close()
    return cap


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
