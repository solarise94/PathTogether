# -*- coding: utf-8 -*-
"""批次 B：金额 policy/window 数据层测试（docs
ai-money-budget-bugfix-and-simplification-plan.md §9.2 全量 + §9.3 窗口投影
相关并发/对账用例）。

纯计算部分（json 模式也跑）：
  - 周边界：上海时区周一 00:00 前后（周日 23:59:59.999 vs 周一 00:00）、
    UTC 输入转换（15:59:59Z vs 16:00:00Z）、全年逐点扫描（起点恒为当地
    周一 00:00、跨度恒 7×24h）；
  - 月边界：月末/月初、2 月（非闰/闰）、12 月→次年 1 月、全年扫描
    （起点恒为当地 1 日 00:00）；
  - DST 无关性：把 SPEND_TIMEZONE 换成 Europe/Berlin（有夏令时）后，
    含夏令时切换的周仍按当地日历取周一 00:00（UTC 跨度 167h/169h，
    证明实现不做固定偏移假设）；
  - naive datetime 拒绝（无法判定周期口径）；epoch/RFC3339/datetime
    三种输入等价；period_kind=none 显式拒绝；
  （旧 json/dual fail-closed pg_backend_required 门已随 R3 Wave3 退役。）

PG 部分（RUN_PG_TESTS=1；conftest 每用例 TRUNCATE 后由
_billing_helpers.seed_spend_policies 幂等重放 0023 种子）：
  - 种子与 shadow 开关：三条默认策略（额度经 parse_balance_to_nano 独立
    换算断言，不从迁移复制常量自证）、部分唯一索引硬性拒绝同 scope 第二条
    enabled 未收口行、enforcement_mode 恒 shadow；
  - fresh PG 全量迁移 0001→0023 幂等（独立 pgserver 实例，ensure_schema
    两遍）；
  - 策略解析：default/override/回退、清除 override 后下个窗口恢复默认、
    enabled 与 effective 区间参与解析、无策略 fail-closed；
  - 窗口语义：新用户当月完整额度（不折算）、默认策略修改不追溯已开窗口
    （CAS + 冲突 409 语义）、显式调整当前窗口（CAS/audit/调低后拒绝新
    预占/已完成消费不动）、Demo 多 subject 归同一周窗口、两用户独立月窗口、
    owner 独立策略；
  - 原子投影：reserve 边界（恰好等于额度放行、超 1 nano 拒绝且不改数）、
    release/settle 数值正确性（actual>estimate 记 overage 与真实成本、
    重放/乱序夹 0 保不变量 + 指标）；
  - 真实 PG 并发（多连接 + threading.Barrier，禁 mock）：两个并发 reserve
    合计只能一个越过临界点、并发 get_or_create_window 只产生一行；
  - 对账器：注入 drift 能发现（usage events / open holds 两路），口径排除
    cutover 前旧影子数据；不自动修。

运行：cd 项目根 && python3 -m pytest tests/test_spend_store.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_spend_store.py -q）
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import pytest  # noqa: E402

import billing_pricing  # noqa: E402
import platform_features  # noqa: E402
import spend_store  # noqa: E402

from pg_compat import BACKEND  # noqa: E402

CNY = billing_pricing.parse_balance_to_nano  # 独立 CNY→nano 换算入口
SH = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

# 种子额度面值（CNY；**额度是 owner 待决策的占位默认**——测试只断言迁移种子
# 与该独立换算一致，不代表额度已定）
SEED_DEMO_WEEK_CNY = "50"
SEED_USER_MONTH_CNY = "20"
SEED_OWNER_MONTH_CNY = "1000"

def _sh(y, m, d, hh=0, mm=0, ss=0, micro=0):
    """上海本地时刻 → aware datetime。"""
    return datetime(y, m, d, hh, mm, ss, micro, tzinfo=SH)

def _backdate_seed_policies():
    """把三条种子策略的 effective_from 提前到 2020（种子默认 now()——用固定
    历史 at 构造窗口的用例需要策略在那之前已生效；不改变额度/版本语义）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET effective_from="
                        "'2020-01-01T00:00:00+00:00' WHERE policy_id = ANY"
                        "(%s)", (list(bh.SEED_POLICY_IDS),))
        conn.commit()
    finally:
        conn.close()

# =========================================================================== #
# 1. 周边界（§1.1 / §9.2）
# =========================================================================== #
def test_week_boundary_sunday_night_vs_monday_midnight():
    """周日（2026-08-30）23:59:59.999+08 属上一周；恰好周一 00:00+08
    （左闭）开启新一周（2026-08-31 是周一）。"""
    start, end = spend_store.week_window_bounds(_sh(2026, 8, 30, 23, 59, 59,
                                                    999999))
    assert start == datetime(2026, 8, 23, 16, 0, tzinfo=UTC)   # 周一 00:00+08
    assert end == datetime(2026, 8, 30, 16, 0, tzinfo=UTC)     # 下周一 00:00+08
    start, end = spend_store.week_window_bounds(_sh(2026, 8, 31))
    assert start == datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 6, 16, 0, tzinfo=UTC)

