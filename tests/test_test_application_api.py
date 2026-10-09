# -*- coding: utf-8 -*-
"""测试申请通道退役测试（2026-10-09 §2「删除测试申请」，R8）。

通道下线后的稳定行为（docs/admin-viewer-round4-20261009.md §2 接口变更）：

  - 用户侧 GET/POST /api/account/test-application：匿名 401（认证闸先于
    退役分支）；任何已登录调用方（含 enrollment 受限会话）410
    endpoint_retired（扁平信封），不读不写 test_applications；
  - 管理侧 GET /api/admin/v1/test-applications 与 POST .../review：
    已登录一律 410 endpoint_retired（admin v1 信封 {error:{code,message}}）；
  - 宿主页 /admin/test-applications：302 → /admin（通知邮件里的历史链接
    仍可点，/admin 自身完成登录/owner 门控）；
  - ``test_application_store`` 模块已删除（Containerfile COPY 同步移除）；
  - ``test_applications`` 表保留为历史数据：research_consent_store.
    legacy_test_application_signal 直接读表的兼容层照常工作（历史证明
    historical_only，不构成当前研究授权）。

历史行为（提交/审批/邀请码收口/修复工具）的既有覆盖：
  - 邀请码激活滞留申请的修复工具见
    tests/test_repair_invite_activated_applications.py；
  - 研究授权权威与旧选项兼容层见 tests/test_research_consent.py /
    tests/test_user_agreements.py。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_test_application_api.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import research_consent_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app, make_client  # noqa: E402

PASSWORD = "longpassword123"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _client():
    return make_client(auth=True)


def _login(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return client


def _count(sql, params=()):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()["count"])
    finally:
        conn.close()


def _store_module_gone():
    """test_application_store 模块随通道退役删除（文件不存在）。"""
    return not os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "test_application_store.py"))


# --------------------------------------------------------------------------- #
# 1. 用户侧端点：匿名 401 / 已登录 410，零副作用
# --------------------------------------------------------------------------- #
def test_account_endpoints_retired_410():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("u1@x.com", "userpass12345678")

    # 匿名：认证闸先于退役分支 → 401 auth_required
    for method in ("get", "post"):
        r = getattr(_client(), method)("/api/account/test-application")
        assert r.status_code == 401
        assert r.get_json()["code"] == "auth_required"

    # 已登录普通用户 / owner：GET/POST 一律 410 endpoint_retired（扁平信封）
    for login in (usera, owner):
        c = _login(_client(), login)
        r = c.get("/api/account/test-application")
        assert r.status_code == 410
        assert r.get_json()["code"] == "endpoint_retired"
        r = c.post("/api/account/test-application",
                   json={"research_direction": "other",
                         "share_research_data": True})
        assert r.status_code == 410
        assert r.get_json()["code"] == "endpoint_retired"

    # 零副作用：无申请行、无通知邮件、无新审计
    assert _count("SELECT count(*) AS count FROM test_applications") == 0
    assert _count("SELECT count(*) AS count FROM registration_mail_jobs "
                  "WHERE purpose IN ('test_application','test_decision')") == 0
    assert _count("SELECT count(*) AS count FROM audit_events "
                  "WHERE action LIKE 'test_application%%'") == 0


def test_enrollment_session_gets_retired_410():
    """enrollment 受限会话（白名单内路径）也收到权威 410，而不是 401。"""
    import secrets
    from werkzeug.security import generate_password_hash
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                "password_hash, role, created_at, disabled, ai_config, "
                "ai_access, activation_state, activation_source, "
                "activation_updated_at, email, email_normalized, "
                "email_verified_at) VALUES (%s,%s,%s,%s,'user', now(), FALSE, "
                "'{}'::jsonb, FALSE, 'pending_activation', "
                "'email_verification', now(), %s, %s, now()) RETURNING user_id",
                ("usr_" + secrets.token_urlsafe(8), "pend@x.com", "pend@x.com",
                 generate_password_hash(PASSWORD), "pend@x.com", "pend@x.com"))
            uid = cur.fetchone()["user_id"]
        conn.commit()
    finally:
        conn.close()
    c = _client()
    with c.session_transaction() as s:
        s.update({app_mod.ENROLLMENT_SESSION_KEY: {
            "user_id": uid, "email": "pend@x.com", "purpose": "activation",
            "issued_at": 1.0, "auth_version": 1}})
    r = c.get("/api/account/test-application")
    assert r.status_code == 410
    assert r.get_json()["code"] == "endpoint_retired"


# --------------------------------------------------------------------------- #
# 2. 管理侧端点：已登录一律 410（admin v1 信封），零副作用
# --------------------------------------------------------------------------- #
def test_admin_v1_endpoints_retired_410():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("u1@x.com", "userpass12345678")

    # 匿名 401（认证闸）
    r = _client().get("/api/admin/v1/test-applications")
    assert r.status_code == 401
    # owner（真实审核载荷）与普通用户：列表/审核一律 410，不激活不发邮件
    for login in (owner, usera):
        c = _login(_client(), login)
        r = c.get("/api/admin/v1/test-applications")
        assert r.status_code == 410
        assert r.get_json()["error"]["code"] == "endpoint_retired"
        r = c.post("/api/admin/v1/test-applications/%s/review"
                   % usera["user_id"], json={"decision": "approved"})
        assert r.status_code == 410
        assert r.get_json()["error"]["code"] == "endpoint_retired"

    # 零副作用：无审核邮件、无审批审计；用户建号自带 1 行额度，无第二行
    assert _count("SELECT count(*) AS count FROM registration_mail_jobs "
                  "WHERE purpose='test_decision'") == 0
    assert _count("SELECT count(*) AS count FROM audit_events "
                  "WHERE action='test_application.review'") == 0
    assert _count("SELECT count(*) AS count FROM ai_spend_total_allowances "
                  "WHERE subject_id=%s", (usera["user_id"],)) == 1


# --------------------------------------------------------------------------- #
# 3. 宿主页重定向 /admin
# --------------------------------------------------------------------------- #
def test_admin_host_page_redirects_to_admin():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    c = _login(_client(), owner)
    r = c.get("/admin/test-applications")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/admin")
    # 非 owner 同样只做重定向（/admin 页自身 403，不在链接层复制门控）
    usera = user_store.create_user("u1@x.com", "userpass12345678")
    r = _login(_client(), usera).get("/admin/test-applications")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/admin")


# --------------------------------------------------------------------------- #
# 4. 存量数据兼容：表保留 + 研究授权兼容层直接读表
# --------------------------------------------------------------------------- #
def test_store_module_removed_and_table_kept_for_legacy_view():
    assert _store_module_gone(), "test_application_store.py 应随通道退役删除"
    # 旧表保留为历史：兼容层直接读表（historical_only 历史证明）
    usera = user_store.create_user("legacy@x.com", "userpass12345678")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO test_applications (user_id, research_direction, "
                "share_research_data, consent_version) VALUES (%s,'other',"
                "TRUE,'research-data-20260916-v1')", (usera["user_id"],))
        conn.commit()
    finally:
        conn.close()
    legacy = research_consent_store.legacy_test_application_signal(
        usera["user_id"])
    assert legacy is not None
    assert legacy["historical_only"] is True
    assert legacy["share_research_data"] is True
    # 无记录用户返回 None（表为空历史，不再有写入方）
    userb = user_store.create_user("nocache@x.com", "userpass12345678")
    assert research_consent_store.legacy_test_application_signal(
        userb["user_id"]) is None
