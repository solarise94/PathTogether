# -*- coding: utf-8 -*-
"""I+J 线测试：邮箱验证 + 邀请码激活 + 邮箱唯一用户名（设计文档第 8 节 +
review J / P2-4 / I-R4 守卫）。

覆盖：
  - 模式：email_verify_invite_activation 前置检查（邮件通道/载荷密钥/哈希盐）
    与 fail-closed 降级；PUT/GET 词表；public 拒绝保留；
  - 注册流程 1-3：邮箱优先（不填邀请码、不发额度）、统一文案、GET
    /verify-email 只展示不消费、POST /api/registration/verify 原子创建
    pending_activation 用户（密码在邮箱确认之后设置；J：login_id=规范化
    邮箱，冲突进待补绑）；
  - 流程 4-5：pending 登录只发 enrollment 受限 session；activate 单事务
    （CAS 消费邀请码 → active → 按面值建总额度 → 审计）；
    already_active 不消费不充值；同码两人只有一人成功；
  - I-R4：require_active_account 统一守卫（pending 全拒）；
  - 展示 J：owner 管理台主列=完整邮箱用户名、精确/模糊邮箱搜索、审计
    actor 身份、公开分享页评论掩码；
  - 邮件：token 只存 hash、载荷加密、一次性/30 分钟/配额、fake 发送器与
    Agent Mail CLI 适配器（两步 confirmation_token）。
"""
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

BASE = "https://path.example.com"


def _pg():
    conn = pg_store_connect()
    return conn


def pg_store_connect():
    import pg_store
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器清空 + 异步排水改同步禁用）。"""
    from _pt_helpers import isolate_app
    import _billing_helpers as bh
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    # 邮件环境：默认 fake 发送器（生产前置不计入）；入队后的异步排水禁用
    # （用例自行 drain_once，时序确定）
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _mk_owner():
    return user_store.create_user("reg-owner@x.com", "ownerpass12345678",
                                  role="owner")


def _owner_session(client, owner):
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner", "auth_version": owner.get("auth_version", 1)})


def _open_email_mode(monkeypatch):
    """打开 email_verify_invite_activation 生效态（含 I 线全部前置 env）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    settings_store.set_registration_mode(
        "email_verify_invite_activation", updated_by="t")


def _enqueue(email, **kw):
    kw.setdefault("base_url", BASE)
    return registration_store.enqueue_email_verification(email, **kw)


def _fake():
    return registration_mail_worker.install_fake_sender()


def _mail_job_row(token_or_hash, by_token=True):
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            h = registration_store.verify_token_hash(token_or_hash) \
                if by_token else token_or_hash
            cur.execute(
                "SELECT * FROM registration_mail_jobs WHERE token_hash=%s",
                (h,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


# =========================================================================== #
# 1. 模式与前置检查
# =========================================================================== #
def test_new_mode_fails_closed_without_mail_channel(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    settings_store.set_registration_mode(
        "email_verify_invite_activation", updated_by="t")
    # 邮件通道未配置（fake 不计入生产）→ 降级 closed
    assert app_mod._effective_registration_mode() == "closed"
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    assert app_mod._effective_registration_mode() == "closed"
    # 配齐真实通道口径 + 载荷密钥 + 哈希盐 → 生效
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    assert app_mod._effective_registration_mode() == "closed"  # 缺载荷密钥/盐
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "k")
    monkeypatch.setenv("SECRET_KEY", "s")
    assert app_mod._effective_registration_mode() == \
        "email_verify_invite_activation"


def test_put_registration_mode_new_mode(monkeypatch):
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    r = client.put("/api/admin/v1/settings/registration",
                   json={"mode": "email_verify_invite_activation"})
    assert r.status_code == 400  # 前置不满足
    assert r.get_json()["error"]["code"] == "registration_preconditions_failed"
    _open_email_mode(monkeypatch)
    r2 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "email_verify_invite_activation"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    body = client.get("/api/admin/v1/settings").get_json()["registration"]
    assert body["supported_modes"] == ["closed", "invite_only",
                                       "email_verify_invite_activation"]
    assert body["mode"] == "email_verify_invite_activation"
    # public 仍拒绝
    r3 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "public"})
    assert r3.status_code == 400
    assert r3.get_json()["error"]["code"] == "public_registration_not_supported"


