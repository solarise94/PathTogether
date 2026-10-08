# -*- coding: utf-8 -*-
"""注册遗留线测试（2026-10-08 §4 邀请码激活退役后保留的部分）。

覆盖（保留面）：
  - 模式：public 前置检查（邮件通道/载荷密钥/哈希盐/管理员邮箱/双文稿）
    与 fail-closed 降级；PUT/GET 词表只剩 closed/public（旧
    invite_only / email_verify_invite_activation 存量值读取按非法值
    fail-closed 为 closed，PUT 一律 400 invalid_request）；
  - 旧 email_verify 链接退役：GET /verify-email 只展示不消费（渲染
    「注册流程已更新」）；legacy token 不再建 pending 账号；
  - I-R4：require_active_account 统一守卫（pending 全拒，直插 SQL 行验证）；
  - 展示 J：owner 管理台主列=完整邮箱用户名、精确/模糊邮箱搜索、审计
    actor 身份、公开分享页评论掩码；
  - 邮件（共享代码不动）：token 只存 hash、载荷加密、一次性/30 分钟/
    配额、fake 发送器与 Agent Mail CLI/SMTP 适配器、closed 停机只停
    email_verify 作业。
"""
import json
import os
import re
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


def _publish_docs_for_public():
    """发布双协议文稿（public 生效前置；幂等）。"""
    import agreement_store
    agreement_store.ensure_builtin_documents()
    for dt in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == dt][0]
        agreement_store.publish_document(dt, doc["version"])


def _open_email_mode(monkeypatch):
    """打开 public 生效态（2026-10-08 §4 后唯一开放模式；含全部前置 env +
    双协议文稿 published + 管理员通知邮箱）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    _publish_docs_for_public()
    settings_store.set_registration_mode("public", updated_by="t")


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
def test_public_mode_fails_closed_without_preconditions(monkeypatch):
    """public 前置阶梯（2026-10-08 §4：唯一开放模式；前置缺失降级 closed）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    settings_store.set_registration_mode("public", updated_by="t")
    # 邮件通道未配置（fake 不计入生产）→ 降级 closed
    assert app_mod._effective_registration_mode() == "closed"
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    assert app_mod._effective_registration_mode() == "closed"
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    assert app_mod._effective_registration_mode() == "closed"  # 缺载荷密钥/盐
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "k")
    monkeypatch.setenv("SECRET_KEY", "s")
    assert app_mod._effective_registration_mode() == "closed"  # 缺管理员邮箱
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    # 缺双协议文稿 → 仍降级 closed
    assert app_mod._effective_registration_mode() == "closed"
    _publish_docs_for_public()
    assert app_mod._effective_registration_mode() == "public"


def test_retired_modes_are_invalid_values(monkeypatch):
    """旧模式值退役（§4）：存量行读取按非法值 fail-closed 为 closed。"""
    settings_store.set_setting(settings_store.REGISTRATION_MODE_KEY,
                                "email_verify_invite_activation")
    assert app_mod._effective_registration_mode() == "closed"
    settings_store.set_setting(settings_store.REGISTRATION_MODE_KEY,
                               "invite_only")
    assert app_mod._effective_registration_mode() == "closed"


def test_put_registration_mode_word_table(monkeypatch):
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    # 旧模式值一律 invalid_request（不再接受邀请码形态）
    for retired in ("invite_only", "email_verify_invite_activation"):
        r = client.put("/api/admin/v1/settings/registration",
                       json={"mode": retired})
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "invalid_request"
    # closed 无前置要求，直接可写
    r0 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "closed"})
    assert r0.status_code == 200
    # public：缺前置 → 400 registration_preconditions_failed；配齐 → 200
    r1 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "public"})
    assert r1.status_code == 400
    assert r1.get_json()["error"]["code"] == "registration_preconditions_failed"
    _open_email_mode(monkeypatch)
    r2 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "public"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    body = client.get("/api/admin/v1/settings").get_json()["registration"]
    assert body["supported_modes"] == ["closed", "public"]
    assert body["mode"] == "public"


# =========================================================================== #
# 2. 验证页（GET 只展示）与 verify 建号（密码后置）
# =========================================================================== #
def test_verify_email_get_does_not_consume(monkeypatch):
    """GET /verify-email 仍只展示不消费；legacy（无 intent）token 渲染退役
    文案（2026-10-08 §4：不再建 pending 账号）。"""
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    out = _enqueue("alice@x.com")  # legacy flow 签发（无 intent 行）
    r = client.get("/verify-email?token=" + out["token"])
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "注册流程已更新" in body
    assert "设置密码" not in body
    assert "research_direction" not in body
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
    # POST legacy token → 403 registration_closed（token 不消费，引导重走公开注册）
    r3 = client.post("/api/registration/verify",
                     json={"token": out["token"],
                           "password": "longpassword123"})
    assert r3.status_code == 403
    assert r3.get_json()["code"] == "registration_closed"
    assert _mail_job_row(out["token"])["consumed_at"] is None


