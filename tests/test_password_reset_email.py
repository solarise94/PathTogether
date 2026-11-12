# -*- coding: utf-8 -*-
"""0095 自助找回密码（忘记密码 → 邮件重置链接 → 设置新密码）测试。

覆盖：
  1. 公开申请入口：未知/owner/禁用/未激活/未验证/符合条件邮箱返回**同一**
     通用响应，只有符合条件账号入队 password_reset 作业；Turnstile 与注册
     同口径（required 时 fail-closed；未配置按注册行为）；
  2. 限流：同邮箱 60s 冷却 / 时 3 / 日 5，超限仍通用响应；每 IP 前缀尝试桶；
  3. token：只存 HMAC hash、GET 只展示不消费、一次性、30 分钟过期、
     auth_version 变化即失效、较新请求作废旧 token；
  4. 提交：统一密码规则/确认密码、auth_version+1（旧 session 立即失效）、
     同事务清登录失败桶、审计 user.password_reset_email（无 secrets）、
     不自动登录并回 /login?password_reset=1；
  5. owner 即使持有伪造/旧 token 也永远不能经此路径重置；
  6. 页面安全头 no-store / Referrer-Policy: no-referrer；两个 POST 均需 CSRF；
  7. 迁移 0095 幂等（详见 tests/test_migration.py）。
"""
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import auth_limit_store  # noqa: E402
import pg_store  # noqa: E402
import registration_antibot  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app, make_client  # noqa: E402

BASE = "https://path.example.com"
PW = "longpassword123456"
NEW_PW = "newlongpassword123456"
GENERIC = "如果该邮箱已注册，我们已发送重置链接，请在 30 分钟内查收。"


