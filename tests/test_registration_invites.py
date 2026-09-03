# -*- coding: utf-8 -*-
"""P0-B 邀请注册测试（docs §4.1–§4.4 / §6.1）。

两段：
  - 模式与 fail-closed（json/PG 双跑，monkeypatch 存储值）：closed/invite_only/
    public 三模式行为；前置条件缺失降级 closed；启动检查告警；PUT 校验；
    CSRF 缺失 400；
  - PG 数据层（仅 RUN_PG_TESTS=1）：token 只存 hash、明文只在创建返回一次；
    无/随机/过期/撤销/已消费统一失败；邮箱规范化与不匹配；20 路并发兑换仅一个
    成功；邮箱唯一冲突时邀请码未消费；token 不出现在列表/审计/异常文本；
    旧布尔 registration_open 开关已删（mode 键缺行降级 closed）；owner 管理 API。
"""
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import platform_features  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client, isolate_app # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="registration_invites 数据层需 PG（RUN_PG_TESTS=1）",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：json 路径隔离 + 恢复注册相关 env/状态。"""
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    # 每用例重置 fail-closed 闸的进程内告警标记与 env
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    for name in ("PUBLIC_BASE_URL", "ADMIN_SESSION_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    if BACKEND == "postgres":
        # review R2-F2：兑换（redeem_invite）与建号统一走「维护闸 + 开通锁」
        # 组合原语，闸 fail-closed（platform_settings 缺 ai_dispatch_maintenance
        # 即拒绝）。conftest TRUNCATE 清掉 0029 种子，每用例幂等重放
        # （target=window + 闸=false）；维护闸矩阵用例自行覆盖闸值。
        import _billing_helpers as bh
        bh.seed_spend_settings()
    yield


def _raw_client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _client(auth=True):
    return csrf_client(_raw_client(auth))


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: mode)


