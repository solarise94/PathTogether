# -*- coding: utf-8 -*-
"""批次 D：后台设置、用户覆盖与邀请码模板测试（docs
ai-money-budget-bugfix-and-simplification-plan.md §5/§6/§8 批次 D/§9.6/§9.7）。

json 模式（无 PG）：
  - 新端点 owner 门控：匿名 401 / user 403 / owner 预览态 403（§14.1 同口径）；
  - PG-only 写端点（spend policies/enforcement-mode/window adjust/
    settings 聚合）稳定 503 pg_backend_required；
  - 注册模式 v1 PUT 校验（public 400 / 非法值 400 / invite_only 前置条件
    400；旧路由已随 R3 wave1 删除，service 语义由 v1 独守）。

PG 模式（RUN_PG_TESTS=1）：
  - spend policies PUT：CAS 版本冲突 409 / JSON number 金额 400 / >2^53
    十进制字符串精确落库 / audit（spend.policy_update）同事务；
  - enforcement-mode PUT：词表外 400 / CAS 409 / audit（§7.3 无保护配置由
    模式派生结构性保证；旧 legacy_turn_guard_enabled 幽灵键校验已随 R3
    Wave2-Compat 删除）；
  - window adjust：缺 confirm → 400 confirm_required / 成功只改 snapshot
    （spent/reserved 不动）/ 409 CAS / audit；
  - 用户月额度覆盖 PUT/DELETE：设置后解析到 user_override、清除后下个窗口
    回退 user_default / owner 目标 400 / 404 不存在 / audit 无敏感字段；
  - 用户创建扩展：total 模式（target=total_allowance）带
    total_limit_nano_cny 同事务建一次性总额度（含注入失败整体回滚：用户不
    创建）；window 模式无金额不建任何额度面；postgres 后端无金额字段也走
    同事务原语（total 模式缺默认 → 400 total_default_missing）；
  - 邀请码模板：创建携带初始总额度（wire 十进制字符串、库内整数）/ 兑换
    total 模式同事务建总额度（含注入失败整体回滚：邀请不消费、用户不创建）/
    明文码仅创建响应一次 + no-store；
  - users 列表 spend 形态纯由 target 驱动（四象限：window+有行也必须
    window 形态）；
  - settings 聚合：注册模式 + spend 分段 + runtime 分段，金额十进制字符串。

运行：cd 项目根 && python3 -m pytest tests/test_admin_batch_d.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_batch_d.py -q）
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
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
    if BACKEND == "postgres":
        # review R2-F2：PG 上建号/兑换统一走「维护闸 + 开通锁」组合原语，
        # 闸 fail-closed。conftest TRUNCATE 清掉迁移种子，每用例幂等重放
        # （0023+0029+0032：闸=false + defaults 物化基线 20 CNY）。R3 单轨后
        # 无 user_spend_target 可切——user 授权恒 allowance。
        bh.seed_spend_settings()
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
    ("PUT", "/api/admin/v1/spend/users/usr_x/total-limit"),
    ("POST", "/api/admin/v1/spend/users/usr_x/restore-default"),
    ("GET", "/api/admin/v1/spend/demo-stats"),
    ("GET", "/api/admin/v1/site-stats"),
    ("GET", "/api/admin/v1/settings"),
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
             {"limit_nano_snapshot": "1", "version": 1, "confirm": True})):
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
# 2. 注册模式：v1 settings/registration PUT（旧路由已随 R3 wave1 删除）
# --------------------------------------------------------------------------- #
def test_registration_v1_put_validates():
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # public 一律 400
    r1 = c.put("/api/admin/v1/settings/registration", json={"mode": "public"})
    assert r1.status_code == 400
    assert r1.get_json()["error"]["code"] == "public_registration_not_supported"
    # 非法值 400
    assert c.put("/api/admin/v1/settings/registration",
                 json={"mode": "oops"}).status_code == 400
    # json 后端 invite_only 前置条件不满足 → 400（PG 模式同理：PUBLIC_BASE_URL
    # 非 https）
    r2 = c.put("/api/admin/v1/settings/registration",
               json={"mode": "invite_only"})
    assert r2.status_code == 400
    assert "前置条件" in r2.get_json()["error"]["message"]


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
    # R3 单轨：user 级策略写拒绝（授权唯一写口是 user-default-total-limit）
    r_ret = c.put("/api/admin/v1/spend/policies/spp_user_default",
                  json={"limit_nano_cny": "1", "version": 1})
    assert r_ret.status_code == 400
    assert r_ret.get_json()["error"]["code"] == "user_spend_policy_retired"
    r = c.put("/api/admin/v1/spend/policies/spp_demo_global",
              json={"limit_nano_cny": OVER_2E53_NANO, "version": 1})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()["policy"]
    # >2^53 十进制字符串精确往返（JSON number 会失真）
    assert body["limit_nano_cny"] == OVER_2E53_NANO
    assert body["version"] == 2
    # 再用旧 version 更新 → 409 version_conflict
    r2 = c.put("/api/admin/v1/spend/policies/spp_demo_global",
               json={"limit_nano_cny": "1", "version": 1})
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "version_conflict"
    # 金额 JSON number / 小数 / 超长一律 400
    for bad in (123, 1.5, "12.5", "", "9" * 20, "-5"):
        rb = c.put("/api/admin/v1/spend/policies/spp_demo_global",
                   json={"limit_nano_cny": bad, "version": 2})
        assert rb.status_code == 400, "limit=%r 应 400" % (bad,)
    # audit 同事务：spend.policy_update 落库且不含敏感字段
    events = _audit_actions("spend.policy_update")
    assert events
    detail = json.dumps(events[0]["detail"], ensure_ascii=False)
    assert "password" not in detail and "token" not in detail
    assert events[0]["detail"]["limit_nano_cny"] == int(OVER_2E53_NANO)
    # 不存在的策略 → 409（CAS 未命中语义）
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
# 6. 用户一次性总额度 PUT/restore-default（Batch B wave 2，§Batch B API/bridge
#    契约）；旧月额度覆盖端点已随 R3 wave1 物理删除
# --------------------------------------------------------------------------- #
@PG
def test_user_total_limit_set_and_restore():
    bh.seed_spend_policies()
    # R3 单轨：user 恒走互斥 total 形态（无 target 可切）
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    # 建号带初始总额度（同一事务建行）
    r = c.post("/api/admin/v1/users", json={
        "login_id": "total@x.com", "password": "password-123456",
        "total_limit_nano_cny": "9007199254740993"})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["user"]["user_id"]
    assert r.get_json()["total_allowance"]["limit_nano_cny"] == \
        OVER_2E53_NANO

    # JSON number 拒绝 / 缺字段拒绝 / 非法 version 拒绝
    base = "/api/admin/v1/spend/users/%s/total-limit" % uid
    assert c.put(base, json={"total_limit_nano_cny": 100,
                             "expected_version": 1}).status_code == 400
    assert c.put(base, json={"expected_version": 1}).status_code == 400
    assert c.put(base, json={"total_limit_nano_cny": "100"}).status_code == 400
    # 绝对值语义：设置 X=5 CNY（不改 spent/reserved，绝不 +=）
    r = c.put(base, json={"total_limit_nano_cny": "5000000000",
                          "expected_version": 1})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["total_allowance"]["limit_nano_cny"] == "5000000000"
    assert body["total_allowance"]["version"] == 2
    cur = bh.connect()
    try:
        with cur.cursor() as qcur:
            qcur.execute("SELECT limit_nano_cny, version FROM "
                         "ai_spend_total_allowances WHERE subject_id=%s",
                         (uid,))
            row = qcur.fetchone()
    finally:
        cur.close()
    assert row["limit_nano_cny"] == 5 * 10 ** 9 and row["version"] == 2
    # CAS 冲突：旧 version 再写 → 409
    r_conflict = c.put(base, json={"total_limit_nano_cny": "6000000000",
                                   "expected_version": 1})
    assert r_conflict.status_code == 409
    assert r_conflict.get_json()["error"]["code"] == "version_conflict"
    # users 列表：user 行 spend.total 互斥形态（无 window/policy 键）
    item = [u for u in c.get("/api/admin/v1/users").get_json()["items"]
            if u["user_id"] == uid][0]
    assert "window" not in item["spend"]
    assert item["spend"]["total"]["total_limit_nano_cny"] == "5000000000"
    assert item["spend"]["total"]["remaining_nano"] == "5000000000"
    assert item["spend"]["total"]["overage_nano"] == "0"
    # owner 行仍为 window 形态（绝不出现在 user 上的 total 键）
    owner_item = [u for u in c.get("/api/admin/v1/users").get_json()["items"]
                  if u["user_id"] == owner["user_id"]][0]
    assert "total" not in owner_item["spend"]
    assert owner_item["spend"]["window"]["limit_nano_snapshot"] is not None
    # 恢复默认：面值取 ai_spend_total_defaults 权威行（20 CNY 基线）；
    # CAS version 沿用当前 version=2
    rd = c.post("/api/admin/v1/spend/users/%s/restore-default" % uid,
                json={"expected_version": 2})
    assert rd.status_code == 200, rd.get_data(as_text=True)
    assert rd.get_json()["total_allowance"]["limit_nano_cny"] == \
        str(20 * 10 ** 9)
    # 恢复默认不清零 spent/reserved：注入 spent 后恢复，spent 保留
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute(
                "UPDATE ai_spend_total_allowances SET spent_nano_cny=%s "
                "WHERE subject_id=%s", (3 * 10 ** 9, uid))
        conn.commit()
    finally:
        conn.close()
    rd2 = c.post("/api/admin/v1/spend/users/%s/restore-default" % uid,
                 json={"expected_version": 3})
    assert rd2.status_code == 200
    assert rd2.get_json()["total_allowance"]["spent_nano_cny"] == \
        str(3 * 10 ** 9)
    # owner 目标 400 / 不存在用户 404
    r_owner = c.put("/api/admin/v1/spend/users/%s/total-limit"
                    % owner["user_id"],
                    json={"total_limit_nano_cny": "100",
                          "expected_version": 1})
    assert r_owner.status_code == 400
    assert c.post("/api/admin/v1/spend/users/%s/restore-default"
                  % owner["user_id"],
                  json={"expected_version": 1}).status_code == 400
    assert c.put("/api/admin/v1/spend/users/usr_nope/total-limit",
                 json={"total_limit_nano_cny": "100",
                       "expected_version": 1}).status_code == 404
    assert c.post("/api/admin/v1/spend/users/usr_nope/restore-default",
                  json={"expected_version": 1}).status_code == 404
    # audit 无敏感字段
    events = _audit_actions("spend.total_allowance_update")
    assert any(e["detail"].get("op") == "set_user_total_limit"
               for e in events)
    assert any(e["detail"].get("op") == "restore_user_total_default"
               for e in events)
    blob = json.dumps([e["detail"] for e in events], ensure_ascii=False)
    assert "password" not in blob and "token" not in blob


# --------------------------------------------------------------------------- #
# 7. 用户创建扩展（同事务一次性总额度 + 不带额度不建行 + 注入失败回滚）
# --------------------------------------------------------------------------- #
@PG
def test_users_create_with_total_limit_atomic():
    """建号带 total_limit_nano_cny → 同事务建一次性总额度（source=admin_create，
    default_version=None；不建 user_override 月策略——写面已随 R3 单轨删除）；
    旧 monthly 字段退役：body 带该键一律 400 retired_spend_field。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "limited@x.com", "password": "password-123456",
        "display_name": "Limited", "total_limit_nano_cny": "30500000000"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    uid = body["user"]["user_id"]
    assert body["total_allowance"]["limit_nano_cny"] == "30500000000"
    allowance = spend_store.get_total_allowance(uid)
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 305 * 10 ** 8
    assert allowance["source"] == "admin_create"
    assert allowance["default_version"] is None  # 显式 X 不锚定默认版本
    # user_override 月策略不再创建（写面已删）
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute("SELECT count(*)::int AS n FROM ai_spend_policies "
                         "WHERE scope_type='user_override'")
            assert qcur.fetchone()["n"] == 0
    finally:
        conn.close()
    # audit 记录 user.create 且 detail 无敏感字段（金额键改 total）
    events = _audit_actions("user.create")
    mine = [e for e in events if e["target_id"] == uid]
    assert mine and mine[0]["detail"]["total_limit_nano_cny"] == 305 * 10 ** 8
    assert mine[0]["detail"]["spend_target"] == "total_allowance"  # wire 兼容标注
    assert mine[0]["detail"]["limit_surface"] == "total_allowance"
    # R3 Wave2-Compat：旧 monthly 字段退役——body 带该键一律 400
    # retired_spend_field（绝不静默忽略），不再有「兼容落总额度」路径
    r_ret = c.post("/api/admin/v1/users", json={
        "login_id": "amb@x.com", "password": "password-123456",
        "total_limit_nano_cny": "1000000000",
        "monthly_limit_nano_cny": "1000000000"})
    assert r_ret.status_code == 400
    assert r_ret.get_json()["error"]["code"] == "retired_spend_field"
    r_ret2 = c.post("/api/admin/v1/users", json={
        "login_id": "legacyfld@x.com", "password": "password-123456",
        "monthly_limit_nano_cny": "2000000000"})
    assert r_ret2.status_code == 400
    assert r_ret2.get_json()["error"]["code"] == "retired_spend_field"


