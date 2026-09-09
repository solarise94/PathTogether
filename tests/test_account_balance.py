# -*- coding: utf-8 -*-
"""GET /api/account/balance（Batch B，升级 Review 2026-09-09 §4.2）。

只读自助余额：
  - user = 一次性总额度口径（十进制字符串；无额度行 400
    spend_total_allowance_missing，绝不呈现为 ¥0）；
  - owner = 当前月窗口（peek 不建行：窗口未开按策略限额 + 0 用量呈现）；
  - 未登录 401；Cache-Control: no-store；subject 来自 effective 身份。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
import pytest  # noqa: E402

import user_store  # noqa: E402
import spend_store  # noqa: E402
import app as app_mod  # noqa: E402
import _billing_helpers as bh  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

UPLOAD_DIR = _bootstrap.UPLOAD_DIR
PW = "longpassword123"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True,
                clear_stores=True)
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True


def _mk_user(email="bal@x.com"):
    return user_store.create_user(email, PW, role="user")


def _login(c, email):
    from _pt_helpers import check as _chk  # noqa: F401
    r = c.post("/login", data={"username": email, "password": PW})
    assert r.status_code in (302, 303), r.get_data(as_text=True)


def test_balance_user_total_allowance():
    bh.seed_spend_policies()
    u = _mk_user()
    allow0 = spend_store.get_total_allowance(u["user_id"])
    spend_store.set_user_total_limit(u["user_id"], 50 * 10**9,
                                     allow0["version"])
    # 制造用量/预占（直接改行，读路径只读投影）
    import pg_store
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_total_allowances SET "
                        "spent_nano_cny=%s, reserved_nano_cny=%s "
                        "WHERE subject_id=%s",
                        (12 * 10**9, 10**8, u["user_id"]))
        conn.commit()
    finally:
        conn.close()

    c = csrf_client(app_mod.app.test_client())
    _login(c, "bal@x.com")
    r = c.get("/api/account/balance")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_json()
    assert body["subject"]["username"] == "bal@x.com"
    assert body["subject"]["display_username"] == "bal"
    assert body["subject"]["role"] == "user"
    assert body["subject"]["preview"] is False
    assert body["spend_target"] == "total_allowance"
    # 金额一律十进制字符串（JS 大整数安全）
    assert body["limit_nano_cny"] == str(50 * 10**9)
    assert body["spent_nano_cny"] == str(12 * 10**9)
    assert body["reserved_nano_cny"] == str(10**8)
    assert body["remaining_nano_cny"] == str(50 * 10**9 - 12 * 10**9 - 10**8)
    assert body["period_start"] is None and body["period_end"] is None
    assert body["as_of"]
    # 不含供应商余额/策略内部 ID/key
    assert "policy_id" not in body and "api_key" not in body


def test_balance_user_missing_allowance_is_stable_error():
    """额度行缺失 → 稳定 400（建号默认开 20 CNY 行，删行构造缺失态）。"""
    u = _mk_user()
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s", (u["user_id"],))
        conn.commit()
    finally:
        conn.close()
    c = csrf_client(app_mod.app.test_client())
    _login(c, "bal@x.com")
    r = c.get("/api/account/balance")
    assert r.status_code == 400
    assert r.get_json()["code"] == "spend_total_allowance_missing"


def test_balance_owner_month_window_peek_only():
    bh.seed_spend_policies()
    owner = user_store.list_enabled_owners()[0] if \
        user_store.list_enabled_owners() else None
    if owner is None:
        owner = user_store.create_user("owner@x.com", PW, role="owner")
    uid = owner["user_id"]
    c = csrf_client(app_mod.app.test_client())
    _login(c, owner["login_id"])

    # 1) 窗口未开（未使用）：策略限额 + 0 用量，**不建行**
    r = c.get("/api/account/balance")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["spend_target"] == "owner_month_window"
    policy_limit = spend_store.resolve_policy("owner", uid)["limit_nano_cny"]
    assert body["limit_nano_cny"] == str(int(policy_limit))
    assert body["spent_nano_cny"] == "0" and body["reserved_nano_cny"] == "0"
    assert spend_store.peek_current_window("owner", uid) is None  # 未建行

    # 2) 有窗口：读窗口值与边界
    win = spend_store.get_or_create_window("owner", uid)
    import pg_store  # noqa: F401
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_windows SET spent_nano_cny=%s, "
                        "reserved_nano_cny=%s WHERE window_id=%s",
                        (3 * 10**9, 10**8, win["window_id"]))
        conn.commit()
    finally:
        conn.close()
    r2 = c.get("/api/account/balance")
    body2 = r2.get_json()
    assert body2["spent_nano_cny"] == str(3 * 10**9)
    assert body2["remaining_nano_cny"] == \
        str(int(policy_limit) - 3 * 10**9 - 10**8)
    assert body2["period_start"] and body2["period_end"]


def test_balance_requires_login():
    c = csrf_client(app_mod.app.test_client())
    assert c.get("/api/account/balance").status_code == 401