def test_week_bounds_utc_input_conversion():
    """UTC 输入与上海输入同刻等价（15:59:59Z = 23:59:59+08 周日）。"""
    sunday_utc = datetime(2026, 8, 30, 15, 59, 59, tzinfo=UTC)
    assert spend_store.week_window_bounds(sunday_utc) == \
        spend_store.week_window_bounds(_sh(2026, 8, 30, 23, 59, 59))
    monday_utc = datetime(2026, 8, 30, 16, 0, 0, tzinfo=UTC)  # 周一 00:00+08
    assert spend_store.week_window_bounds(monday_utc)[0] == monday_utc
    # 其他偏移输入（+05:30）同样先归上海再取周界
    ist = monday_utc.astimezone(ZoneInfo("Asia/Kolkata"))
    assert spend_store.week_window_bounds(ist)[0] == monday_utc

def test_week_sweep_always_local_monday_midnight():
    """全年逐 6 小时扫描：起点恒为当地周一 00:00，跨度恒 7×24h（上海无
    夏令时时跨度必为整周；跨年周界同样成立）。"""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    while at.year == 2026:
        start, end = spend_store.week_window_bounds(at)
        local_start = start.astimezone(SH)
        assert local_start.weekday() == 0
        assert (local_start.hour, local_start.minute, local_start.second,
                local_start.microsecond) == (0, 0, 0, 0)
        assert end - start == timedelta(days=7)
        assert start <= at.astimezone(UTC) < end
        at += timedelta(hours=6)

def test_week_epoch_and_rfc3339_inputs_equivalent():
    dt = _sh(2026, 8, 30, 12, 0, 0)
    assert spend_store.week_window_bounds(dt) == \
        spend_store.week_window_bounds(dt.timestamp()) == \
        spend_store.week_window_bounds(dt.isoformat())

# =========================================================================== #
# 2. 月边界（§1.1 / §9.2：月末/月初、2 月、闰年、跨年）
# =========================================================================== #
def test_month_boundary_end_and_start_of_month():
    start, end = spend_store.month_window_bounds(_sh(2026, 1, 31, 23, 59, 59))
    assert start == datetime(2025, 12, 31, 16, 0, tzinfo=UTC)  # 1 月 1 日 00:00+08
    assert end == datetime(2026, 1, 31, 16, 0, tzinfo=UTC)     # 2 月 1 日 00:00+08
    start, end = spend_store.month_window_bounds(_sh(2026, 2, 1))
    assert start == datetime(2026, 1, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 2, 28, 16, 0, tzinfo=UTC)     # 2026 非闰年：3 月 1 日 00:00+08

def test_month_february_non_leap_and_leap_year():
    # 2026（非闰）：2 月 28 日仍属 2 月，次日直落 3 月（3/1 00:00+08 = 2/28 16:00Z）
    start, end = spend_store.month_window_bounds(_sh(2026, 2, 28, 12))
    assert start == datetime(2026, 1, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 2, 28, 16, 0, tzinfo=UTC)
    # 2024（闰）：2 月 29 天——同为 2/28 输入，窗口终点是 2/29 16:00Z
    # （= 3/1 00:00+08；2/29 这一天存在，终点比非闰年晚一天）
    start, end = spend_store.month_window_bounds(_sh(2024, 2, 28, 12))
    assert end == datetime(2024, 2, 29, 16, 0, tzinfo=UTC)
    start, end = spend_store.month_window_bounds(_sh(2024, 2, 29, 23, 59, 59))
    assert start == datetime(2024, 1, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2024, 2, 29, 16, 0, tzinfo=UTC)

def test_month_december_to_january_rollover():
    start, end = spend_store.month_window_bounds(_sh(2026, 12, 31, 23, 59, 59))
    assert start == datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 12, 31, 16, 0, tzinfo=UTC)    # 2027-01-01 00:00+08
    start, end = spend_store.month_window_bounds(_sh(2027, 1, 1))
    assert start == datetime(2026, 12, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2027, 1, 31, 16, 0, tzinfo=UTC)

def test_month_sweep_always_local_first_day_midnight():
    """逐日扫描两年：起点恒为当地当月 1 日 00:00、终点为次月 1 日 00:00，
    区间左闭右开覆盖 at。"""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    while at < datetime(2028, 1, 1, tzinfo=UTC):
        start, end = spend_store.month_window_bounds(at)
        ls, le = start.astimezone(SH), end.astimezone(SH)
        assert (ls.day, ls.hour) == (1, 0)
        assert (le.day, le.hour) == (1, 0)
        if ls.month == 12:
            assert (le.year, le.month) == (ls.year + 1, 1)
        else:
            assert (le.year, le.month) == (ls.year, ls.month + 1)
        assert start <= at < end
        at += timedelta(days=1)

def test_dst_timezone_week_bounds_use_local_calendar(monkeypatch):
    """DST 无关性：换成有夏令时的 Europe/Berlin，含 2026-03-29 春令时的周
    仍按当地日历取周一 00:00（UTC 跨度 167h）——证明实现不假设固定偏移。"""
    monkeypatch.setattr(spend_store, "SPEND_TIMEZONE", ZoneInfo("Europe/Berlin"))
    start, end = spend_store.week_window_bounds(
        datetime(2026, 3, 25, 12, tzinfo=UTC))
    assert start.astimezone(ZoneInfo("Europe/Berlin")).weekday() == 0
    assert end - start == timedelta(hours=167)  # 周内少一小时（03-29 02:00→03:00）

