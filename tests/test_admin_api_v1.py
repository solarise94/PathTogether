# -*- coding: utf-8 -*-
"""PR3b：Admin API v1 只读子集测试（docs/admin-billing-plugin-implementation-plan.md
§9/§10/§14.1）。

json 模式（无 PG）：
  - 全部端点 owner 门控：匿名 401 / user 403 / **owner 预览态 403**
    （与 PR3a /admin 宿主页同口径，§14.1「preview subject 不能访问 admin」）；
  - json/dual fail-closed：billing 系 + turn-budgets → 503 pg_backend_required
    （稳定 code，不降级进程内数据）；overview 分段标记（billing/turn_budget
    available:false，用户段可用）；users 附属字段 null + billing_available:false；
  - audit 分页 + detail 出口脱敏（敏感键丢弃、idempotency_key 后缀）；
  - users 分页/搜索/筛选 + login ID 只出掩码（原始账号不回显）。

PG 模式（RUN_PG_TESTS=1）：
  - overview「双额度」语义：turn_budget（对话额度）与 billing（金额余额）
    两段同时可用且字段互不混淆；
  - usage-events / ledger / users / audit 的 cursor 分页正确性（无重无漏、
    末页 next_cursor=null）与筛选（model/user_id/status；unpriced 单独过滤，
    金额保持 null 不混 0 元）；
  - 未开户 account:null（不伪造 0 余额）；开户后余额 = ledger 合计；
  - provider balance refresh：mock HTTP（FakeRequests）覆盖成功写快照 / 4xx /
    网络失败 / Decimal 非法 / 不写伪造零余额 / 60s 节流 / key 不进响应。

运行：cd 项目根 && python3 -m pytest tests/test_admin_api_v1.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_api_v1.py -q）
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (FakeRequests, FakeResponse, csrf_client,  # noqa: E402
                         isolate_app)
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="billing/budget 数据路径需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402
    import billing_store  # noqa: E402
    import budget_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True（owner 门控有真实意义）+ 节流复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    # provider balance refresh 的进程内节流状态跨用例必须复位
    monkeypatch.setattr(app_mod, "_provider_balance_refresh_state",
                        {"last_ok_attempt": 0.0})
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _setup_users(n_extra=1):
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    users = []
    for i in range(n_extra):
        users.append(user_store.create_user(
            "user%d@x.com" % i, "userpass%02d-abcdef" % i, role="user",
            display_name="User %d" % i))
    return tuple([owner] + users)


# --------------------------------------------------------------------------- #
# 敏感字段红线扫描（§9：递归键名 + 值形态）
# --------------------------------------------------------------------------- #
_SENSITIVE_KEY_FRAGMENTS = (
    "password", "api_key", "apikey", "token", "secret", "credential",
    "fingerprint",
)


def scan_sensitive(obj, violations=None, path="$"):
    """递归扫描响应体：敏感键名 / api-key 形态值 / Fernet 密文形态值。

    ``*_tokens``（token 计数列，如 cache_hit_input_tokens）是计量字段不是
    凭据——对 "token" 片段白名单，其余片段（password/api_key/secret/...）
    不放宽。
    """
    if violations is None:
        violations = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            is_token_count = kl == "tokens" or kl.endswith("_tokens")
            for frag in _SENSITIVE_KEY_FRAGMENTS:
                if frag in kl and not (frag == "token" and is_token_count):
                    violations.append("%s.%s（敏感键名）" % (path, k))
                    break
            scan_sensitive(v, violations, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            scan_sensitive(v, violations, "%s[%d]" % (path, i))
    elif isinstance(obj, str):
        if obj.startswith("sk-"):
            violations.append("%s = sk-***（API key 形态值）" % path)
        if obj.startswith("enc:"):
            violations.append("%s = enc:***（Fernet 密文形态值）" % path)
    return violations


def _endpoints(owner_id):
    """全部 v1 端点（accounts 路径用 owner id 占位）。"""
    return [
        "/api/admin/v1/overview",
        "/api/admin/v1/users",
        "/api/admin/v1/billing/accounts/" + owner_id,
        "/api/admin/v1/billing/usage-events",
        "/api/admin/v1/billing/ledger",
        "/api/admin/v1/billing/provider-balance",
        "/api/admin/v1/billing/provider-balance/refresh",
        "/api/admin/v1/audit",
        "/api/admin/v1/turn-budgets",
        # 批次 B：金额 policy/window 只读出口（owner-only + PG-only）
        "/api/admin/v1/spend/policies",
        "/api/admin/v1/spend/windows/current",
        "/api/admin/v1/spend/reconcile",
    ]


# --------------------------------------------------------------------------- #
# 1. owner 门控（匿名 / user / preview）
# --------------------------------------------------------------------------- #
def test_anonymous_gets_401_on_every_endpoint():
    owner, _u = _setup_users()
    for path in _endpoints(owner["user_id"]):
        r = _client().get(path)
        assert r.status_code == 401, "%s -> %s" % (path, r.status_code)
        assert r.get_json()["error"] == "auth_required"


def test_user_gets_403_on_every_endpoint():
    owner, usera = _setup_users()
    c = _login(_client(), usera)
    for path in _endpoints(owner["user_id"]):
        r = c.get(path) if "refresh" not in path else c.post(path)
        assert r.status_code == 403, "%s -> %s" % (path, r.status_code)


def test_preview_owner_rejected_on_every_endpoint():
    """owner 预览成 user：actor 虽是 owner，管理 API 一律 403（§14.1）。"""
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/preview/start",
                  json={"user_id": usera["user_id"]}).status_code == 200
    for path in _endpoints(owner["user_id"]):
        r = c.get(path) if "refresh" not in path else c.post(path)
        assert r.status_code == 403, "%s -> %s" % (path, r.status_code)
        if "refresh" not in path:  # POST refresh 在预览写闸先被拦（无错误信封）
            body = r.get_json()
            assert body["error"]["code"] in ("preview_forbidden", "preview_readonly")


# --------------------------------------------------------------------------- #
# 2. json/dual 后端 fail-closed 语义
# --------------------------------------------------------------------------- #
def test_json_billing_endpoints_pg_backend_required():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用反向用例（PG 模式跑正向路径）")
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    for path in ("/api/admin/v1/billing/usage-events",
                 "/api/admin/v1/billing/ledger",
                 "/api/admin/v1/billing/provider-balance",
                 "/api/admin/v1/billing/provider-balance/refresh",
                 "/api/admin/v1/billing/accounts/" + owner["user_id"],
                 "/api/admin/v1/turn-budgets",
                 "/api/admin/v1/spend/policies",
                 "/api/admin/v1/spend/windows/current",
                 "/api/admin/v1/spend/reconcile"):
        r = c.get(path) if "refresh" not in path else c.post(path)
        assert r.status_code == 503, "%s -> %s" % (path, r.status_code)
        assert r.get_json()["error"]["code"] == "pg_backend_required"


def test_json_overview_segmented_availability():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用分段标记用例")
    owner, _u = _setup_users()
    r = _login(_client(), owner).get("/api/admin/v1/overview")
    assert r.status_code == 200
    body = r.get_json()
    # 用户段任何后端都真实可用
    assert body["users"]["total"] >= 2
    assert body["users"]["active"] >= 2
    # billing / turn_budget 分段标记（不整体 503，也不伪造数据）；
    # turn_budget 段另带 legacy=True（批次 F：turn 消费闸退役，冻结历史）
    for seg in ("billing", "turn_budget"):
        assert body[seg]["available"] is False
        assert body[seg]["code"] == "pg_backend_required"
    assert body["turn_budget"]["legacy"] is True
    assert scan_sensitive(body) == []


def test_json_users_list_null_billing_fields():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用附属字段用例")
    owner, _ua, _ub = _setup_users(2)
    r = _login(_client(), owner).get("/api/admin/v1/users")
    assert r.status_code == 200
    body = r.get_json()
    assert body["billing_available"] is False
    assert len(body["items"]) == 3
    for item in body["items"]:
        assert item["billing"] is None
        # 批次 F：turn_used/turn_limit 字段已随 turn 消费闸退役删除
        assert "turn_used" not in item and "turn_limit" not in item
        assert item["last_ai_call_at"] is None
        assert item["campaign"] is None and item["source"] is None


# --------------------------------------------------------------------------- #
# 3. users 列表（两后端通用部分）
# --------------------------------------------------------------------------- #
def test_users_pagination_search_and_filters():
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner")
    for i in range(5):
        user_store.create_user("u%d@x.com" % i, "userpass%02d-abcdef" % i,
                               role="user", display_name="Member %d" % i)
    c = _login(_client(), owner)

    # 分页（offset 游标）：limit=3 两页收齐，无重无漏
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/users?limit=3"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["items"]) <= 3
        seen.extend(item["user_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 6 and len(set(seen)) == 6
    # 创建时间升序
    r0 = c.get("/api/admin/v1/users?limit=100").get_json()
    times = [item["created_at"] for item in r0["items"]]
    assert times == sorted(times)

    # 搜索（login id / 显示名）
    r = c.get("/api/admin/v1/users?q=member 2").get_json()
    assert [item["display_name"] for item in r["items"]] == ["Member 2"]
    r = c.get("/api/admin/v1/users?q=u3@x.com").get_json()
    # 命中按原始 login_id，但输出只给掩码
    assert len(r["items"]) == 1
    assert r["items"][0]["login_id_masked"] == "u***@x.com"
    raw = json.dumps(r)
    assert "u3@x.com" not in raw  # 原始账号绝不回显

    # enabled / ai_access 筛选
    target = user_store.get_user_by_login_id("u1@x.com")
    user_store.set_user_disabled(target["user_id"], True)
    r = c.get("/api/admin/v1/users?enabled=false").get_json()
    assert [item["user_id"] for item in r["items"]] == [target["user_id"]]
    r = c.get("/api/admin/v1/users?ai_access=false&limit=100").get_json()
    assert all(item["ai_access"] is False for item in r["items"])
    assert scan_sensitive(r) == []


# --------------------------------------------------------------------------- #
# 4. audit 分页 + 出口脱敏（两后端）
# --------------------------------------------------------------------------- #
def _write_audit_events(n=5):
    for i in range(n):
        share_store.record_audit(
            action="test.admin_v1.%d" % i,
            actor_user_id=None, actor_role="owner",
            target_type="probe", target_id="p%d" % i, slide=None,
            detail={
                "reason": "probe %d" % i,
                "idempotency_key": "adjust_%016d" % i,
                "password": "SHOULD-DROP",
                "api_key": "sk-should-drop",
                "invite_token": "tok-should-drop",
                "ip_prefix_hash": "should-drop",
                "nested": {"secret": "x", "keep": 1},
            })


def test_audit_pagination_and_sanitization():
    owner, _u = _setup_users()
    _write_audit_events(5)
    c = _login(_client(), owner)
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/audit?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["items"]) <= 2
        seen.extend(item["action"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    # 最新在前、无重无漏
    assert len(seen) == 5 and len(set(seen)) == 5
    assert seen[0] == "test.admin_v1.4"
    # action 精确筛选
    r = c.get("/api/admin/v1/audit?action=test.admin_v1.0").get_json()
    assert [item["action"] for item in r["items"]] == ["test.admin_v1.0"]
    # detail 脱敏：敏感键整键丢弃，idempotency_key 只留后 8 位
    r = c.get("/api/admin/v1/audit?action=test.admin_v1.0").get_json()
    detail = r["items"][0]["detail"]
    assert detail["reason"] == "probe 0"
    assert detail["idempotency_key"] == ("adjust_%016d" % 0)[-8:]  # 仅后 8 位
    assert "password" not in detail and "api_key" not in detail
    assert "invite_token" not in detail and "ip_prefix_hash" not in detail
    assert detail["nested"] == {"keep": 1}
    raw = json.dumps(r)
    for banned in ("SHOULD-DROP", "sk-should-drop", "tok-should-drop"):
        assert banned not in raw
    assert scan_sensitive(r) == []


# --------------------------------------------------------------------------- #
# 5. PG：overview 双额度 / usage / ledger / account
# --------------------------------------------------------------------------- #
def _seed_event(name, subject_type, subject_id, *, hours_back=1,
                session_id=None, request_id=None, mutate=None, occurred=None):
    """经 billing_store.ingest_usage_event 直接入库（绕开插件 JWT 通道）。

    绑定权威行（reservation）后按 fixture 事件投递；``mutate`` 可在投递前
    改事件（构造 unpriced 等）。``occurred`` 显式指定 aware datetime（缺省
    now-hours_back）。返回 (event_id, status)。
    """
    event = bh.load_event(name)
    now = datetime.now(timezone.utc)
    occurred = occurred if occurred is not None \
        else now - timedelta(hours=hours_back)
    event["occurred_at"] = occurred.isoformat().replace("+00:00", "Z")
    event["enqueued_at"] = (occurred + timedelta(seconds=2)
                            ).isoformat().replace("+00:00", "Z")
    event["subject_type"] = subject_type
    event["subject_id"] = subject_id
    # body 的 user_id 是 assertion（§7.2 第 ④ 步）：owner/user 主体必须一致；
    # demo 无用户镜像
    event["user_id"] = subject_id if subject_type != "demo" else None
    event["session_id"] = session_id or ("sess_" + uuid.uuid4().hex[:20])
    if request_id:
        event["request_id"] = request_id
    if mutate:
        mutate(event)
    bh.bind_reservation(event["request_id"], event["session_id"],
                        subject_type, subject_id)
    result = billing_store.ingest_usage_event(
        event, installation_id="inst_test", now=now)
    return result["event_id"], result["status"]


def _make_arithmetic_bad(event):
    """构造算术不符（total != hit+miss+output）→ unpriced(arithmetic_mismatch)。"""
    event["total_tokens"] = (event["cache_hit_input_tokens"]
                             + event["cache_miss_input_tokens"]
                             + event["output_tokens"] + 7)


@PG
def test_overview_dual_quota_semantics():
    """概览必须同时含「对话额度」（turn）与「金额余额」（billing）两段（§10.1）。"""
    import billing_pricing
    owner, usera = _setup_users()
    bh.seed_price_books_with_history()
    # 事件固定在「今日（Asia/Shanghai）零点后 1 秒」：model_calls_today 的
    # 窗口是计价时区当日零点起，若用 now-1h/2h 平移，深夜（00:00–02:00）
    # 跑测试时事件会落在昨日 → 计数 0（曾由此产生跨零点偶发失败）。零点后
    # 1 秒恒 ≤ now 且不超 5 分钟超前容忍，30 天窗口内，任何时段确定性成立。
    today_local = datetime.now(
        tz=billing_pricing.PRICING_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=1)
    _seed_event("01_owner_priced_flash_peak.json", "owner", owner["user_id"],
                occurred=today_local)
    _seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                usera["user_id"], occurred=today_local + timedelta(minutes=30))
    # 周期在事件之后才创建（bind_reservation 首次触发）——把起点回拨到事件
    # 之前，使「本周期」窗口覆盖种子事件（金额聚合断言才有意义）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_budget_periods "
                        "SET started_at = now() - interval '1 day'")
        conn.commit()
    finally:
        conn.close()
    r = _login(_client(), owner).get("/api/admin/v1/overview")
    assert r.status_code == 200
    body = r.get_json()
    # 用户段
    assert body["users"]["total"] == 2
    assert body["users"]["active"] == 2
    # 对话额度段（turn budget）
    turn = body["turn_budget"]
    assert turn["available"] is True
    assert isinstance(turn["platform"]["total"], int)
    assert isinstance(turn["platform"]["limit"], int)
    assert turn["period_id"] is not None
    # 金额余额段（billing）
    billing = body["billing"]
    assert billing["available"] is True
    assert isinstance(billing["model_calls_today"], int)
    assert billing["model_calls_today"] >= 2
    # §5 v0.3（P2）：金额字段一律十进制字符串（wire 禁 JSON number）
    assert isinstance(billing["provider_cost_nano_cny"], str)
    assert isinstance(billing["charge_nano_cny"], str)
    assert int(billing["provider_cost_nano_cny"]) > 0
    assert billing["cache_hit_ratio"] is None or 0 <= billing["cache_hit_ratio"] <= 1
    assert billing["provider_balance_snapshot"] is None  # 尚无快照
    assert isinstance(billing["unpriced_count"], int)
    assert isinstance(billing["ingestion_lag_seconds_max"], float)
    # 两段键不互相混淆（turn 无金额、billing 无次数上限语义）
    assert "charge_nano_cny" not in turn
    assert "platform_turn_limit" not in billing
    assert scan_sensitive(body) == []


@PG
def test_usage_events_pagination_and_filters():
    owner, usera = _setup_users()
    bh.seed_price_books_with_history()
    ids = []
    ids.append(_seed_event("01_owner_priced_flash_peak.json", "owner",
                           owner["user_id"], hours_back=5)[0])
    ids.append(_seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                           usera["user_id"], hours_back=4)[0])
    ids.append(_seed_event("03_user_priced_vision_exp_peak.json", "user",
                           usera["user_id"], hours_back=3)[0])
    ids.append(_seed_event("06_user_priced_flash_no_provider_request_id.json",
                           "user", usera["user_id"], hours_back=2,
                           mutate=_make_arithmetic_bad)[0])
    c = _login(_client(), owner)

    # keyset 分页：limit=2 三页收齐，时间降序，无重无漏
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/billing/usage-events?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        seen.extend(item["event_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids)
    r0 = c.get("/api/admin/v1/billing/usage-events?limit=100").get_json()
    times = [item["occurred_at"] for item in r0["items"]]
    assert times == sorted(times, reverse=True)

    # status 筛选：unpriced 单独可过滤，不与 priced 混排
    r = c.get("/api/admin/v1/billing/usage-events?status=unpriced").get_json()
    assert len(r["items"]) == 1
    assert r["items"][0]["status"] == "unpriced"
    assert r["items"][0]["unpriced_reason"] == "arithmetic_mismatch"
    # unpriced 金额是 null（未计价 ≠ 0 元，§10.4 红线）
    assert r["items"][0]["provider_cost_nano_cny"] is None
    assert r["items"][0]["charge_nano_cny"] is None
    r = c.get("/api/admin/v1/billing/usage-events?status=priced").get_json()
    assert all(item["status"] == "priced" for item in r["items"])
    assert all(item["charge_nano_cny"] is not None for item in r["items"])

    # model / user_id 筛选
    r = c.get("/api/admin/v1/billing/usage-events?model=deepseek-v4-pro").get_json()
    assert len(r["items"]) == 1 and r["items"][0]["model"] == "deepseek-v4-pro"
    r = c.get("/api/admin/v1/billing/usage-events?user_id=%s"
              % usera["user_id"]).get_json()
    assert len(r["items"]) == 3
    assert all(item["user_id"] == usera["user_id"] for item in r["items"])

    # 非法 status → 400
    r = c.get("/api/admin/v1/billing/usage-events?status=whatever")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    # 损坏 cursor → 当作第一页（不 500）
    r = c.get("/api/admin/v1/billing/usage-events?cursor=%% %%bad")
    assert r.status_code == 200
    assert scan_sensitive(r.get_json()) == []


@PG
def test_ledger_pagination_readonly():
    owner, _u = _setup_users()
    account = billing_store.create_billing_account(owner["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 5_000_000_000, "adj:grant-1",
        reason="赠送", actor_user_id=owner["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "topup", 2_000_000_000, "adj:topup-1",
        reason="充值")
    billing_store.append_ledger_entry(
        account["account_id"], "manual_adjustment", -500_000_000,
        "adj:correct-1", reason="冲正（只追加新条目，不编辑历史）")
    c = _login(_client(), owner)
    seen, cursor = [], None
    while True:
        url = "/api/admin/v1/billing/ledger?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        body = c.get(url).get_json()
        seen.extend(item["entry_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 3 and len(set(seen)) == 3
    r0 = c.get("/api/admin/v1/billing/ledger?limit=100").get_json()
    amounts = [item["amount_nano_cny"] for item in r0["items"]]
    # §5 v0.3（P2）：金额为十进制字符串，不做数值比较
    assert "-500000000" in amounts and "5000000000" in amounts
    assert all(isinstance(a, str) for a in amounts)
    assert scan_sensitive(r0) == []


@PG
def test_account_null_when_not_opened_and_balance_when_opened():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # 未开户：account:null（不伪造 0 余额）
    r = c.get("/api/admin/v1/billing/accounts/" + owner["user_id"])
    assert r.status_code == 200
    assert r.get_json() == {"user_id": owner["user_id"],
                            "account": None, "balance_nano": None}
    # 开户 + 两条 entry：余额 = ledger 合计
    account = billing_store.create_billing_account(owner["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 5_000_000_000, "adj:grant-1",
        reason="赠送")
    billing_store.append_ledger_entry(
        account["account_id"], "topup", 2_000_000_000, "adj:topup-1",
        reason="充值")
    r = c.get("/api/admin/v1/billing/accounts/" + owner["user_id"]).get_json()
    assert r["account"]["account_id"] == account["account_id"]
    assert r["account"]["soft_spend_cap_nano"] is None
    # §5 v0.3（P2）：余额为十进制字符串
    assert r["balance_nano"] == "7000000000"
    assert isinstance(r["balance_nano"], str)
    # 用户不存在 → 404
    r = c.get("/api/admin/v1/billing/accounts/usr_missing000")
    assert r.status_code == 404


@PG
def test_users_row_joins_turn_billing_last_call(monkeypatch):
    # 本用例聚焦 users 行联结展示；关闭 PR6 模拟扣费，避免 _seed_event 的
    # priced 事件自动开户/入账改变下方「开户 + 余额」断言的账面（模拟扣费
    # 语义由 tests/test_billing_sim_debit.py 覆盖）
    monkeypatch.setenv("BILLING_SIMULATED_DEBIT", "0")
    owner, usera = _setup_users(1)
    bh.seed_price_books_with_history()
    _seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                usera["user_id"])
    # 直接写一条 ai_budget_usage 行（usage_report 的 per_user 数据源）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_budget_usage (period_id, subject_type, "
                "subject_id, credential_source, accepted_turns, reserved_turns, "
                "updated_at) VALUES (%s,'user',%s,'platform',2,1,now()) "
                "ON CONFLICT DO NOTHING",
                (budget_store.get_current_period()["id"], usera["user_id"]))
        conn.commit()
    finally:
        conn.close()
    account = billing_store.create_billing_account(usera["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 3_000_000_000, "adj:grant-1",
        reason="赠送")

    r = _login(_client(), owner).get(
        "/api/admin/v1/users?q=user0@x.com").get_json()
    assert len(r["items"]) == 1
    item = r["items"][0]
    assert item["display_name"] == "User 0"
    assert item["login_id_masked"] == "u***@x.com"
    assert item["role"] == "user" and item["enabled"] is True
    assert item["registration_method"] == "manual"  # owner 直接创建
    # 批次 F：turn_used/turn_limit 已删（用量断言移至 turn-budgets 只读端点）
    assert "turn_used" not in item and "turn_limit" not in item
    assert item["billing"]["balance_nano"] == "3000000000"
    assert item["billing"]["soft_spend_cap_nano"] is None
    assert item["last_ai_call_at"] is not None
    assert item["campaign"] is None and item["source"] is None
    assert scan_sensitive(r) == []
    # owner 行未开户 → billing null（不伪造）
    r = _login(_client(), owner).get(
        "/api/admin/v1/users?q=owner@x.com").get_json()
    assert r["items"][0]["billing"] is None


@PG
def test_turn_budgets_readonly_shape():
    owner, _u = _setup_users()
    r = _login(_client(), owner).get("/api/admin/v1/turn-budgets")
    assert r.status_code == 200
    body = r.get_json()
    # 批次 F：只读 + legacy 标记（冻结历史）
    assert body["legacy"] is True
    assert "退役" in body["note"]
    assert body["period"]["id"] is not None
    assert "user_turn_limit" in body["limits"]
    for key in ("platform", "demo", "owner", "user_pool", "per_user", "own"):
        assert key in body["usage"]
    assert scan_sensitive(body) == []


@PG
def test_turn_budgets_write_endpoints_retired_410():
    """批次 F：PUT / new-period → 410 turn_budgets_retired + audit 尝试。"""
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.put("/api/admin/v1/turn-budgets", json={"platform_turn_limit": 99})
    assert r.status_code == 410
    assert r.get_json()["error"]["code"] == "turn_budgets_retired"
    assert "金额预算" in r.get_json()["error"]["message"]
    r2 = c.post("/api/admin/v1/turn-budgets/new-period", json={"confirm": True})
    assert r2.status_code == 410
    assert r2.get_json()["error"]["code"] == "turn_budgets_retired"
    # 退役尝试落 audit
    actions = [e.get("action") for e in
               app_mod.share_store.list_audit(limit=20)]
    assert "turn_budgets.retired_write" in actions
    # 周期行不受影响（写入口没了，冻结历史不动）
    assert budget_store.get_current_period()["platform_turn_limit"] == 30


@PG
def test_settings_runtime_endpoint_reads_and_writes_ai_safety():
    """批次 F：PUT /api/admin/v1/settings/runtime（五安全参数子集）。

    - GET settings 的 runtime 段改读 settings_store（ai_safety.*）；
    - 部分更新合法、未知/越界字段 400；
    - 写入 + audit（action=ai_safety.settings_update）同事务；
    - demo_enabled 生效（_demo_public_mode 换源后的读路径）。
    """
    import settings_store
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # 初始：GET runtime 段读新源（缺省回落常量）
    body = c.get("/api/admin/v1/settings").get_json()
    assert body["runtime"]["available"] is True
    assert body["runtime"]["limits"]["demo_enabled"] is False
    assert body["runtime"]["limits"]["demo_task_max_steps"] == 20
    assert body["runtime"]["limits"]["demo_max_concurrency"] == 2
    # 部分更新（允许子集）
    r = c.put("/api/admin/v1/settings/runtime", json={
        "demo_task_max_steps": 25, "demo_enabled": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    limits = r.get_json()["limits"]
    assert limits["demo_task_max_steps"] == 25
    assert limits["demo_enabled"] is True
    assert limits["demo_max_concurrency"] == 2  # 未提交项沿用
    # 读取换源生效
    assert settings_store.get_ai_safety_settings()["demo_task_max_steps"] == 25
    assert app_mod._demo_task_max_steps() == 25
    assert app_mod._demo_public_mode() is True
    # 校验：未知字段 / 非正整数 / 非布尔 / 空对象
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"no_such": 1}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_max_concurrency": 0}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_enabled": "yes"}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_max_concurrency": 1_000_001}).status_code == 400
    # audit 同事务落库
    actions = [e.get("action") for e in
               app_mod.share_store.list_audit(limit=20)]
    assert "ai_safety.settings_update" in actions


# --------------------------------------------------------------------------- #
# 6. PG：provider balance GET / refresh（mock HTTP）
# --------------------------------------------------------------------------- #
def _write_ai_config(monkeypatch, key="sk-official-key-123456",
                     kind="deepseek_official", base=None):
    cfg = {"provider_kind": kind}
    if key is not None:
        cfg["api_key"] = key
    if base:
        cfg["base_url"] = base
    p = Path(os.environ["SHARE_DATA_DIR"]) / "ai_config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


def _fake_requests(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


_BALANCE_OK = {
    "is_available": True,
    "balance_infos": [{
        "currency": "CNY", "total_balance": "110.00",
        "granted_balance": "10.00", "topped_up_balance": "100.00",
    }],
}


@PG
def test_provider_balance_get_empty_then_snapshot_after_refresh(monkeypatch):
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/billing/provider-balance")
    assert r.status_code == 200
    body = r.get_json()
    assert body["provider"] == "deepseek"
    assert body["snapshot"] is None and body["age_seconds"] is None

    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 200, r.get_data(as_text=True)
    snap = r.get_json()["snapshot"]
    # §5 v0.3（P2）：快照金额为十进制字符串
    assert snap["total_balance_nano"] == "110000000000"
    assert snap["granted_balance_nano"] == "10000000000"
    assert snap["topped_up_balance_nano"] == "100000000000"
    assert snap["is_available"] is True
    # key 绝不进响应（也不进请求 URL）
    assert "sk-official-key-123456" not in r.get_data(as_text=True)
    assert all("sk-" not in (call["path"] or "") for call in fake.calls)

    r = c.get("/api/admin/v1/billing/provider-balance").get_json()
    assert r["snapshot"]["snapshot_id"] == snap["snapshot_id"]
    assert r["age_seconds"] is not None and r["age_seconds"] < 60
    assert scan_sensitive(r) == []


@PG
def test_provider_balance_refresh_4xx_no_fake_zero(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=401,
                       body={"error": "bad key"})
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_rejected"
    # 失败不写伪造零余额
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None


@PG
def test_provider_balance_refresh_non_json_body(monkeypatch):
    """HTTP 200 但 body 不是 JSON → provider_error，不写快照。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register("GET", "/user/balance",
                  lambda b, q, h, k: FakeResponse(200, b"<html>not json</html>"))
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_error"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None


