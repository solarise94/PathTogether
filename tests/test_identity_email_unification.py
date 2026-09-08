# -*- coding: utf-8 -*-
"""P1-3 身份收口测试（w1b）：登录 session 主身份邮箱化 + 建号入口只收邮箱 +
存量冲突清单 + orphan pending 处置 + 邮箱改绑闭环 + 分享页掩码不回退。

覆盖（review P1-3 修复范围）：
  1. 登录 session：auth_user = email_normalized → email → login_id
     （display_name 绝不冒充身份）；/api/auth/info 顶层与 actor、身份预览
     subject 同口径；
  2. owner 建号 API：login_id 必须邮箱形态（非邮箱 400 login_id_not_email），
     写入同步 email/email_normalized（email_verified_at NULL=未验证），
     display_name 缺省=邮箱、可选保留；invite_only 注册表单 login_id 必须
     邮箱形态，display_name 输入保留但不再作为身份；
  3. GET /api/admin/v1/users/identity-conflicts：四类冲突行 + 计数（只读）；
  4. POST /api/admin/v1/users/<id>/discard-pending：仅 pending_activation +
     bind.invalid 合成形可物理删除，其余 409（绝不自动夺取已有账号），写审计；
  5. 邮箱改绑闭环：start（唯一预检/配额/无 token 回传）→ 邮件（复用
     registration_mail_jobs，purpose=email_change）→ /verify-email-change
     页面（匿名不泄露状态）→ confirm 单事务（login_id=新邮箱、
     email_verified_at=now、auth_version+1 全端失效、job consumed、审计）；
     占用/过期/一次性/他人 token 全拒绝且不改状态；
  6. 公开分享页评论作者只出掩码邮箱（不回传完整邮箱、不回退 display_name）。
"""
import json
import os
import re
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
import identity_store  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402

BASE = "https://path.example.com"
PW = "longpassword123456"


def pg_conn():
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器清空 + 异步排水禁用）。"""
    from _pt_helpers import isolate_app
    import _billing_helpers as bh
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY"):
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


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _raw_client(auth=True):
    """不带 CSRF 自动附带的裸 client（CSRF 负路径用）。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _mk_owner():
    return user_store.create_user("idn-owner@x.com", PW, role="owner")


def _owner_session(client, owner):
    with client.session_transaction() as s:
        s.update({"auth_user": owner["login_id"],
                  "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})


def _user_session(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user["login_id"],
                  "user_id": user["user_id"], "role": user.get("role", "user"),
                  "auth_version": user.get("auth_version", 1)})


def _login(client, username, password=PW):
    return client.post("/login", data={"username": username,
                                       "password": password})


def _set_email(uid, email, verified=True):
    """给既有用户行补可信邮箱（email_verified_at 非 NULL 才可信）。"""
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


def _user_row(uid):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, login_id, display_name, role, disabled, "
                "auth_version, activation_state, activation_source, "
                "email, email_normalized, "
                "extract(epoch from email_verified_at)::float8 AS "
                "email_verified_at FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _mk_pending_bind_row(login_id="pending-0123456789abcdef@bind.invalid",
                         email_norm="orphan@x.com"):
    """直插一行「待补绑孤儿」（模拟 verify_email_create_user 冲突路径产物）。"""
    from werkzeug.security import generate_password_hash
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                " password_hash, role, disabled, ai_config, ai_access, "
                " activation_state, activation_source, activation_updated_at,"
                " email, email_normalized, email_verified_at) "
                "VALUES ('usr_pendingbind01', %s, %s, %s, 'user', FALSE, "
                " '{}'::jsonb, FALSE, 'pending_activation', "
                " 'invite_activation', now(), %s, %s, now()) "
                "RETURNING user_id",
                (login_id, email_norm, generate_password_hash(PW),
                 email_norm, email_norm))
            uid = cur.fetchone()["user_id"]
        conn.commit()
        return uid
    finally:
        conn.close()


def _mail_job_rows(token=None):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            if token is not None:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs WHERE token_hash=%s",
                    (registration_store.verify_token_hash(token),))
            else:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs "
                    "WHERE purpose='email_change'")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fake():
    return registration_mail_worker.install_fake_sender()