def _satisfy_preconditions(monkeypatch):
    """env 前置条件（HTTPS + Secure Cookie）。

    注意**不**伪造 platform_features.STORAGE_BACKEND：json 后端下存储语义必须
    保持真实（前置条件判定会如实报「非 postgres」）。需要在 json 后端打开路由
    闸的用例改用 _open_route_gate（只 patch 判定函数）。
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://path.example.com")
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")


def _open_route_gate(monkeypatch):
    """json 后端下打开路由层 invite_only 闸（仅 patch 前置条件判定函数）。"""
    _satisfy_preconditions(monkeypatch)
    monkeypatch.setattr(app_mod, "_registration_precondition_failures",
                        lambda environ=None: [])


def _mk_owner():
    return user_store.create_user("inv-owner@x.com", "ownerpass123456", role="owner")


def _owner_session(client, owner):
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"],
                  "role": "owner", "auth_version": owner.get("auth_version", 1)})


# =========================================================================== #
# 1. 三模式 fail-closed 与前置条件（json/PG 双跑）
# =========================================================================== #
def test_register_closed_mode_get_and_post():
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "当前采用邀请注册" in body
    assert "<form" not in body
    r2 = client.post("/register", data={"login_id": "n@x.com",
                                        "password": "password1password1"})
    assert r2.status_code == 403
    assert "邀请注册" in (r2.get_json() or {}).get("error", "")


def test_register_public_mode_not_supported(monkeypatch):
    _set_mode(monkeypatch, "public")
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 503
    assert r.get_json()["code"] == "public_registration_not_supported"
    r2 = client.post("/register", data={"invite_token": "x",
                                        "login_id": "n@x.com",
                                        "password": "password1password1"})
    assert r2.status_code == 503
    assert r2.get_json()["code"] == "public_registration_not_supported"


def test_register_invite_only_renders_form(monkeypatch):
    _set_mode(monkeypatch, "invite_only")
    _open_route_gate(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<form" in body
    assert 'name="invite_token"' in body
    assert 'name="csrf_token"' in body
    assert 'autocomplete="new-password"' in body
    assert "15" in body  # 密码长度口径（批次 A 统一 15..200；minlength=15）


def test_invite_only_degraded_without_preconditions(monkeypatch, caplog):
    """存储值 invite_only 但非 HTTPS/非 Secure/非 PG → 生效模式降级 closed。"""
    _set_mode(monkeypatch, "invite_only")
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    assert "<form" not in r.get_data(as_text=True)  # 关闭态页
    r2 = client.post("/register", data={"invite_token": "x",
                                        "login_id": "n@x.com",
                                        "password": "password1password1"})
    assert r2.status_code == 403
    assert app_mod._effective_registration_mode() == "closed"


@pytest.mark.parametrize("missing", [
    "PUBLIC_BASE_URL", "ADMIN_SESSION_COOKIE_SECURE", "BACKEND",
])
def test_precondition_failures_detected(monkeypatch, missing):
    _satisfy_preconditions(monkeypatch)
    if missing == "PUBLIC_BASE_URL":
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    elif missing == "ADMIN_SESSION_COOKIE_SECURE":
        monkeypatch.delenv("ADMIN_SESSION_COOKIE_SECURE", raising=False)
    else:  # BACKEND：降级为 json
        monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "json")
    failures = app_mod._registration_precondition_failures()
    assert failures, "前置条件缺失未被检测到（%s）" % missing
    _set_mode(monkeypatch, "invite_only")
    assert app_mod._effective_registration_mode() == "closed"


def test_http_public_base_url_rejected(monkeypatch):
    _satisfy_preconditions(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://path.example.com")
    assert app_mod._registration_precondition_failures()


def test_startup_check_warns_and_degrades(monkeypatch, caplog):
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "invite_only")
    monkeypatch.delenv("ADMIN_SESSION_COOKIE_SECURE", raising=False)
    with caplog.at_level("WARNING"):
        app_mod._check_registration_preconditions_or_warn()
    assert any("前置条件" in rec.getMessage() for rec in caplog.records)


def test_put_registration_mode_validates(monkeypatch):
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    # public 拒绝（v1；旧路由已随 R3 wave1 删除）
    r = client.put("/api/admin/v1/settings/registration", json={"mode": "public"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "public_registration_not_supported"
    # invite_only 前置条件不满足 → 400（json 后端）
    r2 = client.put("/api/admin/v1/settings/registration",
                    json={"mode": "invite_only"})
    assert r2.status_code == 400
    assert "前置条件" in r2.get_json()["error"]["message"]
    # 非法值
    r3 = client.put("/api/admin/v1/settings/registration", json={"mode": "oops"})
    assert r3.status_code == 400


def test_put_registration_mode_invite_only_with_preconditions(monkeypatch):
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    r = client.put("/api/admin/v1/settings/registration",
                   json={"mode": "invite_only"})
    if BACKEND == "postgres":
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["mode"] == "invite_only"
        # 生效模式受前置条件闸放行
        assert app_mod._effective_registration_mode() == "invite_only"
    else:
        # json 后端：前置条件如实判定失败（STORAGE_BACKEND 非 postgres）→ 400
        assert r.status_code == 400
        assert "前置条件" in r.get_json()["error"]["message"]
    # GET settings 聚合反映模式与前置条件（旧专用 GET 已删除）
    g = client.get("/api/admin/v1/settings")
    assert g.status_code == 200
    body = g.get_json()["registration"]
    assert body["supported_modes"] == ["closed", "invite_only"]


def test_register_post_csrf_missing_400(monkeypatch):
    """invite_only 下 POST /register 缺 CSRF token → 400（统一 CSRF 层）。"""
    _set_mode(monkeypatch, "invite_only")
    _open_route_gate(monkeypatch)
    app_mod.AUTH_ENABLED = True
    raw = _raw_client()
    raw.get("/register")
    r = raw.post("/register", data={"invite_token": "x", "login_id": "n@x.com",
                                    "password": "password1password1"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


def test_admin_registration_apis_require_owner(monkeypatch):
    _satisfy_preconditions(monkeypatch)
    app_mod.AUTH_ENABLED = True
    client = _client()
    # 未登录（AUTH_ENABLED=True）→ 401（v1；旧 invites 路由已删除）
    assert client.get("/api/admin/v1/invites").status_code == 401
    # 非 owner → 403
    u = user_store.create_user("plain@x.com", "userpass1234567", role="user")
    with client.session_transaction() as s:
        s.update({"auth_user": "p", "user_id": u["user_id"], "role": "user",
                  "auth_version": u.get("auth_version", 1)})
    assert client.get("/api/admin/v1/invites").status_code == 403
    assert client.post("/api/admin/v1/invites",
                       json={}).status_code == 403


# =========================================================================== #
# 2. PG 数据层：token / 兑换 / 并发 / 审计
# =========================================================================== #
def _pg_conn():
    """直连 PG（dict_row），核对库内行（token_hash 等）。仅 PG 模式可调。"""
    import psycopg
    import pg_store
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


@pg_only
def test_create_invite_stores_only_hash():
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="Alice@X.com")
    assert inv["token"] and len(inv["token"]) >= 43  # token_urlsafe(32)
    assert inv["ai_access"] is False
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM registration_invites WHERE invite_id=%s",
                        (inv["invite_id"],))
            row = dict(cur.fetchone())
    finally:
        conn.close()
    assert row["token_hash"] == registration_store.invite_token_hash(
        inv["token"])
    assert inv["token"] not in row["token_hash"]
    assert row["login_id_normalized"] == "alice@x.com"
    assert row["max_uses"] == 1 and row["use_count"] == 0


@pg_only
def test_list_invites_never_returns_token_or_hash():
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"], login_id="a@x.com")
    items = registration_store.list_invites()
    assert len(items) == 1
    dumped = repr(items)
    assert inv["token"] not in dumped
    assert "token_hash" not in items[0]


@pg_only
def test_redeem_success_consumes_invite_and_creates_user():
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"], login_id="bob@x.com",
                                           ai_access=True, cohort="t1")
    out = registration_store.redeem_invite(inv["token"], "bob@x.com",
                                           "longpassword123", "Bob")
    user = user_store.get_user(out["user"]["user_id"])
    assert user["role"] == "user"
    assert user["login_id"] == "bob@x.com"
    assert user["display_name"] == "Bob"
    assert user["ai_access"] is True
    assert user_store.verify_user("bob@x.com", "longpassword123") is not None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 1 and row["consumed_at"] is not None
    assert row["consumed_by_user_id"] == user["user_id"]
    # 再兑换：已消费 → 统一失败
    with pytest.raises(registration_store.InviteRedeemError) as ei:
        registration_store.redeem_invite(inv["token"], "bob@x.com",
                                         "longpassword123")
    assert ei.value.code == "invite_invalid_or_unavailable"


@pg_only
def test_redeem_failures_all_unified():
    owner = _mk_owner()
    msgs = set()
    # 随机 token
    with pytest.raises(registration_store.InviteRedeemError) as e1:
        registration_store.redeem_invite("totally-random-token-xyz",
                                         "x@x.com", "longpassword123")
    msgs.add(str(e1.value))
    # 过期
    expired = registration_store.create_invite(owner["user_id"],
                                               login_id="e@x.com", ttl_seconds=1)
    time.sleep(1.1)
    with pytest.raises(registration_store.InviteRedeemError) as e2:
        registration_store.redeem_invite(expired["token"], "e@x.com",
                                         "longpassword123")
    msgs.add(str(e2.value))
    # 撤销
    revoked = registration_store.create_invite(owner["user_id"],
                                               login_id="r@x.com")
    registration_store.revoke_invite(revoked["invite_id"], owner["user_id"])
    with pytest.raises(registration_store.InviteRedeemError) as e3:
        registration_store.redeem_invite(revoked["token"], "r@x.com",
                                         "longpassword123")
    msgs.add(str(e3.value))
    # 无 token（空）
    with pytest.raises(registration_store.InviteRedeemError) as e4:
        registration_store.redeem_invite("", "x@x.com", "longpassword123")
    msgs.add(str(e4.value))
    # 全部失败对外一个文案（无细分状态信号）
    assert len(msgs) == 1
    assert "邀请码无效或不可用" in msgs.pop()
    # 细分 reason 只在内部属性，不进 str()
    assert e1.value.reason == "not_found"
    assert e2.value.reason == "expired"
    assert e3.value.reason == "revoked"


@pg_only
def test_redeem_email_normalization_and_mismatch():
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="Carol@X.com")
    # 大小写/空白规范化后匹配
    out = registration_store.redeem_invite(inv["token"], "  carol@x.COM ",
                                           "longpassword123")
    assert out["login_id"] == "carol@x.com"
    # 不匹配邮箱：统一失败（不泄露是绑定差异）
    inv2 = registration_store.create_invite(owner["user_id"],
                                            login_id="dave@x.com")
    with pytest.raises(registration_store.InviteRedeemError) as ei:
        registration_store.redeem_invite(inv2["token"], "mallory@x.com",
                                         "longpassword123")
    assert ei.value.reason == "email_mismatch"
    # 邀请码未被消费
    assert registration_store.get_invite(inv2["invite_id"])[
        "consumed_at"] is None


@pg_only
def test_redeem_email_conflict_leaves_invite_unconsumed():
    owner = _mk_owner()
    user_store.create_user("taken@x.com", "existingpass1234", role="user")
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="taken@x.com")
    with pytest.raises(registration_store.InviteRedeemError) as ei:
        registration_store.redeem_invite(inv["token"], "taken@x.com",
                                         "longpassword123")
    assert ei.value.reason == "email_taken"
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 0
    assert row["consumed_at"] is None


@pg_only
def test_concurrent_redeem_same_invite_single_winner():
    """§6.1：同一邀请码 20 路并发兑换仅一个成功，users/invite 状态一致。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="race@x.com")
    n = 20
    barrier = threading.Barrier(n)

    def worker(_i):
        barrier.wait()
        try:
            out = registration_store.redeem_invite(inv["token"], "race@x.com",
                                                   "longpassword123")
            return ("ok", out["user"]["user_id"])
        except registration_store.InviteRedeemError:
            return ("fail", None)
        except Exception as exc:  # 意外异常单列
            return ("unexpected:%s" % type(exc).__name__, None)

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(worker, range(n)))
    ok = [r for r in results if r[0] == "ok"]
    assert len(ok) == 1, results
    assert all(r[0] == "fail" for r in results if r[0] != "ok")
    winner_uid = ok[0][1]
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 1
    assert row["consumed_by_user_id"] == winner_uid
    # users 表只有一个该邮箱账号
    u = user_store.get_user_by_login_id("race@x.com")
    assert u["user_id"] == winner_uid


