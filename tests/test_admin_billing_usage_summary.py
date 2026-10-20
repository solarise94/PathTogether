# -*- coding: utf-8 -*-
"""2026-10-20 review（docs/review-for-opus 一.1/一.3/二）验收测试：

  - 费用聚合 API（``/api/admin/v1/billing/usage-summary``，view=hour|task）：
    41 次调用的任务、同会话多任务、跨小时任务、分组分页、未计价（金额未知
    不是 0）、超大金额（十进制字符串 wire）、先整和后统一舍入、晚到事件、
    void 排除、与账本不重复累计、request_id 缺失 →「历史未归属调用」；
  - 任务明细（``/api/admin/v1/billing/task-detail``）：范围内/完整任务/范围外
    金额、权威生命周期状态（无来源 → unknown）、step A 预算投影（本任务已用 /
    待结算预占 / 账户剩余额度及其类型 / 未设置单次上限）；
  - 身份投影：usage/ledger/summary/task-detail 由服务端批量投影邮箱
    （>50 用户、Dogfood、禁用账号、Demo、未知身份）；邮箱搜索过滤；
  - 活跃任务无 usage（验收 3）与「按任务」视图。

事件一律直插 ``ai_usage_events``（只读聚合的验收对象；金额/状态/时间完全
可控），价格书仅用于满足 priced CHECK 的外键。

运行：cd 项目根 && python3 -m pytest tests/test_admin_billing_usage_summary.py -q
"""
import os
import sys
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import billing_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app, make_client as _client  # noqa: E402

import _billing_helpers as bh  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SHANGHAI = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


# --------------------------------------------------------------------------- #
# 基建：用户 / 事件
# --------------------------------------------------------------------------- #
def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _owner_client():
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    return _login(_client(), owner), owner


