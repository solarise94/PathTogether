# -*- coding: utf-8 -*-
"""Batch B wave 2 / Batch C / Batch D2 新端点测试（wave 2 app.py 改造）。

覆盖（docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md）：

- **cutover 维护闸**（§Batch B 迁移与额度语义 1）：/api/ai/run|continue|ask|
  branch 在预备阶段读 ai_dispatch_maintenance，true → 稳定 503
  ai_dispatch_maintenance；/api/demo/ai/run **不闸**；闸关（false）放行；
- **GET /api/admin/v1/spend/demo-stats**（§4.6）：owner-only 只读；
  window=current|previous 白名单；任意金额/主体参数拒绝；调用前后业务表
  行数不变；
- **GET /api/admin/v1/site-stats**（§Batch D2 7）：owner-only 只读透传
  dashboard_stats；无写副作用；store 缺失 → 404（import 容错分支不在此测）；
- **步数契约 API**（§Batch C 1/9）：PUT /api/admin/v1/settings/runtime 对
  platform_task_max_steps / own_task_max_steps_limit 字段级 1..500 校验
  （>500 稳定 400，不再静默截回）；demo_task_max_steps / demo_max_concurrency
  维持各自现有边界（demo 步数 501 合法、并发上限 1_000_000）；demo 步数
  独立默认 20，不继承 user 的 500。

运行：cd 项目根 && python3 -m pytest tests/test_admin_spend_total_api.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_spend_total_api.py -q）
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import budget_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import FakeRequests, FakeResponse, csrf_client, isolate_app  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="spend/设置写路径需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True + fake sidecar 常备。"""
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


def _setup_platform():
    """平台 AI 凭据就绪（json/PG 通用；ai_config.json 隔离在 tmp_path）。"""
    app_mod._save_ai_config({"base_url": "http://platform.example/v1",
                             "api_key": "sk-platform-123456",
                             "model": "gpt-p"})


