# -*- coding: utf-8 -*-
"""PT-3：注册用户/owner 平台 AI 预算接线 + user max_steps + owner 预算 API 测试。

覆盖（docs demo-access-auth-ui-design §4/§4.1–4.3/§5.3/§9.2/§9.4/§12.3）：

json 模式（默认）：
  - request_id 贯通：非法 400 / 客户端提供原样转发 / 缺省服务端生成；
  - json 后端生产路径 fail-closed：TESTING 关闭后平台 run 503
    pg_backend_required；own 凭据放行（不记账，docs §4.3）；
  - user max_steps：PUT 越界 400、合法持久化、平台模式 GET 返回只读生效步数、
    注入规则（平台=周期 20 / own=已保存值）、请求体临时塞 tuning 不生效；
  - owner 预算 API json fail-closed 503 + CSRF 回归。

PG 模式（RUN_PG_TESTS=1）追加：
  - 批次 F：mode=all 下官方 run 跳过 reserve_turn（零 reservations）+
    ai_run_bindings 绑定/跨主体 409；429 消费断言显式 seed shadow（软闸
    回退语义）；owner 预算 API 写端点 410 turn_budgets_retired；
  - 同 request_id 重试不双扣（reserve 幂等 + consume 幂等）；
  - user 第 11 次平台 AI 被拒（429 user_budget_exhausted）；
  - 平台总量耗尽后 owner/user 均拒（429 platform_ai_budget_exhausted）；
  - own 凭据不扣平台总量（可观测量落 own）；
  - HistoPilot 4xx / 不可达 → release 回退；2xx → consume；
  - run grant 签发失败 → 503 不转发且不扣额度；
  - owner GET/PUT/reset 预算 API（含校验与 audit）；
  - reclaim_expired_reservations 已删除（盲时间回收被确认式对账否定；
    守卫断言符号不存在，防止误接回）。
"""
import inspect
import ipaddress
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import budget_store  # noqa: E402
import demo_store  # noqa: E402
import platform_features  # noqa: E402
from pg_compat import BACKEND, json_only  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, FakeRequests, FakeResponse # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="AI 预算 API 接线需 PG 原子预占（RUN_PG_TESTS=1）",
)


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _ssrf_dns(monkeypatch):
    """测试用 DNS：IP 字面量按字面；localhost/元数据指向私网；其余给公网 IP。"""
    def fake_ips(hostname):
        h = (hostname or "").lower().rstrip(".")
        try:
            return [ipaddress.ip_address(h)]
        except ValueError:
            pass
        if h in ("localhost",) or h.endswith(".localhost"):
            return [ipaddress.ip_address("127.0.0.1")]
        if h in ("metadata.google.internal", "metadata.goog", "metadata"):
            return [ipaddress.ip_address("169.254.169.254")]
        return [ipaddress.ip_address("93.184.216.34")]
    monkeypatch.setattr(app_mod, "_host_ips", fake_ips)


@pytest.fixture(autouse=True)
def _reset_stores(monkeypatch, tmp_path):
    """每用例：独立存储目录（ai_config/用户库/json share 全部落本用例私有目录）。

    通用隔离主体在 _pt_helpers.isolate_app（test-review P3-16 收敛），并附带
    AUTH_ENABLED / app.requests 还原护栏；PG 后端由 conftest 每用例 TRUNCATE。
    """
    isolate_app(monkeypatch, tmp_path)
    yield


def _install_fake():
    fake = FakeRequests()
    app_mod.requests = fake
    return fake


def _sse_ok(session_id="sess-fake-1"):
    """2xx SSE handler（X-AI-Session-ID 头）= HistoPilot 已接受执行。"""
    return FakeResponse(200, b"id: 1\nevent: slide_opened\ndata: {}\n\n",
                        ctype="text/event-stream",
                        headers={"X-AI-Session-ID": session_id})


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _login(client, role, user_id):
    with client.session_transaction() as sess:
        sess["role"] = role
        sess["user_id"] = user_id
        sess["auth_user"] = "t@x.com"
        # 批次 A：手工 session 需携带与库内一致的凭据版本（docs §6.2）
        row = user_store.get_user(user_id)
        sess["auth_version"] = (row or {}).get("auth_version", 1)


def _touch(name="s.svs"):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _own(name, user_id):
    app_mod.share_store.set_slide_meta(name, owner_user_id=user_id)