def test_register_email_mode_page_copy(monkeypatch):
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="email"' in body
    assert 'name="invite_token"' not in body   # 不填邀请码
    assert 'name="login_id"' not in body       # J：不要求独立登录账号
    assert 'name="display_name"' not in body   # J：不要求显示名
    assert "邀请码" in body and "激活" in body  # 文案写明后置激活


def test_register_email_mode_post_unified_copy(monkeypatch):
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    r1 = client.post("/register", data={"email": "New.User@Example.COM "})
    assert r1.status_code == 200
    done1 = r1.get_data(as_text=True)
    # 未知邮箱：同一文案（无枚举信号）
    r2 = client.post("/register", data={"email": "ghost@nowhere.test"})
    assert r2.get_data(as_text=True) == done1
    # 超限（60s 冷却）：仍是同一文案
    r3 = client.post("/register", data={"email": "new.user@example.com"})
    assert r3.get_data(as_text=True) == done1
    # 入队规范化：job email = 规范化值；同邮箱冷却只 1 个 job
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM registration_mail_jobs "
                "WHERE email_normalized=%s", ("new.user@example.com",))
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT payload_enc, token_hash FROM "
                        "registration_mail_jobs")
            row = cur.fetchone()
    finally:
        conn.close()
    # token 明文/链接绝不落库（载荷加密）
    assert "verify-email?token=" not in row["payload_enc"]


# =========================================================================== #
# 2. 验证页（GET 只展示）与 verify 建号（密码后置）
# =========================================================================== #
def test_verify_email_get_does_not_consume(monkeypatch):
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    out = _enqueue("alice@x.com")
    r = client.get("/verify-email?token=" + out["token"])
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "设置密码" in body
    assert "邀请码" in body
    row = _mail_job_row(out["token"])
    assert row["consumed_at"] is None and row["status"] == "queued"
    # 无效 token：状态页（不 500）
    r2 = client.get("/verify-email?token=garbage-token")
    assert r2.status_code == 200
    assert "无效" in r2.get_data(as_text=True)
    # check_verify_token 三态
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "valid"
    assert registration_store.check_verify_token("garbage")["state"] == \
        "unknown"


def test_verify_email_expired_state(monkeypatch):
    out = _enqueue("ttl@x.com", ttl_seconds=1)
    time.sleep(1.1)
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "expired"


def test_verify_creates_pending_user_email_as_login_id(monkeypatch):
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    out = _enqueue("  Alice@X.COM  ")
    r = client.post("/api/registration/verify",
                    json={"token": out["token"],
                          "password": "longpassword123",
                          "password_confirm": "longpassword123"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    user = user_store.get_user_by_login_id("alice@x.com")
    assert user is not None
    # J：login_id = 规范化邮箱；email 身份列可信（已验证）
    assert user["login_id"] == "alice@x.com"
    assert user["email_normalized"] == "alice@x.com"
    assert user["email_verified_at"] is not None
    assert user["activation_state"] == "pending_activation"
    assert user["activation_source"] == "invite_activation"
    # 验证邮箱不授予 AI / 额度
    assert user["ai_access"] is False
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (user["user_id"],))
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()
    # token 一次性：已消费
    row = _mail_job_row(out["token"])
    assert row["status"] == "consumed" and row["consumed_at"] is not None
    # 二次提交同 token → 统一 400
    r2 = client.post("/api/registration/verify",
                     json={"token": out["token"],
                           "password": "longpassword123"})
    assert r2.status_code == 400
    assert r2.get_json()["code"] == "invalid_or_expired"
    # 新 token 同邮箱 → email_taken（部分唯一索引；不建第二个账号）。
    # （先把已消费 job 移出 60s 冷却窗口——store 层配额对直接调用方生效）
    conn = pg_store_connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE registration_mail_jobs SET created_at = now() - "
                    "interval '2 minutes' WHERE email_normalized='alice@x.com'")
    finally:
        conn.close()
    out2 = _enqueue("alice@x.com")
    r3 = client.post("/api/registration/verify",
                     json={"token": out2["token"],
                           "password": "longpassword123"})
    assert r3.status_code == 409
    assert r3.get_json()["code"] == "email_taken"
    assert user_store.get_user_by_login_id("alice@x.com")["user_id"] == \
        user["user_id"]