def test_verify_email_expired_state(monkeypatch):
    out = _enqueue("ttl@x.com", ttl_seconds=1)
    time.sleep(1.1)
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "expired"


# =========================================================================== #
# 3. I-R4 守卫（pending 账号即使拿到普通 session 形态也全拒）
# =========================================================================== #
def _insert_pending_row(email="blocked@x.com",
                        password="pendingpass12345678"):
    """直插一行 pending_activation 用户（verify 建号已退役；email_verified_at
    非空模拟存量 email_verify 形态）。"""
    import secrets as _secrets
    from werkzeug.security import generate_password_hash
    uid = "usr_" + _secrets.token_urlsafe(8)
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                "password_hash, role, created_at, disabled, ai_config, "
                "ai_access, activation_state, activation_source, "
                "activation_updated_at, email, email_normalized, "
                "email_verified_at) VALUES (%s,%s,%s,%s,'user', now(), FALSE, "
                "'{}'::jsonb, FALSE, 'pending_activation', "
                "'invite_activation', now(), %s, %s, now()) RETURNING "
                "user_id, auth_version",
                (uid, email, email, generate_password_hash(password),
                 email, email))
            row = cur.fetchone()
        conn.commit()
        return {"user_id": row["user_id"],
                "auth_version": row["auth_version"]}, password
    finally:
        conn.close()


def test_pending_account_blocked_from_business_api(monkeypatch):
    """I-R4：pending_activation 账号即使拿到普通 session 形态也全拒
    （2026-10-08 §4 后新用户不再进入 pending，守卫对存量行仍成立）。"""
    app_mod.AUTH_ENABLED = True
    user, _ = _insert_pending_row()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "blocked@x.com", "user_id": user["user_id"],
                  "role": "user", "auth_version": user["auth_version"]})
    r = client.get("/api/admin/v1/users")
    assert r.status_code == 403
    assert r.get_json()["error"] == "account_pending"
    # 页面 → 302（无 enrollment 时回 /login）
    r2 = client.get("/admin")
    assert r2.status_code == 302


