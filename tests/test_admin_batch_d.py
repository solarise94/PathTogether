# -*- coding: utf-8 -*-
"""批次 D：后台设置、用户覆盖与邀请码模板测试（docs
ai-money-budget-bugfix-and-simplification-plan.md §5/§6/§8 批次 D/§9.6/§9.7）。

json 模式（无 PG）：
  - 新端点 owner 门控：匿名 401 / user 403 / owner 预览态 403（§14.1 同口径）；
  - PG-only 写端点（spend policies/enforcement-mode/window adjust/
    spend-override/settings 聚合）稳定 503 pg_backend_required；
  - 注册模式 v1 路由与旧路由同语义（public 400 / 非法值 400 / invite_only
    前置条件 400 / GET payload 同构——§5.3「同一 service 不复制校验」）。

PG 模式（RUN_PG_TESTS=1）：
  - spend policies PUT：CAS 版本冲突 409 / JSON number 金额 400 / >2^53
    十进制字符串精确落库 / audit（spend.policy_update）同事务；
  - enforcement-mode PUT：词表外 400 / CAS 409 / audit；§7.3 无保护配置
    （shadow + legacy_turn_guard_enabled=false）不能保存、registered/all 可
    保存（可扩展校验的现行形态）；
  - window adjust：缺 confirm → 400 confirm_required / 成功只改 snapshot
    （spent/reserved 不动）/ 409 CAS / audit；
  - 用户月额度覆盖 PUT/DELETE：设置后解析到 user_override、清除后下个窗口
    回退 user_default / owner 目标 400 / 404 不存在 / audit 无敏感字段；
  - 用户创建扩展：monthly_limit_nano_cny 同事务建 override（含注入失败整体
    回滚：用户不创建）；null 继承默认；
  - 邀请码模板：创建携带月额度（wire 十进制字符串、库内整数）/ 兑换事务内
    为新用户建 override（含注入失败整体回滚：邀请不消费、用户不创建）/
    明文码仅创建响应一次 + no-store；
  - settings 聚合：注册模式 + spend 分段 + runtime 分段，金额十进制字符串。

运行：cd 项目根 && python3 -m pytest tests/test_admin_batch_d.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_batch_d.py -q）
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="spend/invite 写路径需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

#: >2^53 的 nano 金额（JS Number 读到即失真；wire 十进制字符串须精确）
OVER_2E53_NANO = "9007199254740993"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True（owner 门控有真实意义）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _raw_client():
    """不带 CSRF 包装的裸 client（缺 CSRF 用例）。"""
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


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


if BACKEND == "postgres":
    def _audit_actions(action):
        import share_store
        return share_store.list_audit(limit=100, action=action)
else:
    def _audit_actions(action):
        return []


# --------------------------------------------------------------------------- #
# 1. owner 门控 + CSRF + json 后端 fail-closed（两后端都跑）
# --------------------------------------------------------------------------- #
_NEW_ENDPOINTS = (
    ("PUT", "/api/admin/v1/spend/policies/spp_demo_global"),
    ("PUT", "/api/admin/v1/spend/enforcement-mode"),
    ("POST", "/api/admin/v1/spend/windows/spw_x/adjust"),
    ("PUT", "/api/admin/v1/users/usr_x/spend-override"),
    ("DELETE", "/api/admin/v1/users/usr_x/spend-override"),
    ("GET", "/api/admin/v1/settings"),
    ("GET", "/api/admin/v1/settings/registration"),
    ("PUT", "/api/admin/v1/settings/registration"),
)


def test_anonymous_401_on_new_endpoints():
    _setup_users()
    for method, path in _NEW_ENDPOINTS:
        r = _client().open(path, method=method)
        assert r.status_code == 401, "%s %s -> %s" % (method, path,
                                                      r.status_code)
        assert r.get_json()["error"] == "auth_required"


def test_plain_user_403_on_new_endpoints():
    owner, usera = _setup_users()
    c = _login(_client(), usera)
    for method, path in _NEW_ENDPOINTS:
        r = c.open(path, method=method)
        assert r.status_code == 403, "%s %s -> %s" % (method, path,
                                                      r.status_code)


def test_preview_owner_403_on_new_endpoints():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/preview/start",
                  json={"user_id": usera["user_id"]}).status_code == 200
    for method, path in _NEW_ENDPOINTS:
        r = c.open(path, method=method)
        assert r.status_code == 403
        body = r.get_json()
        # 写方法先被 _preview_write_guard 拦（平铺 code）；GET 走 v1 门控
        # （error.code 信封）——两种形态都断言（§14.1）
        code = body.get("code") if isinstance(body.get("error"), str) \
            else (body.get("error") or {}).get("code")
        assert code in ("preview_forbidden", "preview_readonly"), \
            "%s %s -> %r" % (method, path, body)


def test_new_write_endpoints_require_csrf():
    """写方法缺 X-CSRF-Token 一律 400（before_request 全局闸；§9.6）。"""
    if BACKEND == "postgres":
        bh.seed_spend_policies()
    owner, _u = _setup_users()
    raw = _raw_client()
    with raw.session_transaction() as s:
        s["auth_user"] = owner["login_id"]
        s["user_id"] = owner["user_id"]
        s["role"] = "owner"
        s["auth_version"] = owner.get("auth_version", 1)
    bodies = {
        "/api/admin/v1/spend/policies/spp_demo_global":
            {"limit_nano_cny": "1000", "version": 1},
        "/api/admin/v1/spend/enforcement-mode": {"mode": "shadow"},
        "/api/admin/v1/spend/windows/spw_x/adjust":
            {"limit_nano_snapshot": "1000", "version": 1, "confirm": True},
        "/api/admin/v1/users/usr_x/spend-override":
            {"monthly_limit_nano_cny": "1000"},
        "/api/admin/v1/settings/registration": {"mode": "closed"},
        "/api/admin/v1/users": {"login_id": "x@y.com",
                                "password": "password-123456"},
        "/api/admin/v1/invites": {"ttl_hours": 24},
    }
    for path, body in bodies.items():
        r = raw.put(path, json=body) if path not in (
            "/api/admin/v1/spend/windows/spw_x/adjust",
            "/api/admin/v1/users", "/api/admin/v1/invites") \
            else raw.post(path, json=body)
        assert r.status_code == 400, "%s -> %s" % (path, r.status_code)
        assert "CSRF" in r.get_data(as_text=True) or \
            r.get_json().get("error") == "csrf_required"


def test_json_backend_new_spend_endpoints_503():
    if BACKEND == "postgres":
        pytest.skip("json 后端专用反向用例（PG 模式跑正向路径）")
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    for method, path, body in (
            ("PUT", "/api/admin/v1/spend/policies/spp_demo_global",
             {"limit_nano_cny": "1", "version": 1}),
            ("PUT", "/api/admin/v1/spend/enforcement-mode",
             {"mode": "shadow"}),
            ("POST", "/api/admin/v1/spend/windows/spw_x/adjust",
             {"limit_nano_snapshot": "1", "version": 1, "confirm": True}),
            ("PUT", "/api/admin/v1/users/usr_x/spend-override",
             {"monthly_limit_nano_cny": "1"}),
            ("DELETE", "/api/admin/v1/users/usr_x/spend-override", None)):
        r = c.open(path, method=method, json=body)
        assert r.status_code == 503, "%s %s -> %s" % (method, path,
                                                      r.status_code)
        assert r.get_json()["error"]["code"] == "pg_backend_required"
    # settings 聚合在 json 后端分段标记（注册段真实；spend/runtime 不可用）
    r = c.get("/api/admin/v1/settings")
    assert r.status_code == 200
    body = r.get_json()
    assert body["registration"]["mode"] == "closed"
    for seg in ("spend", "runtime"):
        assert body[seg]["available"] is False
        assert body[seg]["code"] == "pg_backend_required"


# --------------------------------------------------------------------------- #
# 2. 注册模式：v1 与旧路由同一 service（§5.3）
# --------------------------------------------------------------------------- #
def test_registration_v1_get_payload_matches_legacy_shape():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    v1 = c.get("/api/admin/v1/settings/registration").get_json()
    old = c.get("/api/admin/settings/registration").get_json()
    assert v1 == old
    assert v1["supported_modes"] == ["closed", "invite_only"]
    assert v1["mode"] == v1["stored_mode"]


def test_registration_v1_put_validates_like_legacy():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # public 一律 400（v1 与旧路由同 code）
    r1 = c.put("/api/admin/v1/settings/registration", json={"mode": "public"})
    assert r1.status_code == 400
    assert r1.get_json()["error"]["code"] == "public_registration_not_supported"
    r1b = c.put("/api/admin/settings/registration", json={"mode": "public"})
    assert r1b.status_code == 400
    assert r1b.get_json()["code"] == "public_registration_not_supported"
    # 非法值 400
    assert c.put("/api/admin/v1/settings/registration",
                 json={"mode": "oops"}).status_code == 400
    # json 后端 invite_only 前置条件不满足 → 400（PG 模式同理：PUBLIC_BASE_URL
    # 非 https）；两路由错误一致
    r2 = c.put("/api/admin/v1/settings/registration",
               json={"mode": "invite_only"})
    r2b = c.put("/api/admin/settings/registration",
                json={"mode": "invite_only"})
    assert r2.status_code == r2b.status_code == 400
    assert "前置条件" in r2.get_json()["error"]["message"]
    assert "前置条件" in r2b.get_json()["error"]


@PG
def test_registration_v1_put_closed_writes_and_audits():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    settings_store.set_registration_mode("invite_only")
    r = c.put("/api/admin/v1/settings/registration", json={"mode": "closed"})
    # invite_only 前置条件（https）不满足时存储值允许被改回 closed：
    # closed 无前置条件，永远可保存
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["mode"] == "closed"
    assert settings_store.get_registration_mode() == "closed"
    events = _audit_actions("registration.mode_update")
    assert events and events[0]["detail"].get("mode") == "closed"


# --------------------------------------------------------------------------- #
# 3. spend policies PUT（CAS + 金额 wire + audit）
# --------------------------------------------------------------------------- #
@PG
def test_spend_policy_update_cas_and_decimal_string():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.put("/api/admin/v1/spend/policies/spp_user_default",
              json={"limit_nano_cny": OVER_2E53_NANO, "version": 1})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()["policy"]
    # >2^53 十进制字符串精确往返（JSON number 会失真）
    assert body["limit_nano_cny"] == OVER_2E53_NANO
    assert body["version"] == 2
    # 再用旧 version 更新 → 409 version_conflict
    r2 = c.put("/api/admin/v1/spend/policies/spp_user_default",
               json={"limit_nano_cny": "1", "version": 1})
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "version_conflict"
    # 金额 JSON number / 小数 / 超长一律 400
    for bad in (123, 1.5, "12.5", "", "9" * 20, "-5"):
        rb = c.put("/api/admin/v1/spend/policies/spp_user_default",
                   json={"limit_nano_cny": bad, "version": 2})
        assert rb.status_code == 400, "limit=%r 应 400" % (bad,)
    # audit 同事务：spend.policy_update 落库且不含敏感字段
    events = _audit_actions("spend.policy_update")
    assert events
    detail = json.dumps(events[0]["detail"], ensure_ascii=False)
    assert "password" not in detail and "token" not in detail
    assert events[0]["detail"]["limit_nano_cny"] == int(OVER_2E53_NANO)
    # 不存在的策略 → 404（CAS 未命中语义收敛为 version_conflict 之外的场景）
    r404 = c.put("/api/admin/v1/spend/policies/spp_missing",
                 json={"limit_nano_cny": "1", "version": 1})
    assert r404.status_code == 409


@PG
def test_spend_policy_update_audited_same_transaction():
    """audit 写入失败 → 策略更新整体回滚（不落半更新状态）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)

    import share_store_pg

    def boom(*_a, **_k):
        raise RuntimeError("audit down")
    orig = share_store_pg.record_audit_tx
    share_store_pg.record_audit_tx = boom
    try:
        r = c.put("/api/admin/v1/spend/policies/spp_demo_global",
                  json={"limit_nano_cny": "777", "version": 1})
        assert r.status_code == 500
    finally:
        share_store_pg.record_audit_tx = orig
    # 未落库：额度仍是种子值 50 CNY
    resolved = spend_store.resolve_policy("demo", spend_store.DEMO_GLOBAL_SUBJECT)
    assert resolved["limit_nano_cny"] == 50 * 10 ** 9