def test_verify_login_id_conflict_goes_to_rebind(monkeypatch):
    """J 红线：与存量 login_id 冲突 → 待补绑合成账号；绝不静默合并、绝不
    给存量账号伪造 email_verified_at。"""
    legacy = user_store.create_user("taken@x.com", "existingpass12345678",
                                    role="user")
    assert legacy["email_verified_at"] is None  # 存量 @ login_id 不标已验证
    out = _enqueue("taken@x.com")
    result = registration_store.verify_email_create_user(
        out["token"], "longpassword123")
    new_user = result["user"]
    assert new_user["user_id"] != legacy["user_id"]
    assert result["pending_bind"] is True
    assert new_user["login_id"].startswith("pending-") \
        and new_user["login_id"].endswith("@bind.invalid")
    assert new_user["email_normalized"] == "taken@x.com"
    assert new_user["email_verified_at"] is not None
    # 存量账号分毫未动
    after = user_store.get_user(legacy["user_id"])
    assert after["email_verified_at"] is None
    assert after["email_normalized"] is None


def test_verify_password_policy(monkeypatch):
    out = _enqueue("pw@x.com")
    r = _client().post("/api/registration/verify",
                       json={"token": out["token"], "password": "short"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_request"
    assert _mail_job_row(out["token"])["consumed_at"] is None


# =========================================================================== #
# 3. pending 登录 → enrollment 受限 session（I-R4 白名单）
# =========================================================================== #
def _make_pending(email="pending@x.com", password="pendingpass12345678"):
    out = _enqueue(email)
    return registration_store.verify_email_create_user(
        out["token"], password)["user"], password


def test_pending_login_enrollment_scope_only(monkeypatch):
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    user, password = _make_pending()
    client = _client()
    r = client.post("/login", data={"username": "pending@x.com",
                                    "password": password})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/activate")
    with client.session_transaction() as s:
        # 独立 scope：不写普通 auth_user/role/user_id
        assert s.get(app_mod.ENROLLMENT_SESSION_KEY)["user_id"] == \
            user["user_id"]
        assert not s.get("auth_user")
        assert not s.get("role")
        assert not s.get("user_id")
    # enrollment 白名单内
    r2 = client.get("/activate")
    assert r2.status_code == 200
    assert "p***@x.com" in r2.get_data(as_text=True)   # 掩码邮箱
    assert "pending@x.com" not in r2.get_data(as_text=True)
    r3 = client.get("/api/account/enrollment")
    assert r3.status_code == 200
    assert r3.get_json()["state"] == "pending_activation"
    assert r3.get_json()["email_masked"] == "p***@x.com"
    # 业务面全拒（enrollment 不是登录态）
    assert client.get("/api/admin/v1/users").status_code == 401
    assert client.get("/api/admin/v1/slides/inventory").status_code == 401
    # 白名单外 /api 一律 401 auth_required
    assert client.get("/api/admin/v1/invites").status_code == 401
    # 登出可用（白名单）
    assert client.post("/logout").status_code == 302
    with client.session_transaction() as s:
        assert not s.get(app_mod.ENROLLMENT_SESSION_KEY)


# =========================================================================== #
# 4. 激活（P2-4：单事务、CAS 消费、面值额度、already_active 不消费）
# =========================================================================== #
def _activate_via_api(client, code):
    return client.post("/api/account/activate", json={"invite_code": code})


def test_activate_happy_path(monkeypatch):
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    user, password = _make_pending("happy@x.com")
    inv = registration_store.create_invite(
        owner["user_id"], ai_access=True, total_limit_nano_cny=3 * 10 ** 9)
    client = _client()
    assert client.post("/login", data={"username": "happy@x.com",
                                       "password": password}).status_code == 302
    # 错误邀请码 → 统一 403（不消费）
    r_bad = _activate_via_api(client, "totally-wrong-code")
    assert r_bad.status_code == 403
    assert r_bad.get_json()["code"] == "invite_invalid_or_unavailable"
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 0
    # 正确邀请码 → 激活成功
    r = _activate_via_api(client, inv["token"])
    assert r.status_code == 200, r.get_data(as_text=True)
    # session 清空（enrollment 使命完成）
    assert client.get("/api/account/enrollment").status_code == 401
    updated = user_store.get_user(user["user_id"])
    assert updated["activation_state"] == "active"
    assert updated["activation_source"] == "invite"
    assert updated["ai_access"] is True
    invite_row = registration_store.get_invite(inv["invite_id"])
    assert invite_row["use_count"] == 1
    assert invite_row["consumed_by_user_id"] == user["user_id"]
    # 按面值建一次性总额度（source=invite）
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT limit_nano_cny, source FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (user["user_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["limit_nano_cny"] == 3 * 10 ** 9
    assert row["source"] == "invite"
    # 正常登录可用（active → 普通 session）
    assert client.post("/login", data={"username": "happy@x.com",
                                       "password": password}).status_code == 302
    with client.session_transaction() as s:
        assert s.get("auth_user")


def test_already_active_second_code_not_consumed(monkeypatch):
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    user, _pw = _make_pending("once@x.com")
    inv1 = registration_store.create_invite(owner["user_id"],
                                            total_limit_nano_cny=10 ** 9)
    registration_store.activate_registered_user(user["user_id"], inv1["token"])
    inv2 = registration_store.create_invite(owner["user_id"],
                                            total_limit_nano_cny=10 ** 9)
    with pytest.raises(registration_store.ActivationError) as ei:
        registration_store.activate_registered_user(user["user_id"],
                                                    inv2["token"])
    assert ei.value.code == "already_active"
    # 不消费、不充值（仍只有 1 条 allowance 行）
    row = registration_store.get_invite(inv2["invite_id"])
    assert row["use_count"] == 0 and row["consumed_at"] is None
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (user["user_id"],))
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


def test_same_invite_two_pending_users_single_winner(monkeypatch):
    owner = _mk_owner()
    ua, _ = _make_pending("racer-a@x.com")
    ub, _ = _make_pending("racer-b@x.com")
    inv = registration_store.create_invite(owner["user_id"])
    registration_store.activate_registered_user(ua["user_id"], inv["token"])
    with pytest.raises(registration_store.InviteRedeemError):
        registration_store.activate_registered_user(ub["user_id"], inv["token"])
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 1
    assert row["consumed_by_user_id"] == ua["user_id"]
    assert user_store.get_user(ub["user_id"])["activation_state"] == \
        "pending_activation"


def test_activation_maintenance_gate(monkeypatch):
    owner = _mk_owner()
    user, _ = _make_pending("maint@x.com")
    inv = registration_store.create_invite(owner["user_id"])
    settings_store.set_setting(settings_store.AI_DISPATCH_MAINTENANCE_KEY,
                               True, updated_by="t")
    with pytest.raises(spend_store.ProvisioningMaintenanceError):
        registration_store.activate_registered_user(user["user_id"],
                                                    inv["token"])
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 0
    assert user_store.get_user(user["user_id"])[
        "activation_state"] == "pending_activation"


def test_activate_api_requires_enrollment_session():
    app_mod.AUTH_ENABLED = True
    client = _client()
    # 匿名 → 401；普通业务身份推导绝不来自请求体
    assert _activate_via_api(client, "some-code").status_code == 401


# =========================================================================== #
# 5. I-R4：require_active_account 统一守卫
# =========================================================================== #
def test_pending_account_blocked_from_business_api(monkeypatch):
    app_mod.AUTH_ENABLED = True
    user, _ = _make_pending("blocked@x.com")
    client = _client()
    # 伪造普通 session 形态（pending 账号绝不该有，但守卫必须独立成立）
    with client.session_transaction() as s:
        s.update({"auth_user": "blocked@x.com", "user_id": user["user_id"],
                  "role": "user", "auth_version": user["auth_version"]})
    r = client.get("/api/admin/v1/users")
    assert r.status_code == 403
    assert r.get_json()["error"] == "account_pending"
    # 页面 → 302（无 enrollment 时回 /login）
    r2 = client.get("/admin")
    assert r2.status_code == 302
    # 激活后放行（普通用户身份可触达的业务出口恢复；admin 端点仍受 owner
    # 门控——那是另一层权限，与本守卫无关）
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"])
    registration_store.activate_registered_user(user["user_id"], inv["token"])
    fresh = user_store.get_user(user["user_id"])
    client2 = _client()
    with client2.session_transaction() as s:
        s.update({"auth_user": "blocked@x.com", "user_id": user["user_id"],
                  "role": "user", "auth_version": fresh["auth_version"]})
    info = client2.get("/api/auth/info")
    assert info.status_code == 200
    assert info.get_json()["user_id"] == user["user_id"]
    admin = client2.get("/api/admin/v1/users")
    assert admin.status_code == 403
    assert admin.get_json()["error"] != "account_pending"


def test_legacy_backfill_state_defaults_active():
    """存量/owner 建号：activation_state=active（迁移 backfill 与新写入同口径）。"""
    u = user_store.create_user("legacy@x.com", "legacy1234567890",
                               role="user")
    assert u["activation_state"] == "active"
    assert u["email_normalized"] is None       # 无可信已验证邮箱
    assert u["email_verified_at"] is None


# =========================================================================== #
# 6. 邮件：worker / fake / Agent Mail CLI / 配额
# =========================================================================== #
def test_worker_drains_with_fake_sender_and_body_has_link():
    out = _enqueue("drain@x.com")
    n = registration_mail_worker.drain_once()
    assert n == 1
    sent = _fake().sent
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "drain@x.com"
    assert "verify-email?token=" + out["token"] in body
    assert "邀请码" in body
    row = _mail_job_row(out["token"])
    assert row["status"] == "sent" and row["sent_at"] is not None


def test_worker_unconfigured_sender_keeps_queued(monkeypatch):
    out = _enqueue("q@x.com")
    monkeypatch.setattr(registration_mail_worker, "get_sender",
                        lambda environ=None: None)
    assert registration_mail_worker.drain_once() == 0
    assert _mail_job_row(out["token"])["status"] == "queued"


def test_verify_token_only_stored_as_hash():
    out = _enqueue("hash@x.com")
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash, payload_enc, status FROM "
                        "registration_mail_jobs WHERE job_id=%s",
                        (out["job_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["token_hash"] == registration_store.verify_token_hash(
        out["token"])
    assert out["token"] not in row["token_hash"]
    blob = repr(row)
    assert out["token"] not in blob
    assert "verify-email?token=" not in blob
    # 载荷可解密回冻结正文（含链接）
    payload = registration_mail_worker.decrypt_payload(row["payload_enc"])
    assert "verify-email?token=" + out["token"] in payload["body"]


def test_resend_quota_cooldown_hourly_daily(monkeypatch):
    email = "quota@x.com"
    _enqueue(email)
    with pytest.raises(registration_store.EmailVerifyError) as ei:
        _enqueue(email)
    assert ei.value.code == "rate_limited"
    # backdate 用 autocommit 连接（store 的 enqueue 用独立连接，必须能看到
    # 已提交的回填时间）
    conn = pg_store_connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # 绕过冷却：把现有 job 回填到 2 小时前（出冷却/时窗、留 24h 内），
            # 逐条灌到「小时 3」上限
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '2 hours' WHERE email_normalized=%s", (email,))
            for _ in range(registration_store.VERIFY_HOURLY_LIMIT):
                # 每轮先把全部 job 回填出冷却/时窗，再入队（新 job 落在 now()）
                cur.execute(
                    "UPDATE registration_mail_jobs SET created_at = now() - "
                    "interval '2 hours' WHERE email_normalized=%s", (email,))
                _enqueue(email)
            with pytest.raises(registration_store.EmailVerifyError) as eh:
                _enqueue(email)
            assert eh.value.code == "rate_limited"
            # 全部回填出 1 小时窗口（仍在 24h 内）→ 填满「日上限 5」后拒绝
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '2 hours' WHERE email_normalized=%s", (email,))
            cur.execute(
                "SELECT count(*)::int AS n FROM registration_mail_jobs "
                "WHERE email_normalized=%s AND created_at > now() - "
                "interval '24 hours'", (email,))
            existing_daily = int(cur.fetchone()["n"])
            for _ in range(registration_store.VERIFY_DAILY_LIMIT
                           - existing_daily):
                _enqueue(email)
            with pytest.raises(registration_store.EmailVerifyError) as ed:
                _enqueue(email)
            assert ed.value.code == "rate_limited"
            # 应用日预算 40：清掉本邮箱 job，全局灌 40 条（24h 内）
            cur.execute(
                "DELETE FROM registration_mail_jobs WHERE "
                "email_normalized=%s", (email,))
            for i in range(registration_store.VERIFY_APP_DAILY_BUDGET):
                cur.execute(
                    "INSERT INTO registration_mail_jobs (job_id, purpose, "
                    "email_normalized, token_hash, payload_enc, status, "
                    "created_at, expires_at) VALUES (%s, 'email_verify', %s, "
                    "%s, 'x', 'superseded', now(), now() + interval '1 hour')",
                    ("rmj_budget_%s" % i, "budget%s@x.com" % i,
                     "hash_budget_%s" % i))
            with pytest.raises(registration_store.EmailVerifyError) as eb:
                _enqueue("fresh-budget@x.com")
            assert eb.value.code == "rate_limited"
    finally:
        conn.close()


def test_resend_api_unified_response(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/api/registration/resend", json={"email": "rs@x.com"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # 冷却超限：同一响应（无枚举/无 429 信号）
    r2 = client.post("/api/registration/resend", json={"email": "rs@x.com"})
    assert r2.status_code == 200 and r2.get_json()["ok"] is True
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM registration_mail_jobs"
                        " WHERE email_normalized='rs@x.com'")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


def _make_cli_script(tmpdir, marker):
    """两步 confirmation_token 的最小 CLI stub（bash；参数数组调用）。"""
    path = Path(tmpdir) / "agent_mail_cli_stub.sh"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "send-request" ]; then\n'
        '  echo "{\\"confirmation_token\\": \\"conf-123\\"}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "send-confirm" ] && [ "$2" = "--confirmation-token" ] '
        '&& [ "$3" = "conf-123" ]; then\n'
        '  echo sent >> "%s"\n'
        "  exit 0\n"
        "fi\n"
        "exit 9\n" % marker)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return str(path)


def test_agent_mail_cli_sender_two_step(tmp_path):
    marker = tmp_path / "sent.log"
    cli = _make_cli_script(tmp_path, marker)
    sender = registration_mail_worker.AgentMailCliSender(cli, sender=None)
    sender.send("cli@x.com", "subj", "hello body")
    assert marker.read_text().strip() == "sent"
    # CLI 缺失 → MailSenderUnavailable（模式前置/发送失败 fail-closed）
    with pytest.raises(registration_mail_worker.MailSenderUnavailable):
        registration_mail_worker.AgentMailCliSender(
            str(tmp_path / "no-such-cli")).send("a@x.com", "s", "b")
    # 未配置路径
    with pytest.raises(registration_mail_worker.MailSenderUnavailable):
        registration_mail_worker.AgentMailCliSender("").send("a@x.com", "s", "b")


def test_sender_configured_production_ignores_fake():
    env = {"REGISTRATION_MAIL_SENDER": "fake"}
    assert registration_mail_worker.sender_configured(env) is False
    assert registration_mail_worker.sender_configured(
        env, production=False) is True
    env2 = {"REGISTRATION_MAIL_SENDER": "agent_mail_cli",
            "REGISTRATION_AGENT_MAIL_CLI": "/usr/bin/true"}
    assert registration_mail_worker.sender_configured(env2) is True
    assert registration_mail_worker.sender_configured({}) is False


# =========================================================================== #
# 7. 展示 J：owner 管理台 / 审计 / 公开分享页
# =========================================================================== #
def _admin_users_items(client, q=None):
    url = "/api/admin/v1/users"
    if q:
        url += "?q=" + q
    return client.get(url).get_json()["items"]


def test_admin_users_identity_email_first_and_search(monkeypatch):
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    # 待激活用户：identity=邮箱
    out = _enqueue("Iden@X.com")
    pending = registration_store.verify_email_create_user(
        out["token"], "longpassword123")["user"]
    # admin 建号：无 email → identity=login_id
    manual = user_store.create_user("manual-user", "manualpass12345678",
                                    role="user")
    items = _admin_users_items(client)
    by_id = {i["user_id"]: i for i in items}
    assert by_id[pending["user_id"]]["identity"] == "iden@x.com"
    assert by_id[pending["user_id"]]["identity_source"] == "email"
    assert by_id[pending["user_id"]]["activation_state"] == "pending_activation"
    assert by_id[manual["user_id"]]["identity"] == "manual-user"
    assert by_id[manual["user_id"]]["identity_source"] == "login_id"
    # 搜索：精确（大小写不敏感）与模糊（子串）
    hits_exact = _admin_users_items(client, q="IDEN@x.com")
    assert any(i["user_id"] == pending["user_id"] for i in hits_exact)
    hits_fuzzy = _admin_users_items(client, q="iden@")
    assert any(i["user_id"] == pending["user_id"] for i in hits_fuzzy)
    assert all(i["user_id"] != manual["user_id"] for i in hits_fuzzy)


def test_admin_slides_inventory_owner_identity(monkeypatch):
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    name = "identity-demo.svs"
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    import share_store
    share_store.set_slide_meta(name, owner_user_id=owner["user_id"])
    resp = client.get("/api/admin/v1/slides/inventory")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    items = resp.get_json()["items"]
    assert items, "inventory 为空（文件未列出）"
    it = next(i for i in items if i["name"] == name)
    assert it["owner_identity"] == "reg-owner@x.com"


def test_admin_audit_actor_identity(monkeypatch):
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    # 触发一条带 actor 的审计（owner 建 user）
    client.post("/api/admin/v1/users",
                json={"login_id": "audit-u@x.com",
                      "password": "auditpass12345678"})
    events = client.get("/api/admin/v1/audit").get_json()["items"]
    ev = next(e for e in events if e["action"] == "user.create")
    assert ev["actor_identity"] == "reg-owner@x.com"


def test_share_page_comments_mask_identity(monkeypatch):
    """公开分享页红线：登录用户作者只出掩码邮箱；guest 保留自报 label。"""
    import share_server
    import share_store
    owner = _mk_owner()
    member = user_store.create_user("member@x.com", "memberpass12345678",
                                    role="user")
    name = "share-identity.svs"
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    share_store.set_slide_meta(name, owner_user_id=owner["user_id"])
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, name, "L", type="rect",
                              x=0, y=0, side_px=10, size_mm=6.0)
    aid = roi["annotation_id"]
    # 成员评论（author_label 快照为 display_name 旧形态也不能外泄）
    share_store.add_comment(aid, name, share_store.ADMIN_TOKEN, "from member",
                            author_user_id=member["user_id"],
                            author_label="member@x.com")
    # guest 评论
    share_store.add_comment(aid, name, share_store.ADMIN_TOKEN, "from guest",
                            author_user_id=None, author_label="访客")
    sh = share_store.create_share([name], 24,
                                  permissions=["view", "annotate"])
    share_server.app.config["TESTING"] = True
    sc = share_server.app.test_client()
    resp = sc.get("/s/%s/api/comments?annotation_id=%s" % (sh["token"], aid))
    assert resp.status_code == 200
    comments = resp.get_json()["comments"]
    member_c = next(c for c in comments if c.get("author_user_id"))
    assert member_c["author_label"] == "m***@x.com"   # 掩码
    assert "member@x.com" not in json.dumps(comments)  # 全量不外泄
    guest_c = next(c for c in comments if not c.get("author_user_id"))
    assert guest_c["author_label"] == "访客"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
