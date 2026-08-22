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
  - 同 request_id 重试不双扣（reserve 幂等 + consume 幂等）；
  - user 第 11 次平台 AI 被拒（429 user_budget_exhausted）；
  - 平台总量耗尽后 owner/user 均拒（429 platform_ai_budget_exhausted）；
  - own 凭据不扣平台总量（可观测量落 own）；
  - HistoPilot 4xx / 不可达 → release 回退；2xx → consume；
  - run grant 签发失败 → 503 不转发且不扣额度；
  - owner GET/PUT/reset 预算 API（含校验与 audit）；
  - reclaim_expired_reservations 时间回收钩子。
"""
import ipaddress
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-pt3-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"

try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import pytest  # noqa: E402

import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import budget_store  # noqa: E402
import demo_store  # noqa: E402
import platform_features  # noqa: E402
from pg_compat import BACKEND, json_only  # noqa: E402
from _pt_helpers import csrf_client  # noqa: E402

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
def _reset_stores():
    """每用例：清空平台 ai_config、用户库与（json）share 数据的预算相关状态。"""
    p = app_mod._ai_config_path()
    if p.is_file():
        p.unlink()
    uf = getattr(user_store, "USER_FILE", None)
    if uf is not None and getattr(uf, "exists", lambda: False)():
        try:
            uf.unlink()
        except OSError:
            pass
    yield


class FakeResponse:
    """模拟 requests.Response：JSON / SSE 两种形态（含 X-AI-Session-ID）。"""

    def __init__(self, status_code=200, content=None, ctype="application/json",
                 headers=None):
        self.status_code = status_code
        self.content = content if content is not None else b"{}"
        if isinstance(self.content, str):
            self.content = self.content.encode("utf-8")
        self.headers = {"Content-Type": ctype}
        if headers:
            self.headers.update(headers)

    def close(self):
        pass

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size=4096):
        yield self.content


class FakeRequests:
    """替换 app.requests：按 (method, path) 注册 handler；记录调用。"""

    def __init__(self):
        self._routes = {}
        self.calls = []
        self.unreachable = False

    ConnectionError = __import__("requests").ConnectionError
    Timeout = __import__("requests").Timeout

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def _dispatch(self, method, url, **kwargs):
        raw_path = url.split("?")[0]
        for prefix in ("http://", "https://"):
            if raw_path.startswith(prefix):
                raw_path = raw_path[len(prefix):]
        slash = raw_path.find("/")
        raw_path = raw_path[slash:] if slash >= 0 else "/"
        self.calls.append({
            "method": method.upper(), "path": raw_path,
            "body": kwargs.get("json"), "params": kwargs.get("params"),
            "headers": kwargs.get("headers"),
        })
        if self.unreachable:
            raise FakeRequests.ConnectionError("sidecar down (test)")
        handler = self._routes.get((method.upper(), raw_path))
        if handler is None:
            return FakeResponse(404, json.dumps({"error": "no route"}))
        return handler(kwargs.get("params"))

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


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
        "u-%s@x.com" % uuid.uuid4().hex[:8], "password1", role=role)


def _rid():
    return "req_" + uuid.uuid4().hex


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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    _own_credentials(u["user_id"], steps=33)
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.get("/api/ai/config")
    j = r.get_json()
    assert j["using"] == "platform"
    # 平台模式：生效步数=周期 platform_task_max_steps（默认 20），忽略存量 33
    assert j["max_steps"] == 20
    assert j["effective_max_steps"] == 20
    assert j["own_max_steps"] == 33  # 存量值仍回显（历史数据）
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/ask", lambda p: _sse_ok("sess-ask"))
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
        s.update({"auth_user": "o", "user_id": o["user_id"], "role": "owner"})
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
    r2 = c.put("/api/admin/settings/ai-budget", json={"platform_turn_limit": 5})
    assert r2.status_code == 503
    r3 = c.post("/api/admin/settings/ai-budget/reset", json={"confirm": True})
    assert r3.status_code == 503
    # user 一律 403
    u = _make_user("user")
    cu = _client()
    _login(cu, "user", u["user_id"])
    assert cu.get("/api/admin/settings/ai-budget").status_code == 403


@pg_only
def test_budget_api_owner_get_put_reset():
    _setup_platform()
    o = _make_user("owner")
    u = _make_user("user")
    # 预造用量：user 1 次平台 + 1 次 own
    budget_store.reserve_turn(_rid(), "user", u["user_id"], "platform")
    budget_store.reserve_turn(_rid(), "user", u["user_id"], "own")
    c = _client()
    _login(c, "owner", o["user_id"])
    # GET：用量 / 限制 / 构成 / 每用户
    r = c.get("/api/admin/settings/ai-budget")
    assert r.status_code == 200
    j = r.get_json()
    assert j["usage"]["platform"]["total"] == 1
    assert j["usage"]["platform"]["limit"] == 30
    assert j["usage"]["own"]["total"] == 1
    assert j["usage"]["by_subject_type"]["user"]["total"] == 1
    assert j["usage"]["per_user"][0]["subject_id"] == u["user_id"]
    assert j["limits"]["platform_turn_limit"] == 30
    assert j["concurrency"]["current"] >= 1
    assert j["demo_sessions"]["consumed"] == 0
    # PUT：改限制不清用量；demo > platform 拒绝；负值/未知字段拒绝
    r2 = c.put("/api/admin/settings/ai-budget", json={
        "platform_turn_limit": 50, "user_turn_limit": 12,
        "platform_task_max_steps": 25, "own_task_max_steps_limit": 400})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json()["limits"]["platform_turn_limit"] == 50
    assert budget_store.usage_report()["platform"]["total"] == 1  # 用量保留
    assert budget_store.usage_report()["platform"]["limit"] == 50
    assert c.put("/api/admin/settings/ai-budget",
                 json={"demo_turn_limit": 10, "platform_turn_limit": 5}
                 ).status_code == 400
    assert c.put("/api/admin/settings/ai-budget",
                 json={"user_turn_limit": -1}).status_code == 400
    assert c.put("/api/admin/settings/ai-budget",
                 json={"no_such": 1}).status_code == 400
    assert c.put("/api/admin/settings/ai-budget", json={}).status_code == 400
    # reset：缺二次确认 400；confirm=true 开新周期用量归零、audit 落库
    assert c.post("/api/admin/settings/ai-budget/reset",
                  json={}).status_code == 400
    r3 = c.post("/api/admin/settings/ai-budget/reset", json={"confirm": True})
    assert r3.status_code == 200
    assert r3.get_json()["demo_runs_reset"] == 0
    report = budget_store.usage_report()
    assert report["platform"]["total"] == 0
    assert report["platform"]["limit"] == 50  # 限制沿用
    actions = [e.get("action") for e in app_mod.share_store.list_audit(limit=20)]
    assert "ai_budget.reset" in actions
    assert "ai_budget.update" in actions


@pg_only
def test_budget_reset_also_clears_demo_browser_and_ip_gates():
    """一键重置同时退回 Demo consumed，IP 桶不再计入。"""
    _setup_platform()
    o = _make_user("owner")
    demo_store.create_capability("dmo_rst", "hash_rst", ip_prefix_hash="ipp_rst")
    demo_store.reserve_run("dmo_rst", "req_rst", "sld_a", "rev_1",
                           ip_prefix_hash="ipp_rst")
    demo_store.consume_run("dmo_rst", "hp_rst")
    assert demo_store.count_ip_runs("ipp_rst")["count"] == 1
    c = _client()
    _login(c, "owner", o["user_id"])
    got = c.get("/api/admin/settings/ai-budget").get_json()
    assert got["demo_sessions"]["consumed"] == 1
    r = c.post("/api/admin/settings/ai-budget/reset", json={"confirm": True})
    assert r.status_code == 200
    assert r.get_json()["demo_runs_reset"] == 1
    assert demo_store.get_session("dmo_rst")["run_state"] == "available"
    assert demo_store.count_ip_runs("ipp_rst")["count"] == 0
    after = c.get("/api/admin/settings/ai-budget").get_json()
    assert after["demo_sessions"]["consumed"] == 0
    assert after["usage"]["platform"]["total"] == 0


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
    fake.register("POST", "/run", lambda p: _sse_ok("sess-idem"))
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
    _setup_platform()
    # P0-B §3.7：单 user 默认初始额度收紧为 3；本用例验证每 user 上限机制，
    # 显式恢复 10 以保留原语义（第 11 次拒）。
    budget_store.update_period_limits({"user_turn_limit": 10})
    u = _make_user("user")
    _touch("u11.svs")
    _own("u11.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    _setup_platform()
    budget_store.update_period_limits({"platform_turn_limit": 2})
    o = _make_user("owner")
    u = _make_user("user")
    _touch("p.svs")
    _own("p.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    _setup_platform()
    budget_store.update_period_limits({"platform_turn_limit": 1})
    u = _make_user("user")
    _touch("mix.svs")
    _own("mix.svs", u["user_id"])
    fake = _install_fake()
    fake.register("POST", "/run", lambda p: _sse_ok())
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
    fake.register("POST", "/run", lambda p: FakeResponse(
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
    fake.register("POST", "/run", lambda p: _sse_ok())
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
                  lambda p: FakeResponse(200, b"id: 1\nevent: delta\ndata: {}\n\n",
                                         ctype="text/event-stream"))
    fake.register("POST", "/cancel", lambda p: FakeResponse(200, b'{"ok":true}'))
    fake.register("GET", "/session/sess-x",
                  lambda p: FakeResponse(200, json.dumps(
                      {"session": {"id": "sess-x", "owner": u["user_id"]}})))
    c = _client()
    _login(c, "user", u["user_id"])
    assert c.get("/api/ai/session/sess-x/stream?after_seq=3").status_code == 200
    assert c.get("/api/ai/session/sess-x").status_code == 200
    assert c.post("/api/ai/cancel", json={"session_id": "sess-x"}).status_code == 200
    assert _platform_report() == 0


@pg_only
def test_reclaim_expired_reservations_hook():
    rid = _rid()
    budget_store.reserve_turn(rid, "user", "usr_z", "platform", ttl_seconds=60)
    # json 语义：wrapper 在无预算后端 no-op；PG 下未过期不回收
    assert app_mod.reclaim_expired_reservations() == []
    reclaimed = app_mod.reclaim_expired_reservations(time.time() + 120)
    assert [r["request_id"] for r in reclaimed] == [rid]
    assert budget_store.get_reservation(rid)["state"] == "released"
    assert _platform_report() == 0


# --------------------------------------------------------------------------- #
# 8. UI 交付物守卫（owner 预算卡片 + user 步数只读；docs §4.2/§8.3）
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ui_budget_card_and_max_steps_sync_present():
    index = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    shell = (REPO_ROOT / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    # owner 后台「AI 预算」卡片：用量展示 + 限制编辑 + 保存/新周期
    assert '{% include "_app_shell.html" %}' in index
    assert 'id="aibudget-mgr-section"' in shell
    assert 'id="aibudget-usage"' in shell
    assert 'id="aibudget-save-btn"' in shell
    assert 'id="aibudget-usage-badge"' in shell
    assert 'id="aibudget-reset-btn"' in shell
    assert 'id="aibudget-demosteps"' in shell
    assert 'id="aibudget-perbrowser"' in shell
    assert 'id="aibudget-concurrency"' in shell
    assert 'id="aibudget-demo-enabled"' in shell
    assert 'data-i18n="sb.section.aibudget"' in shell
    app_js = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "showAiBudgetMgr" in app_js
    assert "syncAiMaxStepsInput" in app_js  # 平台 AI 步数只读同步（§8.3）
    assert "demo_task_max_steps" in app_js
    assert "demo_per_browser_limit" in app_js
    assert "demo_max_concurrency" in app_js
    assert "demo_enabled" in app_js
    assert "demo_sessions" in app_js
    assert "aibudget-usage-badge" in app_js
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in ("sb.section.aibudget", "sb.aibudget.save", "sb.aibudget.reset",
                "sb.aibudget.reset.confirm", "ai.field.maxsteps.platform.title",
                "sb.aibudget.limit.demosteps", "sb.aibudget.limit.perbrowser",
                "sb.aibudget.limit.concurrency", "sb.aibudget.limit.demoenabled",
                "sb.aibudget.gates", "sb.aibudget.gates.detail",
                "demo.admin.usage"):
        # 中英两份字典都必须有（每个 key 出现两次）
        assert i18n.count('"%s":' % key) == 2, key
