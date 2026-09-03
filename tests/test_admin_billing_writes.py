# -*- coding: utf-8 -*-
"""Admin API v1 写端点测试（docs admin-billing-plugin-implementation-plan.md
§9/§12.2/§13 PR5/§14.1）。

R3 wave1 起 billing caps/adjustments、turn-budgets 与 /admin/registration
兼容重定向端点已物理删除（bridge 侧无调用方；billing_store 写原语由
tests/test_billing_store.py 在 store 级锁定），本文件只覆盖仍在线的写面：

json 模式（无 PG）：
  - 全部写端点 owner 门控：匿名 401 / user 403 / owner 预览态 403；
  - users 写端点（create/enable/disable/password-reset）与 break-glass
    不变量（owner 不可禁用/启用/重置密码 → 409；disable 推进 auth_version）；
  - PG-only 写端点（ai-access/invites）稳定
    503 pg_backend_required（不降级）。

PG 模式（RUN_PG_TESTS=1）：
  - invites：创建（含 source_code/campaign slug 校验）token 仅一次 + 列表
    永不回 token / 撤销 / 已消费拒绝撤销 / 不存在 404；
  - ai-access：设置/收回 + 不存在 404。

运行：cd 项目根 && python3 -m pytest tests/test_admin_billing_writes.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_billing_writes.py -q）
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import billing_store  # noqa: E402
import share_store_pg  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="billing/invite/budget 写路径需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import psycopg  # noqa: E402
    import budget_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True（owner 门控有真实意义）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
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


def _pg_count(table, where="", args=()):
    """直连计数（同事务回滚断言用：entry/caps 均未落库）。"""
    conn = billing_store._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM %s %s" % (table, where), args)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. owner 门控（匿名 / user / preview）——全部写端点
# --------------------------------------------------------------------------- #
_WRITE_ENDPOINTS = [
    ("POST", "/api/admin/v1/users"),
    ("POST", "/api/admin/v1/users/u_x/enable"),
    ("POST", "/api/admin/v1/users/u_x/disable"),
    ("POST", "/api/admin/v1/users/u_x/ai-access"),
    ("POST", "/api/admin/v1/users/u_x/password-reset"),
    ("GET", "/api/admin/v1/invites"),
    ("POST", "/api/admin/v1/invites"),
    ("POST", "/api/admin/v1/invites/inv_x/revoke"),
]


def test_anonymous_gets_401_on_every_write_endpoint():
    _setup_users()
    for method, path in _WRITE_ENDPOINTS:
        r = getattr(_client(), method.lower())(path, json={})
        assert r.status_code == 401, "%s %s -> %s" % (method, path, r.status_code)
        assert r.get_json()["error"] == "auth_required"


def test_user_gets_403_on_every_write_endpoint():
    owner, usera = _setup_users()
    c = _login(_client(), usera)
    for method, path in _WRITE_ENDPOINTS:
        r = getattr(c, method.lower())(path, json={})
        assert r.status_code == 403, "%s %s -> %s" % (method, path, r.status_code)


def test_preview_owner_rejected_on_every_write_endpoint():
    """owner 预览成 user：管理写端点一律 403（§14.1 权限行）。"""
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/preview/start",
                  json={"user_id": usera["user_id"]}).status_code == 200
    for method, path in _WRITE_ENDPOINTS:
        r = getattr(c, method.lower())(path, json={})
        assert r.status_code == 403, "%s %s -> %s" % (method, path, r.status_code)


# --------------------------------------------------------------------------- #
# 2. users 写端点（两种后端同语义；break-glass 镜像旧端点）
# --------------------------------------------------------------------------- #
def test_users_create_basic_and_guards():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "newbie@x.com", "password": "longpassword-12345",
        "display_name": "Newbie"})
    assert r.status_code == 200, r.get_json()
    user = r.get_json()["user"]
    assert user["role"] == "user"
    assert user["login_id"] == "newbie@x.com"
    # §9 敏感红线：不回 password_hash / ai_config
    assert "password_hash" not in user
    assert "ai_config" not in user
    # 禁止经此创建 owner
    assert c.post("/api/admin/v1/users", json={
        "login_id": "hack@x.com", "password": "longpassword-12345",
        "role": "owner"}).status_code == 400
    # 密码长度策略（15..200）
    assert c.post("/api/admin/v1/users", json={
        "login_id": "short@x.com", "password": "short"}).status_code == 400
    # 冲突 409
    assert c.post("/api/admin/v1/users", json={
        "login_id": "owner@x.com", "password": "longpassword-12345"
    }).status_code == 409


def test_users_disable_enable_auth_version_and_break_glass():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    before = usera.get("auth_version") or 1
    # owner 不可禁用/启用/重置（break-glass 不变量 5）
    assert c.post("/api/admin/v1/users/%s/disable" % owner["user_id"]
                  ).status_code == 409
    assert c.post("/api/admin/v1/users/%s/enable" % owner["user_id"]
                  ).status_code == 409
    assert c.post("/api/admin/v1/users/%s/password-reset" % owner["user_id"],
                  json={"password": "longpassword-12345"}).status_code == 409
    # 不存在 404
    assert c.post("/api/admin/v1/users/ghost/disable").status_code == 404
    assert c.post("/api/admin/v1/users/ghost/password-reset",
                  json={"password": "longpassword-12345"}).status_code == 404
    # disable → disabled + auth_version 递增（旧 session 失效）
    r = c.post("/api/admin/v1/users/%s/disable" % usera["user_id"])
    assert r.status_code == 200
    body = r.get_json()["user"]
    assert body["disabled"] is True or body["disabled"] == 1
    assert int(body["auth_version"]) == before + 1
    # enable 同样推进 auth_version（docs §6.2）
    r = c.post("/api/admin/v1/users/%s/enable" % usera["user_id"])
    assert r.status_code == 200
    assert int(r.get_json()["user"]["auth_version"]) == before + 2


def test_users_password_reset_validation():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/v1/users/%s/password-reset" % usera["user_id"],
                  json={"password": "short"}).status_code == 400
    assert c.post("/api/admin/v1/users/%s/password-reset" % usera["user_id"],
                  json={}).status_code == 400
    r = c.post("/api/admin/v1/users/%s/password-reset" % usera["user_id"],
               json={"password": "newlongpassword-999"})
    assert r.status_code == 200
    assert "password" not in r.get_json()["user"]


# --------------------------------------------------------------------------- #
# 3. json/dual fail-closed（PG-only 写端点稳定 503）
# --------------------------------------------------------------------------- #
def test_json_pg_only_write_endpoints_fail_closed():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用反向用例（PG 模式跑正向路径）")
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    for method, path, body in (
            ("POST", "/api/admin/v1/users/%s/ai-access" % usera["user_id"],
             {"enabled": True}),
            ("GET", "/api/admin/v1/invites", None),
            ("POST", "/api/admin/v1/invites", {}),
            ("POST", "/api/admin/v1/invites/inv_x/revoke", {})):
        r = getattr(c, method.lower())(path, json=body)
        assert r.status_code == 503, "%s %s -> %s" % (method, path, r.status_code)
        assert r.get_json()["error"]["code"] == "pg_backend_required"


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 7. ai-access（PG）
# --------------------------------------------------------------------------- #
@PG
def test_ai_access_set_and_unset():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users/%s/ai-access" % usera["user_id"],
               json={"enabled": True})
    assert r.status_code == 200
    assert r.get_json()["user"]["ai_access"] in (True, 1)
    r = c.post("/api/admin/v1/users/%s/ai-access" % usera["user_id"],
               json={"enabled": False})
    assert r.status_code == 200
    assert not r.get_json()["user"]["ai_access"]
    assert c.post("/api/admin/v1/users/ghost/ai-access",
                  json={"enabled": True}).status_code == 404
    assert c.post("/api/admin/v1/users/%s/ai-access" % usera["user_id"],
                  json={"enabled": "yes"}).status_code == 400


# --------------------------------------------------------------------------- #
# 8. invites（PG）：创建校验 / token 仅一次 / 撤销（Batch B wave 2：来源字段
#    退役 400 retired_invite_field；初始金额字段 total_limit_nano_cny）
# --------------------------------------------------------------------------- #
@PG
def test_invites_create_token_once_and_slug_validation():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "invitee@x.com", "ttl_hours": 24, "ai_access": True,
        "note": "n"})
    assert r.status_code == 200, r.get_json()
    invite = r.get_json()["invite"]
    assert invite["token"]  # 明文仅此一次
    assert r.headers.get("Cache-Control") == "no-store"
    # 列表永不回 token/token_hash
    r2 = c.get("/api/admin/v1/invites")
    assert r2.status_code == 200
    items = r2.get_json()["invites"]
    assert len(items) == 1
    assert "token" not in items[0] and "token_hash" not in items[0]
    assert items[0]["login_id_masked"]
    # Batch D1 13（§4.4）：来源字段退役——slug 校验随字段一并退役，任何
    # source_code/campaign_id/cohort 出现在请求体 → 400 retired_invite_field
    for field in ("source_code", "campaign_id", "cohort"):
        r_ret = c.post("/api/admin/v1/invites", json={field: "Bad Slug!"})
        assert r_ret.status_code == 400, field
        assert r_ret.get_json()["error"]["code"] == "retired_invite_field"
    # 初始总额度模板：JSON number 拒绝；十进制字符串接受
    assert c.post("/api/admin/v1/invites", json={
        "total_limit_nano_cny": 5}).status_code == 400
    assert c.post("/api/admin/v1/invites", json={
        "total_limit_nano_cny": "5"}).status_code == 200
    # R3 Wave2-Compat：旧 monthly 字段退役——body 带该键（含与 total 同传）
    # 一律 400 retired_spend_field（绝不静默忽略）
    r_amb = c.post("/api/admin/v1/invites", json={
        "total_limit_nano_cny": "5", "monthly_limit_nano_cny": "5"})
    assert r_amb.status_code == 400
    assert r_amb.get_json()["error"]["code"] == "retired_spend_field"
    r_ret = c.post("/api/admin/v1/invites", json={
        "monthly_limit_nano_cny": "5"})
    assert r_ret.status_code == 400
    assert r_ret.get_json()["error"]["code"] == "retired_spend_field"
    # ttl 边界（0 会回退默认 TTL——与旧端点 `or 默认值` 语义一致；负数/超限 400）
    assert c.post("/api/admin/v1/invites",
                  json={"ttl_hours": -5}).status_code == 400
    assert c.post("/api/admin/v1/invites",
                  json={"ttl_hours": 721}).status_code == 400


@PG
def test_invites_pagination():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    for i in range(5):
        assert c.post("/api/admin/v1/invites", json={"ttl_hours": 1}
                      ).status_code == 200
    r = c.get("/api/admin/v1/invites?limit=2")
    body = r.get_json()
    assert len(body["invites"]) == 2 and body["next_cursor"]
    r2 = c.get("/api/admin/v1/invites?limit=2&cursor=" + body["next_cursor"])
    body2 = r2.get_json()
    assert len(body2["invites"]) == 2
    seen = {i["invite_id"] for i in body["invites"] + body2["invites"]}
    assert len(seen) == 4  # 无重复


@PG
def test_invites_revoke_semantics():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    invite = c.post("/api/admin/v1/invites", json={"ttl_hours": 1}
                    ).get_json()["invite"]
    r = c.post("/api/admin/v1/invites/%s/revoke" % invite["invite_id"])
    assert r.status_code == 200
    assert r.get_json()["invite"]["status"] == "revoked"
    # 幂等：再次撤销仍 200
    assert c.post("/api/admin/v1/invites/%s/revoke" % invite["invite_id"]
                  ).status_code == 200
    assert c.post("/api/admin/v1/invites/inv_ghost/revoke").status_code == 404
    # 已消费邀请不可撤销
    import registration_store
    consumed = registration_store.create_invite(
        owner["user_id"], ttl_seconds=3600)
    # 直接把 use_count 置满并写 consumed_at，构造已消费行
    conn = billing_store._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_invites SET consumed_at=now(), "
                "use_count=1 WHERE invite_id=%s", (consumed["invite_id"],))
        conn.commit()
    finally:
        conn.close()
    r = c.post("/api/admin/v1/invites/%s/revoke" % consumed["invite_id"])
    assert r.status_code == 409