def _extract_change_token(body_text):
    m = re.search(r"/verify-email-change\?token=([A-Za-z0-9_\-]+)",
                  body_text)
    return m.group(1) if m else None


def _start_change(client, new_email):
    return client.post("/api/account/email/change/start",
                       json={"new_email": new_email})


# =========================================================================== #
# 1. 登录 session 主身份邮箱化
# =========================================================================== #
def test_login_auth_user_prefers_verified_email(monkeypatch):
    """有可信邮箱的账号：登录后 auth_user=邮箱，display_name 不再冒充身份。"""
    _mk_owner()
    u = user_store.create_user("alice@x.com", PW, display_name="Alice 旧名")
    _set_email(u["user_id"], "alice@x.com")
    client = _client()
    r = _login(client, "alice@x.com")
    assert r.status_code == 302, r.get_data(as_text=True)
    info = client.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "alice@x.com"
    assert info["username"] == "alice@x.com"
    assert "Alice 旧名" not in json.dumps(info)


def test_login_auth_user_falls_back_to_login_id(monkeypatch):
    """存量无邮箱账号：auth_user 回退 login_id（当前唯一用户名）。"""
    owner = user_store.create_user("legacychief", PW, role="owner",
                                   display_name="老管理员")
    client = _client()
    r = _login(client, "legacychief")
    assert r.status_code == 302, r.get_data(as_text=True)
    info = client.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "legacychief"
    assert info["username"] == "legacychief"
    assert owner["user_id"] == info["user_id"]


def test_auth_info_preview_subject_email_first(monkeypatch):
    """身份预览 subject 与登录 session 同口径：email 优先，非 display_name。"""
    owner = _mk_owner()
    u = user_store.create_user("bob@x.com", PW, display_name="Bob 旧名")
    _set_email(u["user_id"], "bob@x.com")
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": owner["login_id"],
                  "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1),
                  "preview": {"subject_user_id": u["user_id"],
                              "expires_at": time.time() + 300,
                              "actor_user_id": owner["user_id"]}})
    info = client.get("/api/auth/info").get_json()
    assert info["preview"] is not None
    assert info["preview"]["subject_username"] == "bob@x.com"
    assert info["actor"]["username"] == owner["login_id"]


# =========================================================================== #
# 2. 建号入口只收邮箱
# =========================================================================== #
def test_admin_create_rejects_non_email_login_id(monkeypatch):
    """非邮箱形态 login_id → 400 login_id_not_email，且无用户行落库。"""
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    r = client.post("/api/admin/v1/users",
                    json={"login_id": "plainuser", "password": PW})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"]["code"] == "login_id_not_email"
    assert user_store.get_user_by_login_id("plainuser") is None


def test_admin_create_writes_email_identity_columns(monkeypatch):
    """邮箱建号：email/email_normalized=规范化邮箱、email_verified_at NULL、
    display_name 缺省=邮箱、显式 display_name 保留。"""
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    r = client.post("/api/admin/v1/users",
                    json={"login_id": "  BOB@X.Com ", "password": PW})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["user"]["user_id"]
    row = _user_row(uid)
    assert row["login_id"] == "bob@x.com"
    assert row["email"] == "bob@x.com"
    assert row["email_normalized"] == "bob@x.com"
    assert row["email_verified_at"] is None  # 绝不伪造验证状态
    assert row["display_name"] == "bob@x.com"  # 缺省=邮箱（纯展示）
    assert row["activation_state"] == "active"
    r2 = client.post("/api/admin/v1/users",
                     json={"login_id": "carol@x.com", "password": PW,
                           "display_name": "Carol 展示名"})
    assert r2.status_code == 200
    row2 = _user_row(r2.get_json()["user"]["user_id"])
    assert row2["display_name"] == "Carol 展示名"
    assert row2["login_id"] == "carol@x.com"


def test_admin_create_email_conflict_409(monkeypatch):
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    r1 = client.post("/api/admin/v1/users",
                     json={"login_id": "dup@x.com", "password": PW})
    assert r1.status_code == 200
    r2 = client.post("/api/admin/v1/users",
                     json={"login_id": "dup@x.com", "password": PW})
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "login_id_conflict"