# =========================================================================== #
# 3. 输入归一与纯函数防御（§1.1）
# =========================================================================== #
def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        spend_store.week_window_bounds(datetime(2026, 8, 30, 12, 0))
    with pytest.raises(ValueError):
        spend_store.month_window_bounds(datetime(2026, 8, 30))
    with pytest.raises(ValueError):
        spend_store.week_window_bounds(object())
    with pytest.raises(ValueError):
        spend_store.month_window_bounds(True)

def test_period_kind_none_has_no_window_semantics():
    with pytest.raises(spend_store.InvalidSpendRequestError):
        spend_store.window_bounds("none", datetime(2026, 8, 30, tzinfo=UTC))

# =========================================================================== #
# 5. PG：种子、shadow 开关与迁移幂等（§9.7）
# =========================================================================== #
import psycopg  # noqa: E402
import _billing_helpers as bh  # noqa: E402

def test_seed_policies_shadow_flag_and_partial_unique():
    """0023 种子：三条默认策略（独立 CNY→nano 换算断言）、shadow 开关、
    部分唯一索引在 DB 层拒绝同 scope 第二条 enabled 未收口行。"""
    bh.seed_spend_policies()
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT policy_id, scope_type, period_kind, "
                        "limit_nano_cny, enabled, version FROM "
                        "ai_spend_policies ORDER BY policy_id")
            rows = {r["policy_id"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()
    assert set(rows) == set(bh.SEED_POLICY_IDS)
    demo = rows["spp_demo_global"]
    assert (demo["scope_type"], demo["period_kind"]) == \
        ("demo_global", "calendar_week")
    assert demo["limit_nano_cny"] == CNY(SEED_DEMO_WEEK_CNY)   # 50 CNY
    user = rows["spp_user_default"]
    assert (user["scope_type"], user["period_kind"]) == \
        ("user_default", "calendar_month")
    assert user["limit_nano_cny"] == CNY(SEED_USER_MONTH_CNY)  # 20 CNY
    owner = rows["spp_owner"]
    assert (owner["scope_type"], owner["period_kind"]) == \
        ("owner", "calendar_month")
    assert owner["limit_nano_cny"] == CNY(SEED_OWNER_MONTH_CNY)  # 1000 CNY
    for row in rows.values():
        assert row["enabled"] is True and row["version"] == 1
    # enforcement 开关 = shadow（0023 只在不存在时种）
    assert spend_store.enforcement_mode() == "shadow"
    # 部分唯一索引：同 scope 第二条 enabled 未收口行被 DB 拒绝（不靠应用层）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO ai_spend_policies (policy_id, scope_type, "
                    "scope_id, period_kind, limit_nano_cny, enabled, "
                    "effective_from) VALUES ('spp_demo_dup', 'demo_global', "
                    "NULL, 'calendar_week', 1, true, now())")
        conn.rollback()
        # user_override 必须带 scope_id（DB CHECK 强制）
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO ai_spend_policies (policy_id, scope_type, "
                    "scope_id, period_kind, limit_nano_cny, enabled, "
                    "effective_from) VALUES ('spp_uo_noid', 'user_override', "
                    "NULL, 'calendar_month', 1, true, now())")
        conn.rollback()
    finally:
        conn.close()
    # 种子幂等：重放 0023 不产生第二条
    bh.seed_spend_policies()
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM ai_spend_policies")
            assert cur.fetchone()["n"] == 3
            cur.execute("SELECT value FROM platform_settings "
                        "WHERE key='spend_enforcement_mode'")
            assert cur.fetchone()["value"] == "shadow"
    finally:
        conn.close()

def test_fresh_database_full_migration_to_0023_idempotent():
    """fresh PG 全量迁移 0001→0025：种子与 shadow 开关就位；ensure_schema
    重跑幂等（种子不翻倍、开关不被改写、0025 邀请列存在）。"""
    pytest.importorskip("pgserver")
    import tempfile
    import pg_store
    data_dir = tempfile.mkdtemp(prefix="m0023-fresh-")
    srv = pytest.importorskip("pgserver").get_server(data_dir)
    try:
        conn = psycopg.connect(srv.get_uri())
        try:
            # ensure_schema 内部按元组行取值——先跑完迁移再切 dict_row
            files = pg_store.ensure_schema(conn)
            pg_store.ensure_schema(conn)  # 幂等重跑
            assert "0023_spend_policies_windows.sql" in files
            assert "0025_invite_monthly_limit.sql" in files
            conn.row_factory = psycopg.rows.dict_row
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM ai_spend_policies "
                            "WHERE enabled AND effective_to IS NULL")
                assert cur.fetchone()["n"] == 3
                cur.execute("SELECT count(*) AS n FROM schema_migrations "
                            "WHERE filename='0023_spend_policies_windows.sql'")
                assert cur.fetchone()["n"] == 1
                cur.execute("SELECT value FROM platform_settings "
                            "WHERE key='spend_enforcement_mode'")
                assert cur.fetchone()["value"] == "shadow"
                cur.execute("SELECT count(*) AS n FROM audit_events WHERE "
                            "event_id='aud_migration_0023_spend_windows'")
                assert cur.fetchone()["n"] == 1
                # 0033（R3 Wave2-Compat）：0025 曾加的邀请月额度模板列被
                # 物理删除（全量迁移跑完后列不存在）
                cur.execute("SELECT count(*) AS n FROM information_schema"
                            ".columns WHERE table_name="
                            "'registration_invites' AND column_name="
                            "'monthly_limit_nano_cny'")
                assert cur.fetchone()["n"] == 0
        finally:
            conn.close()
    finally:
        srv.cleanup()