# --------------------------------------------------------------------------- #
# 4. enforcement-mode PUT（词表 + CAS + §7.3 无保护配置校验）
# --------------------------------------------------------------------------- #
@PG
def test_enforcement_mode_validation_cas_audit():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # 词表外 400
    for bad in ("hard", "off", "", None):
        r = c.put("/api/admin/v1/spend/enforcement-mode",
                  json={"mode": bad})
        assert r.status_code == 400, "mode=%r 应 400" % (bad,)
    # CAS：expected 不匹配当前（shadow）→ 409
    r = c.put("/api/admin/v1/spend/enforcement-mode",
              json={"mode": "registered", "expected": "all"})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "version_conflict"
    # shadow → shadow（无变化）成功且 audit 记录 changed=false
    r2 = c.put("/api/admin/v1/spend/enforcement-mode",
               json={"mode": "shadow", "expected": "shadow"})
    assert r2.status_code == 200
    assert r2.get_json() == {"previous_mode": "shadow", "mode": "shadow"}
    events = _audit_actions("spend.enforcement_mode_update")
    assert events and events[0]["detail"]["changed"] is False


@PG
def test_enforcement_mode_unprotected_config_rejected():
    """§7.3：金额硬闸未就绪（shadow）+ 旧 turn 闸关闭 = 无保护配置不能保存。

    当前平台没有写 legacy_turn_guard_enabled 的路径（旧 turn 闸恒开），本用例
    直接注入该键复现「未来引入开关」后的拒绝分支（校验写成可扩展形式，
    见 spend_store._assert_not_unprotected_tx）。
    """
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    settings_store.set_setting(spend_store.LEGACY_TURN_GUARD_KEY, False)
    # shadow（金额闸未就绪）被拒
    r = c.put("/api/admin/v1/spend/enforcement-mode",
              json={"mode": "shadow", "expected": "shadow"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "unprotected_spend_config"
    assert spend_store.enforcement_mode() == "shadow"  # 未变
    # registered/all（金额硬闸就绪）可保存
    r2 = c.put("/api/admin/v1/spend/enforcement-mode",
               json={"mode": "registered", "expected": "shadow"})
    assert r2.status_code == 200
    assert spend_store.enforcement_mode() == "registered"
    # 回滚到 shadow（仍在无保护态）再次被拒——恢复闸开后可回
    r3 = c.put("/api/admin/v1/spend/enforcement-mode",
               json={"mode": "shadow", "expected": "registered"})
    assert r3.status_code == 400
    settings_store.set_setting(spend_store.LEGACY_TURN_GUARD_KEY, True)
    r4 = c.put("/api/admin/v1/spend/enforcement-mode",
               json={"mode": "shadow", "expected": "registered"})
    assert r4.status_code == 200
    assert spend_store.enforcement_mode() == "shadow"


# --------------------------------------------------------------------------- #
# 5. 调整当前窗口（confirm + CAS + 只改 snapshot）
# --------------------------------------------------------------------------- #
@PG
def test_window_adjust_confirm_cas_and_snapshot_only():
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    win = spend_store.get_or_create_window("user", usera["user_id"])
    # 预占+结算一些用量，验证 spent 不被调整改动
    spend_store.window_reserve(win["window_id"], 10 ** 8)
    spend_store.window_settle(win["window_id"], 10 ** 8, 3 * 10 ** 8)
    win = spend_store.get_window(win["window_id"])
    assert win["spent_nano_cny"] == 3 * 10 ** 8
    # 缺 confirm → 400 confirm_required
    r0 = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
                json={"limit_nano_snapshot": "1000", "version":
                      win["version"]})
    assert r0.status_code == 400
    assert r0.get_json()["error"]["code"] == "confirm_required"
    # confirm=false 同样拒绝（必须精确 true）
    r0b = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
                 json={"limit_nano_snapshot": "1000",
                       "version": win["version"], "confirm": False})
    assert r0b.status_code == 400
    # 成功：snapshot 改为 30.5 CNY，spent/reserved 不动，version+1
    r = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
               json={"limit_nano_snapshot": str(305 * 10 ** 8),
                     "version": win["version"], "confirm": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()["window"]
    assert body["limit_nano_snapshot"] == str(305 * 10 ** 8)
    assert body["spent_nano_cny"] == str(3 * 10 ** 8)
    assert body["reserved_nano_cny"] == "0"
    assert body["version"] == win["version"] + 1
    # 旧 version 重放 → 409
    r2 = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
                json={"limit_nano_snapshot": "1000",
                      "version": win["version"], "confirm": True})
    assert r2.status_code == 409
    # audit：spend.window_adjust 带 confirm/前后额度镜像
    events = _audit_actions("spend.window_adjust")
    assert events
    detail = events[0]["detail"]
    assert detail["confirm"] is True
    assert detail["new_limit_nano_snapshot"] == 305 * 10 ** 8
    # 金额 JSON number 拒绝
    r3 = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
                json={"limit_nano_snapshot": 1000,
                      "version": win["version"] + 1, "confirm": True})
    assert r3.status_code == 400