def _setup_platform(base_url="http://platform.example/v1",
                    key="sk-platform-123456", model="gpt-p"):
    app_mod._save_ai_config({"base_url": base_url, "api_key": key, "model": model})


def _make_user(role="user"):
    return user_store.create_user(
        "u-%s@x.com" % uuid.uuid4().hex[:8], "password1password1", role=role)


def _rid():
    return "req_" + uuid.uuid4().hex


#: 存量 own 凭据步数种子（test-review P3-17 magic 值收敛）：33 不是 app 里的
#: 「官方值」，只是本文件写进存量凭据、再断言 GET 回显一致的同一常量。
OWN_STEPS = 33


def _own_credentials(uid, steps=None, base_url="http://own.example/v1"):
    cfg = {"use_platform": False, "base_url": base_url,
           "model": "gpt-own", "api_key": "sk-own-secret-abcdef"}
    if steps is not None:
        cfg["max_steps"] = steps
    user_store.set_user_ai_config(uid, cfg)


def _run_ok(client, slide, request_id=None):
    body = {"slide": slide}
    if request_id:
        body["request_id"] = request_id
    return client.post("/api/ai/run", json=body)


def _platform_report():
    r = budget_store.usage_report()
    return r["platform"]["total"]


# --------------------------------------------------------------------------- #
# 1. request_id 贯通（json / PG 双跑）
# --------------------------------------------------------------------------- #
def test_request_id_invalid_rejected_400():
    _setup_platform()
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    o = _make_user("owner")
    _touch("r1.svs")
    c = _client()
    _login(c, "owner", o["user_id"])
    for bad in ("bad id!", "x" * 129, "req/斜杠", 12345):
        r = c.post("/api/ai/run", json={"slide": "r1.svs", "request_id": bad})
        assert r.status_code == 400, bad
        assert "request_id" in (r.get_json() or {}).get("error", "")
    assert fake.calls == []  # 未转发


def test_request_id_client_provided_forwarded_and_generated_when_absent():
    _setup_platform()
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    o = _make_user("owner")
    _touch("r2.svs")
    c = _client()
    _login(c, "owner", o["user_id"])
    rid = _rid()
    r = _run_ok(c, "r2.svs", rid)
    assert r.status_code == 200
    assert fake.calls[-1]["body"]["request_id"] == rid  # 同一 id 转发 HistoPilot
    # 缺省 → 服务端生成（仍转发）
    r2 = _run_ok(c, "r2.svs")
    assert r2.status_code == 200
    gen = fake.calls[-1]["body"].get("request_id")
    assert isinstance(gen, str) and gen


# --------------------------------------------------------------------------- #
# 2. json 生产路径 fail-closed（TESTING bypass 只限 pytest）
# --------------------------------------------------------------------------- #
@json_only  # PG 后端预算可用，平台 run 合法放行（本用例锁 json 生产语义）
def test_json_backend_platform_run_fail_closed_without_testing():
    _setup_platform()
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    o = _make_user("owner")
    _touch("fc.svs")
    c = _client()
    _login(c, "owner", o["user_id"])
    # TESTING bypass 放行（pytest 专属；生产不可能设置 TESTING）
    assert _run_ok(c, "fc.svs").status_code == 200
    # 关闭 TESTING → 生产路径：平台凭据 fail-closed（不得无配额放行）
    fake.calls.clear()
    app_mod.app.config["TESTING"] = False
    try:
        r = _run_ok(c, "fc.svs")
        assert r.status_code == 503
        body = r.get_json()
        assert body.get("code") == "pg_backend_required"
        assert fake.calls == []  # 未转发 HistoPilot
    finally:
        app_mod.app.config["TESTING"] = True