def pg_conn():
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器清空 + 异步排水禁用）。"""
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "REGISTRATION_TURNSTILE_REQUIRED", "TURNSTILE_SITE_KEY",
                 "TURNSTILE_SECRET", "TURNSTILE_HOSTNAMES", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _client():
    return make_client(auth=True)


def _raw_client():
    """裸 client（不自动附 CSRF；CSRF 负路径用）。"""
    return make_client(auth=True, raw=True)


def _set_email(uid, email, verified=True):
    """给用户行写可信邮箱（email_verified_at 非 NULL 才可信）。"""
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            if verified:
                cur.execute(
                    "UPDATE users SET email=%s, email_normalized=%s, "
                    "email_verified_at=now() WHERE user_id=%s",
                    (email, email.strip().lower(), uid))
            else:
                cur.execute(
                    "UPDATE users SET email=NULL, email_normalized=NULL, "
                    "email_verified_at=NULL WHERE user_id=%s", (uid,))
        conn.commit()
    finally:
        conn.close()


def _mk_user(email, *, verified=True, activation="active", disabled=False):
    user = user_store.create_user(email, PW)
    _set_email(user["user_id"], email, verified=verified)
    if activation != "active":
        conn = pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET activation_state=%s WHERE user_id=%s",
                    (activation, user["user_id"]))
            conn.commit()
        finally:
            conn.close()
    if disabled:
        user_store.set_user_disabled(user["user_id"], True)
    return user_store.get_user(user["user_id"])


def _mk_owner(email="owner-pwreset@x.com"):
    owner = user_store.create_user(email, PW, role="owner")
    _set_email(owner["user_id"], email)
    return user_store.get_user(owner["user_id"])


def _user_row(uid):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, login_id, display_name, role, disabled, "
                "auth_version, activation_state, email, email_normalized, "
                "extract(epoch from email_verified_at)::float8 AS "
                "email_verified_at FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _post_start(client, email, **extra):
    data = {"email": email, "form_locale": "zh"}
    data.update(extra)
    return client.post("/password-reset", data=data)


def _post_reset(client, token, password=NEW_PW, confirm=None):
    return client.post("/reset-password", data={
        "token": token,
        "new_password": password,
        "password_confirm": password if confirm is None else confirm,
    })


def _jobs(email=None):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            if email is None:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs "
                    "WHERE purpose='password_reset' ORDER BY created_at, job_id")
            else:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs "
                    "WHERE purpose='password_reset' AND email_normalized=%s "
                    "ORDER BY created_at, job_id", (email,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _job_by_token(token):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM registration_mail_jobs WHERE token_hash=%s "
                "AND purpose='password_reset'",
                (registration_store.verify_token_hash(token),))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _fake():
    return registration_mail_worker.install_fake_sender()


def _extract_token(body_text):
    m = re.search(r"/reset-password\?token=([A-Za-z0-9_\-]+)", body_text)
    return m.group(1) if m else None


def _drain():
    return registration_mail_worker.drain_once()


def _start_and_token(email, client=None, **extra):
    """走申请 + 排水 + 从邮件正文取明文 token。"""
    client = client or _client()
    r = _post_start(client, email, **extra)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _drain() == 1
    to, subject, body = _fake().sent[-1]
    assert to == email
    token = _extract_token(body)
    assert token, body
    return client, token, subject, body


def _insert_job(email, user_id, auth_version, *, token=None,
                status="queued", created_seconds_ago=0, expires_seconds=1800):
    """直接插入一条 password_reset 作业（伪造/配额种子用）。"""
    token = token or ("seed-" + secrets.token_urlsafe(12))
    payload_enc = registration_mail_worker.encrypt_payload({
        "subject": "x", "body": "x", "purpose": "password_reset",
        "email": email, "user_id": str(user_id),
        "auth_version": int(auth_version), "locale": "zh",
        "template_key": "password_reset_zh",
        "template_data": {"email": email,
                          "link": BASE + "/reset-password?token=" + token},
    })
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registration_mail_jobs "
                "(job_id, purpose, email_normalized, token_hash, payload_enc, "
                " status, created_at, scheduled_at, expires_at) VALUES "
                "(%s,'password_reset',%s,%s,%s,%s, "
                " now() - (%s * interval '1 second'), "
                " now() - (%s * interval '1 second'), "
                " now() + (%s * interval '1 second'))",
                ("rmj_" + secrets.token_urlsafe(8), email,
                 registration_store.verify_token_hash(token), payload_enc,
                 status, created_seconds_ago, created_seconds_ago,
                 expires_seconds))
        conn.commit()
    finally:
        conn.close()
    return token


# =========================================================================== #
# 1. 公开申请入口：统一通用响应 + 只有符合条件账号入队
# =========================================================================== #
def test_start_generic_response_and_eligible_only(monkeypatch):
    owner = _mk_owner("owner-pwreset@x.com")
    eligible = _mk_user("eligible@x.com")
    _mk_user("disabled@x.com", disabled=True)
    _mk_user("pending@x.com", activation="pending_activation")
    _mk_user("unverified@x.com", verified=False)
    client = _client()

    bodies = []
    for email in ("unknown@x.com", owner["login_id"], "disabled@x.com",
                  "pending@x.com", "unverified@x.com", eligible["login_id"]):
        r = _post_start(client, email)
        assert r.status_code == 200, r.get_data(as_text=True)
        bodies.append(r.get_data())
    assert all(b == bodies[0] for b in bodies), \
        "不同账号状态必须返回逐字节相同的通用响应"
    html = bodies[0].decode("utf-8")
    assert GENERIC in html
    for leaked in ("unknown@x.com", owner["login_id"], "disabled@x.com",
                   "pending@x.com", "unverified@x.com", eligible["login_id"]):
        assert leaked not in html

    jobs = _jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["email_normalized"] == "eligible@x.com"
    assert job["purpose"] == "password_reset"
    assert job["status"] == "queued"
    assert job["token_hash"]
    payload = registration_mail_worker.decrypt_payload(job["payload_enc"])
    assert payload["user_id"] == eligible["user_id"]
    assert payload["auth_version"] == eligible["auth_version"]
    assert payload["template_key"] == "password_reset_zh"
    assert payload["template_data"]["link"].endswith(
        "/reset-password?token=" + _extract_token(payload["body"]))


def test_start_invalid_email_is_local_form_error(monkeypatch):
    _mk_user("shape@x.com")
    client = _client()
    r = _post_start(client, "not-an-email")
    assert r.status_code == 400
    assert "请输入有效的邮箱地址" in r.get_data(as_text=True)
    assert _jobs() == []


def test_start_unknown_entry_host_refuses_delivery(monkeypatch):
    """生产部署未知 Host：不把任意 Host 反射进重置链接（拒绝发信）。"""
    _mk_user("host@x.com")
    monkeypatch.setattr(
        registration_antibot, "resolve_entry_site",
        lambda host, base: {"refused": True})
    client = _client()
    r = _post_start(client, "host@x.com")
    assert r.status_code == 200
    assert GENERIC in r.get_data(as_text=True)
    assert _jobs() == []


# =========================================================================== #
# 2. Turnstile：与注册同口径（required 时 fail-closed；未配置按注册行为）
# =========================================================================== #
def _turnstile_cfg(required=True):
    return registration_antibot.TurnstileConfig(
        required=required, site_key="site-key", secret="secret",
        hostnames=("localhost",))


def test_turnstile_rejected_and_unavailable_do_not_enqueue(monkeypatch):
    _mk_user("ts@x.com")
    monkeypatch.setattr(registration_antibot, "load_turnstile_config",
                        lambda: _turnstile_cfg())
    client = _client()

    monkeypatch.setattr(
        registration_antibot, "verify",
        lambda *a, **k: registration_antibot.TurnstileResult(
            "rejected", "challenge_failed"))
    r = _post_start(client, "ts@x.com", **{"cf-turnstile-response": "tok"})
    assert r.status_code == 200
    assert "安全验证未完成" in r.get_data(as_text=True)
    assert _jobs() == []

    monkeypatch.setattr(
        registration_antibot, "verify",
        lambda *a, **k: registration_antibot.TurnstileResult(
            "unavailable", "network"))
    r = _post_start(client, "ts@x.com", **{"cf-turnstile-response": "tok"})
    assert r.status_code == 200
    assert "验证服务暂时不可用" in r.get_data(as_text=True)
    assert _jobs() == []

    monkeypatch.setattr(
        registration_antibot, "verify",
        lambda *a, **k: registration_antibot.TurnstileResult("ok", ""))
    r = _post_start(client, "ts@x.com", **{"cf-turnstile-response": "tok"})
    assert r.status_code == 200
    assert GENERIC in r.get_data(as_text=True)
    assert len(_jobs()) == 1


def test_turnstile_required_but_unconfigured_fails_closed(monkeypatch):
    """required 但未配齐：与注册一致 unavailable，绝不静默放行。"""
    _mk_user("ts2@x.com")
    monkeypatch.setattr(
        registration_antibot, "load_turnstile_config",
        lambda: registration_antibot.TurnstileConfig(required=True))
    client = _client()
    r = _post_start(client, "ts2@x.com", **{"cf-turnstile-response": "tok"})
    assert r.status_code == 200
    assert "验证服务暂时不可用" in r.get_data(as_text=True)
    assert _jobs() == []


def test_turnstile_not_required_has_no_widget(monkeypatch):
    _mk_user("ts3@x.com")
    page = _client().get("/password-reset").get_data(as_text=True)
    assert "cf-turnstile" not in page
    assert 'name="csrf_token"' in page


def test_failed_turnstile_does_not_consume_ip_bucket(monkeypatch):
    """无挑战 token 的脚本流量不得消耗 IP 前缀桶（Turnstile 之后才计数）。

    否则同一 CGNAT/校园网前缀下的真实用户被静默锁死：因为所有分支都只
    显示通用页面，用户无法察觉。N 次失败挑战后，同前缀的有效挑战仍必须
    能入队 password_reset 作业。
    """
    _mk_user("tsip@x.com")
    monkeypatch.setattr(registration_antibot, "load_turnstile_config",
                        lambda: _turnstile_cfg())

    def _verify(token, **kwargs):
        if token == "good-token":
            return registration_antibot.TurnstileResult("ok", "")
        return registration_antibot.TurnstileResult(
            "rejected", "challenge_failed")

    monkeypatch.setattr(registration_antibot, "verify", _verify)

    client = _client()
    n = auth_limit_store.PASSWORD_RESET_IP_ATTEMPT_LIMIT + 5
    for _ in range(n):
        r = _post_start(client, "tsip@x.com",
                        **{"cf-turnstile-response": "bad-token"})
        assert r.status_code == 200
        assert "安全验证未完成" in r.get_data(as_text=True)
    assert _jobs() == []
    # 失败挑战不计入 IP 前缀桶
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM auth_rate_limits "
                "WHERE scope=%s", (auth_limit_store.SCOPE_PASSWORD_RESET_IP,))
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()

    # 同前缀、有效挑战、符合条件邮箱：必须照常入队
    r = _post_start(client, "tsip@x.com",
                    **{"cf-turnstile-response": "good-token"})
    assert r.status_code == 200
    assert GENERIC in r.get_data(as_text=True)
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0]["email_normalized"] == "tsip@x.com"


# =========================================================================== #
# 3. 限流：同邮箱冷却/日上限 + 每 IP 前缀尝试桶（超限仍通用响应）
# =========================================================================== #
def test_start_email_cooldown_returns_generic(monkeypatch):
    _mk_user("cool@x.com")
    client = _client()
    r1 = _post_start(client, "cool@x.com")
    r2 = _post_start(client, "cool@x.com")
    assert r1.status_code == r2.status_code == 200
    assert r1.get_data() == r2.get_data()
    assert len(_jobs()) == 1


def test_start_email_daily_cap_returns_generic(monkeypatch):
    user = _mk_user("cap@x.com")
    for i in range(5):
        _insert_job("cap@x.com", user["user_id"], user["auth_version"],
                    token="cap-%d" % i, status="superseded",
                    created_seconds_ago=120)
    client = _client()
    r = _post_start(client, "cap@x.com")
    assert r.status_code == 200
    assert GENERIC in r.get_data(as_text=True)
    assert len(_jobs()) == 5  # 未新增


def test_start_ip_limit_returns_generic_without_enqueue(monkeypatch):
    _mk_user("ip1@x.com")
    _mk_user("ip2@x.com")
    monkeypatch.setattr(auth_limit_store, "PASSWORD_RESET_IP_ATTEMPT_LIMIT", 2)
    client = _client()
    r1 = _post_start(client, "ip1@x.com")
    r2 = _post_start(client, "ip2@x.com")
    assert r1.status_code == r2.status_code == 200
    assert r1.get_data() == r2.get_data()
    assert len(_jobs()) == 1
    assert _jobs()[0]["email_normalized"] == "ip1@x.com"


# =========================================================================== #
# 4. 邮件 zh/en、token 只存 hash、GET 不消费、密码规则/一次性
# =========================================================================== #
def test_mail_zh_and_en_and_token_hash(monkeypatch):
    _mk_user("mailzh@x.com")
    client = _client()
    _post_start(client, "mailzh@x.com")
    assert _drain() == 1
    _to, subject, body = _fake().sent[-1]
    assert "密码重置" in subject
    assert "30 分钟" in body
    assert "如果不是你本人操作" in body
    token = _extract_token(body)
    assert token
    job = _job_by_token(token)
    assert job is not None
    assert token not in json.dumps(job, default=str)  # 明文 token 绝不落库

    _mk_user("mailen@x.com")
    _post_start(client, "mailen@x.com", form_locale="en")
    assert _drain() == 1
    _to, subject, body = _fake().sent[-1]
    assert "password reset" in subject.lower()
    assert "30 minutes" in body
    assert "If you did not request this" in body


def test_get_does_not_consume_and_password_rules(monkeypatch):
    user = _mk_user("get@x.com")
    client, token, _s, _b = _start_and_token("get@x.com")
    before = _job_by_token(token)
    assert before["status"] == "sent" and before["consumed_at"] is None

    for _ in range(2):
        page = client.get("/reset-password?token=%s" % token)
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert "设置新密码" in html
        assert 'name="token"' in html
        assert "get@x.com" not in html  # 不回显完整邮箱
    after = _job_by_token(token)
    assert after["status"] == "sent" and after["consumed_at"] is None

    # 密码过短：400，token 不消费
    r = _post_reset(client, token, password="short")
    assert r.status_code == 400
    assert "密码长度须在" in r.get_data(as_text=True)
    assert _job_by_token(token)["consumed_at"] is None
    # 全空白：400，token 不消费
    r = _post_reset(client, token, password=" " * 20)
    assert r.status_code == 400
    assert "全空白" in r.get_data(as_text=True)
    assert _job_by_token(token)["consumed_at"] is None
    # 两次输入不一致：400，token 不消费
    r = _post_reset(client, token, confirm=NEW_PW + "x")
    assert r.status_code == 400
    assert "两次输入的密码不一致" in r.get_data(as_text=True)
    assert _job_by_token(token)["consumed_at"] is None
    assert _user_row(user["user_id"])["auth_version"] == user["auth_version"]


def test_reset_success_single_use_audit_and_login_message(monkeypatch):
    user = _mk_user("success@x.com")
    old_version = user["auth_version"]
    client, token, _s, _b = _start_and_token("success@x.com")

    r = _post_reset(client, token)
    assert r.status_code == 303
    assert r.headers["Location"] == "/login?password_reset=1"
    row = _user_row(user["user_id"])
    assert row["auth_version"] == old_version + 1
    job = _job_by_token(token)
    assert job["status"] == "consumed" and job["consumed_at"] is not None

    # 二次提交：新 client 先 GET 建立 CSRF，再提交同一 token → 中性提示
    repeat = _client()
    repeat.get("/reset-password?token=%s" % token)
    r2 = _post_reset(repeat, token)
    assert r2.status_code == 200
    assert "链接无效或已过期" in r2.get_data(as_text=True)
    assert _user_row(user["user_id"])["auth_version"] == old_version + 1

    # 不自动登录；新密码可登录、旧密码不可登录
    fresh = _client()
    assert fresh.post("/login", data={"username": "success@x.com",
                                      "password": NEW_PW}).status_code == 302
    old = _client()
    assert old.post("/login", data={"username": "success@x.com",
                                    "password": PW}).status_code == 401

    # 登录页提示
    page = _client().get("/login?password_reset=1").get_data(as_text=True)
    assert "密码已重置，请用新密码登录" in page

    # 审计：actor=本人、detail 无 token/密码/完整邮箱
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_user_id, actor_role, detail FROM audit_events "
                "WHERE action='user.password_reset_email' AND target_id=%s",
                (user["user_id"],))
            events = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(events) == 1
    ev = events[0]
    assert ev["actor_user_id"] == user["user_id"]
    assert ev["actor_role"] == "user"
    detail_text = json.dumps(ev["detail"])
    assert ev["detail"]["sessions_revoked"] is True
    for secret in (token, NEW_PW, "success@x.com"):
        assert secret not in detail_text


def test_reset_bumps_auth_version_revokes_old_session_and_clears_failures(
        monkeypatch):
    user = _mk_user("session@x.com")
    old_client = _client()
    assert old_client.post("/login", data={
        "username": "session@x.com", "password": PW}).status_code == 302
    assert old_client.get("/api/auth/info").status_code == 200

    # 先制造登录失败桶（账号 + IP 前缀）
    account_hash = app_mod._auth_subject_hash("session@x.com")
    ip_hash = app_mod._ip_prefix_hash("127.0.0.1")
    app_mod._record_login_failure(account_hash, ip_hash)
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM auth_rate_limits "
                "WHERE subject_hash IN (%s,%s)", (account_hash, ip_hash))
            assert cur.fetchone()["n"] >= 1
    finally:
        conn.close()

    client, token, _s, _b = _start_and_token("session@x.com")
    r = _post_reset(client, token)
    assert r.status_code == 303
    # 旧 session 的 auth_version 已过期 → 401
    assert old_client.get("/api/auth/info").status_code == 401
    # 登录失败桶已清（账号 + 本次来源 IP 前缀）
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM auth_rate_limits "
                "WHERE subject_hash IN (%s,%s) "
                "AND scope IN ('account','ip_prefix')",
                (account_hash, ip_hash))
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_token_expiry_and_auth_version_invalidation(monkeypatch):
    user = _mk_user("expire@x.com")
    client, token, _s, _b = _start_and_token("expire@x.com")
    conn = pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET expires_at="
                "now() - interval '1 second' WHERE token_hash=%s",
                (registration_store.verify_token_hash(token),))
    finally:
        conn.close()
    assert "链接无效或已过期" in client.get(
        "/reset-password?token=%s" % token).get_data(as_text=True)
    r = _post_reset(client, token)
    assert r.status_code == 200
    assert "链接无效或已过期" in r.get_data(as_text=True)
    assert _user_row(user["user_id"])["auth_version"] == user["auth_version"]

    # 另一条 token：其它改密路径推进 auth_version 后立即失效
    user2 = _mk_user("version@x.com")
    client2, token2, _s, _b = _start_and_token("version@x.com")
    user_store.set_user_password(user2["user_id"], "anotherlongpassword123")
    assert "链接无效或已过期" in client2.get(
        "/reset-password?token=%s" % token2).get_data(as_text=True)
    r = _post_reset(client2, token2)
    assert r.status_code == 200
    assert "链接无效或已过期" in r.get_data(as_text=True)
    # 密码仍是 set_user_password 写入的那个
    fresh = _client()
    assert fresh.post("/login", data={
        "username": "version@x.com",
        "password": "anotherlongpassword123"}).status_code == 302


def test_newer_request_supersedes_older_token(monkeypatch):
    user = _mk_user("newer@x.com")
    client, token1, _s1, _b1 = _start_and_token("newer@x.com")
    conn = pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at="
                "now() - interval '2 minutes' WHERE token_hash=%s",
                (registration_store.verify_token_hash(token1),))
    finally:
        conn.close()
    r = _post_start(client, "newer@x.com")
    assert r.status_code == 200
    assert _drain() == 1
    token2 = _extract_token(_fake().sent[-1][2])
    assert token2 and token2 != token1

    assert _job_by_token(token1)["status"] == "superseded"
    assert "链接无效或已过期" in client.get(
        "/reset-password?token=%s" % token1).get_data(as_text=True)
    r = _post_reset(client, token1)
    assert r.status_code == 200
    assert "链接无效或已过期" in r.get_data(as_text=True)
    assert _user_row(user["user_id"])["auth_version"] == user["auth_version"]

    r = _post_reset(client, token2)
    assert r.status_code == 303
    assert _user_row(user["user_id"])["auth_version"] == \
        user["auth_version"] + 1


# =========================================================================== #
# 5. owner 永远不能经此路径重置（即使伪造/持有旧 token）
# =========================================================================== #
def test_owner_never_reset_even_with_forged_token(monkeypatch):
    owner = _mk_owner("owner-only@x.com")
    token = _insert_job("owner-only@x.com", owner["user_id"],
                        owner["auth_version"], token="forged-owner-token")
    client = _client()
    page = client.get("/reset-password?token=%s" % token)
    assert "链接无效或已过期" in page.get_data(as_text=True)
    r = _post_reset(client, token)
    assert r.status_code == 200
    assert "链接无效或已过期" in r.get_data(as_text=True)
    row = _user_row(owner["user_id"])
    assert row["auth_version"] == owner["auth_version"]
    assert _job_by_token(token)["consumed_at"] is None
    # 原密码仍可登录（owner 走本人改密 / break-glass）
    fresh = _client()
    assert fresh.post("/login", data={"username": "owner-only@x.com",
                                      "password": PW}).status_code == 302


# =========================================================================== #
# 6. 安全头 + CSRF
# =========================================================================== #
def test_password_reset_pages_security_headers(monkeypatch):
    for path in ("/password-reset", "/reset-password?token=whatever"):
        r = _client().get(path)
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "no-store"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "no-referrer" in r.get_data(as_text=True)


def test_password_reset_posts_require_csrf(monkeypatch):
    _mk_user("csrf@x.com")
    raw = _raw_client()
    r = raw.post("/password-reset", data={"email": "csrf@x.com"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    assert _jobs() == []
    r = raw.post("/reset-password", data={
        "token": "x", "new_password": NEW_PW,
        "password_confirm": NEW_PW})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


def test_login_dialog_forgot_link(monkeypatch):
    page = _client().get("/login").get_data(as_text=True)
    assert 'href="/password-reset"' in page
    assert "忘记密码？" in page
    assert "联系管理员重置" not in page