def _set_email(uid, email, *, verified=True):
    """给用户补可信邮箱（email_normalized 与 users 列表 identity 同源）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET email=%s, email_normalized=%s, "
                " email_verified_at=%s WHERE user_id=%s",
                (email, email.strip().lower(),
                 datetime.now(timezone.utc) if verified else None, uid))
        conn.commit()
    finally:
        conn.close()


_SESSION_SEQ = {"n": 0}


def _next_session():
    _SESSION_SEQ["n"] += 1
    return "sess_review%04d" % _SESSION_SEQ["n"]


def _insert_event(*, subject_type="user", subject_id, user_id=None,
                  request_id=None, session_id=None, occurred_at=None,
                  status="priced", charge_nano=0, provider_cost_nano=0,
                  model="deepseek-flash", unpriced_reason="",
                  received_at=None, event_id=None, tokens=(10, 20, 30)):
    """直插一行 ai_usage_events（金额/状态/时间精确可控；满足表 CHECK）。"""
    hit, miss, out = tokens
    eid = event_id or ("use_" + uuid.uuid4().hex)
    occurred = occurred_at or datetime.now(timezone.utc)
    received = received_at or occurred
    priced = status == "priced"
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_usage_events "
                "(event_id, call_id, payload_hash, schema_version, request_id, "
                " session_id, subject_type, subject_id, user_id, provider, "
                " model, cache_hit_input_tokens, cache_miss_input_tokens, "
                " output_tokens, total_tokens, occurred_at, enqueued_at, "
                " received_at, status, unpriced_reason, "
                " provider_price_book_id, charge_price_book_id, "
                " provider_cost_nano_cny, charge_nano_cny) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,'deepseek',%s,%s,%s,%s,%s,"
                " %s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (eid, "call_" + uuid.uuid4().hex, "a" * 64, request_id,
                 session_id or _next_session(), subject_type, subject_id,
                 user_id, model,
                 hit if priced else None, miss if priced else None,
                 out if priced else None,
                 (hit + miss + out) if priced else None,
                 occurred, occurred, received, status, unpriced_reason or "",
                 "pb_deepseek_provider_cost_v3_flash_repricing" if priced
                 else None,
                 "pb_deepseek_customer_charge_v3_flash_repricing" if priced
                 else None,
                 int(provider_cost_nano) if priced else None,
                 int(charge_nano) if priced else None))
        conn.commit()
    finally:
        conn.close()
    return eid


def _seed_price_books():
    bh.seed_price_books()


def _range(hours_ago_start=24):
    """[now-``hours_ago_start``h, now+60s) —— 上界留 1 分钟余量，避免刚插入的
    事件落在半开区间外。"""
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=hours_ago_start), now + timedelta(seconds=60)


def _summary(client, **params):
    qs = "&".join("%s=%s" % (k, v) for k, v in params.items() if v is not None)
    url = "/api/admin/v1/billing/usage-summary" + ("?" + qs if qs else "")
    r = client.get(url)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _range_qs(since, until, extra=None):
    q = {"since": repr(since.timestamp()), "until": repr(until.timestamp()),
         "limit": 50}
    q.update(extra or {})
    return q


# --------------------------------------------------------------------------- #
# 1. 41 次调用的任务：小时行 + 内联任务行（review 一.3 的原始场景）
# --------------------------------------------------------------------------- #
def test_hour_summary_41_call_task_matches_authoritative_sum():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=3)
    rid = "req_review_41"
    session = _next_session()
    total = 0
    for i in range(41):
        nano = 9_500_000 + i * 1_000  # 逐条 0.00950…0.00954 CNY
        total += nano
        _insert_event(subject_type="owner", subject_id=owner["user_id"],
                      user_id=owner["user_id"], request_id=rid,
                      session_id=session, occurred_at=occurred,
                      charge_nano=nano, provider_cost_nano=nano // 2)
    body = _summary(c, **_range_qs(since, until))
    assert body["view"] == "hour"
    items = [it for it in body["items"] if it["subject_id"] == owner["user_id"]]
    assert len(items) == 1
    hour = items[0]
    assert hour["call_count"] == 41
    assert hour["task_count"] == 1
    assert hour["unpriced_call_count"] == 0
    assert hour["charge_nano_cny"] == str(total)
    # 先整和、后统一舍入一次
    assert hour["charge_cny"] == app_mod._admin_v1_cny_from_nano(total)
    tasks = hour["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["request_id"] == rid
    assert tasks[0]["call_count"] == 41
    assert tasks[0]["charge_nano_cny"] == str(total)
    assert tasks[0]["session_id"] == session
    # 权威事件合计一致（范围 totals）
    assert body["totals"]["call_count"] == 41
    assert body["totals"]["charge_nano_cny"] == str(total)
    assert body["totals"]["task_count"] == 1
    assert body["totals"]["amount_scope"] == "priced_only"
    assert body["totals"]["ledger_included"] is False
    assert body["range"]["timezone"] == "Asia/Shanghai"
    # wire：金额全部十进制字符串（前端不得 Number 累加）
    assert isinstance(hour["charge_nano_cny"], str)
    assert isinstance(body["totals"]["provider_cost_nano_cny"], str)


def test_hour_bucket_is_asia_shanghai_natural_hour():
    """小时桶 = Asia/Shanghai 自然小时（桶起点对齐整点，标签按 +08 口径）。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range(hours_ago_start=48)
    # 以「上海本地整点」构造：UTC+8 无夏令时，桶边界与整点对齐
    now_local = datetime.now(SHANGHAI)
    base_local = (now_local - timedelta(hours=5)).replace(
        minute=30, second=0, microsecond=0)
    base = base_local.astimezone(timezone.utc)
    rid = "req_hour_edge"
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid,
                  session_id=_next_session(), occurred_at=base,
                  charge_nano=1_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid,
                  session_id=_next_session(),
                  occurred_at=base + timedelta(hours=1),
                  charge_nano=2_000_000_000)
    body = _summary(c, **_range_qs(since, until))
    hours = [it for it in body["items"] if it["subject_id"] == owner["user_id"]]
    assert len(hours) == 2
    starts = sorted(it["hour_start"] for it in hours)
    assert starts[1] - starts[0] == 3600
    for start in starts:
        # 桶起点必须落在上海自然小时边界（分/秒为 0）且与事件所属小时一致
        local = datetime.fromtimestamp(start, SHANGHAI)
        assert (local.minute, local.second, local.microsecond) == (0, 0, 0)
        assert start % 3600 == 0
    assert starts[0] == int(base.timestamp()) - int(base.timestamp()) % 3600
    # 桶内合计只含本小时的事件（逐小时拆分，不是完整任务总额）
    charges = {it["hour_start"]: it["charge_nano_cny"] for it in hours}
    assert charges[starts[0]] == "1000000000"
    assert charges[starts[1]] == "2000000000"
    # 跨小时任务在任务视图仍是一行（完整任务金额）
    task_body = _summary(c, **_range_qs(since, until, {"view": "task"}))
    task = [it for it in task_body["items"] if it["request_id"] == rid]
    assert len(task) == 1 and task[0]["charge_nano_cny"] == "3000000000"