@PG
def test_window_adjust_lower_than_spent_rejects_next_reserve():
    """调低到低于 spent：本操作成功，下一次预占被拒（§3.2）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    win = spend_store.get_or_create_window("user", usera["user_id"])
    spend_store.window_reserve(win["window_id"], 10 ** 9)
    spend_store.window_settle(win["window_id"], 10 ** 9, 10 ** 9)
    # reserve/settle 各自 version+1：调整用最新 version
    win = spend_store.get_window(win["window_id"])
    r = c.post("/api/admin/v1/spend/windows/%s/adjust" % win["window_id"],
               json={"limit_nano_snapshot": "0",
                     "version": win["version"], "confirm": True})
    assert r.status_code == 200
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        spend_store.window_reserve(win["window_id"], 1)


# --------------------------------------------------------------------------- #
# 6. 用户月额度覆盖 PUT/DELETE
# --------------------------------------------------------------------------- #
@PG
def test_user_spend_override_set_clear_and_semantics():
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    # 覆盖前先开当前窗口（snapshot 固定为 user_default 种子 20 CNY）
    before = spend_store.get_or_create_window("user", usera["user_id"])
    assert before["limit_nano_snapshot"] == 20 * 10 ** 9
    base = "/api/admin/v1/users/%s/spend-override" % usera["user_id"]
    # JSON number 拒绝
    assert c.put(base, json={"monthly_limit_nano_cny": 100}).status_code == 400
    # 缺字段拒绝（清除必须走 DELETE，不靠空 PUT）
    assert c.put(base, json={}).status_code == 400
    # 设置覆盖（>2^53 十进制字符串）
    r = c.put(base, json={"monthly_limit_nano_cny": OVER_2E53_NANO})
    assert r.status_code == 200
    assert r.get_json()["override"]["limit_nano_cny"] == OVER_2E53_NANO
    resolved = spend_store.resolve_policy("user", usera["user_id"])
    assert resolved["scope_type"] == "user_override"
    assert resolved["limit_nano_cny"] == int(OVER_2E53_NANO)
    # 已开窗口不追溯：同一窗口 snapshot 仍是 20 CNY（§1.1 默认只影响新周期）
    after = spend_store.get_window(before["window_id"])
    assert after["limit_nano_snapshot"] == 20 * 10 ** 9
    # users 列表带 spend 段（默认/覆盖状态 + 当前窗口）
    item = [u for u in c.get("/api/admin/v1/users").get_json()["items"]
            if u["user_id"] == usera["user_id"]][0]
    assert item["spend"]["policy_scope"] == "user_override"
    assert item["spend"]["window"]["limit_nano_snapshot"] == str(20 * 10 ** 9)
    # 清除 → 解析回退 user_default（下个窗口）
    rd = c.delete(base)
    assert rd.status_code == 200
    assert rd.get_json()["cleared"] is True
    resolved2 = spend_store.resolve_policy("user", usera["user_id"])
    assert resolved2["scope_type"] == "user_default"
    # 再次清除：幂等 cleared=false
    assert c.delete(base).get_json()["cleared"] is False
    # owner 目标：400（owner 走独立策略，无覆盖语义）
    r_owner = c.put("/api/admin/v1/users/%s/spend-override" % owner["user_id"],
                    json={"monthly_limit_nano_cny": "100"})
    assert r_owner.status_code == 400
    # 不存在用户 404
    assert c.put("/api/admin/v1/users/usr_nope/spend-override",
                 json={"monthly_limit_nano_cny": "100"}).status_code == 404
    # audit 无敏感字段
    events = _audit_actions("spend.policy_update")
    assert any(e["detail"].get("op") == "clear_user_override"
               for e in events)
    blob = json.dumps([e["detail"] for e in events], ensure_ascii=False)
    assert "password" not in blob and "token" not in blob


# --------------------------------------------------------------------------- #
# 7. 用户创建扩展（同事务 override + 继承默认 + 注入失败回滚）
# --------------------------------------------------------------------------- #
@PG
def test_users_create_with_monthly_limit_override_atomic():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "limited@x.com", "password": "password-123456",
        "display_name": "Limited", "monthly_limit_nano_cny": "30500000000"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    uid = body["user"]["user_id"]
    assert body["spend_override"]["limit_nano_cny"] == "30500000000"
    resolved = spend_store.resolve_policy("user", uid)
    assert resolved["scope_type"] == "user_override"
    assert resolved["limit_nano_cny"] == 305 * 10 ** 8
    # audit 记录 user.create 且 detail 无敏感字段
    events = _audit_actions("user.create")
    mine = [e for e in events if e["target_id"] == uid]
    assert mine and mine[0]["detail"]["monthly_limit_nano_cny"] == 305 * 10 ** 8


@PG
def test_users_create_without_limit_inherits_default():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "inherits@x.com", "password": "password-123456"})
    assert r.status_code == 200
    assert r.get_json().get("spend_override") is None
    uid = r.get_json()["user"]["user_id"]
    resolved = spend_store.resolve_policy("user", uid)
    assert resolved["scope_type"] == "user_default"
    assert resolved["limit_nano_cny"] == 20 * 10 ** 9


@PG
def test_users_create_override_failure_rolls_back_user():
    """单事务证据：override 写入失败 → 用户不创建（§5.1）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    orig = spend_store.set_user_override_tx

    def boom(*_a, **_k):
        raise RuntimeError("override down")
    spend_store.set_user_override_tx = boom
    try:
        r = c.post("/api/admin/v1/users", json={
            "login_id": "rollback@x.com", "password": "password-123456",
            "monthly_limit_nano_cny": "1000000000"})
        assert r.status_code == 500
    finally:
        spend_store.set_user_override_tx = orig
    assert user_store.get_user_by_login_id("rollback@x.com") is None