def test_invite_only_register_requires_email_login_id(monkeypatch):
    """invite_only 表单：login_id 必须邮箱形态；display_name 保留可填。"""
    owner = _mk_owner()
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    settings_store.set_registration_mode("invite_only", updated_by="t")
    inv = registration_store.create_invite(
        owner["user_id"], login_id="dave@x.com")
    client = _client()

    def _post(login_id, display_name=""):
        return client.post("/register", data={
            "invite_token": inv["token"], "login_id": login_id,
            "display_name": display_name, "password": PW,
            "password_confirm": PW})

    # 非邮箱形态：本地形状错误（200 页面回显），无用户行
    r_bad = _post("dave")
    assert r_bad.status_code == 200
    assert "邮箱" in r_bad.get_data(as_text=True)
    assert user_store.get_user_by_login_id("dave") is None
    # 邮箱形态（大小写/空白规范化）+ 可选显示名：302 /login，兑换成功
    r_ok = _post("  DAVE@X.com ", display_name="Dave 展示名")
    assert r_ok.status_code == 302, r_ok.get_data(as_text=True)
    u = user_store.get_user_by_login_id("dave@x.com")
    assert u is not None
    assert u["display_name"] == "Dave 展示名"
    # GET 页面：登录账号输入框为邮箱形态（type=email + 邮箱占位符）
    page = client.get("/register").get_data(as_text=True)
    assert 'type="email"' in page
    assert "you@example.com" in page


# =========================================================================== #
# 3. 存量冲突清单 API
# =========================================================================== #
def test_identity_conflicts_empty(monkeypatch):
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    r = client.get("/api/admin/v1/users/identity-conflicts")
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == []
    assert body["counts"]["total_conflicting_rows"] == 0
    for key in ("login_id_not_email", "email_login_mismatch",
                "pending_bind_synthetic", "email_shared"):
        assert body["counts"][key] == 0