@PG
def test_provider_balance_refresh_network_failure(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.set_unreachable()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_unreachable"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None


@PG
def test_provider_balance_refresh_invalid_decimal(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    bad = {"is_available": True, "balance_infos": [{
        "currency": "CNY", "total_balance": "12.3456789012",  # >9 位小数
        "granted_balance": "0", "topped_up_balance": "0"}]}
    fake.register_json("GET", "/user/balance", status=200, body=bad)
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "invalid_balance_response"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None


@PG
def test_provider_balance_refresh_throttled(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    c = _login(_client(), owner)
    assert c.post("/api/admin/v1/billing/provider-balance/refresh").status_code == 200
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "refresh_throttled"


@PG
def test_provider_balance_refresh_not_configured(monkeypatch):
    owner, _u = _setup_users()
    _fake_requests(monkeypatch)
    c = _login(_client(), owner)
    # 无官方 key → 400（且不消耗节流窗口）
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "provider_not_configured"
    # generic provider 同样拒绝
    _write_ai_config(monkeypatch, kind="generic", base="http://127.0.0.1:8317/v1")
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "provider_not_configured"


# --------------------------------------------------------------------------- #
# 批次 B：金额 policy/window 只读出口（/api/admin/v1/spend/*）
# --------------------------------------------------------------------------- #
@PG
def test_spend_endpoints_readonly_owner_only():
    """PG：三端点 200；金额十进制字符串；窗口含 demo 周池 + 每用户月窗口；
    普通用户 403（批次 B 不做写 API——POST 不存在路由，Flask 405）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)

    r = c.get("/api/admin/v1/spend/policies")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enforcement_mode"] == "shadow"
    assert body["backend"] == "postgres"
    by_id = {p["policy_id"]: p for p in body["items"]}
    assert set(by_id) >= {"spp_demo_global", "spp_user_default", "spp_owner"}
    # nano 金额一律十进制字符串（§5 v0.3；独立 CNY→nano 换算断言：
    # 50 CNY = 50×1e9 = 50000000000，不从迁移常量自证）
    assert isinstance(by_id["spp_demo_global"]["limit_nano_cny"], str)
    assert by_id["spp_demo_global"]["limit_nano_cny"] == "50000000000"
    assert isinstance(by_id["spp_user_default"]["limit_nano_cny"], str)
    assert by_id["spp_user_default"]["limit_nano_cny"] == "20000000000"
    assert by_id["spp_owner"]["limit_nano_cny"] == "1000000000000"
    # 当前生效解析
    assert body["resolved"]["demo_global"]["policy_id"] == "spp_demo_global"
    assert body["resolved"]["user_default"]["policy_id"] == "spp_user_default"
    assert body["resolved"]["owner"]["policy_id"] == "spp_owner"

    r = c.get("/api/admin/v1/spend/windows/current")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enforcement_mode"] == "shadow"
    demo = body["demo"]
    assert demo["subject_type"] == "demo" and demo["subject_id"] == "demo_global"
    assert demo["limit_nano_snapshot"] == "50000000000"
    assert demo["policy_id"] == "spp_demo_global"
    assert demo["spent_nano_cny"] == "0" and demo["reserved_nano_cny"] == "0"
    assert demo["remaining_nano"] == "50000000000"
    assert demo["window_start"] < demo["window_end"]
    assert demo["status"] == "open"
    subjects = {(u["subject_type"], u["subject_id"]) for u in body["users"]}
    assert ("user", usera["user_id"]) in subjects
    assert ("owner", owner["user_id"]) in subjects
    user_row = next(u for u in body["users"]
                    if u["subject_id"] == usera["user_id"])
    assert user_row["policy_id"] == "spp_user_default"
    assert user_row["limit_nano_snapshot"] == "20000000000"
    owner_row = next(u for u in body["users"]
                     if u["subject_id"] == owner["user_id"])
    assert owner_row["policy_id"] == "spp_owner"
    assert owner_row["limit_nano_snapshot"] == "1000000000000"
    # 敏感字段红线扫描
    assert scan_sensitive(body) == []

    r = c.get("/api/admin/v1/spend/reconcile")
    assert r.status_code == 200
    body = r.get_json()
    assert body["checked"] >= 3  # demo 周池 + owner/用户月窗口
    assert body["drift_windows"] == 0
    item = body["items"][0]
    for key in ("expected_spent_nano", "actual_spent_nano", "spent_drift_nano",
                "expected_reserved_nano", "actual_reserved_nano",
                "reserved_drift_nano", "limit_nano_snapshot"):
        assert isinstance(item[key], str), key
    assert scan_sensitive(body) == []

    # 普通用户 403（owner 门控不因后端可用而放宽）
    rc = _login(_client(), usera)
    for path in ("/api/admin/v1/spend/policies",
                 "/api/admin/v1/spend/windows/current",
                 "/api/admin/v1/spend/reconcile"):
        assert rc.get(path).status_code == 403
    # 批次 B 无写 API：POST 未注册路由 → 405
    assert c.post("/api/admin/v1/spend/policies").status_code == 405


@PG
def test_spend_windows_current_reports_policy_missing_per_subject():
    """user_default 被禁用时：单主体降级为稳定 error 项，整页仍 200（管理页
    需要看到「谁没有有效策略」，而不是整页失败）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    _bh_connect = bh.connect()
    try:
        with _bh_connect.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_user_default'")
        _bh_connect.commit()
    finally:
        _bh_connect.close()
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/spend/windows/current")
    assert r.status_code == 200
    body = r.get_json()
    user_row = next(u for u in body["users"]
                    if u["subject_id"] == usera["user_id"])
    assert user_row["error"] == "spend_policy_missing"
    # demo/owner 策略未动，照常给窗口
    assert body["demo"]["policy_id"] == "spp_demo_global"
    assert next(u for u in body["users"]
                if u["subject_id"] == owner["user_id"])["policy_id"] == \
        "spp_owner"