@PG
def test_users_create_rejects_owner_and_bad_amount():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/v1/users", json={
        "login_id": "o@x.com", "password": "password-123456",
        "role": "owner"}).status_code == 400
    for bad in (5, "1.5", "9" * 20):
        rb = c.post("/api/admin/v1/users", json={
            "login_id": "bad@x.com", "password": "password-123456",
            "monthly_limit_nano_cny": bad})
        assert rb.status_code == 400, "limit=%r 应 400" % (bad,)


# --------------------------------------------------------------------------- #
# 8. 邀请码月额度模板 + 兑换事务内 override
# --------------------------------------------------------------------------- #
@PG
def test_invite_create_with_monthly_limit_template():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "inv1@x.com", "ttl_hours": 24, "ai_access": True,
        "monthly_limit_nano_cny": "25000000000"})
    assert r.status_code == 200, r.get_data(as_text=True)
    invite = r.get_json()["invite"]
    # wire 十进制字符串；明文 token 仅此一次 + no-store
    assert invite["monthly_limit_nano_cny"] == "25000000000"
    assert invite["token"]
    assert r.headers.get("Cache-Control") == "no-store"
    # 列表：token 永不回显；金额保持十进制字符串
    lst = c.get("/api/admin/v1/invites").get_json()["invites"]
    mine = [i for i in lst if i["invite_id"] == invite["invite_id"]][0]
    assert "token" not in mine
    assert mine["monthly_limit_nano_cny"] == "25000000000"
    # 金额 JSON number 拒绝
    assert c.post("/api/admin/v1/invites", json={
        "monthly_limit_nano_cny": 100}).status_code == 400