# --------------------------------------------------------------------------- #
# 2. 同会话多任务 / session 不作任务键
# --------------------------------------------------------------------------- #
def test_same_session_multiple_tasks_are_separate_tasks():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    session = _next_session()
    occurred = datetime.now(timezone.utc) - timedelta(hours=2)
    for rid, nano in (("req_a", 1_000_000_000), ("req_b", 2_000_000_000),
                      ("req_c", 3_000_000_000)):
        _insert_event(subject_type="owner", subject_id=owner["user_id"],
                      user_id=owner["user_id"], request_id=rid,
                      session_id=session, occurred_at=occurred,
                      charge_nano=nano)
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    # 一次会话 3 个任务 ≠ 1 个任务（session_id 不作任务键）
    assert hour["call_count"] == 3
    assert hour["task_count"] == 3
    assert sorted(t["request_id"] for t in hour["tasks"]) == [
        "req_a", "req_b", "req_c"]
    assert all(t["session_id"] == session for t in hour["tasks"])
    assert hour["charge_nano_cny"] == str(6_000_000_000)


def test_cross_hour_task_single_row_in_task_view():
    """跨小时任务：小时视图按小时拆分，任务视图保留一项（不重复叠加）。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range(hours_ago_start=6)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    rid = "req_cross_hour"
    session = _next_session()
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid, session_id=session,
                  occurred_at=base, charge_nano=1_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid, session_id=session,
                  occurred_at=base + timedelta(hours=2),
                  charge_nano=2_000_000_000)
    hour_body = _summary(c, **_range_qs(since, until, {"view": "hour"}))
    hours = [it for it in hour_body["items"]
             if it["subject_id"] == owner["user_id"]]
    assert len(hours) == 2
    assert sum(it["call_count"] for it in hours) == 2
    # 每个小时只计本小时发生的事件（不是完整任务总额）
    assert sorted(it["charge_nano_cny"] for it in hours) == [
        "1000000000", "2000000000"]
    task_body = _summary(c, **_range_qs(since, until,
                                        {"view": "task"}))
    tasks = [it for it in task_body["items"] if it["request_id"] == rid]
    assert len(tasks) == 1
    assert tasks[0]["call_count"] == 2
    assert tasks[0]["charge_nano_cny"] == "3000000000"
    assert task_body["totals"]["charge_nano_cny"] == "3000000000"


# --------------------------------------------------------------------------- #
# 3. 分页：稳定分组键、跨页无重无漏
# --------------------------------------------------------------------------- #
def test_summary_pagination_across_groups_no_dup_no_loss():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range(hours_ago_start=72)
    base = datetime.now(timezone.utc) - timedelta(hours=30)
    created = []
    for i in range(7):
        rid = "req_page_%02d" % i
        created.append(rid)
        _insert_event(subject_type="owner", subject_id=owner["user_id"],
                      user_id=owner["user_id"], request_id=rid,
                      session_id=_next_session(),
                      occurred_at=base + timedelta(hours=i),
                      charge_nano=1_000_000_000 * (i + 1))
    for view in ("hour", "task"):
        seen = []
        cursor = None
        pages = 0
        while True:
            params = _range_qs(since, until, {"view": view, "limit": 2})
            if cursor:
                params["cursor"] = cursor
            body = _summary(c, **params)
            pages += 1
            seen.extend(it.get("request_id") or it.get("group_key")
                        for it in body["items"])
            cursor = body["next_cursor"]
            assert pages < 20
            if not cursor:
                break
        if view == "task":
            got = sorted(it for it in seen if it and it.startswith("req_page"))
            assert got == sorted(created), (view, seen)
        else:
            # 小时视图：每行一个 (小时, 主体)，总数 = 7 个不同小时
            assert len(seen) == 7
            assert len(set(seen)) == 7
    # charge_desc 排序：费用降序（谁在消耗）
    body = _summary(c, **_range_qs(since, until, {"view": "task",
                                                  "sort": "charge_desc"}))
    charges = [int(it["charge_nano_cny"]) for it in body["items"]]
    assert charges == sorted(charges, reverse=True)


def test_stale_cursor_semantics_and_invalid_sort_rejected():
    c, _owner = _owner_client()
    since, until = _range()
    r = c.get("/api/admin/v1/billing/usage-summary?sort=weird")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    r = c.get("/api/admin/v1/billing/usage-summary?view=weird")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# 4. unpriced / void / 超大金额 / 舍入
# --------------------------------------------------------------------------- #
def test_unpriced_counted_amount_unknown_never_zero():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_priced",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=1_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_unpriced",
                  session_id=_next_session(), occurred_at=occurred,
                  status="unpriced", unpriced_reason="no_active_price_book")
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    assert hour["call_count"] == 2
    assert hour["unpriced_call_count"] == 1
    # 未计价金额保持未知：合计只含 priced，且显式标注 unknown
    assert hour["charge_nano_cny"] == "1000000000"
    assert body["totals"]["unpriced_call_count"] == 1
    assert body["totals"]["unpriced_amount"] == "unknown"
    assert "unpriced_count" not in body["totals"]
    task = [t for t in hour["tasks"] if t["request_id"] == "req_unpriced"][0]
    assert task["unpriced_call_count"] == 1
    assert task["charge_nano_cny"] == "0"


def test_void_events_excluded_everywhere():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(minutes=30)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_live",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=5_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_void",
                  session_id=_next_session(), occurred_at=occurred,
                  status="void", charge_nano=7_000_000_000)
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    assert hour["call_count"] == 1
    assert hour["task_count"] == 1
    assert hour["charge_nano_cny"] == "5000000000"
    assert all(t["request_id"] != "req_void" for t in hour["tasks"])
    assert body["totals"]["void_excluded_count"] == 1
    assert body["totals"]["charge_nano_cny"] == "5000000000"


def test_large_amounts_are_decimal_strings_on_wire():
    """>2^53 的 nano（JS Number 会失真）在 wire 上必须是十进制字符串。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    big = 9_007_199_254_740_993  # 2^53 + 1
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_big",
                  session_id=_next_session(),
                  occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  charge_nano=big, provider_cost_nano=big)
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    assert hour["charge_nano_cny"] == str(big)
    assert isinstance(hour["charge_nano_cny"], str)
    assert hour["charge_cny"] == "9007199.25"
    assert body["totals"]["charge_nano_cny"] == str(big)