@PG
def test_users_create_without_limit_inherits_default():
    """R3 单轨：无金额建号 → 同事务按 ai_spend_total_defaults 权威行（20 CNY）
    建 allowance（source=admin_create，default_version 锚定默认行版本）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "inherits@x.com", "password": "password-123456"})
    assert r.status_code == 200
    # 单轨恒建行：响应携带 total_allowance（默认 20 CNY）
    assert r.get_json()["total_allowance"]["limit_nano_cny"] == \
        str(20 * 10 ** 9)
    uid = r.get_json()["user"]["user_id"]
    allowance = spend_store.get_total_allowance(uid)
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 20 * 10 ** 9
    assert allowance["source"] == "admin_create"
    assert allowance["default_version"] is not None  # 锚定默认行版本
    # users 列表：total 形态（现行唯一授权面）
    item = [u for u in c.get("/api/admin/v1/users").get_json()["items"]
            if u["user_id"] == uid][0]
    assert item["spend"]["spend_target"] == "total_allowance"
    assert item["spend"]["total"]["total_limit_nano_cny"] == str(20 * 10 ** 9)
    assert "window" not in item["spend"]


@PG
def test_users_list_spend_total_mode_missing_row_reports_stable_error():
    """user 无 allowance 行 → 互斥形态的稳定 error code
    （spend_total_allowance_missing），不拖垮整页。

    R3 单轨后「无行 user」= 数据损坏形态（建号恒建行，正常路径造不出来），
    用 SQL 直删 allowance 行构造。这也正是 owner 钦定语义：切换后任何 user
    缺 allowance 都视为数据损坏并 fail-closed。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute("DELETE FROM ai_spend_total_allowances "
                         "WHERE subject_id=%s", (usera["user_id"],))
        conn.commit()
    finally:
        conn.close()
    c = _login(_client(), owner)
    items = c.get("/api/admin/v1/users").get_json()["items"]
    by_id = {u["user_id"]: u for u in items}
    assert by_id[usera["user_id"]]["spend"]["error"] == \
        "spend_total_allowance_missing"
    assert "window" not in by_id[usera["user_id"]]["spend"]
    assert "total" not in by_id[usera["user_id"]]["spend"]
    # owner 行恒 window 形态
    assert "total" not in by_id[owner["user_id"]]["spend"]
    assert by_id[owner["user_id"]]["spend"]["window"] is not None