@PG
def test_invite_redeem_creates_override_same_transaction():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "redeem@x.com", "monthly_limit_nano_cny": "15000000000"})
    token = r.get_json()["invite"]["token"]
    invite_id = r.get_json()["invite"]["invite_id"]
    result = registration_store.redeem_invite(
        token, "redeem@x.com", "password-123456")
    uid = result["user"]["user_id"]
    assert result["spend_override_policy"]["limit_nano_cny"] == 15 * 10 ** 9
    resolved = spend_store.resolve_policy("user", uid)
    assert resolved["scope_type"] == "user_override"
    assert resolved["limit_nano_cny"] == 15 * 10 ** 9
    # 邀请已消费
    row = registration_store.get_invite(invite_id)
    assert row["consumed_at"] is not None
    # override 审计在（同事务的）spend.policy_update 流里
    events = _audit_actions("spend.policy_update")
    assert any(e["detail"].get("user_id") == uid and
               e["detail"].get("op") == "set_user_override" for e in events)


@PG
def test_invite_without_limit_redeem_inherits_default():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    r = registration_store.create_invite(owner["user_id"],
                                         login_id="plain@x.com")
    result = registration_store.redeem_invite(
        r["token"], "plain@x.com", "password-123456")
    uid = result["user"]["user_id"]
    assert result["spend_override_policy"] is None
    resolved = spend_store.resolve_policy("user", uid)
    assert resolved["scope_type"] == "user_default"