@pg_only
def test_token_never_in_audit_or_exceptions():
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"], login_id="aud@x.com")
    # 失败兑换（不匹配邮箱）与成功兑换各来一次（另一个邀请）
    try:
        registration_store.redeem_invite(inv["token"], "wrong@x.com",
                                         "longpassword123")
    except registration_store.InviteRedeemError:
        pass
    inv2 = registration_store.create_invite(owner["user_id"], login_id="aud2@x.com")
    registration_store.redeem_invite(inv2["token"], "aud2@x.com",
                                     "longpassword123")
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_id, actor_user_id, action, target_type, "
                "target_id, detail::text AS detail FROM audit_events "
                "WHERE action LIKE 'registration%'")
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows, "缺少注册审计事件"
    for r in rows:
        blob = repr(dict(r))
        assert inv["token"] not in blob
        assert inv2["token"] not in blob
        assert "password" not in blob.lower()
    # 异常文本不含 token
    try:
        registration_store.redeem_invite(inv["token"], "wrong@x.com",
                                         "longpassword123")
    except registration_store.InviteRedeemError as ei:
        assert inv["token"] not in str(ei)


@pg_only
def test_owner_invite_api_lifecycle(monkeypatch):
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    # 创建：token 仅出现一次 + no-store；初始总额度字段 total_limit_nano_cny
    #（Batch B wave 2）；cohort/source/campaign 不再出现在请求或响应
    r = client.post("/api/admin/v1/invites",
                    json={"login_id": "flow@x.com", "ai_access": False,
                          "note": "n1",
                          "total_limit_nano_cny": "3000000000"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_json()["invite"]
    assert body["token"]
    invite_id = body["invite_id"]
    assert "token_hash" not in body
    assert body["total_limit_nano_cny"] == "3000000000"
    for retired in ("cohort", "source_code", "campaign_id",
                    "monthly_limit_nano_cny"):
        assert retired not in body, retired
    # 列表：无 token/token_hash，登录账号掩码，无来源字段
    r2 = client.get("/api/admin/v1/invites")
    assert r2.status_code == 200
    items = r2.get_json()["invites"]
    assert len(items) == 1
    it = items[0]
    assert "token" not in it and "token_hash" not in it
    assert it["login_id_masked"] == "f***@x.com"
    assert it["status"] == "open"
    assert it["ai_access"] is False
    assert it["total_limit_nano_cny"] == "3000000000"
    for retired in ("cohort", "source_code", "campaign_id",
                    "monthly_limit_nano_cny"):
        assert retired not in it, retired
    # 撤销
    r3 = client.post("/api/admin/v1/invites/%s/revoke" % invite_id,
                     json={})
    assert r3.status_code == 200
    assert r3.get_json()["invite"]["status"] == "revoked"
    # 再撤销（幂等）仍 200；已消费的撤销 409
    assert client.post(
        "/api/admin/v1/invites/%s/revoke" % invite_id,
        json={}).status_code == 200
    # 不存在 404
    assert client.post(
        "/api/admin/v1/invites/inv_missing/revoke",
        json={}).status_code == 404
    # CSRF 缺失 400
    r4 = client._base.post("/api/admin/v1/invites", json={})
    assert r4.status_code == 400
    assert r4.get_json()["error"] == "csrf_required"


@pg_only
def test_register_route_full_flow(monkeypatch):
    """invite_only 全链路：创建 → 表单兑换 → 清 session 轮换 CSRF → 跳登录。"""
    _satisfy_preconditions(monkeypatch)
    # 真实模式写入（PG）：owner PUT 生效
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    r = client.put("/api/admin/v1/settings/registration",
                   json={"mode": "invite_only"})
    assert r.status_code == 200, r.get_data(as_text=True)
    inv = client.post("/api/admin/v1/invites",
                      json={"login_id": "flow2@x.com"}).get_json()["invite"]
    # 匿名 client 兑换
    anon = _client()
    anon.get("/register")
    with anon.session_transaction() as s:
        s["poison"] = "anon-session-data"  # 模拟匿名 session 残留
    r2 = anon.post("/register", data={
        "invite_token": inv["token"], "login_id": "flow2@x.com",
        "password": "longpassword123", "password_confirm": "longpassword123"})
    assert r2.status_code == 302
    assert r2.headers["Location"].endswith("/login")
    with anon.session_transaction() as s:
        assert s.get("poison") is None       # 匿名 session 已清理
        assert not s.get("auth_user")        # 不自动登录
    assert user_store.get_user_by_login_id("flow2@x.com") is not None
    # 错误兑换：统一文案、邀请码不回显
    anon2 = _client()
    r3 = anon2.post("/register", data={
        "invite_token": inv["token"], "login_id": "flow2@x.com",
        "password": "longpassword123", "password_confirm": "longpassword123"})
    assert r3.status_code == 403
    body = r3.get_data(as_text=True)
    assert "邀请码无效或当前不可用" in body
    assert inv["token"] not in body
    # 切回 closed
    r4 = client.put("/api/admin/v1/settings/registration", json={"mode": "closed"})
    assert r4.status_code == 200


@pg_only
def test_redeem_invite_whitespace_password_bad_input():
    """兑换防御层（P2）：全空白密码（长度达标）→ bad_input，不建号不消费。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"], login_id="ws@x.com")
    with pytest.raises(registration_store.InviteRedeemError) as ei:
        registration_store.redeem_invite(inv["token"], "ws@x.com", " " * 15)
    assert ei.value.reason == "bad_input"
    assert user_store.get_user_by_login_id("ws@x.com") is None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["use_count"] == 0 and row["consumed_at"] is None


@pg_only
def test_register_route_whitespace_password_rejected(monkeypatch):
    """注册表单（P2）：全空白密码 → 表单错误回显，不建号不消费邀请码。"""
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    assert client.put("/api/admin/v1/settings/registration",
                      json={"mode": "invite_only"}).status_code == 200
    inv = client.post("/api/admin/v1/invites",
                      json={"login_id": "wsform@x.com"}).get_json()["invite"]
    anon = _client()
    anon.get("/register")
    r = anon.post("/register", data={
        "invite_token": inv["token"], "login_id": "wsform@x.com",
        "password": " " * 15, "password_confirm": " " * 15})
    assert r.status_code == 200  # 表单错误回显（不 302）
    body = r.get_data(as_text=True)
    assert "全空白" in body
    assert user_store.get_user_by_login_id("wsform@x.com") is None
    # 邀请码未被消费：同一邀请码随后可正常兑换
    r2 = anon.post("/register", data={
        "invite_token": inv["token"], "login_id": "wsform@x.com",
        "password": "longpassword123", "password_confirm": "longpassword123"})
    assert r2.status_code == 302


@pg_only
def test_registration_mode_missing_row_fails_closed(monkeypatch):
    """§4.1 + R3 Wave2-Compat：旧布尔 registration_open 开关已删除——mode 键
    缺行直接降级 closed 并 bootstrap 回写（fail-closed，不读任何旧键）。"""
    mode = settings_store.get_registration_mode()
    assert mode == "closed"
    # 固化后 PG 权威为 closed；owner 显式切 invite_only 才能开放
    assert settings_store.get_setting(settings_store.REGISTRATION_MODE_KEY) \
        == "closed"
    settings_store.set_registration_mode("invite_only", updated_by="t")
    assert settings_store.get_registration_mode() == "invite_only"


@pg_only
def test_registration_mode_env_and_invalid_values():
    assert settings_store.get_registration_mode() == "closed"  # bootstrap
    settings_store.set_setting(settings_store.REGISTRATION_MODE_KEY,
                               "invite_only", updated_by="t")
    assert settings_store.get_registration_mode() == "invite_only"
    # 非法存量值按 closed
    settings_store.set_setting(settings_store.REGISTRATION_MODE_KEY, 123,
                               updated_by="t")
    assert settings_store.get_registration_mode() == "closed"
    # setter 拒绝 public
    with pytest.raises(ValueError):
        settings_store.set_registration_mode("public")


# =========================================================================== #
# 3. PG 数据层（Batch D1 13 / Batch B wave 2）：来源字段退役与兑换解耦
# =========================================================================== #
@pg_only
def test_create_invite_retired_source_fields_ignored_not_written():
    """Batch B §4.4：邀请只负责注册——source/campaign/cohort 参数兼容接受
    但忽略（不校验不写库，app.py wave 2 才改调用方）；不做 campaign 校验。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(
        owner["user_id"], login_id="sc1@x.com", source_code="Bad Slug!",
        campaign_id="no-such-campaign", cohort="c1")
    # 返回/落库三退役字段均为空（新邀请不再写入）
    assert inv["source_code"] in ("", None)
    assert inv["campaign_id"] in ("", None)
    assert inv["cohort"] in ("", None)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_code, campaign_id, cohort, "
                        "total_limit_nano_cny FROM registration_invites "
                        "WHERE invite_id=%s", (inv["invite_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["source_code"] == "" and row["campaign_id"] is None
    assert row["cohort"] == ""
    # create audit 不再携带 source/campaign/cohort/acq 字段（历史 audit 不变）
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detail FROM audit_events WHERE action="
                        "'registration.invite_create' AND target_id=%s",
                        (inv["invite_id"],))
            detail = cur.fetchone()["detail"]
    finally:
        conn.close()
    assert not ({"source_code", "campaign_id", "cohort", "acq",
                 "campaign_bound"} & set(detail))
    # 新字段：total_limit_nano_cny 落列；旧 monthly 形参已物理删除
    # （R3 Wave2-Compat；传入即 TypeError）
    inv2 = registration_store.create_invite(
        owner["user_id"], login_id="sc2@x.com", total_limit_nano_cny=10 ** 9)
    assert inv2["total_limit_nano_cny"] == 10 ** 9
    with pytest.raises(TypeError):
        registration_store.create_invite(
            owner["user_id"], login_id="sc3@x.com",
            monthly_limit_nano_cny=10 ** 9, total_limit_nano_cny=10 ** 9)


@pg_only
def test_invite_list_exposes_source_campaign_and_owner_api(monkeypatch):
    """Batch D1 13 / Batch B wave 2：来源字段退役——owner API 带 source/
    campaign/cohort 请求一律 400 retired_invite_field（不再校验 slug/campaign
    存在性）；响应不回显来源字段；初始总额度字段可用。"""
    _satisfy_preconditions(monkeypatch)
    owner = _mk_owner()
    app_mod.AUTH_ENABLED = True
    client = _client()
    _owner_session(client, owner)
    r = client.post("/api/admin/v1/invites", json={
        "login_id": "apisrc@x.com", "source_code": "src-api",
        "campaign_id": "camp-api"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "retired_invite_field"
    # 非法 slug / 未知 campaign / cohort 同样 400 retired_invite_field
    for payload in ({"source_code": "NOT A SLUG"},
                    {"campaign_id": "ghost"}, {"cohort": "c1"}):
        r2 = client.post("/api/admin/v1/invites", json=payload)
        assert r2.status_code == 400, payload
        assert r2.get_json()["error"]["code"] == "retired_invite_field", payload
    # 新契约：创建带初始总额度 → 响应 total_limit_nano_cny、无来源字段
    r3 = client.post("/api/admin/v1/invites", json={
        "login_id": "apitotal@x.com", "total_limit_nano_cny": "7000000000"})
    assert r3.status_code == 200, r3.get_data(as_text=True)
    body = r3.get_json()["invite"]
    assert body["total_limit_nano_cny"] == "7000000000"
    for retired in ("source_code", "campaign_id", "cohort"):
        assert retired not in body
    # 列表同样无来源字段；掩码规则不变（login_id_masked，无 token/hash）
    r4 = client.get("/api/admin/v1/invites").get_json()
    it = next(i for i in r4["invites"] if i["invite_id"] == body["invite_id"])
    for retired in ("source_code", "campaign_id", "cohort"):
        assert retired not in it
    assert it["login_id_masked"] == "a***@x.com"
    assert "token" not in it and "token_hash" not in it


@pg_only
def test_redeem_writes_no_user_acquisition_but_allowance():
    """Batch B：兑换不再写 user_acquisition（归因写路径冻结）；R3 Wave1-Money
    单轨后无初始额度的邀请也**恒建** allowance 行（defaults 默认解析）；
    恒 None 兼容键 acquisition/spend_override_policy 已随 R3 Wave2-Compat
    物理删除（不在返回 dict 中）。"""
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="legacy@x.com")
    out = registration_store.redeem_invite(inv["token"], "legacy@x.com",
                                           "pass123456789012")
    assert "acquisition" not in out              # 兼容键已物理删除
    assert "spend_override_policy" not in out    # 兼容键已物理删除
    assert out["total_allowance"] is not None    # 无面值 → defaults 解析建行
    assert out["total_allowance"]["source"] == "invite"
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM user_acquisition "
                "WHERE user_id=%s", (out["user"]["user_id"],))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*)::int AS n FROM user_acquisition")
            total_after = cur.fetchone()["n"]
    finally:
        conn.close()
    assert total_after == 0  # 全表零新增（兑换全链路不再触达归因）


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