def test_rounding_sums_nanos_then_rounds_once():
    """逐条舍入再相加 ≠ 先整和再舍入；出口必须与后者一致。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    # 3 × 0.005 CNY：逐条舍入会得 0.01×3 = 0.03，正确合计 = 0.015 → 0.02
    for i, rid in enumerate(("req_r1", "req_r2", "req_r3")):
        _insert_event(subject_type="owner", subject_id=owner["user_id"],
                      user_id=owner["user_id"], request_id=rid,
                      session_id=_next_session(), occurred_at=occurred,
                      charge_nano=5_000_000)
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    assert hour["charge_nano_cny"] == "15000000"
    assert hour["charge_cny"] == "0.02"
    assert body["totals"]["charge_cny"] == "0.02"
    # 负向（冲正类）金额 half away from zero
    assert app_mod._admin_v1_cny_from_nano(-1_234_500_000) == "-1.23"
    assert app_mod._admin_v1_cny_from_nano(-1_235_000_000) == "-1.24"


def test_late_event_updates_summary_in_original_hour():
    """晚到事件（received_at 远晚于 occurred_at）刷新后进入原小时汇总。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=5)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_late",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=4_000_000_000,
                  received_at=occurred + timedelta(hours=4, minutes=59))
    body = _summary(c, **_range_qs(since, until))
    hour = [it for it in body["items"]
            if it["subject_id"] == owner["user_id"]][0]
    assert hour["charge_nano_cny"] == "4000000000"
    assert datetime.fromtimestamp(hour["hour_start"], SHANGHAI).hour == \
        datetime.fromtimestamp(occurred.timestamp(), SHANGHAI).hour


def test_summary_does_not_double_count_ledger_entries():
    """费用统计只读 ai_usage_events：同额账本条目不得二次累计。"""
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    nano = 6_000_000_000
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_ledger",
                  session_id=_next_session(),
                  occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  charge_nano=nano)
    acct = billing_store.create_billing_account(owner["user_id"])
    before = _summary(c, **_range_qs(since, until))
    # 追加一条同额人工调账条目（模拟「账本里也有一笔」）
    billing_store.append_ledger_entry(
        acct["account_id"], "manual_adjustment", nano,
        "review_ledger_%s" % uuid.uuid4().hex[:12], reason="review double",
        metadata={"simulated": False})
    after = _summary(c, **_range_qs(since, until))
    assert before["totals"]["charge_nano_cny"] == str(nano)
    assert after["totals"]["charge_nano_cny"] == str(nano)
    assert after["totals"]["ledger_included"] is False
    # 账本出口仍然如实展示自己的金额（两套账分开）
    ledger = c.get("/api/admin/v1/billing/ledger?limit=100").get_json()
    assert any(int(it["amount_nano_cny"]) == nano for it in ledger["items"])