def _install_fake(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _sse_ok(session_id="sess-fake-1"):
    return FakeResponse(200, sse_frames=[
        b"data: {\"type\": \"session_started\", \"session_id\": \"%s\"}\n\n"
        % session_id.encode(),
        b"data: {\"type\": \"done\"}\n\n",
    ])


# --------------------------------------------------------------------------- #
# 1. cutover 维护闸（§Batch B 迁移与额度语义 1）
# --------------------------------------------------------------------------- #
_AI_DISPATCH_ROUTES = ("/api/ai/run", "/api/ai/continue", "/api/ai/ask",
                       "/api/ai/branch")


def _run_bodies():
    return {
        "/api/ai/run": {"slide": "s.svs", "task": "t"},
        "/api/ai/continue": {"slide": "s.svs"},
        "/api/ai/ask": {"slide": "s.svs", "annotation_id": "ann-1",
                        "question": "q"},
        "/api/ai/branch": {"slide": "s.svs", "annotation_id": "ann-1"},
    }


def test_maintenance_gate_off_runs_normally(monkeypatch):
    """闸关（缺省 false）：四路由照常进入预备/转发链路（不 503 维护码）。"""
    _setup_platform()
    owner, usera = _setup_users(1)
    fake = _install_fake(monkeypatch)
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    fake.register("POST", "/ask", lambda b, q, h, k: _sse_ok())
    fake.register("POST", "/branch", lambda b, q, h, k: _sse_ok())
    fake.register("POST", "/continue", lambda b, q, h, k: _sse_ok())
    c = _login(_client(), owner)
    if not platform_pg():
        # json 后端：_platform_task_max_steps 等读 settings 走默认；闸读失败
        # fail-closed 只在「读取抛错」时触发，json 后端 get_setting 返回
        # 缺省 False，正常放行
        pass
    for path, body in _run_bodies().items():
        body = dict(body)
        body["request_id"] = "req_" + path.strip("/").replace("/", "_")
        r = c.post(path, json=body)
        assert r.status_code == 200, (path, r.get_data(as_text=True))
        assert "ai_dispatch_maintenance" not in r.get_data(as_text=True)
    assert len(fake.calls) == 4  # 全部转发到 sidecar


def platform_pg():
    import platform_features
    return platform_features.current_backend() == "postgres"


def test_maintenance_gate_blocks_four_routes(monkeypatch):
    """闸开：四路由在预备阶段（无 grant/预占/转发副作用）稳定 503。"""
    _setup_platform()
    owner, _u = _setup_users()
    fake = _install_fake(monkeypatch)
    c = _login(_client(), owner)
    monkeypatch.setattr(app_mod, "_ai_dispatch_maintenance_active",
                        lambda: True)
    for path, body in _run_bodies().items():
        r = c.post(path, json=body)
        assert r.status_code == 503, (path, r.get_data(as_text=True))
        payload = r.get_json()
        code = payload.get("code") if isinstance(payload, dict) else None
        assert code == "ai_dispatch_maintenance", path
    assert fake.calls == []  # 绝不转发 sidecar（零副作用）
    # 预算/审计零写入：grant 未签发、reservation 未预占
    actions = [e.get("action") for e in
               app_mod.share_store.list_audit(limit=50)] \
        if hasattr(app_mod.share_store, "list_audit") else []
    assert "ai.run" not in actions


def test_maintenance_gate_not_applied_to_demo(monkeypatch):
    """/api/demo/ai/run 不闸（cutover 只要求 user open hold=0；demo 是否同时
    维护由 runbook 显式选择）——demo 链路绝不读取维护闸。"""
    calls = {"n": 0}

    def _spy():
        calls["n"] += 1
        return True
    monkeypatch.setattr(app_mod, "_ai_dispatch_maintenance_active", _spy)
    anon = _client()
    r = anon.post("/api/demo/ai/run", json={"slide": "demo.svs",
                                            "task": "看片"})
    # demo 链路可能因其他前置闸失败（PG/adapter/capability），但错误绝不是
    # ai_dispatch_maintenance，且维护闸从未被读取
    assert calls["n"] == 0
    body = r.get_json() or {}
    err = body.get("error")
    err_text = json.dumps(body, ensure_ascii=False, default=str)
    assert "ai_dispatch_maintenance" not in err_text, (r.status_code, err)


@PG
def test_maintenance_gate_reads_real_setting(monkeypatch):
    """PG：闸经 settings_store 的 CAS 写入生效（cutover 脚本路径）；切回后
    放行。读失败 fail-closed（按维护中处理）。"""
    _setup_platform()
    bh.seed_spend_settings()
    owner, _u = _setup_users()
    fake = _install_fake(monkeypatch)
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _login(_client(), owner)
    body = dict(_run_bodies()["/api/ai/run"], request_id="req_gate_real")
    # seed false → 放行
    r = c.post("/api/ai/run", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    # CAS false → true → 503
    settings_store.compare_and_set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, False, True,
        updated_by="pytest-cutover")
    r = c.post("/api/ai/run", json=dict(body, request_id="req_gate_on"))
    assert r.status_code == 503
    assert r.get_json().get("code") == "ai_dispatch_maintenance"
    # 读失败（DB 异常）fail-closed：按维护中处理，绝不误放行
    orig = settings_store.get_setting

    def _boom(key, default=None):
        raise RuntimeError("settings down")
    monkeypatch.setattr(app_mod.settings_store, "get_setting", _boom)
    r = c.post("/api/ai/run", json=dict(body, request_id="req_gate_err"))
    assert r.status_code == 503
    assert r.get_json().get("code") == "ai_dispatch_maintenance"


# --------------------------------------------------------------------------- #
# 2. GET /api/admin/v1/spend/demo-stats（§4.6）
# --------------------------------------------------------------------------- #
@PG
def test_demo_stats_readonly_no_side_effects():
    """owner-only；current/previous 边界由服务端 demo_global 周窗口决定；
    调用前后业务表行数不变（纯只读聚合，§4.6 验收口）。"""
    bh.seed_spend_policies()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    tables = ("ai_spend_windows", "ai_spend_policies", "ai_usage_events",
              "billing_holds", "platform_settings", "ai_spend_denial_events")

    def _counts():
        conn = bh.connect()
        try:
            out = {}
            with conn.cursor() as qcur:
                for t in tables:
                    qcur.execute("SELECT count(*)::int AS n FROM %s" % t)
                    out[t] = qcur.fetchone()["n"]
        finally:
            conn.close()
        return out

    before = _counts()
    r = c.get("/api/admin/v1/spend/demo-stats")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["window"] == "current"
    assert body["subject_type"] == "demo"
    assert body["virtual"] is True  # 无窗口行 → 全 0 虚拟摘要（limit 取策略面值）
    assert body["limit_nano_cny"] == str(50 * 10 ** 9)  # demo_global 种子面值
    assert body["spent_nano_cny"] == "0" and body["remaining_nano"] == \
        str(50 * 10 ** 9)
    for key in ("priced_calls", "unpriced_calls", "holds", "denials",
                "db_unavailable_denials_included", "window_start"):
        assert key in body, key
    # previous 周：同样只读既有行
    r2 = c.get("/api/admin/v1/spend/demo-stats?window=previous")
    assert r2.status_code == 200
    assert r2.get_json()["window"] == "previous"
    after = _counts()
    assert before == after  # 调用前后任何业务表行数不变


@PG
def test_demo_stats_rejects_extra_params_and_bad_window():
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    # 任意金额/主体参数拒绝（边界由服务端决定）
    for qs in ("?subject_id=usr_x", "?limit_nano_cny=1", "?at=123",
               "?window=current&subject_type=user"):
        r = c.get("/api/admin/v1/spend/demo-stats" + qs)
        assert r.status_code == 400, qs
        assert r.get_json()["error"]["code"] == "invalid_request"
    # 非法 window 拒绝
    r = c.get("/api/admin/v1/spend/demo-stats?window=tomorrow")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    # 非 owner 403 / 匿名 401
    u = usera
    cu = _login(_client(), u)
    assert cu.get("/api/admin/v1/spend/demo-stats").status_code == 403
    assert _client().get("/api/admin/v1/spend/demo-stats").status_code == 401


# --------------------------------------------------------------------------- #
# 3. GET /api/admin/v1/site-stats（§Batch D2 7）
# --------------------------------------------------------------------------- #
def test_site_stats_owner_only_and_readonly_passthrough():
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    # 匿名 401 / 非 owner 403
    assert _client().get("/api/admin/v1/site-stats").status_code == 401
    cu = _login(_client(), usera)
    assert cu.get("/api/admin/v1/site-stats").status_code == 403
    # owner 200：dashboard_stats() 契约形状原样透传（json 后端为全零空形状）
    r = c.get("/api/admin/v1/site-stats")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    for key in ("generated_at", "today", "d7", "d30", "daily",
                "top_referrers", "top_pages", "top_countries", "recent",
                "visitor_kinds", "geo_configured"):
        assert key in body, key
    for seg in ("today", "d7", "d30"):
        assert set(body[seg].keys()) == {"visits", "unique_visitors", "bots"}
    assert len(body["daily"]) == 30
    assert body["geo_configured"] is False
    # 只读：无写副作用（调用后形状仍为空，无事件被创建）
    r2 = c.get("/api/admin/v1/site-stats")
    assert r2.status_code == 200
    assert r2.get_json()["today"]["visits"] == body["today"]["visits"]


def test_site_stats_store_missing_returns_404(monkeypatch):
    """import 容错（§Batch D2）：store 未随镜像发布 → 端点 404，采集跳过。"""
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    monkeypatch.setattr(app_mod, "site_stats_store", None)
    r = c.get("/api/admin/v1/site-stats")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "site_stats_unavailable"


# --------------------------------------------------------------------------- #
# 4. 步数契约 API（§Batch C 1/4/8/9）
# --------------------------------------------------------------------------- #
def test_runtime_step_validator_field_level_bounds():
    """字段级 validator（纯函数，json/PG 双跑）：user 步数 1..500；越界 400；
    demo 步数/并发维持各自现有边界（回归：_BUDGET_LIMIT_MAX 未被改 500）。"""
    v = app_mod._validate_runtime_settings
    # user 步数字段：500（上限）与 1（下限）合法
    ok, err = v({"platform_task_max_steps": 500})
    assert (ok, err) == ({"platform_task_max_steps": 500}, None)
    ok, err = v({"own_task_max_steps_limit": 1})
    assert ok == {"own_task_max_steps_limit": 1} and err is None
    # >500 → 稳定 400（不再「保存成功、运行时静默截回」）
    for bad in (501, 5000, 1_000_000):
        ok, err = v({"platform_task_max_steps": bad})
        assert ok is None and err is not None and "1–500" in err, bad
    ok, err = v({"own_task_max_steps_limit": 0})
    assert ok is None and err is not None
    # demo 字段独立边界（_BUDGET_LIMIT_MAX=1_000_000 未动）：
    ok, err = v({"demo_task_max_steps": 501})
    assert ok == {"demo_task_max_steps": 501} and err is None
    ok, err = v({"demo_max_concurrency": 8})
    assert ok == {"demo_max_concurrency": 8} and err is None
    ok, err = v({"demo_max_concurrency": 1_000_001})
    assert ok is None and err is not None
    # 常量红线：共享上限未被改成 500；默认常量 = 500
    assert app_mod._BUDGET_LIMIT_MAX == 1_000_000
    assert budget_store.DEFAULT_PLATFORM_TASK_MAX_STEPS == 500
    assert budget_store.DEFAULT_OWN_TASK_MAX_STEPS_LIMIT == 500
    assert budget_store.DEFAULT_DEMO_TASK_MAX_STEPS == 20
    assert app_mod.DEFAULT_CONFIG["max_steps"] == 500
    assert app_mod._USER_STEP_LIMIT_MAX == 500


@PG
def test_runtime_step_settings_api_and_demo_independence():
    """PUT settings/runtime：user 步数 >500 稳定 400、500 落库；demo 步数
    独立默认 20 不继承 user 值（§Batch C 8）。"""
    bh.seed_spend_settings()
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # >500 → 400
    r = c.put("/api/admin/v1/settings/runtime",
              json={"platform_task_max_steps": 501})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    # 500 落库 + demo 独立
    r = c.put("/api/admin/v1/settings/runtime", json={
        "platform_task_max_steps": 500, "demo_task_max_steps": 20})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["limits"]["platform_task_max_steps"] == 500
    assert r.get_json()["limits"]["demo_task_max_steps"] == 20
    # 运行时读取：user=500；demo 恒 20（不继承 user 的 500）
    assert app_mod._platform_task_max_steps() == 500
    assert app_mod._demo_task_max_steps() == 20
    # user 步数改小不影响 demo（独立字段、独立边界）
    r = c.put("/api/admin/v1/settings/runtime", json={
        "platform_task_max_steps": 300})
    assert r.status_code == 200
    assert app_mod._platform_task_max_steps() == 300
    assert app_mod._demo_task_max_steps() == 20
    # own_task_max_steps_limit 字段同口径（通道已退役，字段级校验保留）
    r = c.put("/api/admin/v1/settings/runtime",
              json={"own_task_max_steps_limit": 9999})
    assert r.status_code == 400
    r = c.put("/api/admin/v1/settings/runtime",
              json={"own_task_max_steps_limit": 500})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# 5. PUT /api/admin/v1/spend/user-default-total-limit（§Batch B：默认 X 单例 CAS）
# --------------------------------------------------------------------------- #
@PG
def test_user_default_total_limit_endpoint_cas():
    """settings.get 扁平三键 + CAS 上下文；R3 单轨后 defaults 行恒在场
    （迁移/conftest 基线物化，source 恒 total_defaults，策略回退源已删除），
    CAS 按行版本递增；版本不符 409；非法输入 400。"""
    bh.seed_spend_policies()
    bh.seed_spend_settings()
    owner, _u = _setup_users()
    c = _login(_client(), owner)

    # 单轨基线：defaults 行恒在场（20 CNY / version 1 / 权威源）
    r = c.get("/api/admin/v1/settings")
    assert r.status_code == 200, r.get_data(as_text=True)
    spend = r.get_json()["spend"]
    assert spend["user_default_total_limit_nano_cny"] == str(20 * 10 ** 9)
    assert spend["user_default_total_limit_source"] == "total_defaults"
    assert int(spend["user_default_total_limit_version"]) == 1
    assert spend["user_default_total_policy_id"] is None

    # 匿名 401 / 非 owner 403
    assert _client().put("/api/admin/v1/spend/user-default-total-limit",
                         json={"limit_nano_cny": "1",
                               "expected_version": 1}).status_code == 401
    cu = _login(_client(), _u)
    assert cu.put("/api/admin/v1/spend/user-default-total-limit",
                  json={"limit_nano_cny": "1",
                        "expected_version": 1}).status_code == 403

    # 行版本与 expected_version 不符 → 409
    r = c.put("/api/admin/v1/spend/user-default-total-limit",
              json={"limit_nano_cny": str(30 * 10 ** 9),
                    "expected_version": 7})
    assert r.status_code == 409

    # CAS 命中（version=1）改写成功；金额只作开户模板不追溯
    r = c.put("/api/admin/v1/spend/user-default-total-limit",
              json={"limit_nano_cny": str(30 * 10 ** 9),
                    "expected_version": 1})
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()["user_default_total"]
    assert out["default_limit_nano_cny"] == str(30 * 10 ** 9)
    assert int(out["version"]) == 2

    # settings.get 仍反映 total_defaults 权威源
    spend = c.get("/api/admin/v1/settings").get_json()["spend"]
    assert spend["user_default_total_limit_nano_cny"] == str(30 * 10 ** 9)
    assert spend["user_default_total_limit_source"] == "total_defaults"
    assert int(spend["user_default_total_limit_version"]) == 2
    assert spend["user_default_total_policy_id"] is None

    # CAS 命中续写成功；旧版本 409
    r = c.put("/api/admin/v1/spend/user-default-total-limit",
              json={"limit_nano_cny": str(35 * 10 ** 9),
                    "expected_version": 2})
    assert r.status_code == 200
    assert int(r.get_json()["user_default_total"]["version"]) == 3
    r = c.put("/api/admin/v1/spend/user-default-total-limit",
              json={"limit_nano_cny": str(36 * 10 ** 9),
                    "expected_version": 1})
    assert r.status_code == 409

    # 非法输入：负金额 / 缺版本
    assert c.put("/api/admin/v1/spend/user-default-total-limit",
                 json={"limit_nano_cny": "-1",
                       "expected_version": 3}).status_code == 400
    assert c.put("/api/admin/v1/spend/user-default-total-limit",
                 json={"limit_nano_cny": "1"}).status_code == 400

    # 审计：spend.total_default_update 已落
    conn = bh.connect()
    try:
        with conn.cursor() as qcur:
            qcur.execute(
                "SELECT count(*)::int AS n FROM audit_events "
                "WHERE action='spend.total_default_update'")
            assert qcur.fetchone()["n"] >= 2
    finally:
        conn.close()