@json_only  # PG 后端 own 存量凭据同样被忽略走平台；本用例锁 json 放行语义
def test_json_backend_legacy_own_credentials_now_platform():
    """自带 API 通道下线：存量 own 凭据（use_platform=False）不再放行——
    user 恒走平台凭据，json 生产路径 fail-closed（503 pg_backend_required）。"""
    _setup_platform()  # 平台已配，但用户存量 use_platform=False + 自带凭据
    u = _make_user("user")
    _own_credentials(u["user_id"])
    _touch("own.svs")
    _own("own.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _client()
    _login(c, "user", u["user_id"])
    app_mod.app.config["TESTING"] = False
    try:
        # json 下平台凭据 fail-closed（own 存量凭据不再是无配额逃生通道）
        r = _run_ok(c, "own.svs")
        assert r.status_code == 503
        assert r.get_json().get("code") == "pg_backend_required"
        assert fake.calls == []  # 未转发 HistoPilot
    finally:
        app_mod.app.config["TESTING"] = True


# --------------------------------------------------------------------------- #
# 3. user PUT 全拒 + GET 只读形态（json / PG 双跑）
# --------------------------------------------------------------------------- #
def test_user_put_any_field_rejected_400():
    """AI 服务由平台统一提供：user PUT 凭据四字段 + max_steps + 调优一律 400。"""
    _setup_platform()
    u = _make_user("user")
    c = _client()
    _login(c, "user", u["user_id"])
    for body in ({"max_steps": 33}, {"max_steps": 501}, {"max_steps": "abc"},
                 {"max_steps": 0}, {"use_platform": False},
                 {"base_url": "http://own.example/v1", "model": "gpt-own",
                  "api_key": "sk-own-secret-abcdef"},
                 {"fork_active_limit": 999}, {}):
        r = c.put("/api/ai/config", json=body)
        assert r.status_code == 400, body
        assert "平台统一提供" in (r.get_json() or {}).get("error", "")
    # 不落库（users.json 无新增凭据 / 步数）
    cfg = user_store.get_user_ai_config(u["user_id"]) or {}
    assert not cfg.get("base_url")
    assert cfg.get("max_steps", 20) == 20  # 缺省值未被改写
    # owner PUT 不受影响（对照）
    o = _make_user("owner")
    co = _client()
    _login(co, "owner", o["user_id"])
    r = co.put("/api/ai/config", json={"max_steps": 60})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["max_steps"] == 60


def test_user_get_effective_max_steps_platform_readonly():
    """user GET 只读：using 恒 platform（平台已配）；生效步数=周期平台步数；
    存量 own 步数仅作回显（own_max_steps），无写入通道。"""
    _setup_platform()  # 平台 ai_config.json 的 max_steps 用默认 50
    u = _make_user("user")
    _own_credentials(u["user_id"], steps=OWN_STEPS)
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.get("/api/ai/config")
    j = r.get_json()
    assert j["using"] == "platform"
    # 平台模式：生效步数=周期 platform_task_max_steps（默认 20），忽略存量 OWN_STEPS
    assert j["max_steps"] == 20
    assert j["effective_max_steps"] == 20
    assert j["own_max_steps"] == OWN_STEPS  # 存量回显：值是本测试写入的种子，不是生效步数
    assert j["own_task_max_steps_limit"] >= 20
    # 切回 own 的 PUT 已下线：use_platform=false 同样 400
    r2 = c.put("/api/ai/config", json={"use_platform": False})
    assert r2.status_code == 400
    j3 = c.get("/api/ai/config").get_json()
    assert j3["using"] == "platform"
    assert j3["effective_max_steps"] == 20


def test_max_steps_injection_rules_for_sidecar_config():
    """user 恒平台模式：注入周期 20；存量 own 步数（use_platform=False）被忽略。"""
    _setup_platform()
    u = _make_user("user")
    _own_credentials(u["user_id"], steps=7)  # use_platform=False + steps=7
    _touch("inj.svs")
    _own("inj.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _client()
    _login(c, "user", u["user_id"])
    r = _run_ok(c, "inj.svs")
    assert r.status_code == 200
    cfg = fake.calls[-1]["body"]["config"]
    assert cfg["max_steps"] == 20  # 平台周期步数，忽略存量 own 7
    assert cfg["base_url"] == "http://platform.example/v1"  # 平台凭据
    assert cfg.get("ssrf_guard") is not True


def test_run_body_cannot_smuggle_tuning_fields():
    """浏览器不能靠请求体临时塞未保存的调优值（注入只读已保存配置）。"""
    _setup_platform()
    u = _make_user("user")
    _own_credentials(u["user_id"], steps=9)  # 存量 own 步数（已不再生效）
    _touch("smug.svs")
    _own("smug.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.post("/api/ai/run", json={
        "slide": "smug.svs",
        "request_id": _rid(),
        "config": {"max_steps": 500, "api_key": "sk-smuggled"},
        "max_steps": 500,
    })
    assert r.status_code == 200
    cfg = fake.calls[-1]["body"]["config"]
    assert cfg["max_steps"] == 20         # 平台周期步数（存量 own 9 被忽略）
    assert cfg["api_key"] == "sk-platform-123456"  # 平台 key，请求体整体被忽略


# --------------------------------------------------------------------------- #
# 4. run grant fail-closed（json / PG 双跑）
# --------------------------------------------------------------------------- #
def test_run_grant_failure_rejects_run(monkeypatch):
    _setup_platform()
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    o = _make_user("owner")
    _touch("g.svs")
    c = _client()
    _login(c, "owner", o["user_id"])

    def _boom(**kwargs):
        raise RuntimeError("grant store down")
    monkeypatch.setattr(app_mod.share_store, "create_run_grant", _boom)
    r = _run_ok(c, "g.svs")
    assert r.status_code == 503
    assert "run grant" in (r.get_json() or {}).get("error", "")
    assert fake.calls == []  # 未转发
    if platform_features.budget_features_available():
        assert _platform_report() == 0  # 不扣额度（grant 在预占之前失败）


def test_ask_does_not_require_run_grant(monkeypatch):
    """/ask 为 lite fork 无写工具：不签发也不因缺 grant 被拒（docs §5.4-5）。"""
    _setup_platform()
    u = _make_user("user")
    _own("a.svs", u["user_id"])
    _touch("a.svs")
    fake = _install_fake()
    fake.register("POST", "/ask", lambda b, q, h, k: _sse_ok("sess-ask"))
    c = _client()
    _login(c, "user", u["user_id"])

    def _boom(**kwargs):
        raise RuntimeError("grant store down")
    monkeypatch.setattr(app_mod.share_store, "create_run_grant", _boom)
    r = c.post("/api/ai/ask", json={
        "slide": "a.svs", "annotation_id": "ann-1", "request_id": _rid()})
    assert r.status_code == 200
    sent = fake.calls[-1]["body"]
    assert "run_grant" not in sent["config"]
    assert sent["request_id"]


# --------------------------------------------------------------------------- #
# 5. CSRF 回归（新写接口；json / PG 双跑——CSRF 层先于业务）
# --------------------------------------------------------------------------- #
def test_csrf_required_on_budget_and_ai_write_endpoints():
    _setup_platform()
    o = _make_user("owner")
    raw = app_mod.app.test_client()
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    with raw.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": o["user_id"], "role": "owner",
                  "auth_version": o.get("auth_version", 1)})
    # 无 token → 400 csrf_required（统一 before_request）
    r = raw.put("/api/admin/settings/ai-budget", json={"platform_turn_limit": 30})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    r2 = raw.post("/api/admin/settings/ai-budget/reset", json={"confirm": True})
    assert r2.status_code == 400
    assert r2.get_json()["error"] == "csrf_required"
    r3 = raw.post("/api/ai/run", json={"slide": "x.svs"})
    assert r3.status_code == 400
    assert r3.get_json()["error"] == "csrf_required"