def test_identity_conflicts_classifies_and_gates(monkeypatch):
    owner = _mk_owner()
    # ① login_id 非邮箱形态（存量 display_name != login_id 的旧账号形态）
    legacy = user_store.create_user("oldchief", PW, role="user",
                                    display_name="老 Chief")
    # ② email_normalized 与 login_id 不一致
    mismatch = user_store.create_user("mike@x.com", PW)
    _set_email(mismatch["user_id"], "mike-renamed@x.com")
    # ③ 待补绑孤儿（pending + bind.invalid 合成形）
    orphan_uid = _mk_pending_bind_row()
    # ④ 同 email_normalized 多行：该形态被 users_email_identity_key 部分
    # 唯一索引在库层拦截（pending_activation+active 两态内唯一）——清单里的
    # email_shared 是针对「历史/损坏数据」的防御性分类，此处临时移除索引
    # 构造（断言后解除冲突并恢复索引，不污染 session 级共享 PG 的 schema）。
    shared_a = user_store.create_user("shared-a@x.com", PW)
    shared_b = user_store.create_user("shared-b@x.com", PW)
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS users_email_identity_key")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET email=%s, email_normalized=%s, "
                "email_verified_at=now() WHERE user_id IN (%s,%s)",
                ("clash@x.com", "clash@x.com", shared_a["user_id"],
                 shared_b["user_id"]))
        conn.commit()
    finally:
        conn.close()

    client = _client()
    # 非 owner：403
    _user_session(client, legacy)
    assert client.get(
        "/api/admin/v1/users/identity-conflicts").status_code == 403
    # 匿名：401
    anon = _client()
    assert anon.get(
        "/api/admin/v1/users/identity-conflicts").status_code == 401
    # owner：四类齐出 + 计数（owner 本行 login_id=邮箱形，不在清单内）
    _owner_session(client, owner)
    body = client.get("/api/admin/v1/users/identity-conflicts").get_json()
    by_uid = {it["user_id"]: it for it in body["items"]}
    assert set(by_uid) == {legacy["user_id"], mismatch["user_id"],
                           orphan_uid, shared_a["user_id"],
                           shared_b["user_id"]}
    assert by_uid[legacy["user_id"]]["conflicts"] == ["login_id_not_email"]
    assert by_uid[mismatch["user_id"]]["conflicts"] == ["email_login_mismatch"]
    # 待补绑孤儿：合成 login_id 必然 ≠ email_normalized，双类命中
    # （信息更全：该行既可 discard，也提示 login_id 与邮箱不一致）
    assert set(by_uid[orphan_uid]["conflicts"]) == {
        "email_login_mismatch", "pending_bind_synthetic"}
    assert by_uid[orphan_uid]["discardable"] is True
    # shared 两行同时命中 email_login_mismatch（login_id 是各自旧名）+
    # email_shared
    for uid in (shared_a["user_id"], shared_b["user_id"]):
        assert "email_shared" in by_uid[uid]["conflicts"]
        assert "email_login_mismatch" in by_uid[uid]["conflicts"]
        assert by_uid[uid]["email_shared_key"] == "clash@x.com"
    counts = body["counts"]
    assert counts["login_id_not_email"] == 1
    assert counts["email_login_mismatch"] == 4
    assert counts["pending_bind_synthetic"] == 1
    assert counts["email_shared"] == 2
    assert counts["total_conflicting_rows"] == 5
    # 只读：响应前后用户行数不变
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM users")
            assert int(cur.fetchone()["n"]) == 6
    finally:
        conn.close()
    # 清理：解除冲突 → 重建唯一索引（与 0037 同定义；测试后 schema 复原）
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET email=NULL, email_normalized=NULL, "
                "email_verified_at=NULL WHERE user_id=%s",
                (shared_b["user_id"],))
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_identity_key "
                "ON users (lower(email_normalized)) "
                "WHERE email_normalized IS NOT NULL "
                "AND activation_state IN ('pending_activation','active')")
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
# 4. orphan pending 处置
# =========================================================================== #
def test_discard_pending_deletes_orphan_and_audits(monkeypatch):
    owner = _mk_owner()
    orphan_uid = _mk_pending_bind_row()
    client = _client()
    _owner_session(client, owner)
    r = client.post("/api/admin/v1/users/%s/discard-pending" % orphan_uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    assert _user_row(orphan_uid) is None  # 物理删除
    events = client.get(
        "/api/admin/v1/audit?action=user.pending_discard").get_json()["items"]
    assert any(e["target_id"] == orphan_uid for e in events)
    ev = next(e for e in events if e["target_id"] == orphan_uid)
    # 审计 detail 无明文邮箱（掩码）
    assert "orphan@x.com" not in json.dumps(ev["detail"])
    # 二次删除：404
    r2 = client.post("/api/admin/v1/users/%s/discard-pending" % orphan_uid)
    assert r2.status_code == 404


def test_discard_pending_rejects_everything_else(monkeypatch):
    """红线：绝不自动夺取已有账号——active/正常 pending/owner 一律 409。"""
    owner = _mk_owner()
    active = user_store.create_user("keeper@x.com", PW)
    _set_email(active["user_id"], "keeper@x.com")
    # pending 但 login_id 是正常邮箱形（邮箱验证后、绑定前的正常形态）
    from werkzeug.security import generate_password_hash
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                " password_hash, role, disabled, ai_config, ai_access, "
                " activation_state, activation_source, activation_updated_at,"
                " email, email_normalized, email_verified_at) "
                "VALUES ('usr_pendingnormal', 'normal@x.com', 'normal@x.com',"
                " %s, 'user', FALSE, '{}'::jsonb, FALSE, "
                " 'pending_activation', 'invite_activation', now(), "
                " 'normal@x.com', 'normal@x.com', now())",
                (generate_password_hash(PW),))
        conn.commit()
    finally:
        conn.close()
    client = _client()
    _owner_session(client, owner)
    for uid in (active["user_id"], owner["user_id"], "usr_pendingnormal"):
        r = client.post("/api/admin/v1/users/%s/discard-pending" % uid)
        assert r.status_code == 409, (uid, r.get_data(as_text=True))
        assert r.get_json()["error"]["code"] == "not_discardable"
    # 行全部原样存在
    assert _user_row(active["user_id"]) is not None
    assert _user_row(owner["user_id"]) is not None
    assert _user_row("usr_pendingnormal") is not None
    # 非 owner / 匿名：403 / 401
    _user_session(client, active)
    assert client.post(
        "/api/admin/v1/users/%s/discard-pending"
        % active["user_id"]).status_code == 403
    anon = _client()
    assert anon.post(
        "/api/admin/v1/users/%s/discard-pending"
        % active["user_id"]).status_code == 401


