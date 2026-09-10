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
from datetime import datetime, timezone
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
    # 白名单外 /api 一律 401（D2：code=auth_required + 中文 error）
    r_inv = client.get("/api/admin/v1/invites")
    assert r_inv.status_code == 401
    body = r_inv.get_json()
    assert body["code"] == "auth_required"
    assert body["error"] != "auth_required"
    assert "重新登录" in body["error"]
    # 登出可用（白名单）
    assert client.post("/logout").status_code == 302
    with client.session_transaction() as s:
        assert not s.get(app_mod.ENROLLMENT_SESSION_KEY)


# =========================================================================== #
# 3.5 D1/D2 回归（2026-09-10）：enrollment 会话不被非白名单请求清掉、
#     favicon 放行、401 错误契约（中文 error + code=auth_required）
# =========================================================================== #
def test_activation_survives_favicon_request(monkeypatch):
    """D1 锁定：GET /activate 后浏览器自动 GET /favicon.ico（公开路径，204），
    再 POST 正确邀请码仍 200 ok=true。旧代码 favicon 会话被清 → 必 401。"""
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    user, password = _make_pending("fav@x.com")
    inv = registration_store.create_invite(
        owner["user_id"], total_limit_nano_cny=10 ** 9)
    client = _client()
    assert client.post("/login", data={"username": "fav@x.com",
                                       "password": password}).status_code == 302
    assert client.get("/activate").status_code == 200
    # 浏览器自动请求 favicon：公开路径放行（204），不得影响 enrollment 会话
    r_fav = client.get("/favicon.ico")
    assert r_fav.status_code == 204
    # 激活仍成功（旧代码此处必 401 auth_required）
    r = _activate_via_api(client, inv["token"])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    assert user_store.get_user(user["user_id"])["activation_state"] == "active"


def test_enrollment_session_survives_nonwhitelist_challenge(monkeypatch):
    """D1 锁定：非白名单路径只拒绝、不清会话——401 后 enrollment 状态接口
    仍 200（激活页刷新/再提交不丢会话）；401 body 符合 D2 契约。"""
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    user, password = _make_pending("keep@x.com")
    client = _client()
    assert client.post("/login", data={"username": "keep@x.com",
                                       "password": password}).status_code == 302
    # 任意非白名单路径（页面 302 /login、/api 401），cookie 保留
    r_api = client.get("/api/admin/v1/users")
    assert r_api.status_code == 401
    body = r_api.get_json()
    assert body["code"] == "auth_required"
    assert body["error"] != "auth_required"      # 不是裸机器码
    assert "重新登录" in body["error"]            # 中文引导文案
    assert client.get("/some/random/page").status_code == 302
    # 会话没有被清：enrollment 状态接口仍可用
    r_enr = client.get("/api/account/enrollment")
    assert r_enr.status_code == 200
    assert r_enr.get_json()["state"] == "pending_activation"
    assert user_store.get_user(user["user_id"]) \
        ["activation_state"] == "pending_activation"


def test_enrollment_whitelist_invalid_session_still_cleared(monkeypatch):
    """D1 边界：白名单路径上会话/用户本身已无效（禁用）仍清会话（既有
    fail-closed 语义不放宽；激活成功/already_active 清会话由既有用例锁定）。"""
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    user, password = _make_pending("ban@x.com")
    client = _client()
    assert client.post("/login", data={"username": "ban@x.com",
                                       "password": password}).status_code == 302
    # 管理员禁用 pending 用户 → enrollment 会话立即失效
    user_store.set_user_disabled(user["user_id"], True)
    assert client.get("/activate").status_code == 302   # 会话已清 → 去登录
    with client.session_transaction() as s:
        assert not s.get(app_mod.ENROLLMENT_SESSION_KEY)
    assert client.get("/api/account/enrollment").status_code == 401