# --------------------------------------------------------------------------- #
# 6. owner 预算 API（json fail-closed / PG 全量）
# --------------------------------------------------------------------------- #
def test_budget_api_json_backend_fail_closed():
    if platform_features.budget_features_available():
        pytest.skip("PG 后端预算可用（503 语义仅 json/dual）")
    _setup_platform()
    o = _make_user("owner")
    c = _client()
    _login(c, "owner", o["user_id"])
    r = c.get("/api/admin/settings/ai-budget")
    assert r.status_code == 503
    assert r.get_json().get("code") == "pg_backend_required"
    # 批次 F：写端点退役（410 turn_budgets_retired）——退役判定先于后端
    r2 = c.put("/api/admin/settings/ai-budget", json={"platform_turn_limit": 5})
    assert r2.status_code == 410
    assert r2.get_json().get("code") == "turn_budgets_retired"
    r3 = c.post("/api/admin/settings/ai-budget/reset", json={"confirm": True})
    assert r3.status_code == 410
    assert r3.get_json().get("code") == "turn_budgets_retired"
    # user 一律 403
    u = _make_user("user")
    cu = _client()
    _login(cu, "user", u["user_id"])
    assert cu.get("/api/admin/settings/ai-budget").status_code == 403


@pg_only
def test_budget_api_owner_get_legacy_and_writes_retired():
    """批次 F：GET 保留（冻结历史 + legacy 标记）；PUT/reset 410 + audit 尝试。"""
    _setup_platform()
    o = _make_user("owner")
    u = _make_user("user")
    # 预造用量：user 1 次平台 + 1 次 own（软闸路径直连 store）
    budget_store.reserve_turn(_rid(), "user", u["user_id"], "platform")
    budget_store.reserve_turn(_rid(), "user", u["user_id"], "own")
    c = _client()
    _login(c, "owner", o["user_id"])
    # GET：用量 / 限制 / 构成 / 每用户 + legacy 标记
    r = c.get("/api/admin/settings/ai-budget")
    assert r.status_code == 200
    j = r.get_json()
    assert j["legacy"] is True
    assert "退役" in j["note"]
    assert j["usage"]["platform"]["total"] == 1
    assert j["usage"]["platform"]["limit"] == 30
    assert j["usage"]["own"]["total"] == 1
    assert j["usage"]["by_subject_type"]["user"]["total"] == 1
    assert j["usage"]["per_user"][0]["subject_id"] == u["user_id"]
    assert j["limits"]["platform_turn_limit"] == 30
    assert j["concurrency"]["current"] >= 1
    # 批次 E：run 用量卡片改为 demo_runs 流水计数（demo_sessions 键退役）
    assert j["demo_runs"]["total"] == 0
    assert j["demo_runs"]["active"] == 0
    assert "demo_sessions" not in j
    # PUT/reset：退役（410 turn_budgets_retired），并 audit 这次尝试
    for bad in (c.put("/api/admin/settings/ai-budget",
                      json={"platform_turn_limit": 50}),
                c.post("/api/admin/settings/ai-budget/reset",
                       json={"confirm": True})):
        assert bad.status_code == 410
        assert bad.get_json().get("code") == "turn_budgets_retired"
    actions = [e.get("action") for e in app_mod.share_store.list_audit(limit=20)]
    assert "turn_budgets.retired_write" in actions
    # 用量保留（写入口没了，冻结历史不受影响）
    assert budget_store.usage_report()["platform"]["total"] == 1