# =========================================================================== #
# 5. 邮箱改绑闭环
# =========================================================================== #
def _mk_login_user(name):
    u = user_store.create_user(name, PW)
    _set_email(u["user_id"], name)
    return u


def _logged_in_client(name):
    u = _mk_login_user(name)
    client = _client()
    r = _login(client, name)
    assert r.status_code == 302
    return u, client


def test_email_change_start_requires_login_and_csrf(monkeypatch):
    # 匿名 /api：401 auth_required（鉴权闸先于 CSRF 闸）
    anon = _client()
    assert anon.post("/api/account/email/change/start",
                     json={"new_email": "n@x.com"}).status_code == 401
    # 登录态但无 CSRF token：400 csrf_required（裸 client 不带 header）
    u = user_store.create_user("csrfless@x.com", PW)
    _set_email(u["user_id"], "csrfless@x.com")
    raw = _raw_client()
    with raw.session_transaction() as s:
        s.update({"auth_user": u["login_id"], "user_id": u["user_id"],
                  "role": "user", "auth_version": u.get("auth_version", 1)})
    r = raw.post("/api/account/email/change/start",
                 json={"new_email": "else@x.com"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


def test_email_change_start_validates_shape_and_uniqueness(monkeypatch):
    u, client = _logged_in_client("startu@x.com")
    _mk_owner()
    other = _mk_login_user("taken@x.com")
    # 形状错误 → 400
    r_bad = _start_change(client, "not-an-email")
    assert r_bad.status_code == 400
    assert r_bad.get_json()["code"] == "invalid_request"
    # 目标邮箱被他人占用（users_email_identity_key 口径）→ 409，无作业行
    r_taken = _start_change(client, "Taken@X.com ")
    assert r_taken.status_code == 409
    assert r_taken.get_json()["code"] == "email_taken"
    # 目标邮箱与另一账号 login_id 冲突（改名会撞唯一键）→ 409
    r_login = _start_change(client, other["login_id"])
    assert r_login.status_code == 409
    # 合法请求：200；响应只含掩码，token 绝不回传；作业行落库
    r_ok = _start_change(client, "New-Mail@X.com")
    assert r_ok.status_code == 200, r_ok.get_data(as_text=True)
    body = r_ok.get_data(as_text=True)
    assert r_ok.get_json()["email_masked"] == "n***@x.com"
    assert "token" not in r_ok.get_json()
    jobs = _mail_job_rows()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["purpose"] == "email_change"
    assert job["status"] == "queued"
    assert job["email_normalized"] == "new-mail@x.com"
    assert job["token_hash"]  # 只存 hash
    payload = registration_mail_worker.decrypt_payload(job["payload_enc"])
    assert payload["user_id"] == u["user_id"]  # payload 绑定 user_id
    assert payload["email"] == "new-mail@x.com"
    del other


def test_email_change_start_rate_limited_per_email(monkeypatch):
    _logged_in_client("rl@x.com")
    client = _client()
    _login(client, "rl@x.com")
    assert _start_change(client, "a1@x.com").status_code == 200
    # 同邮箱 60s 冷却内第二次 → 429（配额权威=作业行）
    r = _start_change(client, "a1@x.com")
    assert r.status_code == 429
    assert r.get_json()["code"] == "rate_limited"


def test_email_change_start_unconfigured_channel(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    _logged_in_client("ch@x.com")
    client = _client()
    _login(client, "ch@x.com")
    r = _start_change(client, "elsewhere@x.com")
    assert r.status_code == 503
    assert r.get_json()["code"] == "email_channel_unavailable"
    assert _mail_job_rows() == []  # fail-closed：无任何落库


def test_email_change_mail_and_page(monkeypatch):
    """邮件经既有通道外发；GET 页面只展示（valid 掩码/匿名只提示登录）。"""
    u, client = _logged_in_client("pageu@x.com")
    assert _start_change(client, "forward@x.com").status_code == 200
    sent_before = len(_fake().sent)
    assert registration_mail_worker.drain_once() == 1
    to, subject, body = _fake().sent[-1]
    assert to == "forward@x.com"
    assert "改绑" in subject
    token = _extract_change_token(body)
    assert token, body
    # 库内只有 hash，明文 token 不落库
    rows = _mail_job_rows(token)
    assert len(rows) == 1 and token not in json.dumps(
        _mail_job_rows(), default=str)
    # 本人在册：valid + 掩码邮箱
    page = client.get("/verify-email-change?token=%s" % token)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "确认改绑" in html
    assert "f***@x.com" in html
    assert "forward@x.com" not in html  # 掩码，不回显完整
    # 匿名：只提示先登录，不泄露状态/邮箱
    anon = _client()
    anon_page = anon.get("/verify-email-change?token=%s" % token)
    anon_html = anon_page.get_data(as_text=True)
    assert "请先登录" in anon_html
    assert "f***@x.com" not in anon_html
    # 他人登录态：绑定不符按 unknown 渲染
    _mk_login_user("otherguy@x.com")
    other_client = _client()
    _login(other_client, "otherguy@x.com")
    other_html = other_client.get(
        "/verify-email-change?token=%s" % token).get_data(as_text=True)
    assert "链接无效" in other_html
    assert "f***@x.com" not in other_html
    assert sent_before == 0  # fake 全程无真实外发前无历史
    del u


def test_email_change_confirm_happy_path(monkeypatch):
    """闭环：login_id=新邮箱、email_verified_at=now、auth_version+1、
    job consumed、审计、全端会话失效、新用户名可登录。"""
    owner = _mk_owner()
    u, client = _logged_in_client("oldname@x.com")
    old_version = _user_row(u["user_id"])["auth_version"]
    assert _start_change(client, "New.Name@X.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    r = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["login_id"] == "new.name@x.com"
    row = _user_row(u["user_id"])
    assert row["login_id"] == "new.name@x.com"
    assert row["email"] == "new.name@x.com"
    assert row["email_normalized"] == "new.name@x.com"
    assert row["email_verified_at"] is not None
    assert row["auth_version"] == old_version + 1  # 全端会话失效
    jobs = _mail_job_rows(token)
    assert jobs[0]["status"] == "consumed" and jobs[0]["consumed_at"]
    # 同事务审计（掩码，无 token；经 owner 端点读取——本人 session 已失效）
    oc = _client()
    _owner_session(oc, owner)
    events = oc.get(
        "/api/admin/v1/audit?action=account.email_change").get_json()["items"]
    ev = next(e for e in events if e["target_id"] == u["user_id"])
    assert "new.name@x.com" not in json.dumps(ev["detail"])
    # 旧 session（auth_version 过期）已被 _require_auth 清除 → 401
    assert client.get("/api/auth/info").status_code == 401
    # 新用户名（新邮箱）+ 旧密码可登录
    fresh = _client()
    assert _login(fresh, "new.name@x.com").status_code == 302
    info = fresh.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "new.name@x.com"
    # 旧用户名不复存在
    assert user_store.get_user_by_login_id("oldname@x.com") is None


def test_email_change_confirm_rejections_keep_state(monkeypatch):
    """一次性 / 他人 token / 过期：全部拒绝且不改任何状态。"""
    u, client = _logged_in_client("one@x.com")
    assert _start_change(client, "two@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    # 他人登录态确认：400，状态不变
    _mk_login_user("intruder@x.com")
    other = _client()
    _login(other, "intruder@x.com")
    r_other = other.post("/api/account/email/change/confirm",
                         json={"token": token})
    assert r_other.status_code == 400
    assert _user_row(u["user_id"])["login_id"] == "one@x.com"
    assert _mail_job_rows(token)[0]["consumed_at"] is None
    # 伪造 token：400
    r_fake = client.post("/api/account/email/change/confirm",
                         json={"token": "forged-token-value"})
    assert r_fake.status_code == 400
    # 本人确认成功后重复使用：400（一次性；client 旧 session 已随
    # auth_version+1 失效，重播需换新登录态）
    r1 = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r1.status_code == 200
    row_after = _user_row(u["user_id"])
    replayer = _client()
    with replayer.session_transaction() as s:
        s.update({"auth_user": row_after["login_id"],
                  "user_id": u["user_id"], "role": "user",
                  "auth_version": row_after["auth_version"]})
    r2 = replayer.post("/api/account/email/change/confirm",
                       json={"token": token})
    assert r2.status_code == 400
    # 过期 token：重新发起 → 改库内 expires_at → 确认 400
    client2 = _client()
    with client2.session_transaction() as s:
        s.update({"auth_user": "new.name@x.com", "user_id": u["user_id"],
                  "role": "user",
                  "auth_version": _user_row(u["user_id"])["auth_version"]})
    assert _start_change(client2, "three@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token3 = _extract_change_token(_fake().sent[-1][2])
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET expires_at="
                "now() - interval '1 second' WHERE token_hash=%s",
                (registration_store.verify_token_hash(token3),))
        conn.commit()
    finally:
        conn.close()
    r3 = client2.post("/api/account/email/change/confirm",
                      json={"token": token3})
    assert r3.status_code == 400
    # 全程只有第一次确认生效（one@x.com → two@x.com）
    assert _user_row(u["user_id"])["login_id"] == "two@x.com"


def test_email_change_confirm_email_taken_rolls_back(monkeypatch):
    """确认时目标邮箱已被其他账号占用：409，job 不消费、用户行不动。"""
    u, client = _logged_in_client("racer@x.com")
    assert _start_change(client, "prize@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    # 竞争者先用 owner 建号通道占住 prize@x.com
    owner_client = _client()
    _owner_session(owner_client, _mk_owner())
    r_create = owner_client.post(
        "/api/admin/v1/users",
        json={"login_id": "prize@x.com", "password": PW})
    assert r_create.status_code == 200
    old_version = _user_row(u["user_id"])["auth_version"]
    r = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r.status_code == 409
    assert r.get_json()["code"] == "email_taken"
    row = _user_row(u["user_id"])
    assert row["login_id"] == "racer@x.com"  # 未改名
    assert row["auth_version"] == old_version  # 未推进版本
    assert _mail_job_rows(token)[0]["status"] in ("queued", "sent")
    assert _mail_job_rows(token)[0]["consumed_at"] is None  # 保持未消费


# =========================================================================== #
# 6. 公开分享页身份输出保持掩码（不回传完整邮箱）
# =========================================================================== #
def test_share_comment_author_stays_masked(monkeypatch):
    """P1-3 回归护栏：auth_user/身份改邮箱后，分享页评论作者仍只出掩码。"""
    import share_server
    import share_store
    owner = _mk_owner()
    member = user_store.create_user("member@x.com", PW, display_name="老展示名")
    _set_email(member["user_id"], "member@x.com")
    name = "idn-share.svs"
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    share_store.set_slide_meta(name, owner_user_id=owner["user_id"])
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, name, "L", type="rect",
                              x=0, y=0, side_px=10, size_mm=6.0)
    aid = roi["annotation_id"]
    share_store.add_comment(aid, name, share_store.ADMIN_TOKEN, "from member",
                            author_user_id=member["user_id"],
                            author_label="member@x.com")
    sh = share_store.create_share([name], 24,
                                  permissions=["view", "annotate"])
    share_server.app.config["TESTING"] = True
    sc = share_server.app.test_client()
    resp = sc.get("/s/%s/api/comments?annotation_id=%s" % (sh["token"], aid))
    assert resp.status_code == 200
    text = json.dumps(resp.get_json())
    assert "member@x.com" not in text  # 完整邮箱绝不外泄
    assert "m***@x.com" in text  # 掩码出线
    assert "老展示名" not in text  # 不回退 display_name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_closed_pauses_verify_mail_but_sends_email_change(monkeypatch):
    """P1-2 语义细化（合并后修正）：closed 只停注册类邮件（email_verify
    保持 queued 不发送）；登录用户的本人账户服务邮件（email_change）
    不在注册停机范围内，照常被 worker 发送。"""
    settings_store.set_registration_mode("closed", updated_by="t")
    registration_store.enqueue_email_verification("paused-verify@x.com",
                                               base_url="https://pt.test")
    u, client = _logged_in_client("stillmail@x.com")
    assert _start_change(client, "stillmail-new@x.com").status_code == 200
    assert registration_mail_worker.drain_once() == 1
    # 只发了改绑邮件；注册验证邮件仍 queued
    assert len(_fake().sent) == 1
    assert _fake().sent[0][0] == "stillmail-new@x.com"
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM registration_mail_jobs "
                "WHERE purpose='email_verify' "
                "AND email_normalized='paused-verify@x.com'")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert rows and all(r["status"] == "queued" for r in rows)