# =========================================================================== #
# 3.5 D1 回归：favicon 放行（公开路径）
# =========================================================================== #
def test_favicon_public_without_session():
    """favicon 对匿名也公开（与 /healthz 同类）：204 且不 302 /login。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/favicon.ico")
    assert r.status_code == 204
    assert r.get_data() == b""


# =========================================================================== #
# 4. 退役端点：/activate、/api/account/activate、/api/account/enrollment
# =========================================================================== #
def test_activation_endpoints_retired(monkeypatch):
    """激活面退役（2026-10-08 §4）：GET /activate 302 /login；activate/
    enrollment API 对已登录调用方稳定 410 endpoint_retired。"""
    app_mod.AUTH_ENABLED = True
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    # GET /activate：不再有激活页（匿名与登录一致 302 /login）
    assert client.get("/activate").status_code == 302
    anon = _client()
    assert anon.get("/activate").status_code == 302
    # enrollment 状态端点：410（不读 session、不回显 token）
    r = client.get("/api/account/enrollment")
    assert r.status_code == 410
    assert r.get_json()["code"] == "endpoint_retired"
    # 激活 POST：410，不消费任何东西
    r = client.post("/api/account/activate", json={"invite_code": "whatever"})
    assert r.status_code == 410
    assert r.get_json()["code"] == "endpoint_retired"
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM registration_invites")
            assert cur.fetchone()["n"] == 0  # 不消费/不建任何邀请行
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4.5 邀请管理端点退役（审计口径）
# --------------------------------------------------------------------------- #
def test_admin_invite_endpoints_retired(monkeypatch):
    """邀请管理退役（2026-10-08 §4）：列表/创建/撤销 410，零副作用。"""
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    before = len(app_mod.share_store.list_audit(limit=1000))
    assert client.get("/api/admin/v1/invites").status_code == 410
    r = client.post("/api/admin/v1/invites",
                    json={"login_id": "x@x.com", "ttl_hours": 24})
    assert r.status_code == 410
    assert client.post("/api/admin/v1/invites/inv_x/revoke").status_code == 410
    conn = pg_store_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM registration_invites")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()
    after = app_mod.share_store.list_audit(limit=1000)
    assert len(after) == before


# =========================================================================== #
# 5. I-R4：require_active_account 统一守卫
# =========================================================================== #
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


def test_resend_quota_cooldown_daily_and_global_budget(monkeypatch):
    """2026-10-08 设计 §3 配额：同邮箱 5 分钟冷却 + 滚动 24h 两次接纳投递
    + 全站 24h 40 封（jobs+redeliveries 合计；legacy 路径经兼容包装验证
    cooldown/limit 分类仍为统一 rate_limited 文案）。"""
    email = "quota@x.com"
    _enqueue(email)
    # 直接置 sent（否则出冷却后的请求会命中 processing：worker 仍在处理
    # 时不并行新增投递，§3；本用例不开注册模式，不走 drain）
    conn0 = pg_store_connect()
    conn0.autocommit = True
    try:
        with conn0.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET status='sent' "
                "WHERE email_normalized=%s", (email,))
    finally:
        conn0.close()
    with pytest.raises(registration_store.EmailVerifyError) as ei:
        _enqueue(email)
    assert ei.value.code == "rate_limited"
    # backdate 用 autocommit 连接（store 的 enqueue 用独立连接，必须能看到
    # 已提交的回填时间）
    conn = pg_store_connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # 出 5 分钟冷却后第二封可入队；第三封触发同邮箱 24h 两次上限
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '10 minutes' WHERE email_normalized=%s", (email,))
            _enqueue(email)
            with pytest.raises(registration_store.EmailVerifyError) as ed:
                _enqueue(email)
            assert ed.value.code == "rate_limited"
            # 仍在 24h 窗口内（回填不出窗口）→ 依旧拒绝
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '23 hours' WHERE email_normalized=%s", (email,))
            with pytest.raises(registration_store.EmailVerifyError) as ed2:
                _enqueue(email)
            assert ed2.value.code == "rate_limited"
            # 应用日预算 40：清掉本邮箱 job，全局灌满 40 条（24h 内；含
            # redeliveries 计数的权威口径）
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


def test_resend_api_endpoint_retired(monkeypatch):
    """旧 JSON 重发接口退役（2026-10-08 §4）：410，不写队列（public 重发
    走 POST /register/resend，见 test_public_registration）。"""
    _open_email_mode(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/api/registration/resend", json={"email": "rs@x.com"})
    assert r.status_code == 410
    assert r.get_json()["code"] == "endpoint_retired"
    assert _mail_job_count("rs@x.com") == 0


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
    # 用户手里「已收到」的链接仍处于 valid 态（有效期内 uncertain 一律放行）
    assert registration_store.check_verify_token(out["token"])[
        "state"] == "valid"
    # 2026-10-08 §4：legacy 链接不再建号（POST 403，token 不消费）
    r = _client().post("/api/registration/verify",
                       json={"token": out["token"],
                             "password": "longpassword123"})
    assert r.status_code == 403
    assert user_store.get_user_by_login_id("uncertain@x.com") is None
    row = _mail_job_row(out["token"])
    assert row["status"] == "uncertain" and row["consumed_at"] is None


def test_uncertain_token_superseded_by_new_request(monkeypatch):
    """P1-1：uncertain 作业持有的链接与新请求互斥——同邮箱重新入队即作废
    （单活 token 红线对 uncertain 同样成立）。"""
    _open_email_mode(monkeypatch)
    out = _enqueue("sup@x.com")
    snd = _UncertainAfterDataSender()
    registration_mail_worker.drain_once(sender=snd)
    assert _mail_job_row(out["token"])["status"] == "uncertain"
    # 出 5 分钟冷却窗后重新入队（2026-10-08 设计 §3；冷却以投递行数计）
    conn = pg_store_connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = now() - "
                "interval '10 minutes' WHERE email_normalized='sup@x.com'")
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


def test_resend_blocked_when_registration_closed(monkeypatch):
    """closed 下旧 JSON resend 退役 410（模式无关稳定响应），且不写队列。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/api/registration/resend",
                    json={"email": "closedrs@x.com"})
    assert r.status_code == 410
    assert r.get_json()["code"] == "endpoint_retired"
    assert _mail_job_count("closedrs@x.com") == 0


def test_register_email_start_blocked_when_closed(monkeypatch):
    """P1-2：closed 下验证请求（verify start）403，且不写队列。"""
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/register", data={"email": "closedstart@x.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    assert _mail_job_count("closedstart@x.com") == 0
    # 恢复开放后 public 流程可正常入队（双协议勾选 + 版本标识）
    _open_email_mode(monkeypatch)
    import agreement_store
    terms = agreement_store.current_published("user_agreement")
    research = agreement_store.current_published("research_sharing")
    r2 = client.post("/register", data={
        "email": "closedstart@x.com",
        "terms_accepted": "1",
        "terms_version": terms["version"],
        "terms_sha256": terms["content_sha256"],
        "research_version": research["version"],
        "research_sha256": research["content_sha256"]})
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
    settings_store.set_registration_mode("public", updated_by="t")
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
    # 待激活用户（存量 pending 行）：identity=邮箱
    pending, _ = _insert_pending_row("iden@x.com")
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
    # 触发一条带 actor 的审计（owner 经建号组合原语建 user；R6 后
    # POST /api/admin/v1/users 已 410 退役，user.create 审计由原语直写）
    import user_store_pg
    user_store_pg.create_user_with_total_allowance(
        "audit-u@x.com", "auditpass12345678",
        actor_user_id=owner["user_id"])
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
    # 工单 A（0056）：admin 标注默认私有，显式授予该分享后访客才可读评论
    share_store.grant_annotation_to_share(aid, sh["token"])
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