@PG
def test_users_list_spend_display_single_track_locked():
    """R3 单轨形态锁定（取代四象限 target 驱动裁定）：user+有行 → 恒 total
    形态；user+无行 → 稳定 error（fail-closed 不伪造窗口）；owner/demo 恒
    window。spend_target 为纯展示标注（user 恒 total_allowance）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    naked = user_store.create_user("naked@x.com", "userpass123456-xx",
                                   role="user")
    # 数据损坏形态：直删 naked 的 allowance 行
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute("DELETE FROM ai_spend_total_allowances "
                         "WHERE subject_id=%s", (naked["user_id"],))
        conn.commit()
    finally:
        conn.close()
    c = _login(_client(), owner)

    def _spend(uid):
        items = c.get("/api/admin/v1/users").get_json()["items"]
        return {u["user_id"]: u for u in items}[uid]["spend"]

    # user + 有行 → total 形态
    spend = _spend(usera["user_id"])
    assert spend["spend_target"] == "total_allowance"
    assert "window" not in spend and "error" not in spend
    assert spend["total"]["total_limit_nano_cny"] == str(20 * 10 ** 9)
    # user + 无行 → 稳定 error（fail-closed，不伪造窗口）
    spend = _spend(naked["user_id"])
    assert spend["spend_target"] == "total_allowance"
    assert spend["error"] == "spend_total_allowance_missing"
    assert "window" not in spend and "total" not in spend
    # owner 恒 window
    spend = _spend(owner["user_id"])
    assert spend["spend_target"] == "window"
    assert "total" not in spend
    assert spend["window"] is not None


@PG
def test_users_create_pg_always_atomic_and_total_default_gate():
    """finding 1 建号变体收口：postgres 后端**不带金额字段**也走同事务组合
    原语——R3 单轨后 defaults 表无可用默认时 400 total_default_missing
    （绝不建出无额度行的用户）；defaults 恢复后照常 200 并建行；
    json 后端保留 legacy 旁路（无金额可建号）。"""
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    if BACKEND != "postgres":
        r = c.post("/api/admin/v1/users", json={
            "login_id": "plain@x.com", "password": "password-123456"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert user_store.get_user_by_login_id("plain@x.com") is not None
        pytest.skip("json 后端 legacy 旁路已验证")
    bh.seed_spend_policies()
    # 构造「defaults 缺行」→ 无可用默认（策略回退已删，唯一来源是 defaults 表）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_spend_total_defaults "
                        "WHERE singleton='global'")
        conn.commit()
    finally:
        conn.close()
    r = c.post("/api/admin/v1/users", json={
        "login_id": "gate@x.com", "password": "password-123456"})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"]["code"] == "total_default_missing"
    # 整体回滚：不留无额度行的用户
    assert user_store.get_user_by_login_id("gate@x.com") is None
    # defaults 恢复（重放 0032 物化）→ 无金额字段照常建号并按默认建行
    bh.seed_spend_settings()
    r2 = c.post("/api/admin/v1/users", json={
        "login_id": "plain-pg@x.com", "password": "password-123456"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json()["total_allowance"]["limit_nano_cny"] == \
        str(20 * 10 ** 9)
    assert spend_store.get_total_allowance(
        r2.get_json()["user"]["user_id"]) is not None


@PG
def test_users_create_override_failure_rolls_back_user():
    """单事务证据：总额度行写入失败 → 用户不创建（§5.1；allowance 注入目标，
    单轨恒触达 allowance 原语）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    orig = spend_store.create_user_total_allowance_tx

    def boom(*_a, **_k):
        raise RuntimeError("allowance down")
    spend_store.create_user_total_allowance_tx = boom
    try:
        r = c.post("/api/admin/v1/users", json={
            "login_id": "rollback@x.com", "password": "password-123456",
            "total_limit_nano_cny": "1000000000"})
        assert r.status_code == 500
    finally:
        spend_store.create_user_total_allowance_tx = orig
    assert user_store.get_user_by_login_id("rollback@x.com") is None
    assert spend_store.get_total_allowance("rollback@x.com") is None


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
            "total_limit_nano_cny": bad})
        assert rb.status_code == 400, "limit=%r 应 400" % (bad,)
    # 旧字段（已退役）：坏值同样 400 retired_spend_field（键出现即拒绝）
    rb = c.post("/api/admin/v1/users", json={
        "login_id": "bad2@x.com", "password": "password-123456",
        "monthly_limit_nano_cny": 5})
    assert rb.status_code == 400
    assert rb.get_json()["error"]["code"] == "retired_spend_field"