def test_missing_request_id_grouped_per_principal_and_session():
    """request_id 缺失 →「历史未归属调用」，不把所有空值并成一次任务。"""
    c, owner = _owner_client()
    other = user_store.create_user("u2@x.com", "userpass99-abcdef",
                                   role="user")
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=2)
    # 同一主体两个会话各缺 request_id → 两行；另一主体再一行
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=None,
                  session_id="sess_hist_a", occurred_at=occurred,
                  charge_nano=1_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=None,
                  session_id="sess_hist_b", occurred_at=occurred,
                  charge_nano=2_000_000_000)
    _insert_event(subject_type="user", subject_id=other["user_id"],
                  user_id=other["user_id"], request_id=None,
                  session_id="sess_hist_c", occurred_at=occurred,
                  charge_nano=3_000_000_000)
    body = _summary(c, **_range_qs(since, until, {"view": "task"}))
    rows = [it for it in body["items"] if it.get("unattributed")]
    assert len(rows) == 3
    assert all(it["request_id"] is None for it in rows)
    assert all(it["task_label"] == "历史未归属调用" for it in rows)
    assert sorted(it["subject_id"] for it in rows) == sorted(
        [owner["user_id"], owner["user_id"], other["user_id"]])
    assert body["totals"]["task_count"] == 3
    # 小时视图同样按 (主体, 会话) 分别计数
    hour_body = _summary(c, **_range_qs(since, until, {"view": "hour"}))
    owner_hour = [it for it in hour_body["items"]
                  if it["subject_id"] == owner["user_id"]][0]
    assert owner_hour["task_count"] == 2


# --------------------------------------------------------------------------- #
# 5. 活跃任务无 usage（验收 3）+ 权威生命周期状态
# --------------------------------------------------------------------------- #
def test_inflight_task_without_usage_visible_in_task_view():
    c, owner = _owner_client()
    since, until = _range()
    rid = "req_brand_new"
    import budget_store
    budget_store.ensure_run_binding_pending(rid, "owner", owner["user_id"])
    body = _summary(c, **_range_qs(since, until, {"view": "task"}))
    rows = [it for it in body["items"] if it["request_id"] == rid]
    assert len(rows) == 1
    row = rows[0]
    assert row["lifecycle_only"] is True
    assert row["call_count"] == 0
    assert row["charge_nano_cny"] == "0"
    # 权威来源 = ai_run_bindings（pending：等待会话绑定）——不猜「进行中」
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=owner&subject_id=%s"
                   % (rid, owner["user_id"])).get_json()
    assert detail["status"] == "accepted"
    assert detail["status_source"] == "ai_run_bindings"
    assert detail["status_detail"] == "pending_session"
    assert detail["lifetime"]["call_count"] == 0


def test_lifecycle_status_only_from_authoritative_sources():
    c, owner = _owner_client()
    _seed_price_books()
    since, until = _range()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    # ① 只有 usage、无任何生命周期行 → unknown（绝不用最后一条 usage 猜）
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id="req_no_source",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=1_000_000_000)
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=req_no_source"
                   "&subject_type=owner&subject_id=%s"
                   % owner["user_id"]).get_json()
    assert detail["status"] == "unknown"
    assert detail["status_source"] is None
    # ② demo_runs finished → finished（demo 主体）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO demo_sessions (id, token_hash, created_at, "
                " expires_at) VALUES ('cap_review', 'tok_review', now(), "
                " now() + interval '1 day') ON CONFLICT (id) DO NOTHING")
            cur.execute(
                "INSERT INTO demo_runs (demo_run_id, capability_id, request_id,"
                " state, histopilot_session_id, accepted_at, expires_at, "
                " finished_at, updated_at) "
                "VALUES ('dmr_review', 'cap_review', 'req_demo_done', "
                " 'finished', 'sess_demo_done', now(), "
                " now() + interval '1 hour', now(), now())")
        conn.commit()
    finally:
        conn.close()
    _insert_event(subject_type="demo", subject_id="cap_review", user_id=None,
                  request_id="req_demo_done", session_id="sess_demo_done",
                  occurred_at=occurred, charge_nano=2_000_000_000)
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=req_demo_done"
                   "&subject_type=demo&subject_id=cap_review").get_json()
    assert detail["status"] == "finished"
    assert detail["status_source"] == "demo_runs"
    assert detail["identity"] == "Demo"
    assert detail["identity_source"] == "demo"