def test_favicon_public_without_session():
    """favicon 对匿名也公开（与 /healthz 同类）：204 且不 302 /login。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/favicon.ico")
    assert r.status_code == 204
    assert r.get_data() == b""


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


# --------------------------------------------------------------------------- #
# 4.5 P0-1：邀请码绑定邮箱校验（激活面）
# --------------------------------------------------------------------------- #
def _allowance_count(user_id):
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (user_id,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _redeem_attempt_audit_status(invite_id):
    """取该邀请码最近一条 redeem_attempt 审计的 status（真实原因核验用）。"""
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events "
                "WHERE action='registration.redeem_attempt' "
                "AND target_id=%s ORDER BY ts DESC LIMIT 1", (invite_id,))
            row = cur.fetchone()
            return (row["detail"] or {}).get("status") if row else None
    finally:
        conn.close()


def test_activate_invite_bound_match_succeeds_normalized(monkeypatch):
    """P0-1：绑定匹配（大小写/空白归一后相等）→ 激活成功。"""
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    # pending 用户邮箱带大小写/空白；规范化后与绑定值一致
    user, _pw = _make_pending("  Bound-A@X.COM ")
    inv = registration_store.create_invite(
        owner["user_id"], login_id=" bound-a@X.com ",
        total_limit_nano_cny=2 * 10 ** 9)
    # 绑定值落库即规范化
    assert registration_store.get_invite(inv["invite_id"])[
        "login_id_normalized"] == "bound-a@x.com"
    result = registration_store.activate_registered_user(
        user["user_id"], inv["token"])
    assert result["user"]["activation_state"] == "active"
    updated = user_store.get_user(user["user_id"])
    assert updated["activation_state"] == "active"
    assert updated["activation_source"] == "invite"
    # 面值额度照常建立
    assert _allowance_count(user["user_id"]) == 1


def test_activate_invite_bound_mismatch_rejected_and_not_consumed(monkeypatch):
    """P0-1 红线：绑定给 Alice 的邀请码不能激活 Bob——整体回滚（邀请码不
    消费、状态不变、不建额度），对外统一 403 文案，真实原因只进审计。"""
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    _alice, _ = _make_pending("alice@x.com")
    bob, _ = _make_pending("bob@x.com")
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="alice@x.com")
    # store 层：InviteRedeemError（对外统一 code）
    with pytest.raises(registration_store.InviteRedeemError) as ei:
        registration_store.activate_registered_user(bob["user_id"],
                                                    inv["token"])
    assert ei.value.code == "invite_invalid_or_unavailable"
    # 整体回滚三件套
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 0 and row["consumed_at"] is None
    assert user_store.get_user(bob["user_id"])[
        "activation_state"] == "pending_activation"
    assert _allowance_count(bob["user_id"]) == 0
    # 审计行记录真实原因 bound_mismatch（对外不泄露）
    assert _redeem_attempt_audit_status(inv["invite_id"]) == \
        "activate:bound_mismatch"
    # 绑定者本人随后仍可成功（码未被抢用）
    registration_store.activate_registered_user(_alice["user_id"],
                                                inv["token"])
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 1
    assert user_store.get_user(_alice["user_id"])["activation_state"] == \
        "active"


def test_activate_api_bound_mismatch_unified_403(monkeypatch):
    """P0-1 API 面：绑定不匹配 → 403 统一 invite_invalid_or_unavailable
    （反枚举：与无效码同文案同 code），且不消费。"""
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    _alice, _ = _make_pending("alice@x.com")
    bob, bob_pw = _make_pending("bob@x.com")
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="alice@x.com")
    client = _client()
    assert client.post("/login", data={"username": "bob@x.com",
                                       "password": bob_pw}).status_code == 302
    r = _activate_via_api(client, inv["token"])
    assert r.status_code == 403
    assert r.get_json()["code"] == "invite_invalid_or_unavailable"
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 0
    assert user_store.get_user(bob["user_id"])[
        "activation_state"] == "pending_activation"


def test_activate_bound_invite_only_bound_user_wins(monkeypatch):
    """P0-1：两个 pending 用户抢同一绑定码——非绑定者先到被拒且不消费，
    绑定者（大小写/空白归一匹配）随后成功；反向顺序只有绑定者成功。"""
    _open_email_mode(monkeypatch)
    owner = _mk_owner()
    bound_user, _ = _make_pending("  Racer-A@X.COM ")
    other, _ = _make_pending("racer-b@x.com")
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id=" racer-a@x.com ")
    # 顺序 A：非绑定者先到 → 拒绝、码不消费
    with pytest.raises(registration_store.InviteRedeemError):
        registration_store.activate_registered_user(other["user_id"],
                                                    inv["token"])
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 0
    assert user_store.get_user(other["user_id"])[
        "activation_state"] == "pending_activation"
    # 顺序 B：绑定者（归一化匹配）后到 → 成功消费
    registration_store.activate_registered_user(bound_user["user_id"],
                                                inv["token"])
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 1
    assert row["consumed_by_user_id"] == bound_user["user_id"]
    # 非绑定者仍未被波及
    assert user_store.get_user(other["user_id"])[
        "activation_state"] == "pending_activation"


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
def test_worker_drains_with_fake_sender_and_body_has_link(monkeypatch):
    _open_email_mode(monkeypatch)  # P1-2：worker 排水前置=生效注册模式
    out = _enqueue("drain@x.com")
    n = registration_mail_worker.drain_once(sender=_fake())
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
    _open_email_mode(monkeypatch)  # 模式生效后专测「通道未配置」分支
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
    _open_email_mode(monkeypatch)  # P1-2：resend 写前查生效模式
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
    env3 = {"REGISTRATION_MAIL_SENDER": "smtp",
            "REGISTRATION_SMTP_HOST": "smtp.example.test",
            "REGISTRATION_SMTP_USER": "bot@example.test",
            "REGISTRATION_SMTP_PASSWORD": "x"}
    assert registration_mail_worker.sender_configured(env3) is True
    assert registration_mail_worker.sender_configured({
        "REGISTRATION_MAIL_SENDER": "smtp",
        "REGISTRATION_SMTP_HOST": "smtp.example.test",
    }) is False
    assert registration_mail_worker.sender_configured({}) is False


class _ScriptedSmtp:
    """docmd 级 smtplib.SMTP_SSL 替身：按类级脚本决定各阶段行为。

    P1-1 阶段划分验证用：
      - ``next_getreply_exc`` 非空 → getreply()（最终响应等待段）抛该异常，
        模拟「远端已接受、本地等响应超时/断连」；
      - ``next_terminator_send_exc`` 非空 → send(b".\\r\\n")（DATA 结束符
        写出段）抛该异常，模拟「结束符开始写出后断连——远端可能已完整接收」
        （二轮 review P1-1 反例探针）；
      - ``next_body_send_exc`` 非空 → 正文 send（阶段 1）抛该异常；
      - ``next_final_code`` = 远端对整个事务的最终响应码（250=接受）；
      - ``next_auth_exc`` 非空 → login 抛该异常（DATA 之前的确定失败）。
    """

    next_getreply_exc = None
    next_terminator_send_exc = None
    next_body_send_exc = None
    next_final_code = 250
    next_auth_exc = None
    last = None  # 最后一个实例（断言信封与线上字节用）

    def __init__(self, *a, **k):
        self.sent_data = b""
        self.mail_from = None
        self.rcpt_to = None
        self.logged = None
        _ScriptedSmtp.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        return (250, b"greeting")

    def starttls(self, context=None):
        return (220, b"ready")

    def login(self, user, password):
        if _ScriptedSmtp.next_auth_exc is not None:
            raise _ScriptedSmtp.next_auth_exc
        self.logged = (user, password)

    def docmd(self, cmd, arg=None):
        c = str(cmd).upper()
        if c == "MAIL":
            self.mail_from = arg
            return (250, b"ok")
        if c == "RCPT":
            self.rcpt_to = arg
            return (250, b"ok")
        if c == "DATA":
            return (354, b"go ahead")
        return (250, b"ok")

    def send(self, data):
        if data == b".\r\n" and _ScriptedSmtp.next_terminator_send_exc:
            exc = _ScriptedSmtp.next_terminator_send_exc
            _ScriptedSmtp.next_terminator_send_exc = None
            raise exc
        if _ScriptedSmtp.next_body_send_exc is not None:
            exc = _ScriptedSmtp.next_body_send_exc
            _ScriptedSmtp.next_body_send_exc = None
            raise exc
        self.sent_data += data

    def getreply(self):
        if _ScriptedSmtp.next_getreply_exc is not None:
            raise _ScriptedSmtp.next_getreply_exc
        return (_ScriptedSmtp.next_final_code, b"done")


def _smtp_sender():
    return registration_mail_worker.SmtpMailSender({
        "REGISTRATION_SMTP_HOST": "smtp.example.test",
        "REGISTRATION_SMTP_PORT": "465",
        "REGISTRATION_SMTP_USER": "bot@example.test",
        "REGISTRATION_SMTP_PASSWORD": "secret-auth-code",
        "REGISTRATION_SMTP_FROM": "bot@example.test",
    })


@pytest.fixture(autouse=True)
def _scripted_smtp_state():
    """每个用例前后复位 _ScriptedSmtp 类级脚本（防用例间泄漏）。"""
    _ScriptedSmtp.next_getreply_exc = None
    _ScriptedSmtp.next_terminator_send_exc = None
    _ScriptedSmtp.next_body_send_exc = None
    _ScriptedSmtp.next_final_code = 250
    _ScriptedSmtp.next_auth_exc = None
    yield
    _ScriptedSmtp.next_getreply_exc = None
    _ScriptedSmtp.next_terminator_send_exc = None
    _ScriptedSmtp.next_body_send_exc = None
    _ScriptedSmtp.next_final_code = 250
    _ScriptedSmtp.next_auth_exc = None


def test_smtp_sender_sends_without_logging_password(monkeypatch):
    import base64
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    sender.send("user@example.com", "PathTogether · 验证邮箱",
                "hello\n.leading dot line\nbye")
    rec = _ScriptedSmtp.last
    assert rec.mail_from == "FROM:<bot@example.test>"
    assert rec.rcpt_to == "TO:<user@example.com>"
    assert rec.logged == ("bot@example.test", "secret-auth-code")
    # 解出 MIME 载荷（utf-8 → base64 传输编码）
    _head, _, payload_b64 = rec.sent_data.partition(b"\r\n\r\n")
    decoded = base64.b64decode(
        payload_b64.replace(b"\r\n", b"")).decode("utf-8")
    assert "hello" in decoded
    assert ".leading dot line" in decoded
    assert rec.sent_data.endswith(b".\r\n")
    assert "secret-auth-code" not in rec.sent_data.decode("utf-8", "replace")
    with pytest.raises(registration_mail_worker.MailSenderUnavailable):
        registration_mail_worker.SmtpMailSender({
            "REGISTRATION_SMTP_HOST": "smtp.example.test",
        })


def test_smtp_transmit_dot_quotes_leading_dots():
    """线级点引用（与 smtplib.data 同款）：行首 '.' → '..'，结束符 '.' 收尾。"""
    sender = _smtp_sender()
    smtp = _ScriptedSmtp()
    sender._transmit(smtp, "line1\n.top secret\nline2\r\n",
                     "user@example.com", None)
    wire = smtp.sent_data
    assert b"\r\n..top secret" in wire
    assert wire.endswith(b".\r\n")


def test_smtp_uncertain_when_final_response_missing(monkeypatch):
    """P1-1：DATA 结束符已写出、等最终响应期间本地超时 → 不确定（绝非
    failed——远端可能已接受，按失败重试会重复发信）。"""
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_getreply_exc = TimeoutError("local wait timeout")
    with pytest.raises(
            registration_mail_worker.MailSenderUncertainError) as ei:
        sender.send("user@example.com", "s", "hello")
    assert "smtp_final_response_missing" in str(ei.value)
    # 不确定窗口边界证据：结束符已全部写出
    assert _ScriptedSmtp.last.sent_data.endswith(b".\r\n")
    # 子类关系：既有 MailSenderError 捕获方语义不变（worker 先捕 uncertain）
    assert issubclass(registration_mail_worker.MailSenderUncertainError,
                      registration_mail_worker.MailSenderError)


def test_smtp_uncertain_when_terminator_send_times_out(monkeypatch):
    """二轮 review P1-1 反例：DATA 结束符 send 本身超时 → 必须判 uncertain。

    结束符开始写出后，客户端无法证明远端未完整接收（TCP 缓冲/对端已读均
    不可观测）；此前的实现把该异常留在阶段 1，被外层 except OSError 译成
    可重试 MailSenderError → worker 自动重发 → 重复发信。
    """
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_terminator_send_exc = TimeoutError("terminator write")
    with pytest.raises(
            registration_mail_worker.MailSenderUncertainError) as ei:
        sender.send("user@example.com", "s", "hello")
    assert "smtp_final_response_missing" in str(ei.value)
    assert "TimeoutError" in str(ei.value)


def test_smtp_uncertain_when_terminator_send_disconnects(monkeypatch):
    """结束符写出段连接断开（非超时类 OSError）同样归 uncertain。"""
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_terminator_send_exc = ConnectionResetError("reset")
    with pytest.raises(
            registration_mail_worker.MailSenderUncertainError):
        sender.send("user@example.com", "s", "hello")


def test_smtp_body_send_failure_is_deterministic(monkeypatch):
    """正文写出（阶段 1）失败仍=确定未发出 → 普通可重试 MailSenderError。

    sendall 语义保证数据未完整送达即抛错，远端事务未提交；这是阶段划分
    的另一半边界，不得被误放大到 uncertain（否则会卡死不发）。
    """
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_body_send_exc = TimeoutError("body write")
    with pytest.raises(registration_mail_worker.MailSenderError) as ei:
        sender.send("user@example.com", "s", "hello")
    assert not isinstance(
        ei.value, registration_mail_worker.MailSenderUncertainError)


def test_smtp_data_rejected_is_deterministic_failure(monkeypatch):
    """远端明确回绝（收到最终非 250 响应）= 确定未发出 → 普通失败可重试。"""
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_final_code = 452
    with pytest.raises(registration_mail_worker.MailSenderError) as ei:
        sender.send("user@example.com", "s", "hello")
    assert not isinstance(
        ei.value, registration_mail_worker.MailSenderUncertainError)
    assert "smtp_data_rejected_452" in str(ei.value)


def test_smtp_pre_data_failure_is_deterministic(monkeypatch):
    """DATA 之前（认证）失败 = 确定未发出 → failed 可重试。"""
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", _ScriptedSmtp)
    sender = _smtp_sender()
    _ScriptedSmtp.next_auth_exc = smtplib.SMTPAuthenticationError(535, b"no")
    with pytest.raises(registration_mail_worker.MailSenderError) as ei:
        sender.send("user@example.com", "s", "hello")
    assert not isinstance(
        ei.value, registration_mail_worker.MailSenderUncertainError)
    assert "smtp_auth_failed" in str(ei.value)


# --------------------------------------------------------------------------- #
# 6.5 P1-1：发送不确定态（uncertain）——worker 落状态 / 链接仍可验证 / 有界重试
# --------------------------------------------------------------------------- #
def _backdate_scheduled_by_token(token, seconds_ago=3600):
    """把作业 scheduled_at 回填到过去（autocommit；出指数退避窗）。"""
    conn = pg_store_connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET scheduled_at = now() - "
                "(%s * interval '1 second') WHERE token_hash=%s",
                (seconds_ago, registration_store.verify_token_hash(token)))
    finally:
        conn.close()


class _UncertainAfterDataSender:
    """模拟「远端已接受、本地等响应超时」的发送器（DATA 后抛 TimeoutError
    类别，经 SmtpMailSender 阶段划分映射为 MailSenderUncertainError）。"""

    def __init__(self):
        self.calls = 0

    def send(self, to, subject, body):
        self.calls += 1
        raise registration_mail_worker.MailSenderUncertainError(
            "smtp_final_response_missing（TimeoutError）")


class _AlwaysFailSender:
    """确定未发出（DATA 之前失败类别）的发送器。"""

    def __init__(self):
        self.calls = 0

    def send(self, to, subject, body):
        self.calls += 1
        raise registration_mail_worker.MailSenderError(
            "smtp_connect_failed（ConnectionRefusedError）")


def test_worker_uncertain_no_resend_and_link_still_verifiable(monkeypatch):
    """P1-1 主线：远端接受后本地超时 → 作业 uncertain、收到的链接仍可验证
    建号、worker 重跑不重复发送。"""
    _open_email_mode(monkeypatch)
    out = _enqueue("uncertain@x.com")
    snd = _UncertainAfterDataSender()
    assert registration_mail_worker.drain_once(sender=snd) == 0
    row = _mail_job_row(out["token"])
    assert row["status"] == "uncertain"          # 0038 词表含 uncertain
    assert row["attempts"] == 1
    assert "smtp_final_response_missing" in (row["last_error"] or "")
    # worker 重跑：uncertain 不在领取范围 → 不重复发送
    assert registration_mail_worker.drain_once(sender=snd) == 0
    assert registration_mail_worker.drain_once(sender=_fake()) == 0
    assert snd.calls == 1 and _fake().sent == []
    # 用户手里「已收到」的链接仍可验证建号（有效期内 uncertain 一律放行）
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "valid"
    r = _client().post("/api/registration/verify",
                       json={"token": out["token"],
                             "password": "longpassword123"})
    assert r.status_code == 200, r.get_data(as_text=True)
    user = user_store.get_user_by_login_id("uncertain@x.com")
    assert user is not None
    assert user["activation_state"] == "pending_activation"
    row = _mail_job_row(out["token"])
    assert row["status"] == "consumed" and row["consumed_at"] is not None


def test_uncertain_token_superseded_by_new_request(monkeypatch):
    """P1-1：uncertain 作业持有的链接与新请求互斥——同邮箱重新入队即作废
    （单活 token 红线对 uncertain 同样成立）。"""
    _open_email_mode(monkeypatch)
    out = _enqueue("sup@x.com")
    snd = _UncertainAfterDataSender()
    registration_mail_worker.drain_once(sender=snd)
    assert _mail_job_row(out["token"])["status"] == "uncertain"
    # 出 60s 冷却窗后重新入队（冷却以 job 行数计）
    conn = pg_store_connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '2 minutes' WHERE email_normalized='sup@x.com'")
    finally:
        conn.close()
    out2 = _enqueue("sup@x.com")
    assert _mail_job_row(out["token"])["status"] == "superseded"
    # 旧（uncertain→superseded）链接失效；新链接可用
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "unknown"
    assert registration_store.check_verify_token(out2["token"])[
        "state"] == "valid"


def test_worker_failed_retry_backoff_then_success(monkeypatch):
    """确定失败 → failed + 指数退避（scheduled_at 顺延、退避期内不重试）；
    退避出窗后用正常 sender 重试成功。"""
    _open_email_mode(monkeypatch)
    out = _enqueue("retry@x.com")
    snd = _AlwaysFailSender()
    assert registration_mail_worker.drain_once(sender=snd) == 0
    row = _mail_job_row(out["token"])
    assert row["status"] == "failed" and row["attempts"] == 1
    # 退避顺延（第 0 次失败 → +30s）
    assert row["scheduled_at"] > datetime.now(timezone.utc)
    # 退避期内不重试
    assert registration_mail_worker.drain_once(sender=snd) == 0
    assert snd.calls == 1
    # 出退避窗 → 重试（仍失败，attempts=2，退避翻倍）
    _backdate_scheduled_by_token(out["token"])
    assert registration_mail_worker.drain_once(sender=snd) == 0
    row = _mail_job_row(out["token"])
    assert row["attempts"] == 2
    assert row["scheduled_at"] > datetime.now(timezone.utc)
    # 换正常 sender + 出退避窗 → 重试成功置 sent
    _backdate_scheduled_by_token(out["token"])
    assert registration_mail_worker.drain_once(sender=_fake()) == 1
    row = _mail_job_row(out["token"])
    assert row["status"] == "sent"
    assert len(_fake().sent) == 1


def test_worker_failed_retry_stops_at_attempts_cap(monkeypatch):
    """attempts 达上限（5）后保持 failed 不再发送（回填退避窗也不领取）。"""
    _open_email_mode(monkeypatch)
    out = _enqueue("cap@x.com")
    snd = _AlwaysFailSender()
    for expected in range(1, registration_mail_worker._MAX_SEND_ATTEMPTS + 1):
        if expected > 1:
            _backdate_scheduled_by_token(out["token"])
        assert registration_mail_worker.drain_once(sender=snd) == 0
        row = _mail_job_row(out["token"])
        assert row["attempts"] == expected
        assert row["status"] == "failed"
    assert snd.calls == registration_mail_worker._MAX_SEND_ATTEMPTS
    # 超上限：出退避窗也不再领取（不再发送）
    _backdate_scheduled_by_token(out["token"])
    assert registration_mail_worker.drain_once(sender=snd) == 0
    assert registration_mail_worker.drain_once(sender=_fake()) == 0
    assert snd.calls == registration_mail_worker._MAX_SEND_ATTEMPTS
    assert _fake().sent == []
    row = _mail_job_row(out["token"])
    assert row["status"] == "failed"
    assert row["attempts"] == registration_mail_worker._MAX_SEND_ATTEMPTS


# --------------------------------------------------------------------------- #
# 6.9 P1-2：注册模式停机语义（写端点 + worker 排水）
# --------------------------------------------------------------------------- #
def _mail_job_count(email_norm):
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM registration_mail_jobs "
                "WHERE email_normalized=%s", (email_norm,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def test_activate_blocked_when_registration_closed(monkeypatch):
    """P1-2：closed 下 activate 403 registration_closed，且邀请码不消费、
    用户状态不变（只暂停）。"""
    # 先配齐全部前置 env（token 哈希盐/载荷密钥全程稳定），再模拟停机：
    # 存储模式 closed ≠ 前置缺失，二者语义分离
    _open_email_mode(monkeypatch)
    settings_store.set_registration_mode("closed", updated_by="t")
    owner = _mk_owner()
    user, password = _make_pending("closedact@x.com")
    inv = registration_store.create_invite(owner["user_id"])
    client = _client()
    assert client.post("/login", data={"username": "closedact@x.com",
                                       "password": password}).status_code == 302
    r = _activate_via_api(client, inv["token"])
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    # 只暂停：邀请码不消费、状态不变、无额度
    assert registration_store.get_invite(inv["invite_id"])["use_count"] == 0
    assert user_store.get_user(user["user_id"])[
        "activation_state"] == "pending_activation"
    assert _allowance_count(user["user_id"]) == 0
    # 恢复开放后同一请求成功（停机只暂停，不销毁）
    settings_store.set_registration_mode("email_verify_invite_activation",
                                         updated_by="t")
    r2 = _activate_via_api(client, inv["token"])
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert user_store.get_user(user["user_id"])["activation_state"] == "active"


def test_resend_blocked_when_registration_closed(monkeypatch):
    """P1-2：closed 下 resend 403 registration_closed，且不写队列。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/api/registration/resend",
                    json={"email": "closedrs@x.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    assert _mail_job_count("closedrs@x.com") == 0


def test_register_email_start_blocked_when_closed(monkeypatch):
    """P1-2：closed 下验证请求（verify start）403，且不写队列。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/register", data={"email": "closedstart@x.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    assert _mail_job_count("closedstart@x.com") == 0
    # 恢复开放后可正常入队
    _open_email_mode(monkeypatch)
    r2 = client.post("/register", data={"email": "closedstart@x.com"})
    assert r2.status_code == 200
    assert _mail_job_count("closedstart@x.com") == 1


def test_worker_keeps_queued_when_registration_closed(monkeypatch):
    """P1-2：closed 下 worker 不发信、作业留 queued（不是 failed）。"""
    out = _enqueue("closedq@x.com")
    assert registration_mail_worker.drain_once(sender=_fake()) == 0
    assert _fake().sent == []
    row = _mail_job_row(out["token"])
    assert row["status"] == "queued"


def test_worker_after_reopen_skips_expired_sends_unexpired(monkeypatch):
    """P1-2：恢复开放后只发未过期作业；过期作业保持 queued（验证端报
    expired），不发也不 fail。"""
    # 先配齐全部前置 env（载荷加密密钥全程稳定），再经存储模式切换停机/恢复
    _open_email_mode(monkeypatch)
    expired = _enqueue("expired-open@x.com", ttl_seconds=1)
    fresh = _enqueue("fresh-open@x.com")
    time.sleep(1.1)
    settings_store.set_registration_mode("closed", updated_by="t")
    # closed：都不发
    assert registration_mail_worker.drain_once(sender=_fake()) == 0
    assert _fake().sent == []
    # 切回 open：只发未过期作业
    settings_store.set_registration_mode("email_verify_invite_activation",
                                         updated_by="t")
    assert registration_mail_worker.drain_once(sender=_fake()) == 1
    sent = _fake().sent
    assert len(sent) == 1 and sent[0][0] == "fresh-open@x.com"
    # 过期作业：留 queued、不发送；验证端报 expired
    row = _mail_job_row(expired["token"])
    assert row["status"] == "queued"
    assert registration_store.check_verify_token(expired["token"])[
        "state"] == "expired"
    # 未过期作业正常消费
    assert _mail_job_row(fresh["token"])["status"] == "sent"


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