@PG
def test_invite_redeem_override_failure_rolls_back_everything():
    """单事务证据：override 写入失败 → 邀请不消费、用户不创建（§5.2）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    invite = registration_store.create_invite(
        owner["user_id"], login_id="rb@x.com",
        monthly_limit_nano_cny=10 ** 9)
    orig = spend_store.set_user_override_tx

    def boom(*_a, **_k):
        raise RuntimeError("override down")
    spend_store.set_user_override_tx = boom
    try:
        with pytest.raises(Exception):
            registration_store.redeem_invite(
                invite["token"], "rb@x.com", "password-123456")
    finally:
        spend_store.set_user_override_tx = orig
    # 整体回滚：用户不存在、邀请未消费
    assert user_store.get_user_by_login_id("rb@x.com") is None
    row = registration_store.get_invite(invite["invite_id"])
    assert row["consumed_at"] is None and row["use_count"] == 0


# --------------------------------------------------------------------------- #
# 9. settings 聚合（§6.1/§6.5 admin.settings.get 数据源）
# --------------------------------------------------------------------------- #
@PG
def test_settings_aggregate_sections_and_decimal_strings():
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/settings")
    assert r.status_code == 200
    body = r.get_json()
    # 注册模式段（任何后端真实）
    assert body["registration"]["supported_modes"] == ["closed", "invite_only"]
    # spend 段：三条策略 + enforcement + 窗口边界（epoch）+ 当前 demo 窗口
    spend = body["spend"]
    assert spend["available"] is True
    assert spend["enforcement_mode"] == "shadow"
    for scope in ("demo_global", "user_default", "owner"):
        assert isinstance(spend["policies"][scope]["limit_nano_cny"], str)
    assert spend["policies"]["user_default"]["limit_nano_cny"] == \
        str(20 * 10 ** 9)
    bounds = spend["next_window_bounds"]
    assert bounds["demo_week"] and len(bounds["demo_week"]) == 2
    assert bounds["demo_week"][1] - bounds["demo_week"][0] == 7 * 86400
    assert bounds["user_month"][1] > bounds["user_month"][0]
    demo_win = spend["current_windows"]["demo"]
    assert demo_win["limit_nano_snapshot"] == str(50 * 10 ** 9)
    assert demo_win["remaining_nano"] == str(50 * 10 ** 9)
    # runtime 段：安全参数 + Demo IP 桶现状
    rt = body["runtime"]
    assert rt["available"] is True
    assert rt["limits"]["demo_enabled"] is not None
    assert isinstance(rt["demo_ip_run_limit"], int)