def test_task_detail_range_lifetime_and_outside_amounts():
    """展开看完整任务金额与范围外部分（不把完整总额重复叠加到每小时）。"""
    c, owner = _owner_client()
    _seed_price_books()
    occurred = datetime.now(timezone.utc) - timedelta(hours=10)
    rid = "req_span"
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid,
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=1_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rid,
                  session_id=_next_session(),
                  occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  charge_nano=4_000_000_000)
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    until = datetime.now(timezone.utc)
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=owner&subject_id=%s&since=%r&until=%r"
                   % (rid, owner["user_id"], since.timestamp(),
                      until.timestamp())).get_json()
    assert detail["in_range"]["charge_nano_cny"] == "4000000000"
    assert detail["lifetime"]["charge_nano_cny"] == "5000000000"
    assert detail["outside_range"]["charge_nano_cny"] == "1000000000"
    assert detail["in_range"]["call_count"] == 1
    assert detail["lifetime"]["call_count"] == 2
    assert detail["outside_range"]["call_count"] == 1
    assert detail["in_range"]["charge_cny"] == "4.00"
    assert detail["lifetime"]["charge_cny"] == "5.00"


# --------------------------------------------------------------------------- #
# 6. step A 预算投影（本任务已用 / 待结算预占 / 账户剩余额度及其类型）
# --------------------------------------------------------------------------- #
def test_task_budget_step_a_no_per_task_cap_and_account_types():
    c, owner = _owner_client()
    _seed_price_books()
    import spend_store
    spend_store.set_total_default(50_000_000_000, 1, updated_by="pytest")
    import user_store_pg
    user = user_store_pg.create_user_with_total_allowance(
        "budget@x.com", "userpass-budget-1", display_name="Budget")[0]
    rid = "req_budget"
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    _insert_event(subject_type="user", subject_id=user["user_id"],
                  user_id=user["user_id"], request_id=rid,
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=3_000_000_000)
    # 一条未结算 hold（同会话）→ 待结算预占（不并进已用）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO billing_holds (hold_id, call_id, subject_type, "
                " subject_id, installation_id, session_id, model, "
                " estimated_nano_cny, status, expires_at, reserved_nano_cny) "
                "VALUES ('hold_reviewbudget', %s, 'user', %s, 'inst_test', %s,"
                " 'deepseek-flash', 7000000, 'open', now() + interval '30 min',"
                " 7000000)",
                ("call_" + "b" * 32, user["user_id"],
                 "sess_hold_review"))
        conn.commit()
    finally:
        conn.close()
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=user&subject_id=%s&session_id=%s"
                   % (rid, user["user_id"], "sess_hold_review")).get_json()
    budget = detail["budget"]
    assert budget["spent_nano_cny"] == "3000000000"      # 本任务已用
    assert budget["reserved_nano_cny"] == "7000000"      # 待结算预占（独立字段）
    assert budget["open_holds_count"] == 1
    assert budget["reserved_scope"] == "session"
    # step A：不伪造单次上限（step B 才做真正的 per-task cap）
    assert budget["per_task_cap_nano_cny"] is None
    assert budget["per_task_cap_configured"] is False
    assert budget["note"] == "未设置单次上限，按账户剩余额度控制"
    assert budget["ledger_included"] is False
    # 账户剩余额度及其类型（注册用户 = 一次性总额度）
    account = detail["account"]
    assert account["limit_type"] == "user_total_allowance"
    assert account["limit_type_label"] == "注册用户一次性总额度"
    assert int(account["remaining_nano_cny"]) > 0
    assert isinstance(account["remaining_nano_cny"], str)
    # 主体身份投影（邮箱）
    _set_email(user["user_id"], "budget@x.com")
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=user&subject_id=%s"
                   % (rid, user["user_id"])).get_json()
    assert detail["identity"] == "budget@x.com"
    assert detail["identity_source"] == "email"


def test_task_budget_account_types_owner_and_demo():
    c, owner = _owner_client()
    _seed_price_books()
    bh.seed_spend_policies()
    import spend_store
    rider = "req_owner_acct"
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=owner["user_id"], request_id=rider,
                  session_id=_next_session(),
                  occurred_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                  charge_nano=1_000_000_000)
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=owner&subject_id=%s"
                   % (rider, owner["user_id"])).get_json()
    assert detail["account"]["limit_type"] == "owner_month_window"
    assert detail["account"]["limit_type_label"] == "Owner 月窗口额度"
    assert detail["budget"]["note"] == "未设置单次上限，按账户剩余额度控制"
    # Demo：周窗口（全站共享）
    rided = "req_demo_acct"
    _insert_event(subject_type="demo", subject_id="cap_acct", user_id=None,
                  request_id=rided, session_id=_next_session(),
                  occurred_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                  charge_nano=1_000_000_000)
    detail = c.get("/api/admin/v1/billing/task-detail?request_id=%s"
                   "&subject_type=demo&subject_id=cap_acct"
                   % rided).get_json()
    assert detail["account"]["limit_type"] == "demo_week_window"
    assert detail["account"]["limit_type_label"] == "Demo 周窗口额度（全站共享）"
    assert detail["identity"] == "Demo"