@pg_only
def test_budget_reset_expires_inflight_demo_runs():
    """reset_demo_runs 原语回归：在途 demo run 转 expired 终态（capability
    立即可再开）。批次 F 起 HTTP reset 端点已退役（410），本用例直连 store
    锁定残余语义（管理面不再暴露，线程侧/运维仍可用）。
    """
    _setup_platform()
    o = _make_user("owner")  # noqa: F841 — 登录态不再需要（无 HTTP 入口）
    demo_store.create_capability("dmo_rst", "hash_rst", ip_prefix_hash="ipp_rst")
    run = demo_store.reserve_run("dmo_rst", "req_rst", "sld_a", "rev_1",
                                 ip_prefix_hash="ipp_rst")
    assert run is not None
    assert demo_store.count_run_states()["active"] == 1
    reset_ids = demo_store.reset_demo_runs()
    assert reset_ids == [run["demo_run_id"]]
    assert demo_store.get_run(run["demo_run_id"])["state"] == "expired"
    assert demo_store.count_run_states()["active"] == 0
    # capability 立即可再开（不使 cookie 失效）
    nxt = demo_store.reserve_run("dmo_rst", "req_rst2", "sld_a", "rev_1")
    assert nxt is not None and nxt["state"] == "reserved"
    assert demo_store.count_run_states()["total"] == 2  # 流水保留（append-only）