# =========================================================================== #
# 6. PG：策略解析（§3.1 / §9.2）
# =========================================================================== #
def test_resolution_default_and_subject_kinds():
    """R3 单轨：user_override 写面已删，解析只剩各主体默认策略。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    p = spend_store.resolve_policy("user", "usr_resolve_1")
    assert p["policy_id"] == "spp_user_default"
    assert p["limit_nano_cny"] == CNY(SEED_USER_MONTH_CNY)
    # demo → 唯一 demo_global；owner → 独立 owner
    assert spend_store.resolve_policy("demo", "cap_whatever")["policy_id"] == \
        "spp_demo_global"
    assert spend_store.resolve_policy("owner", "usr_owner")["policy_id"] == \
        "spp_owner"

def test_resolution_honors_enabled_flag():
    """enabled=false → 回退/拒绝（R3 单轨后 override 区间用例删除：override
    写面已退役；策略 effective 区间由 update_policy_limit 收口另测）。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    uid = "usr_resolve_2"
    assert spend_store.resolve_policy("user", uid)["policy_id"] == \
        "spp_user_default"
    # user_default 整体禁用 → user 无有效策略（fail-closed，不落无额度窗口）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_user_default'")
        conn.commit()
    finally:
        conn.close()
    assert spend_store.resolve_policy("user", uid) is None
    with pytest.raises(spend_store.SpendPolicyMissingError):
        spend_store.get_or_create_window("user", uid)
    # demo_global 禁用同理
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_demo_global'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(spend_store.SpendPolicyMissingError):
        spend_store.get_or_create_window("demo", "cap_x")

# =========================================================================== #
# 7. PG：窗口语义（§3.2 / §9.2）
# =========================================================================== #
def test_new_user_full_month_limit_not_prorated():
    """月中注册的新用户拿完整月额度（不按剩余天数折算，§1.1）。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    win = spend_store.get_or_create_window(
        "user", "usr_mid_month", at=_sh(2026, 8, 15, 12))
    assert win["limit_nano_snapshot"] == CNY(SEED_USER_MONTH_CNY)
    # 窗口边界仍是整月 [8/1, 9/1)，不是 [8/15, 9/1)
    assert win["window_start"] == datetime(2026, 7, 31, 16, 0,
                                           tzinfo=UTC).timestamp()
    assert win["window_end"] == datetime(2026, 8, 31, 16, 0,
                                         tzinfo=UTC).timestamp()
    assert win["policy_id"] == "spp_user_default"

def test_policy_update_cas_and_no_retroaction():
    """默认策略修改只影响新窗口；CAS 冲突 → 409 语义异常。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    uid = "usr_retro"
    w1 = spend_store.get_or_create_window("user", uid,
                                          at=_sh(2026, 8, 15, 12))
    # CAS 更新默认额度（20 → 30 CNY）
    updated = spend_store.update_policy_limit(
        "spp_user_default", CNY("30"), 1, updated_by="pytest")
    assert updated["version"] == 2
    assert updated["limit_nano_cny"] == CNY("30")
    # 旧 version 再更新 → 冲突（不做 last-write-wins）
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.update_policy_limit("spp_user_default", CNY("40"), 1)
    # 已开窗口不追溯
    again = spend_store.get_or_create_window("user", uid,
                                             at=_sh(2026, 8, 20, 12))
    assert again["window_id"] == w1["window_id"]
    assert again["limit_nano_snapshot"] == CNY(SEED_USER_MONTH_CNY)
    # 下个月的新窗口取新额度
    w2 = spend_store.get_or_create_window("user", uid,
                                          at=_sh(2026, 9, 5, 12))
    assert w2["window_id"] != w1["window_id"]
    assert w2["limit_nano_snapshot"] == CNY("30")
    assert w2["policy_version"] == 2