def test_task_detail_requires_owner_and_arguments():
    c, _owner = _owner_client()
    assert _client().get(
        "/api/admin/v1/billing/task-detail?subject_type=user&subject_id=x"
    ).status_code == 401
    r = c.get("/api/admin/v1/billing/task-detail?request_id=x")
    assert r.status_code == 400
    r = c.get("/api/admin/v1/billing/task-detail?subject_type=user"
              "&subject_id=x")
    assert r.status_code == 400  # 无 request_id 必须带 session_id
    r = c.get("/api/admin/v1/billing/usage-summary?since=bad")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# 7. 身份投影（review 一.1）：>50 用户 / Dogfood / 禁用 / Demo / 未知
# --------------------------------------------------------------------------- #
def _seed_identity_world():
    """建 60 个用户（含 dogfood / 禁用 / 已验证邮箱），返回关键 user_id。"""
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    users = []
    for i in range(55):
        u = user_store.create_user("bulk%02d@x.com" % i,
                                   "bulkpass%02d-abcdef" % i, role="user")
        _set_email(u["user_id"], "bulk%02d@x.com" % i)
        users.append(u)
    dogfood = user_store.create_user("dogfood@x.com", "dogfoodpass-abcdef",
                                     role="user", display_name="Dogfood")
    _set_email(dogfood["user_id"], "dogfood@x.com")
    user_store.set_user_account_kind(dogfood["user_id"], "dogfood")
    disabled = user_store.create_user("disabled@x.com", "disabledpass-abcdef",
                                      role="user", display_name="Disabled")
    _set_email(disabled["user_id"], "disabled@x.com")
    user_store.set_user_disabled(disabled["user_id"], True)
    login_only = user_store.create_user("loginonly@x.com",
                                        "loginonlypass-abcdef", role="user")
    return owner, users, dogfood, disabled, login_only


def test_identity_projection_covers_60_users_dogfood_disabled_demo_unknown():
    owner, users, dogfood, disabled, login_only = _seed_identity_world()
    c = _login(_client(), owner)
    _seed_price_books()
    since, until = _range(hours_ago_start=48)
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    # 覆盖：普通用户（>50 分页外的账号）、Dogfood、禁用账号、只有 login_id
    # 的账号、Demo、未知身份（user_id 不在 users 表）、无 user_id 的事件
    for uid, nano in ((users[54]["user_id"], 1_000_000_000),
                      (dogfood["user_id"], 2_000_000_000),
                      (disabled["user_id"], 3_000_000_000),
                      (login_only["user_id"], 4_000_000_000)):
        _insert_event(subject_type="user", subject_id=uid, user_id=uid,
                      request_id="req_%s" % uid, session_id=_next_session(),
                      occurred_at=occurred, charge_nano=nano)
    ghost = "usr_ghost_review"
    # 未知身份 = 行内没有 user_id 镜像（主体未就绪的历史行；ai_usage_events.
    # user_id 对 users 有外键，不存在「有 user_id 但查不到用户」的形态）
    _insert_event(subject_type="user", subject_id=ghost, user_id=None,
                  request_id="req_ghost", session_id=_next_session(),
                  occurred_at=occurred, charge_nano=5_000_000_000)
    _insert_event(subject_type="demo", subject_id="cap_ident", user_id=None,
                  request_id="req_demo_ident", session_id=_next_session(),
                  occurred_at=occurred, charge_nano=6_000_000_000)
    _insert_event(subject_type="owner", subject_id=owner["user_id"],
                  user_id=None, request_id="req_nouser",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=7_000_000_000)
    body = _summary(c, **_range_qs(since, until, {"view": "task", "limit": 50}))
    by_req = {it["request_id"]: it for it in body["items"]}
    assert by_req["req_%s" % users[54]["user_id"]]["identity"] == \
        "bulk54@x.com"
    assert by_req["req_%s" % users[54]["user_id"]]["identity_source"] == "email"
    assert by_req["req_%s" % dogfood["user_id"]]["identity"] == "dogfood@x.com"
    # 禁用账号照常显示邮箱（禁用不抹身份；identity_disabled 只进技术详情）
    assert by_req["req_%s" % disabled["user_id"]]["identity"] == \
        "disabled@x.com"
    assert by_req["req_%s" % disabled["user_id"]]["identity_disabled"] is True
    # 无邮箱 → login_id（与用户列表同规则）
    assert by_req["req_%s" % login_only["user_id"]]["identity"] == \
        "loginonly@x.com"
    assert by_req["req_%s" % login_only["user_id"]]["identity_source"] == \
        "login_id"
    # Demo 显示 Demo（不伪装成注册用户）
    assert by_req["req_demo_ident"]["identity"] == "Demo"
    assert by_req["req_demo_ident"]["identity_source"] == "demo"
    # 未知身份：显式降级，原始 ID 只在技术详情
    ghost_row = by_req["req_ghost"]
    assert ghost_row["identity"] is None
    assert ghost_row["identity_source"] == "unknown"
    assert ghost_row["identity_user_id"] is None
    assert ghost_row["subject_id"] == ghost
    assert ghost_row["identity_note"]
    no_user_row = by_req["req_nouser"]
    assert no_user_row["identity"] is None
    assert no_user_row["identity_source"] == "unknown"
    assert no_user_row["identity_user_id"] is None
    # 刷新（新请求）不依赖任何用户页访问：同一账号在冷启动即可见邮箱
    fresh = _summary(c, **_range_qs(since, until,
                                    {"view": "task", "limit": 50,
                                     "user_id": users[54]["user_id"]}))
    assert fresh["items"][0]["identity"] == "bulk54@x.com"