# --------------------------------------------------------------------------- #
# 8. 邀请码总额度模板 + 兑换事务内一次性总额度（Batch B wave 2）
# --------------------------------------------------------------------------- #
@PG
def test_invite_create_with_total_limit_template():
    """Batch B wave 2 + R3 Wave2-Compat：邀请初始金额字段为
    total_limit_nano_cny（wire 十进制字符串）；旧 monthly 字段退役——body 带
    该键一律 400 retired_spend_field（绝不静默忽略）；来源字段接受即 400
    retired_invite_field。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "inv1@x.com", "ttl_hours": 24, "ai_access": True,
        "total_limit_nano_cny": "25000000000"})
    assert r.status_code == 200, r.get_data(as_text=True)
    invite = r.get_json()["invite"]
    # wire 十进制字符串；明文 token 仅此一次 + no-store
    assert invite["total_limit_nano_cny"] == "25000000000"
    assert "monthly_limit_nano_cny" not in invite
    assert invite["token"]
    assert r.headers.get("Cache-Control") == "no-store"
    # 列表：token 永不回显；金额保持十进制字符串；无来源字段回显
    lst = c.get("/api/admin/v1/invites").get_json()["invites"]
    mine = [i for i in lst if i["invite_id"] == invite["invite_id"]][0]
    assert "token" not in mine
    assert mine["total_limit_nano_cny"] == "25000000000"
    for retired in ("source_code", "campaign_id", "cohort"):
        assert retired not in mine
    # 金额 JSON number 拒绝
    assert c.post("/api/admin/v1/invites", json={
        "total_limit_nano_cny": 100}).status_code == 400
    # R3 Wave2-Compat：旧 monthly 字段退役——单独传 / 与 total 同传 /
    # 坏值，一律 400 retired_spend_field（不再有「兼容落总额度」路径）
    for payload in (
            {"login_id": "invlegacy@x.com",
             "monthly_limit_nano_cny": "18000000000"},
            {"total_limit_nano_cny": "1000000000",
             "monthly_limit_nano_cny": "1000000000"},
            {"monthly_limit_nano_cny": 5}):
        r_ret = c.post("/api/admin/v1/invites", json=payload)
        assert r_ret.status_code == 400, payload
        assert r_ret.get_json()["error"]["code"] == "retired_spend_field", \
            payload
    # 来源字段退役：接受即 400 retired_invite_field（不静默忽略）
    for field in ("source_code", "campaign_id", "cohort"):
        r_ret = c.post("/api/admin/v1/invites", json={field: "whatever"})
        assert r_ret.status_code == 400, field
        assert r_ret.get_json()["error"]["code"] == "retired_invite_field"
    # 创建 audit 无来源字段（store 层保证，wave 2 锁定 API 行为）
    events = _audit_actions("registration.invite_create")
    blob = json.dumps([e["detail"] for e in events], ensure_ascii=False)
    for retired in ("source_code", "campaign_id", "cohort"):
        assert retired not in blob


@PG
def test_invite_redeem_creates_total_allowance_same_transaction():
    """兑换带模板面值：同一事务内建一次性总额度（source=invite，
    default_version=None），不建 user_override（写面已删）；恒 None 兼容键
    acquisition/spend_override_policy 已随 R3 Wave2-Compat 物理删除（不在
    返回 dict 中）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/invites", json={
        "login_id": "redeem@x.com", "total_limit_nano_cny": "15000000000"})
    token = r.get_json()["invite"]["token"]
    invite_id = r.get_json()["invite"]["invite_id"]
    result = registration_store.redeem_invite(
        token, "redeem@x.com", "password-123456")
    uid = result["user"]["user_id"]
    assert result["total_allowance"]["limit_nano_cny"] == 15 * 10 ** 9
    assert result["total_allowance"]["source"] == "invite"
    assert result["total_allowance"]["default_version"] is None
    # 兼容键已物理删除（不是恒 None，是整键不在）
    assert "acquisition" not in result
    assert "spend_override_policy" not in result
    allowance = spend_store.get_total_allowance(uid)
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 15 * 10 ** 9
    # user_override 月策略不再创建
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute("SELECT count(*)::int AS n FROM ai_spend_policies "
                         "WHERE scope_type='user_override'")
            assert qcur.fetchone()["n"] == 0
    finally:
        conn.close()
    # 邀请已消费
    row = registration_store.get_invite(invite_id)
    assert row["consumed_at"] is not None
    # 总额度审计在（同事务）spend.total_allowance_create 流里
    events = _audit_actions("spend.total_allowance_create")
    assert any(e["detail"].get("user_id") == uid and
               e["detail"].get("op") == "create_user_total_allowance"
               for e in events)


