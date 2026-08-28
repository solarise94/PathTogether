# -*- coding: utf-8 -*-
"""PR5：Admin API v1 写端点 + billing caps/adjustments 测试（docs
admin-billing-plugin-implementation-plan.md §9/§12.2/§13 PR5/§14.1）。

json 模式（无 PG）：
  - 全部写端点 owner 门控：匿名 401 / user 403 / owner 预览态 403；
  - users 写端点（create/enable/disable/password-reset）与 break-glass
    不变量（owner 不可禁用/启用/重置密码 → 409；disable 推进 auth_version）；
  - PG-only 写端点（ai-access/invites/turn-budgets/caps/adjustments）稳定
    503 pg_backend_required（不降级）；
  - /admin/registration 302 → /admin#invites（旧独立页已删，方案 §13 PR5）。

PG 模式（RUN_PG_TESTS=1）：
  - caps：未开户 404 / CAS 版本冲突 409 / null 清除 / soft>hard 400 /
    非 NEG 400 / 与 audit 同事务（强制 audit 失败 → caps 未落库）；
  - adjustments：kind 符号 400（grant 负数、manual_adjustment 0）/
    reason 空 400 / 缺 idempotency_key 400（§6.5 PR5 修订：调用方生成，
    服务端不代生成）/ grant 未开户自动开户 / refund 未开户 404 / 幂等键
    重放 duplicate:true 不重复入账 / audit 同事务回滚（ledger 无残留）；
  - invites：创建（含 source_code/campaign slug 校验）token 仅一次 + 列表
    永不回 token / 撤销 / 已消费拒绝撤销 / 不存在 404；
  - turn-budgets：PUT 全字段校验（未知字段 400、子池和 > platform 400），
    new-period 需 confirm=true；
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
os.environ["ADMIN_PASSWORD"] = ""
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
# 1. owner 门控（匿名 / user / preview）——全部 13 个写端点
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
    ("PUT", "/api/admin/v1/turn-budgets"),
    ("POST", "/api/admin/v1/turn-budgets/new-period"),
    ("PUT", "/api/admin/v1/billing/accounts/u_x/caps"),
    ("POST", "/api/admin/v1/billing/adjustments"),
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
            ("POST", "/api/admin/v1/invites/inv_x/revoke", {}),
            ("PUT", "/api/admin/v1/turn-budgets",
             {"platform_turn_limit": 10}),
            ("POST", "/api/admin/v1/turn-budgets/new-period",
             {"confirm": True}),
            ("PUT", "/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
             {"soft_cap_nano_cny": None, "hard_cap_nano_cny": None,
              "version": 1}),
            ("POST", "/api/admin/v1/billing/adjustments",
             {"user_id": usera["user_id"], "kind": "grant",
              "amount_nano_cny": "1", "reason": "x"})):
        r = getattr(c, method.lower())(path, json=body)
        assert r.status_code == 503, "%s %s -> %s" % (method, path, r.status_code)
        assert r.get_json()["error"]["code"] == "pg_backend_required"


# --------------------------------------------------------------------------- #
# 4. /admin/registration 302 兼容（PR5：独立页删除，重定向一个版本）
# --------------------------------------------------------------------------- #
def test_admin_registration_redirects_to_admin_invites():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.get("/admin/registration")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/admin#invites")
    # 匿名仍先走登录闸（_require_auth 页面路径统一 302 /login）
    assert _client().get("/admin/registration").status_code == 302


# --------------------------------------------------------------------------- #
# 5. billing caps（§9 规则逐条；仅 PG）
# --------------------------------------------------------------------------- #
@PG
def test_caps_unopened_account_404_no_implicit_open():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": "100", "hard_cap_nano_cny": "200",
                    "version": 1})
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "billing_account_not_found"
    # 不得伪造开户：账户表仍无行
    assert _pg_count("billing_accounts", "WHERE user_id=%s",
                     (usera["user_id"],)) == 0
    # 用户不存在（区别于未开户）
    r = c.put("/api/admin/v1/billing/accounts/ghost/caps",
              json={"soft_cap_nano_cny": "100", "hard_cap_nano_cny": "200",
                    "version": 1})
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "user_not_found"


@PG
def test_caps_cas_version_conflict_and_update():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    acct = billing_store.create_billing_account(usera["user_id"])
    # 旧 version → 409 version_conflict（不做 last-write-wins）
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": "1", "hard_cap_nano_cny": "2",
                    "version": acct["version"] - 1 if acct["version"] > 1 else 99})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "version_conflict"
    # 正确 version → 200 + version 递增 + 新值生效（§5 v0.3：字符串金额）
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": "1500000000",
                    "hard_cap_nano_cny": "2000000000",
                    "version": acct["version"]})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["account"]["version"] == acct["version"] + 1
    assert body["account"]["soft_spend_cap_nano"] == "1500000000"
    assert body["balance_nano"] == "0"
    # null=清除该上限（§9）
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": None,
                    "hard_cap_nano_cny": "2000000000",
                    "version": body["account"]["version"]})
    assert r.status_code == 200
    assert r.get_json()["account"]["soft_spend_cap_nano"] is None


@PG
def test_caps_validation_errors():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    acct = billing_store.create_billing_account(usera["user_id"])
    # soft > hard → 400
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": "300", "hard_cap_nano_cny": "200",
                    "version": acct["version"]})
    assert r.status_code == 400
    # 负字符串 / 小数 / 布尔 / 数字型 / 超长 / 缺字段 / 坏 version → 400
    for body in (
            {"soft_cap_nano_cny": "-1", "hard_cap_nano_cny": "200",
             "version": acct["version"]},
            {"soft_cap_nano_cny": "1.5", "hard_cap_nano_cny": "200",
             "version": acct["version"]},
            {"soft_cap_nano_cny": True, "hard_cap_nano_cny": "200",
             "version": acct["version"]},
            {"soft_cap_nano_cny": 100, "hard_cap_nano_cny": "200",
             "version": acct["version"]},
            {"soft_cap_nano_cny": "1" * 20, "hard_cap_nano_cny": "200",
             "version": acct["version"]},
            {"hard_cap_nano_cny": "200", "version": acct["version"]},
            {"soft_cap_nano_cny": "1", "hard_cap_nano_cny": "200",
             "version": 0},
            {"soft_cap_nano_cny": "1", "hard_cap_nano_cny": "200",
             "version": "3"},
    ):
        r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
                  json=body)
        assert r.status_code == 400, body
    # 校验失败不产生任何写入或 audit
    assert _pg_count("audit_events", "WHERE action='billing.caps_update'") == 0


@PG
def test_caps_amount_wire_decimal_string_only():
    """§5 v0.3（P2）：caps 金额 wire 只接受十进制字符串，JSON number 一律 400。

    错误消息写明「金额须为十进制字符串（防 float 失真）」；19 位内但超出
    PG BIGINT 的值同样确定性 400（不是 INSERT 500）。
    """
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    acct = billing_store.create_billing_account(usera["user_id"])
    url = "/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"]
    # JSON number（bool 排除后的 int/float）→ 400 + 统一提示
    for bad in (1_500_000_000, 1.5):
        r = c.put(url, json={"soft_cap_nano_cny": bad,
                             "hard_cap_nano_cny": "2000000000",
                             "version": acct["version"]})
        assert r.status_code == 400, bad
        assert "金额须为十进制字符串" in r.get_json()["error"]["message"]
    # 合法 19 位但溢出 BIGINT → 400（确定性，不进 INSERT）
    r = c.put(url, json={"soft_cap_nano_cny": "9223372036854775808",
                         "hard_cap_nano_cny": "9223372036854775808",
                         "version": acct["version"]})
    assert r.status_code == 400
    assert "BIGINT" in r.get_json()["error"]["message"]
    assert _pg_count("audit_events", "WHERE action='billing.caps_update'") == 0


@PG
def test_caps_audit_same_transaction_rollback(monkeypatch):
    """强制 audit 失败 → caps 更新随事务回滚（§9：更新与 audit 必须同事务）。"""
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    acct = billing_store.create_billing_account(usera["user_id"])

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(share_store_pg, "record_audit_tx", _boom)
    r = c.put("/api/admin/v1/billing/accounts/%s/caps" % usera["user_id"],
              json={"soft_cap_nano_cny": "123", "hard_cap_nano_cny": "456",
                    "version": acct["version"]})
    assert r.status_code == 500
    # caps 未落库（version/值保持原样）→ 证明业务写与 audit 同一事务
    after = billing_store.get_billing_account_by_user(usera["user_id"])
    assert after["version"] == acct["version"]
    assert after["soft_spend_cap_nano"] is None


# --------------------------------------------------------------------------- #
# 6. billing adjustments（§9 + §12.2 Phase B；仅 PG）
# --------------------------------------------------------------------------- #
def _adjust(client, body):
    return client.post("/api/admin/v1/billing/adjustments", json=body)


@PG
def test_adjustment_kind_sign_and_reason_validation():
    owner, usera = _setup_users()
    billing_store.create_billing_account(usera["user_id"])
    c = _login(_client(), owner)
    uid = usera["user_id"]
    # kind 符号：grant/topup/refund 必须 >0（400，而非 DB CHECK 的 500）
    assert _adjust(c, {"user_id": uid, "kind": "grant",
                       "amount_nano_cny": "-5", "reason": "r"}).status_code == 400
    assert _adjust(c, {"user_id": uid, "kind": "topup",
                       "amount_nano_cny": "0", "reason": "r"}).status_code == 400
    assert _adjust(c, {"user_id": uid, "kind": "refund",
                       "amount_nano_cny": "-1", "reason": "r"}).status_code == 400
    # manual_adjustment ≠ 0（正负皆可）
    assert _adjust(c, {"user_id": uid, "kind": "manual_adjustment",
                       "amount_nano_cny": "0", "reason": "r"}).status_code == 400
    # 非法 kind / 非十进制字符串金额
    assert _adjust(c, {"user_id": uid, "kind": "usage_debit",
                       "amount_nano_cny": "-5", "reason": "r"}).status_code == 400
    assert _adjust(c, {"user_id": uid, "kind": "grant",
                       "amount_nano_cny": "1.5", "reason": "r"}).status_code == 400
    # reason 必填非空（trim 后 ≥1）
    for bad in (None, "", "   ", "x" * 501):
        r = _adjust(c, {"user_id": uid, "kind": "grant",
                        "amount_nano_cny": "5", "reason": bad})
        assert r.status_code == 400, repr(bad)
    # 校验失败零写入
    assert _pg_count("billing_ledger_entries") == 0


@PG
def test_adjustment_amount_wire_decimal_string_only():
    """§5 v0.3（P2）：调账金额 wire 只接受十进制字符串，JSON number 一律 400。"""
    owner, usera = _setup_users()
    billing_store.create_billing_account(usera["user_id"])
    c = _login(_client(), owner)
    uid = usera["user_id"]
    # JSON number（int/float）/ 缺字段 / 超长字符串 → 400，消息含统一提示
    for bad in (5_000_000_000, 1.5, None, "9" * 20):
        body = {"user_id": uid, "kind": "grant", "reason": "r",
                "idempotency_key": "adj_wire_%r" % (bad,)}
        if bad is not None:
            body["amount_nano_cny"] = bad
        r = _adjust(c, body)
        assert r.status_code == 400, repr(bad)
        if isinstance(bad, (int, float)):
            # JSON number 专属提示（缺失字段走「需为十进制整数字符串」消息）
            assert "金额须为十进制字符串" in r.get_json()["error"]["message"]
    # 19 位但溢出 PG BIGINT → 确定性 400（不是 INSERT 500）
    r = _adjust(c, {"user_id": uid, "kind": "manual_adjustment",
                    "amount_nano_cny": "-9223372036854775809", "reason": "r",
                    "idempotency_key": "adj_overflow"})
    assert r.status_code == 400
    assert "BIGINT" in r.get_json()["error"]["message"]
    assert _pg_count("billing_ledger_entries") == 0


@PG
def test_adjustment_grant_auto_opens_account_and_balances():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    uid = usera["user_id"]
    # grant：未开户 → 同事务显式开户后入账（§9「首次 grant/topup 显式创建」）
    r = _adjust(c, {"user_id": uid, "kind": "grant",
                    "amount_nano_cny": "5000000000", "reason": "新用户体验金",
                    "idempotency_key": "adj_test_grant_1"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["duplicate"] is False
    assert body["entry"]["kind"] == "grant"
    # §5 v0.3（P2）：金额字段一律十进制字符串
    assert body["entry"]["amount_nano_cny"] == "5000000000"
    assert body["balance_nano"] == "5000000000"
    # topup 追加 + manual_adjustment 可为负
    assert _adjust(c, {"user_id": uid, "kind": "topup",
                       "amount_nano_cny": "1000000000",
                       "reason": "充值",
                       "idempotency_key": "adj_test_topup_1"}).status_code == 200
    r = _adjust(c, {"user_id": uid, "kind": "manual_adjustment",
                    "amount_nano_cny": "-500000000", "reason": "冲正多充",
                    "idempotency_key": "adj_test_manual_1"})
    assert r.status_code == 200
    assert r.get_json()["balance_nano"] == "5500000000"
    # 余额 = ledger SUM（权威口径）
    acct = billing_store.get_billing_account_by_user(uid)
    assert billing_store.account_balance_nano(acct["account_id"]) == 5_500_000_000


@PG
def test_adjustment_refund_requires_existing_account():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    uid = usera["user_id"]
    # refund / manual_adjustment 未开户 → 404（不隐式开户）
    r = _adjust(c, {"user_id": uid, "kind": "refund",
                    "amount_nano_cny": "100", "reason": "r",
                    "idempotency_key": "adj_refund_no_acct"})
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "billing_account_not_found"
    r = _adjust(c, {"user_id": uid, "kind": "manual_adjustment",
                    "amount_nano_cny": "-100", "reason": "r",
                    "idempotency_key": "adj_manual_no_acct"})
    assert r.status_code == 404
    assert _pg_count("billing_accounts") == 0
    # 用户不存在 → user_not_found
    r = _adjust(c, {"user_id": "ghost", "kind": "grant",
                    "amount_nano_cny": "1", "reason": "r",
                    "idempotency_key": "adj_ghost_user"})
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "user_not_found"


@PG
def test_adjustment_idempotent_replay():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    uid = usera["user_id"]
    key = "adj_replay_key_1"
    r1 = _adjust(c, {"user_id": uid, "kind": "grant",
                     "amount_nano_cny": "2000000000", "reason": "一次就好",
                     "idempotency_key": key})
    assert r1.status_code == 200
    # UI 语义（PR5 §6.5 修订）：插件在「一次逻辑提交」内生成一个 key，服务端
    # 已入账但浏览器超时后管理员直接重试（表单未动 → 复用同 key 同载荷），
    # 必须命中 duplicate 而不是二次入账。
    r2 = _adjust(c, {"user_id": uid, "kind": "grant",
                     "amount_nano_cny": "2000000000", "reason": "一次就好",
                     "idempotency_key": key})
    assert r2.status_code == 200  # 重放 200（非 4xx）
    body = r2.get_json()
    assert body["duplicate"] is True
    assert body["entry"]["entry_id"] == r1.get_json()["entry"]["entry_id"]
    # 不重复入账：仍只有一条 entry，余额不变（字符串金额，§5 v0.3）
    assert _pg_count("billing_ledger_entries") == 1
    assert body["balance_nano"] == "2000000000"
    # 同一 key 被其他账户占用 → 409（不是重放）
    userb = user_store.create_user("userb@x.com", "userpassbb-abcdef",
                                   role="user")
    billing_store.create_billing_account(userb["user_id"])
    r3 = _adjust(c, {"user_id": userb["user_id"], "kind": "grant",
                     "amount_nano_cny": "5", "reason": "撞 key",
                     "idempotency_key": key})
    assert r3.status_code == 409
    assert r3.get_json()["error"]["code"] == "idempotency_key_conflict"
    # 同一 key + 同一账户但载荷不同（金额/kind/reason 任一不一致）→ 409：
    # 真重放必须逐项一致，否则是客户端 bug 或伪造重放，不得静默返回原行
    for mutated in ({"amount_nano_cny": "3000000000"},
                    {"kind": "topup"},
                    {"reason": "换个理由"}):
        payload = {"user_id": uid, "kind": "grant",
                   "amount_nano_cny": "2000000000", "reason": "一次就好",
                   "idempotency_key": key}
        payload.update(mutated)
        r4 = _adjust(c, payload)
        assert r4.status_code == 409, mutated
        assert r4.get_json()["error"]["code"] == "idempotency_key_conflict"
    # 冲突请求均未入账：仍只有最初一条 entry
    assert _pg_count("billing_ledger_entries") == 1


@PG
def test_adjustment_audit_same_transaction_rollback(monkeypatch):
    """强制 audit 失败 → ledger 入账随事务回滚（§6.5：入账与 audit 同事务）。"""
    owner, usera = _setup_users()
    billing_store.create_billing_account(usera["user_id"])
    c = _login(_client(), owner)

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(share_store_pg, "record_audit_tx", _boom)
    r = _adjust(c, {"user_id": usera["user_id"], "kind": "grant",
                    "amount_nano_cny": "7000000000", "reason": "会被回滚",
                    "idempotency_key": "adj_rollback_1"})
    assert r.status_code == 500
    # ledger 无残留（自动开户场景同样适用：开户 + 入账 + audit 同事务）
    assert _pg_count("billing_ledger_entries") == 0
    acct = billing_store.get_billing_account_by_user(usera["user_id"])
    assert billing_store.account_balance_nano(acct["account_id"]) == 0
    assert _pg_count("audit_events", "WHERE action='billing.adjust'") == 0


@PG
def test_adjustment_requires_caller_generated_idempotency_key():
    """§6.5 PR5 修订：幂等键必须由调用方生成——缺失/空白一律 400，不代生成。

    服务端代生成会让「已入账 + 浏览器超时 + 重试」以新 key 产出第二笔账，
    因此这里锁定：缺 key 的合法载荷也绝不入账。
    """
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    uid = usera["user_id"]
    billing_store.create_billing_account(uid)
    # 缺 key / None / 空白 / 全空格 / 超长 → 400 invalid_request（旧版会代生成）
    for bad in (None, "", "  ", "x" * 129):
        body = {"user_id": uid, "kind": "topup",
                "amount_nano_cny": "100", "reason": "r"}
        if bad is not None:
            body["idempotency_key"] = bad
        r = _adjust(c, body)
        assert r.status_code == 400, repr(bad)
        assert r.get_json()["error"]["code"] == "invalid_request"
    assert _pg_count("billing_ledger_entries") == 0
    # 带调用方生成的 key → 正常入账
    r = _adjust(c, {"user_id": uid, "kind": "topup", "amount_nano_cny": "100",
                    "reason": "r", "idempotency_key": "adj_caller_gen_1"})
    assert r.status_code == 200, r.get_json()
    assert _pg_count("billing_ledger_entries") == 1


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
# 8. invites（PG）：创建校验 / token 仅一次 / 撤销
# --------------------------------------------------------------------------- #
@PG
def test_invites_create_token_once_and_slug_validation():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "invitee@x.com", "ttl_hours": 24, "ai_access": True,
        "cohort": "c1", "note": "n", "source_code": "mywebpage"})
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
    # slug 校验：非法 source_code / 未登记 campaign → 400
    assert c.post("/api/admin/v1/invites",
                  json={"source_code": "Bad Slug!"}).status_code == 400
    assert c.post("/api/admin/v1/invites",
                  json={"campaign_id": "no_such_campaign"}).status_code == 400
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


# --------------------------------------------------------------------------- #
# 9. turn-budgets 写（PG）：字段校验 + confirm
# --------------------------------------------------------------------------- #
@PG
def test_turn_budgets_update_validation_and_apply():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # 未知字段 / 非法关系 → 400
    assert c.put("/api/admin/v1/turn-budgets",
                 json={"unknown_field": 1}).status_code == 400
    assert c.put("/api/admin/v1/turn-budgets",
                 json={"platform_turn_limit": 10, "user_pool_turn_limit": 20}
                 ).status_code == 400
    r = c.put("/api/admin/v1/turn-budgets",
              json={"platform_turn_limit": 500, "user_turn_limit": 50,
                    "demo_enabled": True, "demo_max_concurrency": 3})
    assert r.status_code == 200, r.get_json()
    limits = r.get_json()["limits"]
    assert limits["platform_turn_limit"] == 500
    assert limits["demo_enabled"] in (True, 1)
    # 保存不清空用量（读取端由既有测试覆盖）


@PG
def test_turn_budgets_new_period_requires_confirm():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/turn-budgets/new-period", json={})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "confirm_required"
    r = c.post("/api/admin/v1/turn-budgets/new-period", json={"confirm": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["period_id"]