def test_identity_projection_on_usage_events_and_ledger_and_search():
    owner, users, _dog, disabled, _login_only = _seed_identity_world()
    c = _login(_client(), owner)
    _seed_price_books()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    target = users[7]
    _insert_event(subject_type="user", subject_id=target["user_id"],
                  user_id=target["user_id"], request_id="req_search",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=1_500_000_000)
    _insert_event(subject_type="user", subject_id=users[8]["user_id"],
                  user_id=users[8]["user_id"], request_id="req_other",
                  session_id=_next_session(), occurred_at=occurred,
                  charge_nano=2_500_000_000)
    # 明细接口：身份由服务端投影（不依赖用户页缓存）
    items = c.get("/api/admin/v1/billing/usage-events?limit=50").get_json()[
        "items"]
    row = [it for it in items if it["request_id"] == "req_search"][0]
    assert row["identity"] == "bulk07@x.com"
    assert row["identity_source"] == "email"
    # 邮箱搜索（子串、大小写不敏感）
    page = c.get("/api/admin/v1/billing/usage-events"
                 "?email=BULK07@x.com").get_json()
    assert [it["request_id"] for it in page["items"]] == ["req_search"]
    # 搜索无结果 → 空页（不退化成全表）
    empty = c.get("/api/admin/v1/billing/usage-events"
                  "?email=nobody@nowhere.invalid").get_json()
    assert empty["items"] == []
    # 汇总接口同样支持邮箱搜索
    since, until = _range()
    body = _summary(c, **_range_qs(since, until,
                                   {"email": "bulk07@x.com", "view": "task"}))
    assert [it["request_id"] for it in body["items"]] == ["req_search"]
    assert body["items"][0]["identity"] == "bulk07@x.com"
    # 账本：身份来自 account_id → billing_accounts.user_id
    acct = billing_store.create_billing_account(target["user_id"])
    billing_store.append_ledger_entry(
        acct["account_id"], "manual_adjustment", 1_500_000_000,
        "review_ledger_ident", reason="review", metadata={})
    ledger = c.get("/api/admin/v1/billing/ledger?limit=50").get_json()
    led = [it for it in ledger["items"]
           if it["account_id"] == acct["account_id"]][0]
    assert led["user_id"] == target["user_id"]
    assert led["identity"] == "bulk07@x.com"
    led_search = c.get("/api/admin/v1/billing/ledger"
                       "?email=bulk07@x.com").get_json()
    assert all(it["account_id"] == acct["account_id"]
               for it in led_search["items"])
    led_disabled = c.get("/api/admin/v1/billing/ledger"
                         "?email=disabled@x.com").get_json()
    assert led_disabled["items"] == []


# --------------------------------------------------------------------------- #
# 8. 时间范围与默认值
# --------------------------------------------------------------------------- #
def test_range_default_is_last_seven_shanghai_days():
    c, _owner = _owner_client()
    body = _summary(c)
    assert body["range"]["default_applied"] is True
    assert body["range"]["timezone"] == "Asia/Shanghai"
    since = datetime.fromtimestamp(body["range"]["since"], SHANGHAI)
    assert (since.hour, since.minute, since.second) == (0, 0, 0)
    now_local = datetime.now(SHANGHAI)
    assert (now_local.date() - since.date()).days == 6
    # 超出 90 天护栏 → 400（不静默全量）
    r = c.get("/api/admin/v1/billing/usage-summary?since=0&until=%r"
              % (datetime.now(timezone.utc).timestamp() + 1))
    assert r.status_code == 400