@PG
def test_invite_without_limit_redeem_uses_default():
    """R3 单轨：兑换无模板面值 → 同事务按 ai_spend_total_defaults 权威行
    （20 CNY）建 allowance（source=invite，default_version 锚定）；兼容键
    spend_override_policy 已随 R3 Wave2-Compat 物理删除。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    r = registration_store.create_invite(owner["user_id"],
                                         login_id="plain@x.com")
    result = registration_store.redeem_invite(
        r["token"], "plain@x.com", "password-123456")
    uid = result["user"]["user_id"]
    assert "spend_override_policy" not in result
    assert result["total_allowance"]["limit_nano_cny"] == 20 * 10 ** 9
    allowance = spend_store.get_total_allowance(uid)
    assert allowance is not None
    assert allowance["source"] == "invite"
    assert allowance["default_version"] is not None  # 锚定默认行版本


@PG
def test_invite_redeem_override_failure_rolls_back_everything():
    """单事务证据：总额度写入失败 → 邀请不消费、用户不创建（allowance 注入
    目标；§5.2，单轨恒触达 allowance 原语）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    invite = registration_store.create_invite(
        owner["user_id"], login_id="rb@x.com",
        total_limit_nano_cny=10 ** 9)
    orig = spend_store.create_user_total_allowance_tx

    def boom(*_a, **_k):
        raise RuntimeError("allowance down")
    spend_store.create_user_total_allowance_tx = boom
    try:
        with pytest.raises(Exception):
            registration_store.redeem_invite(
                invite["token"], "rb@x.com", "password-123456")
    finally:
        spend_store.create_user_total_allowance_tx = orig
    # 整体回滚：用户不存在、邀请未消费、无半创建 allowance 行
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
    # Batch B wave 2（§4.5 设置页）：三键拆分**扁平**进 spend 顶层
    # （金额十进制字符串；user 默认额度附带 CAS 上下文 version/policy_id）
    assert spend["user_default_total_limit_nano_cny"] == str(20 * 10 ** 9)
    # R3 单轨：defaults 行恒在场（迁移/conftest 物化），source 恒
    # total_defaults，策略回退源已删（policy_id 不再关联）
    assert spend["user_default_total_limit_source"] == "total_defaults"
    assert int(spend["user_default_total_limit_version"]) >= 1
    assert spend["user_default_total_policy_id"] is None
    assert spend["demo_weekly_limit_nano_cny"] == str(50 * 10 ** 9)
    assert spend["demo_weekly_policy_id"]
    assert spend["owner_monthly_limit_nano_cny"] == str(1000 * 10 ** 9)
    assert spend["owner_monthly_policy_id"]
    # runtime 段：安全参数 + Demo IP 短窗口请求速率现状（批次 E 起只读观测）
    rt = body["runtime"]
    assert rt["available"] is True
    assert rt["limits"]["demo_enabled"] is not None
    assert isinstance(rt["demo_ip_request_rate_per_minute"], int)