def test_adjust_current_window_cas_audit_and_denial():
    """显式调整当前窗口：CAS、audit、不取消已完成消费、调低后拒绝新预占。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    uid = "usr_adjust"
    win = spend_store.get_or_create_window("user", uid,
                                           at=_sh(2026, 8, 15, 12))
    wid, ver = win["window_id"], win["version"]
    # 已有消费：reserve 1 CNY → settle 真实 2 CNY（overage 1 CNY）
    spend_store.window_reserve(wid, CNY("1"))
    settled = spend_store.window_settle(wid, CNY("1"), CNY("2"))
    assert settled["spent_nano_cny"] == CNY("2")
    assert settled["reserved_nano_cny"] == 0
    assert settled["overage_nano"] == CNY("1")
    # 调低到 2.5 CNY（低于 spent+新预占的临界）：CAS 命中 + audit
    adjusted = spend_store.adjust_current_window(
        wid, CNY("2.5"), ver + 2, actor_user_id="usr_owner",
        confirm=True)  # ver：reserve/settle 各 +1 → 当前 version = ver+2
    assert adjusted["limit_nano_snapshot"] == CNY("2.5")
    # 已完成消费不动
    assert adjusted["spent_nano_cny"] == CNY("2")
    assert adjusted["reserved_nano_cny"] == 0
    # 下一次 reserve 必须拒绝（2 + 0.6 > 2.5），且数字不变
    with pytest.raises(spend_store.SpendBudgetExhaustedError) as exc:
        spend_store.window_reserve(wid, CNY("0.6"))
    assert exc.value.code == "spend_budget_exhausted"
    after = spend_store.get_window(wid)
    assert after["spent_nano_cny"] == CNY("2")
    assert after["reserved_nano_cny"] == 0
    assert after["limit_nano_snapshot"] == CNY("2.5")
    # 旧 version 再调整 → 409 语义冲突
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.adjust_current_window(wid, CNY("9"), ver)
    # audit 落库（spend.window_adjust，detail 含 confirm 与前后额度）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detail FROM audit_events WHERE action="
                        "'spend.window_adjust' AND target_id=%s", (wid,))
            detail = cur.fetchone()["detail"]
    finally:
        conn.close()
    assert detail["confirm"] is True
    assert detail["previous_limit_nano_snapshot"] == CNY(SEED_USER_MONTH_CNY)
    assert detail["new_limit_nano_snapshot"] == CNY("2.5")
    assert detail["spent_nano_cny"] == CNY("2")

def test_demo_subjects_share_one_week_window():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    a = spend_store.get_or_create_window("demo", "demo_cap_aaa",
                                         at=_sh(2026, 8, 26, 12))  # 周三
    b = spend_store.get_or_create_window("demo", "demo_cap_bbb",
                                         at=_sh(2026, 8, 28, 9))   # 周五
    assert a["window_id"] == b["window_id"]
    assert a["subject_id"] == "demo_global"
    assert a["window_start"] == datetime(2026, 8, 23, 16, 0,
                                         tzinfo=UTC).timestamp()  # 周一 00:00+08
    # 预占互通：同一池
    spend_store.window_reserve(a, CNY("1"))
    refreshed = spend_store.get_window(b["window_id"])
    assert refreshed["reserved_nano_cny"] == CNY("1")
    # 下周一 → 新窗口，额度重置为策略面值
    nxt = spend_store.get_or_create_window("demo", "demo_cap_ccc",
                                           at=_sh(2026, 9, 2, 8))
    assert nxt["window_id"] != a["window_id"]
    assert nxt["limit_nano_snapshot"] == CNY(SEED_DEMO_WEEK_CNY)
    assert nxt["spent_nano_cny"] == 0

def test_two_users_independent_month_windows():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    at = _sh(2026, 8, 20, 12)
    w1 = spend_store.get_or_create_window("user", "usr_a", at=at)
    w2 = spend_store.get_or_create_window("user", "usr_b", at=at)
    assert w1["window_id"] != w2["window_id"]
    assert w1["subject_id"] == "usr_a" and w2["subject_id"] == "usr_b"
    spend_store.window_reserve(w1, CNY("3"))
    assert spend_store.window_remaining_nano(w2) == CNY(SEED_USER_MONTH_CNY)
    assert spend_store.window_remaining_nano(
        spend_store.get_window(w1["window_id"])) == \
        CNY(SEED_USER_MONTH_CNY) - CNY("3")

def test_owner_window_uses_owner_policy():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    win = spend_store.get_or_create_window(
        "owner", "usr_owner_1", at=_sh(2026, 8, 20, 12))
    assert win["policy_id"] == "spp_owner"
    assert win["limit_nano_snapshot"] == CNY(SEED_OWNER_MONTH_CNY)
    assert win["window_start"] == datetime(2026, 7, 31, 16, 0,
                                           tzinfo=UTC).timestamp()

# =========================================================================== #
# 8. PG：原子投影数值语义（§3.2 / §9.3）
# =========================================================================== #
def test_reserve_boundary_exact_limit_and_one_nano_over():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    win = spend_store.get_or_create_window("user", "usr_boundary",
                                           at=_sh(2026, 8, 20))
    limit = win["limit_nano_snapshot"]
    # 恰好等于额度：放行（<=）
    ok = spend_store.window_reserve(win["window_id"], limit)
    assert ok["reserved_nano_cny"] == limit
    # 超 1 nano：拒绝且不改数
    before = spend_store.metrics_snapshot().get("spend_reserve_denied_total",
                                                0)
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        spend_store.window_reserve(win["window_id"], 1)
    after = spend_store.get_window(win["window_id"])
    assert after["reserved_nano_cny"] == limit
    assert after["version"] == ok["version"]  # 拒绝不加 version（没改数）
    assert spend_store.metrics_snapshot().get(
        "spend_reserve_denied_total", 0) == before + 1

def test_release_settle_numerics_and_overage():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    win = spend_store.get_or_create_window("user", "usr_numerics",
                                           at=_sh(2026, 8, 20))
    wid = win["window_id"]
    # reserve 100 nano → release 40 → reserved 60
    spend_store.window_reserve(wid, 100)
    r = spend_store.window_release(wid, 40)
    assert r["reserved_nano_cny"] == 60 and r["spent_nano_cny"] == 0
    # settle(60, 50)：reserved 0、spent 50（差额自然释放）
    s = spend_store.window_settle(wid, 60, 50)
    assert (s["reserved_nano_cny"], s["spent_nano_cny"]) == (0, 50)
    assert s["overage_nano"] == 0
    # actual < estimate 已覆盖；actual > estimate：真实成本入账 + overage 指标
    spend_store.window_reserve(wid, 30)
    over_before = spend_store.metrics_snapshot().get(
        "spend_settle_overage_total", 0)
    s = spend_store.window_settle(wid, 30, 150)
    assert s["spent_nano_cny"] == 200
    assert s["reserved_nano_cny"] == 0
    assert s["overage_nano"] == 120
    assert spend_store.metrics_snapshot().get(
        "spend_settle_overage_total", 0) == over_before + 1
    # 重放/乱序：settled 后再 release/settle 同 estimate —— reserved 夹 0、
    # 不变量保持（调用级幂等由批次 C hold 状态机负责，这里验证原语不炸）
    clamp_before = spend_store.metrics_snapshot().get(
        "spend_release_clamp_total", 0)
    rr = spend_store.window_release(wid, 30)
    assert rr["reserved_nano_cny"] == 0
    assert spend_store.metrics_snapshot().get(
        "spend_release_clamp_total", 0) == clamp_before + 1
    s2 = spend_store.window_settle(wid, 30, 50)
    assert s2["reserved_nano_cny"] == 0
    assert s2["spent_nano_cny"] == 250  # 原语按调用累加（去重职责在 hold 层）
    # 负数/非整数入参拒绝
    for bad in (-1, 1.5, "10", True):
        with pytest.raises(spend_store.InvalidSpendRequestError):
            spend_store.window_reserve(wid, bad)

def test_reserve_on_missing_or_closed_window_rejected():
    bh.seed_spend_policies()
    _backdate_seed_policies()
    with pytest.raises(spend_store.SpendWindowUnavailableError):
        spend_store.window_reserve("spw_does_not_exist", 1)
    win = spend_store.get_or_create_window("user", "usr_closed",
                                           at=_sh(2026, 8, 20))
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_windows SET status='closed' "
                        "WHERE window_id=%s", (win["window_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(spend_store.SpendWindowUnavailableError):
        spend_store.window_reserve(win["window_id"], 1)
    # release/settle 不要求 open（迟到结算仍要收敛 reserved / 记真实成本）
    out = spend_store.window_settle(win["window_id"], 0, 5)
    assert out["spent_nano_cny"] == 5

# =========================================================================== #
# 9. PG：真实并发（§9.3：多连接 + 屏障，禁 mock）
# =========================================================================== #
def test_concurrent_reserves_cannot_overdraw_window():
    """两个并发 reserve（各 = 全额度）：FOR UPDATE 串行化后只能一个越过
    临界点；另一个稳定拒绝；最终 reserved == limit。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    win = spend_store.get_or_create_window("user", "usr_race",
                                           at=_sh(2026, 8, 20))
    limit = win["limit_nano_snapshot"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        try:
            out = spend_store.window_reserve(win["window_id"], limit)
            results.append(("ok", out))
        except spend_store.SpendBudgetExhaustedError as exc:
            results.append(("denied", exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert sum(1 for kind, _ in results if kind == "denied") == 1
    final = spend_store.get_window(win["window_id"])
    assert final["reserved_nano_cny"] == limit
    assert final["spent_nano_cny"] == 0
    assert spend_store.window_remaining_nano(final) == 0

def test_concurrent_get_or_create_window_single_row():
    """并发创建同一窗口：UNIQUE 兜底，DB 里只有一行，各线程拿到同一 id。"""
    bh.seed_spend_policies()
    _backdate_seed_policies()
    at = _sh(2026, 8, 20, 12)
    n = 4
    barrier = threading.Barrier(n)
    ids = []

    def worker(subject_id):
        barrier.wait()
        ids.append(spend_store.get_or_create_window("demo", subject_id,
                                                    at=at)["window_id"])

    threads = [threading.Thread(target=worker, args=("demo_cap_%d" % i,))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(ids) == n
    assert len(set(ids)) == 1
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM ai_spend_windows "
                        "WHERE subject_type='demo'")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()

# =========================================================================== #
# 10. PG：对账器（§9.7 / §8 批次 B「对账器与指标」）
# =========================================================================== #
def _insert_priced_event(cur, hex32, subject_type, subject_id, occurred_at,
                         charge_nano):
    """直接写一条 priced usage event（绕过 ingest 的权威主体绑定——对账器
    只读事件行，不需要绑定链路）。priced CHECK 要求双价格书 id 与两种金额
    非空，价格书 id 取 0022 corrected v2（调用方需先 seed_price_books）。"""
    cur.execute(
        "INSERT INTO ai_usage_events (event_id, call_id, payload_hash, "
        "schema_version, session_id, subject_type, subject_id, provider, "
        "model, cache_hit_input_tokens, cache_miss_input_tokens, "
        "output_tokens, reasoning_tokens, total_tokens, occurred_at, "
        "enqueued_at, received_at, status, provider_price_book_id, "
        "charge_price_book_id, provider_cost_nano_cny, charge_nano_cny) "
        "VALUES (%s,%s,%s,1,'sess_rc',%s,%s,'deepseek','deepseek-v4-flash',"
        "0,0,0,0,0,%s,%s,%s,'priced',%s,%s,%s,%s)",
        ("use_" + hex32, "call_" + hex32, "0" * 64, subject_type, subject_id,
         occurred_at, occurred_at, occurred_at, bh.CORRECTED_BOOK_IDS[0],
         bh.CORRECTED_BOOK_IDS[1], charge_nano, charge_nano))

def _insert_open_hold(cur, hex32, subject_type, subject_id, estimated_nano,
                      created_at):
    cur.execute(
        "INSERT INTO billing_holds (hold_id, call_id, subject_type, "
        "subject_id, installation_id, session_id, model, estimated_nano_cny, "
        "status, expires_at, created_at) VALUES (%s,%s,%s,%s,'inst_rc',"
        "'sess_rc','deepseek-v4-flash',%s,'open',%s + interval '1 hour',%s)",
        ("hold_" + hex32[:20], "call_" + hex32, subject_type, subject_id,
         estimated_nano, created_at, created_at))

def _set_cutover(conn, when):
    """把 pricing_v2_cutover_at 固定为测试给定时刻（对账器只读该设置，
    固定值让「cutover 前事件被排除」可确定性验证）。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO platform_settings (key, value, updated_at, "
            "updated_by) VALUES ('pricing_v2_cutover_at', %s, now(), "
            "'pytest') ON CONFLICT (key) DO UPDATE SET "
            "value=EXCLUDED.value",
            (psycopg.types.json.Jsonb(when.timestamp()),))
    conn.commit()

def test_reconcile_reports_drift_without_fixing():
    bh.seed_price_books()  # priced CHECK 需要真实价格书 id
    bh.seed_spend_policies()
    _backdate_seed_policies()
    uid = "usr_reconcile"
    win = spend_store.get_or_create_window("user", uid,
                                           at=_sh(2026, 8, 20, 12))
    wid = win["window_id"]
    start = datetime.fromtimestamp(win["window_start"], tz=UTC)
    end = datetime.fromtimestamp(win["window_end"], tz=UTC)
    # 固定 cutover 在窗口中部：隔离「窗口内但 cutover 前的旧影子数据被排除」
    cutover = datetime(2026, 8, 15, 4, 0, tzinfo=UTC)
    conn = bh.connect()
    try:
        _set_cutover(conn, cutover)
        with conn.cursor() as cur:
            # 窗口内、cutover 后：计入
            _insert_priced_event(cur, "a" * 32, "user", uid,
                                 cutover + timedelta(minutes=20), 700)
            # 窗口内但 cutover 前：**必须排除**（legacy 错误价格影子数据）
            assert start < cutover - timedelta(days=1) < end
            _insert_priced_event(cur, "b" * 32, "user", uid,
                                 cutover - timedelta(days=1), 999_999)
            _insert_open_hold(cur, "c" * 32, "user", uid, 300,
                              cutover + timedelta(minutes=30))
        conn.commit()
    finally:
        conn.close()

    # 注入 drift：窗口 spent/reserved 与应有值不一致
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_windows SET spent_nano_cny=%s, "
                        "reserved_nano_cny=%s WHERE window_id=%s",
                        (500, 999, wid))
        conn.commit()
    finally:
        conn.close()

    # 对账时刻固定在 hold 有效期内（open 未过期才计入 reserved 重建）
    result = spend_store.reconcile_spend_windows(
        at=cutover + timedelta(minutes=45))
    item = next(i for i in result["items"] if i["window_id"] == wid)
    assert result["checked"] >= 1
    assert result["drift_windows"] >= 1
    assert item["matches"] is False
    # expected：cutover 后事件 700（cutover 前的 999999 排除）+ open hold 300
    assert item["expected_spent_nano"] == 700
    assert item["expected_reserved_nano"] == 300
    assert item["actual_spent_nano"] == 500
    assert item["actual_reserved_nano"] == 999
    assert item["spent_drift_nano"] == 500 - 700
    assert item["reserved_drift_nano"] == 999 - 300
    assert result["pricing_cutover_epoch"] == cutover.timestamp()
    # 不自动修：DB 值原样保留
    assert spend_store.get_window(wid)["spent_nano_cny"] == 500

    # 修正投影后重跑 → 无 drift
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_windows SET spent_nano_cny=%s, "
                        "reserved_nano_cny=%s WHERE window_id=%s",
                        (700, 300, wid))
        conn.commit()
    finally:
        conn.close()
    result = spend_store.reconcile_spend_windows(
        at=cutover + timedelta(minutes=45))
    assert all(i["matches"] for i in result["items"])
    assert result["drift_windows"] == 0

def test_reconcile_demo_window_counts_all_demo_subjects():
    """demo 窗口对账把所有 demo 主体的事件都计入同一周窗口（固定时刻，避免
    真实时钟跨过周日 00:00 边界时事件落到窗口外）。"""
    bh.seed_price_books()
    bh.seed_spend_policies()
    _backdate_seed_policies()
    spend_store.get_or_create_window("demo", "demo_global",
                                     at=_sh(2026, 8, 26, 12))
    conn = bh.connect()
    try:
        _set_cutover(conn, datetime(2026, 1, 1, tzinfo=UTC))  # 早于窗口起点
        with conn.cursor() as cur:
            _insert_priced_event(cur, "d" * 32, "demo", "demo_cap_x",
                                 _sh(2026, 8, 27, 10), 111)
            _insert_priced_event(cur, "e" * 32, "demo", "demo_cap_y",
                                 _sh(2026, 8, 27, 11), 222)
        conn.commit()
    finally:
        conn.close()
    result = spend_store.reconcile_spend_windows()
    item = next(i for i in result["items"] if i["subject_type"] == "demo")
    assert item["expected_spent_nano"] == 333
    assert item["actual_spent_nano"] == 0
    assert item["matches"] is False

# =========================================================================== #
# 11. 批次 D：enforcement 写入口（§7.3）+ override tx 变体（§5.1/§5.2）
# =========================================================================== #
def test_set_enforcement_mode_vocab_cas_and_audit():
    """词表校验、CAS（expected 不符 409 语义）、audit 与写入同事务。"""
    bh.seed_spend_policies()
    assert spend_store.enforcement_mode() == "shadow"
    # 词表外拒绝（不落库）
    for bad in ("hard", "off", "", None, 1):
        with pytest.raises(spend_store.InvalidSpendRequestError):
            spend_store.set_enforcement_mode(bad)
    assert spend_store.enforcement_mode() == "shadow"
    # CAS：expected 不匹配当前值 → version_conflict
    with pytest.raises(spend_store.SpendVersionConflictError):
        spend_store.set_enforcement_mode("all", expected="registered")
    # 切 registered → all → 回 shadow（测试内切换；生产缺省恒 shadow）
    out = spend_store.set_enforcement_mode("registered", expected="shadow")
    assert out == {"previous_mode": "shadow", "mode": "registered"}
    assert spend_store.mode_is_hard("registered", "user") is True
    assert spend_store.mode_is_hard("registered", "demo") is False
    out2 = spend_store.set_enforcement_mode("all", expected="registered")
    assert out2["previous_mode"] == "registered"
    assert spend_store.mode_is_hard("all", "demo") is True
    out3 = spend_store.set_enforcement_mode("shadow", expected="all")
    assert out3["mode"] == "shadow"
    assert spend_store.enforcement_mode() == "shadow"
    # audit：每次写入一条 spend.enforcement_mode_update（含前后模式）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detail FROM audit_events WHERE action="
                        "'spend.enforcement_mode_update' ORDER BY ts")
            details = [r["detail"] for r in cur.fetchall()]
    finally:
        conn.close()
    assert [d["mode"] for d in details] == \
        ["registered", "all", "shadow"]
    assert details[0]["previous_mode"] == "shadow"

def test_admin_users_spend_summaries_batch():
    """admin_users_spend_summaries（R3 单轨）：user 恒 total 形态（缺行稳定
    error=spend_total_allowance_missing）、owner 窗口形态、批量混查。"""
    bh.seed_spend_policies()
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            spend_store.create_user_total_allowance_tx(
                cur, "usr_s_1", 42 * 10 ** 9, source="admin_create")
        conn.commit()
    finally:
        conn.close()
    summaries = spend_store.admin_users_spend_summaries(
        [("user", "usr_s_1"), ("user", "usr_s_2"),
         ("owner", "usr_s_owner")])
    assert summaries["usr_s_1"]["spend_target"] == "total_allowance"
    assert summaries["usr_s_1"]["total"]["total_limit_nano_cny"] == 42 * 10 ** 9
    assert summaries["usr_s_1"]["total"]["remaining_nano"] == 42 * 10 ** 9
    assert summaries["usr_s_2"]["spend_target"] == "total_allowance"
    assert summaries["usr_s_2"]["error"] == "spend_total_allowance_missing"
    assert summaries["usr_s_owner"]["spend_target"] == "window"
    assert summaries["usr_s_owner"]["policy_scope"] == "owner"
    assert summaries["usr_s_owner"]["window"]["subject_type"] == "owner"
    assert summaries["usr_s_owner"]["window"]["limit_nano_snapshot"] == \
        1000 * 10 ** 9