# --------------------------------------------------------------------------- #
# 7. PG：预占 / 消费 / 释放 时序
# --------------------------------------------------------------------------- #
@pg_only
def test_same_request_id_retry_no_double_charge():
    _setup_platform()
    u = _make_user("user")
    _touch("idem.svs")
    _own("idem.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok("sess-idem"))
    c = _client()
    _login(c, "user", u["user_id"])
    rid = _rid()
    assert _run_ok(c, "idem.svs", rid).status_code == 200
    assert _run_ok(c, "idem.svs", rid).status_code == 200  # 同 id 重试
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 1        # 不双扣
    assert report["platform"]["accepted"] == 1     # 已 consume（幂等）
    assert report["platform"]["reserved"] == 0
    resv = budget_store.get_reservation(rid)
    assert resv["state"] == "consumed"
    assert resv["histopilot_session_id"] == "sess-idem"


@pg_only
def test_user_11th_platform_run_rejected():
    import spend_store
    spend_store.set_enforcement_mode("shadow")  # 批次 F：软闸回退语义
    _setup_platform()
    # P0-B §3.7：单 user 默认初始额度收紧为 3；本用例验证每 user 上限机制，
    # 显式恢复 10 以保留原语义（第 11 次拒）。
    budget_store.update_period_limits({"user_turn_limit": 10})
    u = _make_user("user")
    _touch("u11.svs")
    _own("u11.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _client()
    _login(c, "user", u["user_id"])
    for _ in range(10):
        r = _run_ok(c, "u11.svs")
        assert r.status_code == 200
    assert _platform_report() == 10
    fake.calls.clear()
    r11 = _run_ok(c, "u11.svs")
    assert r11.status_code == 429
    body = r11.get_json()
    assert body.get("code") == "user_budget_exhausted"
    assert fake.calls == []  # 拒绝在转发之前
    # 其它用户不受影响
    u2 = _make_user("user")
    _own("u11.svs", u["user_id"])  # 仍是同一 slide；给 u2 协作权不便，另开切片
    _touch("u11b.svs")
    _own("u11b.svs", u2["user_id"])
    c2 = _client()
    _login(c2, "user", u2["user_id"])
    assert _run_ok(c2, "u11b.svs").status_code == 200


@pg_only
def test_platform_total_exhausted_rejects_owner_and_user():
    import spend_store
    spend_store.set_enforcement_mode("shadow")  # 批次 F：软闸回退语义
    _setup_platform()
    budget_store.update_period_limits({"platform_turn_limit": 2})
    o = _make_user("owner")
    u = _make_user("user")
    _touch("p.svs")
    _own("p.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    co = _client()
    _login(co, "owner", o["user_id"])
    cu = _client()
    _login(cu, "user", u["user_id"])
    assert _run_ok(co, "p.svs").status_code == 200
    assert _run_ok(cu, "p.svs").status_code == 200
    # 第 3 次：user 子额度未满，但平台总量已满 → owner/user 均拒
    fake.calls.clear()
    r_u = _run_ok(cu, "p.svs")
    assert r_u.status_code == 429
    assert r_u.get_json().get("code") == "platform_ai_budget_exhausted"
    r_o = _run_ok(co, "p.svs")
    assert r_o.status_code == 429
    assert r_o.get_json().get("code") == "platform_ai_budget_exhausted"
    assert fake.calls == []


@pg_only
def test_legacy_own_credentials_are_not_quota_escape_hatch():
    """自带 API 通道下线：存量 own 凭据不再是平台配额的逃生通道——
    平台总量打满后，即使 user 存量 use_platform=False 也按平台凭据拒（429）。"""
    import spend_store
    spend_store.set_enforcement_mode("shadow")  # 批次 F：软闸回退语义
    _setup_platform()
    budget_store.update_period_limits({"platform_turn_limit": 1})
    u = _make_user("user")
    _touch("mix.svs")
    _own("mix.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    c = _client()
    _login(c, "user", u["user_id"])
    # 第 1 次平台凭据 → 平台总量满
    assert _run_ok(c, "mix.svs").status_code == 200
    # 存量 own 凭据（use_platform=False）：同样走平台 → 平台总量已满 → 拒
    _own_credentials(u["user_id"], steps=15)
    fake.calls.clear()
    r2 = _run_ok(c, "mix.svs")
    assert r2.status_code == 429
    assert r2.get_json().get("code") == "platform_ai_budget_exhausted"
    assert fake.calls == []
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 1
    assert report["own"]["total"] == 0  # own 维度不再产生用量


@pg_only
def test_histopilot_4xx_releases_reservation():
    _setup_platform()
    u = _make_user("user")
    _touch("rel.svs")
    _own("rel.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: FakeResponse(
        409, json.dumps({"error": "会话正在运行中"}).encode()))
    c = _client()
    _login(c, "user", u["user_id"])
    rid = _rid()
    r = _run_ok(c, "rel.svs", rid)
    assert r.status_code == 409  # 透传
    resv = budget_store.get_reservation(rid)
    assert resv["state"] == "released"
    assert _platform_report() == 0  # usage 回退
    # 释放后同 rid 重试：reserve 幂等命中 released 行——不再占用额度，
    # 修正后的重试应使用新 request_id（客户端责任）；这里验证 released 不再计费
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 0


@pg_only
def test_histopilot_unreachable_releases_reservation():
    _setup_platform()
    u = _make_user("user")
    _touch("down.svs")
    _own("down.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok())
    fake.unreachable = True
    c = _client()
    _login(c, "user", u["user_id"])
    rid = _rid()
    r = _run_ok(c, "down.svs", rid)
    assert r.status_code == 503
    assert budget_store.get_reservation(rid)["state"] == "released"
    assert _platform_report() == 0


@pg_only
def test_stream_reconnect_and_cancel_do_not_reserve():
    """SSE 重连 / session 读取 / cancel 不预占（docs §4.1）。"""
    _setup_platform()
    u = _make_user("user")
    fake = _install_fake()
    fake.register("GET", "/session/sess-x/stream",
                  lambda b, q, h, k: FakeResponse(200, b"id: 1\nevent: delta\ndata: {}\n\n",
                                         ctype="text/event-stream"))
    fake.register("POST", "/cancel", lambda b, q, h, k: FakeResponse(200, b'{"ok":true}'))
    fake.register("GET", "/session/sess-x",
                  lambda b, q, h, k: FakeResponse(200, json.dumps(
                      {"session": {"id": "sess-x", "owner": u["user_id"]}})))
    c = _client()
    _login(c, "user", u["user_id"])
    assert c.get("/api/ai/session/sess-x/stream?after_seq=3").status_code == 200
    assert c.get("/api/ai/session/sess-x").status_code == 200
    assert c.post("/api/ai/cancel", json={"session_id": "sess-x"}).status_code == 200
    assert _platform_report() == 0


def test_reclaim_expired_reservations_removed():
    """盲时间回收钩子已删除（review 2026-08-29 §10.3 阶段 5）。

    其语义（按 expires_at 到期即退款）被确认式对账明确否定：HistoPilot
    不可达/未确认的过期预占必须顺延，盲回收会把已接受的执行误退款。守卫
    断言 app 层符号不存在，防止未来误接回；budget_store.reclaim_expired
    原语本身也已随批次 F 删除（无生产调用方，见 test_budget_store 的
    record_run_binding 用例替代）。
    """
    assert not hasattr(app_mod, "reclaim_expired_reservations")
    assert "def reclaim_expired_reservations" not in inspect.getsource(app_mod)
    assert not hasattr(budget_store, "reclaim_expired")
    # 软闸回退底板仍在：reserve_turn 原语保留（shadow 路径调用）
    assert hasattr(budget_store, "reserve_turn")
    # 后台线程只走确认式对账（源码守卫）
    loop_src = inspect.getsource(app_mod._start_budget_reclaim_thread)
    assert "reconcile_expired_reservations()" in loop_src.split("def _loop")[1]


@pg_only
def test_hard_mode_run_skips_reservations_and_writes_binding():
    """批次 F 核心分流：mode=all 下官方 run 200 且**不写** ai_budget_reservations。

    - 不预占/不消费/不释放（usage 平移为零）；
    - request_id 幂等与主体绑定由 ai_run_bindings 承担：2xx 后写入绑定行
      （session 匹配），重复同 rid run 仍 200（replayed）；
    - 跨主体复用同 request_id → 409 request_id_subject_conflict（预检）。
    """
    import spend_store
    spend_store.set_enforcement_mode("all")
    _setup_platform()
    u = _make_user("user")
    _touch("hard.svs")
    _own("hard.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda b, q, h, k: _sse_ok("sess-hard"))
    c = _client()
    _login(c, "user", u["user_id"])
    rid = _rid()
    r = _run_ok(c, "hard.svs", rid)
    assert r.status_code == 200, r.get_data(as_text=True)
    # 零 reservations / 零 usage（硬闸主体不写消费闸）
    assert budget_store.get_reservation(rid) is None
    assert _platform_report() == 0
    assert (budget_store.usage_report()["own"]["total"]) == 0
    # 绑定行在 2xx 接受后写入（session 匹配）
    binding = budget_store.get_run_binding(rid)
    assert binding is not None
    assert binding["subject_type"] == "user"
    assert binding["subject_id"] == u["user_id"]
    assert binding["histopilot_session_id"] == "sess-hard"
    # 同 rid 重试（同主体）→ 200（幂等由绑定行承担）
    assert _run_ok(c, "hard.svs", rid).status_code == 200
    # 跨主体复用同 rid → 409（预检拒绝，且不转发）
    o = _make_user("owner")
    co = _client()
    _login(co, "owner", o["user_id"])
    fake.calls.clear()
    r_conflict = co.post("/api/ai/run", json={"slide": "hard.svs",
                                              "request_id": rid})
    assert r_conflict.status_code == 409
    assert r_conflict.get_json().get("code") == "request_id_subject_conflict"
    assert fake.calls == []  # 预检在转发之前拒绝
    assert budget_store.get_reservation(rid) is None  # 全程零 reservation


# --------------------------------------------------------------------------- #
# 8. UI 交付物守卫（owner 预算卡片 + user 步数只读；docs §4.2/§8.3）
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ui_budget_card_and_max_steps_sync_present():
    """批次 F UI 退役守卫：turn 预算管理 UI 降级为只读 legacy 卡。

    Viewer 侧栏的 users/plugins/aibudget 三个管理 section 已删（方案 §13
    PR5）；批次 F 起 admin 插件内的 turn 编辑表单/保存/开新周期/二次确认
    一并移除（服务端写端点 410 turn_budgets_retired），只保留 GET 冻结
    历史展示卡（adm-billing-turn）与 overview 的 legacy 徽标。平台侧保留
    user max_steps 只读同步（syncAiMaxStepsInput，§8.3）。
    """
    index = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    shell = (REPO_ROOT / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    plugin_ui = (REPO_ROOT / "plugins" / "pathtogether-admin" / "ui"
                 / "index.html").read_text(encoding="utf-8")
    plugin_js = (REPO_ROOT / "plugins" / "pathtogether-admin" / "ui"
                 / "main.js").read_text(encoding="utf-8")
    bridge_js = (REPO_ROOT / "static" / "admin-host.js").read_text(
        encoding="utf-8")
    assert '{% include "_app_shell.html" %}' in index
    # 旧侧栏管理块已删（含指向 /admin/registration 的链接）
    assert "aibudget-mgr-section" not in shell
    assert "users-mgr-section" not in shell
    assert "plugins-mgr-section" not in shell
    assert "admin/registration" not in shell
    # 只读 legacy 卡保留（GET 数据展示 + 已退役标记）；编辑入口全部移除
    assert 'id="adm-billing-turn"' in plugin_ui
    assert 'id="adm-turn-legacy-card"' in plugin_ui
    assert "已退役" in plugin_ui
    assert 'id="adm-ov-turn-legacy"' in plugin_ui
    for gone in ("adm-turn-edit-form", "adm-turn-save-btn",
                 "adm-turn-newperiod-btn", "adm-turn-confirm",
                 "adm-turn-demosteps", "adm-turn-perbrowser",
                 "adm-turn-concurrency", "adm-turn-demo-enabled",
                 "adm-turn-platform", "adm-turn-demo"):
        assert gone not in plugin_ui, gone
    assert "admin.turnBudgets.update" not in plugin_js
    assert "admin.turnBudgets.newPeriod" not in plugin_js
    assert "admin.turnBudgets.get" in plugin_js  # 只读保留
    # 桥层：update/newPeriod 的权限映射与 schema 已删；runtime 写改打新端点
    assert '"admin.turnBudgets.update"' not in bridge_js
    assert '"admin.turnBudgets.newPeriod"' not in bridge_js
    assert '"admin.turnBudgets.get": "admin:turn-budgets:read"' in bridge_js
    assert '"/api/admin/v1/settings/runtime", "PUT"' in bridge_js
    manifest = json.loads((REPO_ROOT / "plugins" / "pathtogether-admin"
                           / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pluginVersion"] == "0.3.0"
    assert "admin:turn-budgets:write" not in manifest["adminPermissions"]
    assert "admin:turn-budgets:read" in manifest["adminPermissions"]
    app_js = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "syncAiMaxStepsInput" in app_js  # 平台 AI 步数只读同步（§8.3）
    assert "showAiBudgetMgr" not in app_js  # 旧侧栏实现已删
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in ("ai.field.maxsteps.platform.title", "demo.admin.usage"):
        # 中英两份字典都必须有（每个 key 出现两次）
        assert i18n.count('"%s":' % key) == 2, key
    # 旧侧栏管理 i18n 键已随 UI 一并删除（中英两份都不再出现）
    for key in ("sb.section.aibudget", "sb.aibudget.save",
                "sb.section.users", "sb.section.plugins"):
        assert '"%s":' % key not in i18n, key
