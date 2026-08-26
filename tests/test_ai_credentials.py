# -*- coding: utf-8 -*-
"""Stage 3a 第二节点 2b：AI 凭据规则（§5.1.2）+ AI 端点资源鉴权 + 会话归属。

覆盖：
  - per-user api_key 加密落盘（users.json 原文无明文）；
  - 凭据解析决策表（_build_sidecar_config user_ctx）：
      use_platform + 平台已配 → 平台；
      use_platform 但平台未配 → 自己的；
      自己的缺 key/base_url/model → None（调用端点回 400）；
      owner / AUTH_ENABLED=False → 平台；
  - user 改 tuning → 403；user 只能见掩码（不回显明文）；
  - GET /api/ai/config 返回 platform_configured 与 using；
  - AI run 无权切片 → 403；凭据未配置 → 400；
  - sessions 按归属过滤（user 仅自己名下，owner 全量）；
  - session 详情越权 403（user 访问他人会话）；
  - AUTH_ENABLED=False 全兼容（owner 语义，不注入 owner，不 filter）。

方案：隔离临时 SHARE_DATA_DIR；用 FakeRequests 替换 app.requests（无需真 sidecar）。
登录状态用 Flask client.session_transaction 直接写 session["role"]/["user_id"]。
运行：cd 项目根 && python3 -m pytest tests/test_ai_credentials.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, isolate_app # noqa: E402
from pg_compat import json_only  # noqa: E402

import ipaddress  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例：独立存储目录（ai_config/users.json 落本用例私有目录）。

    通用隔离主体在 _pt_helpers.isolate_app（test-review P3-16 收敛），并附带
    AUTH_ENABLED / app.requests 还原护栏（_install_fake 的裸赋值不再跨用例泄漏）。
    """
    isolate_app(monkeypatch, tmp_path)
    yield


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


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _login(client, role, user_id):
    """用 session_transaction 直接注入身份（等价登录成功后的 session 状态）。

    auth_version（批次 A docs §6.2）：手工 session 也要携带与库内一致的
    凭据版本，否则 _require_auth 版本比对会判失效。
    """
    with client.session_transaction() as sess:
        sess["role"] = role
        sess["user_id"] = user_id
        sess["auth_user"] = "t@x.com"
        row = user_store.get_user(user_id) if user_id else None
        sess["auth_version"] = (row or {}).get("auth_version", 1)


def _touch(name="demo.svs"):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _own(name, user_id):
    app_mod.share_store.set_slide_meta(name, owner_user_id=user_id)


def _reset_config():
    p = app_mod._ai_config_path()
    if p.is_file():
        p.unlink()
    # 清掉 user 表
    uf = user_store.USER_FILE
    if uf.exists():
        uf.unlink()


def _setup_platform(base_url="http://platform/v1", key="sk-platform-123456", model="gpt-p"):
    app_mod._save_ai_config({"base_url": base_url, "api_key": key, "model": model})


class FakeResponse:
    def __init__(self, status_code=200, content=None, ctype="application/json"):
        self.status_code = status_code
        self.content = content if content is not None else b"{}"
        if isinstance(self.content, str):
            self.content = self.content.encode("utf-8")
        self.headers = {"Content-Type": ctype}

    def close(self):
        pass

    def iter_content(self, chunk_size=4096):
        yield self.content


class FakeRequests:
    def __init__(self):
        self._routes = {}
        self.calls = []

    def register(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def _dispatch(self, method, url, **kwargs):
        # 提取 path（不含 query）
        raw_path = url.split("?")[0]
        for prefix in ("http://", "https://"):
            if raw_path.startswith(prefix):
                raw_path = raw_path[len(prefix):]
        slash = raw_path.find("/")
        raw_path = raw_path[slash:] if slash >= 0 else "/"
        handler = self._routes.get((method.upper(), raw_path))
        self.calls.append({"method": method, "path": raw_path,
                           "params": kwargs.get("params"), "headers": kwargs.get("headers"),
                           "body": kwargs.get("json")})
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


# =========================================================================== #
# 1. per-user api_key 加密落盘
# =========================================================================== #
@json_only  # 断言 users.json 原文（PG 后端加密态在库里，无 json 文件）
def test_user_api_key_encrypted_on_disk():
    _reset_config()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    # app 侧负责在落盘前加密（_encrypt_api_key）；store 层原样存储。
    enc = app_mod._encrypt_api_key("sk-user-secret-abcdef")
    assert enc.startswith("enc:")
    user_store.set_user_ai_config(u["user_id"], {"api_key": enc})
    # users.json 原文不得含明文
    raw = user_store.USER_FILE.read_text(encoding="utf-8")
    assert "sk-user-secret-abcdef" not in raw, "users.json 泄漏明文 api_key！"
    assert "enc:" in raw, "users.json 应存 Fernet 加密密文"
    # 读回应解密为明文
    cfg = user_store.get_user_ai_config(u["user_id"])
    assert app_mod._decrypt_api_key(cfg["api_key"]) == "sk-user-secret-abcdef"


# =========================================================================== #
# 2. 凭据解析决策表（_build_sidecar_config user_ctx）
# =========================================================================== #
def test_resolve_use_platform_when_platform_configured():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {"use_platform": True, "api_key": "sk-own"})
    cfg = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg is not None
    assert cfg["base_url"] == "http://platform/v1"
    assert cfg["api_key"] == "sk-platform-123456"
    assert cfg["model"] == "gpt-p"
    assert cfg.get("ssrf_guard") is not True


def test_resolve_user_ignores_legacy_own_credentials():
    """user 自带 API 通道已下线：use_platform=False + 自带凭据齐备仍走平台。"""
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {
        "use_platform": False, "base_url": "http://own/v1",
        "model": "gpt-own", "api_key": "sk-own-secret"})
    source, cred = app_mod._resolve_ai_credentials(
        {"role": "user", "user_id": u["user_id"]})
    assert source == "platform"
    assert cred["base_url"] == "http://platform/v1"
    cfg = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg is not None
    assert cfg["base_url"] == "http://platform/v1"
    assert cfg.get("ssrf_guard") is not True  # 平台 URL 不受 SSRF 限制


def test_resolve_platform_missing_returns_none_even_with_own():
    """平台未配置 → user 不可用（自带凭据齐备也不作为回退）。"""
    _reset_config()  # 平台未配
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {
        "use_platform": False, "base_url": "http://own/v1",
        "model": "gpt-own", "api_key": "sk-own-secret"})
    source, cred = app_mod._resolve_ai_credentials(
        {"role": "user", "user_id": u["user_id"]})
    assert source is None and cred is None
    cfg = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg is None


def test_resolve_own_missing_key_returns_none():
    _reset_config()  # 平台未配
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {"base_url": "http://own/v1", "model": "gpt-own"})
    cfg = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg is None


def test_resolve_owner_uses_platform():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("o@x.com", "password1password1", role="owner")
    cfg = app_mod._build_sidecar_config({"role": "owner", "user_id": u["user_id"]})
    assert cfg is not None
    assert cfg["base_url"] == "http://platform/v1"
    assert cfg["api_key"] == "sk-platform-123456"


def test_resolve_no_auth_uses_platform():
    """AUTH_ENABLED=False：current_identity 归一 owner → 平台配置。"""
    _reset_config()
    _setup_platform()
    cfg = app_mod._build_sidecar_config(None)
    assert cfg is not None
    assert cfg["base_url"] == "http://platform/v1"


# =========================================================================== #
# 3. /api/ai/config 角色化
# =========================================================================== #
def test_user_config_get_masked_and_platform_fields():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {"api_key": "sk-user-long-secret-12345678", "base_url": "http://own/v1", "model": "gpt-own"})
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.get("/api/ai/config")
    j = r.get_json()
    assert r.status_code == 200
    assert j["platform_configured"] is True
    assert j["using"] == "platform"  # use_platform 缺省 True 且平台已配
    # 存量 use_platform=False 同样恒平台（自带通道已下线）
    user_store.set_user_ai_config(u["user_id"], {"use_platform": False})
    j2 = c.get("/api/ai/config").get_json()
    assert j2["using"] == "platform"
    # 平台未配置 → using 为 null（前端提示联系管理员）；注意 _reset_config 会
    # 清掉 users.json（用户被删会话即失效），需重建用户再登录
    _reset_config()
    u2 = user_store.create_user("u2@x.com", "password1password1", role="user")
    _login(c, "user", u2["user_id"])
    j3 = c.get("/api/ai/config").get_json()
    assert j3["platform_configured"] is False
    assert j3["using"] is None
    _setup_platform()
    # 平台模型名不下发普通用户（平台运营信息，仅 owner 侧折叠摘要可见）
    assert "platform_model" not in j
    # api_key 只回显掩码，不回显明文（存量数据过渡期回显）
    assert "sk-user-long-secret-12345678" not in r.get_data(as_text=True)
    assert j["api_key_mask"]
    # tuning 字段从平台值回显
    assert j["model"] == "gpt-own"


def test_user_put_tuning_differs_returns_400():
    """user PUT 任何字段一律 400（tuning 与 max_steps 同样拒绝）。"""
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    c = _client()
    _login(c, "user", u["user_id"])
    for body in ({"fork_active_limit": 999}, {"max_steps": 33},
                 {"max_tokens": 9999}, {"api_protocol": "gemini"}):
        r = c.put("/api/ai/config", json=body)
        assert r.status_code == 400, body
        assert "平台统一提供" in (r.get_json() or {}).get("error", "")


@json_only  # 断言 users.json 原文不落盘（PG 后端无 json 文件）
def test_user_put_credentials_rejected_and_not_persisted():
    """user 凭据四字段一律 400；存量数据不被改写、新数据不落盘。"""
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.put("/api/ai/config", json={
        "use_platform": False, "base_url": "http://own/v1",
        "model": "gpt-own", "api_key": "sk-own-secret-abcdef"})
    assert r.status_code == 400
    assert "平台统一提供" in (r.get_json() or {}).get("error", "")
    # 空负载同样拒绝（user 无任何可写字段）
    r_empty = c.put("/api/ai/config", json={})
    assert r_empty.status_code == 400
    # 未落盘：users.json 无新增凭据
    raw = user_store.USER_FILE.read_text(encoding="utf-8")
    assert "sk-own-secret-abcdef" not in raw
    cfg = user_store.get_user_ai_config(u["user_id"]) or {}
    assert not cfg.get("base_url")


def test_owner_config_get_uses_platform():
    _reset_config()
    _setup_platform()
    o = user_store.create_user("o@x.com", "password1password1", role="owner")
    c = _client()
    _login(c, "owner", o["user_id"])
    r = c.get("/api/ai/config")
    j = r.get_json()
    assert r.status_code == 200
    assert j["base_url"] == "http://platform/v1"
    assert j["using"] == "platform"


def test_owner_put_tuning_ok():
    _reset_config()
    o = user_store.create_user("o@x.com", "password1password1", role="owner")
    c = _client()
    _login(c, "owner", o["user_id"])
    r = c.put("/api/ai/config", json={"max_steps": 60})
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["max_steps"] == 60


# =========================================================================== #
# 4. AI 端点资源鉴权 + 凭据缺失
# =========================================================================== #
def test_ai_run_unauthorized_slide_403():
    _reset_config()
    _setup_platform()
    ua = user_store.create_user("a@x.com", "password1password1", role="user")
    ub = user_store.create_user("b@x.com", "password1password1", role="user")
    _touch("a.svs")
    _own("a.svs", ua["user_id"])
    fake = _install_fake()
    c = _client()
    _login(c, "user", ub["user_id"])
    r = c.post("/api/ai/run", json={"slide": "a.svs"})
    assert r.status_code == 403
    assert fake.calls == []  # 未转发到 sidecar


def test_ai_run_public_readonly_403():
    """公共只读切片可 view 但不可 annotate，不得起跑并签发写 grant。"""
    _reset_config()
    _setup_platform()
    ua = user_store.create_user("a@x.com", "password1password1", role="user")
    ub = user_store.create_user("b@x.com", "password1password1", role="user")
    _touch("pub.svs")
    app_mod.share_store.set_slide_meta(
        "pub.svs", public=True, owner_user_id=ua["user_id"])
    fake = _install_fake()
    c = _client()
    _login(c, "user", ub["user_id"])
    r = c.post("/api/ai/run", json={"slide": "pub.svs"})
    assert r.status_code == 403
    assert fake.calls == []


def test_ai_run_no_credentials_400():
    _reset_config()  # 平台未配
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    _touch("own400.svs")
    _own("own400.svs", u["user_id"])
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.post("/api/ai/run", json={"slide": "own400.svs"})
    assert r.status_code == 400
    j = r.get_json()
    assert j and "平台 AI 未配置" in (j.get("error") or "")
    assert "联系管理员" in (j.get("error") or "")


def test_ai_run_authorized_proxies_with_owner():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    _touch("ownok.svs")
    _own("ownok.svs", u["user_id"])
    fake = _install_fake()

    def handler(params):
        return FakeResponse(200, ctype="text/event-stream")
    fake.register("POST", "/run", handler)
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.post("/api/ai/run", json={"slide": "ownok.svs"})
    assert r.status_code == 200
    # 断言注入 session_owner（起跑类端点按当前 user_id 注入）
    assert fake.calls
    sent = fake.calls[-1]["body"] or {}
    cfg = sent.get("config") or {}
    assert cfg.get("session_owner") == u["user_id"]
    assert cfg.get("base_url") == "http://platform/v1"


# =========================================================================== #
# 5. sessions 按归属过滤 / session 详情越权
# =========================================================================== #
def test_sessions_user_filter_owner_all():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    o = user_store.create_user("o@x.com", "password1password1", role="owner")
    _touch("s.svs")
    _own("s.svs", u["user_id"])

    fake = _install_fake()

    def sess_handler(params):
        return FakeResponse(200, json.dumps({"sessions": [{"id": "s1"}, {"id": "s2"}]}))
    fake.register("GET", "/sessions", sess_handler)

    # user → ?owner=<user_id> 过滤
    cu = _client()
    _login(cu, "user", u["user_id"])
    fake.calls.clear()
    cu.get("/api/ai/sessions?slide=s.svs")
    assert fake.calls and fake.calls[-1]["params"] == {"slide": "s.svs", "owner": u["user_id"]}

    # owner → 不过滤（只传 slide）
    co = _client()
    _login(co, "owner", o["user_id"])
    fake.calls.clear()
    co.get("/api/ai/sessions?slide=s.svs")
    assert fake.calls and fake.calls[-1]["params"] == {"slide": "s.svs"}


def test_session_detail_owner_check():
    _reset_config()
    _setup_platform()
    ua = user_store.create_user("a@x.com", "password1password1", role="user")
    ub = user_store.create_user("b@x.com", "password1password1", role="user")
    fake = _install_fake()

    def detail_handler(params):
        return FakeResponse(200, json.dumps({"session": {"id": "s-a", "owner": ua["user_id"]}}))
    fake.register("GET", "/session/s-a", detail_handler)

    # userB 访问 userA 的会话 → 403
    cb = _client()
    _login(cb, "user", ub["user_id"])
    r = cb.get("/api/ai/session/s-a")
    assert r.status_code == 403
    # owner 访问任意会话 → 放行（透传）
    o = user_store.create_user("o@x.com", "password1password1", role="owner")
    co = _client()
    _login(co, "owner", o["user_id"])
    r2 = co.get("/api/ai/session/s-a")
    assert r2.status_code == 200
    assert r2.get_json()["session"]["id"] == "s-a"


# =========================================================================== #
# 6. AUTH_ENABLED=False 全兼容
# =========================================================================== #
def test_no_auth_full_compat():
    _reset_config()
    _setup_platform()
    _touch("x.svs")
    fake = _install_fake()

    def run_handler(params):
        return FakeResponse(200, ctype="text/event-stream")
    fake.register("POST", "/run", run_handler)
    fake.register("GET", "/sessions",
                  lambda p: FakeResponse(200, json.dumps({"sessions": []})))

    c = _client(auth=False)
    # run：不注入 session_owner
    fake.calls.clear()
    r = c.post("/api/ai/run", json={"slide": "x.svs"})
    assert r.status_code == 200
    # sessions：不过滤 owner
    fake.calls.clear()
    c.get("/api/ai/sessions?slide=x.svs")
    assert fake.calls and fake.calls[-1]["params"] == {"slide": "x.svs"}


def test_owner_run_does_not_inject_session_owner():
    """role=owner 起跑不注入 session_owner（owner 全量可见、可续跑任意会话，
    sidecar acquire 归属守卫对无 owner 注入的 run 不生效）；owner 名下会话保持
    无 owner 字段，user 的 ?owner= 过滤自然看不到。"""
    _reset_config()
    _setup_platform()
    o = user_store.create_user("o2@x.com", "password1password1", role="owner")
    _touch("ownr.svs")
    fake = _install_fake()

    def handler(params):
        return FakeResponse(200, ctype="text/event-stream")
    fake.register("POST", "/run", handler)
    c = _client()
    _login(c, "owner", o["user_id"])
    r = c.post("/api/ai/run", json={"slide": "ownr.svs"})
    assert r.status_code == 200
    sent = fake.calls[-1]["body"] or {}
    cfg = sent.get("config") or {}
    assert "session_owner" not in cfg


def test_user_put_loopback_base_url_rejected():
    """user PUT 凭据字段整包 400（不再进入 SSRF 校验——无写入通道）。"""
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.put("/api/ai/config", json={
        "use_platform": False, "base_url": "http://127.0.0.1:8317/v1",
        "model": "gpt-own", "api_key": "sk-own-secret-abcdef"})
    assert r.status_code == 400
    assert "平台统一提供" in (r.get_json() or {}).get("error", "")


def test_user_put_link_local_metadata_base_url_rejected():
    _reset_config()
    _setup_platform()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    c = _client()
    _login(c, "user", u["user_id"])
    r = c.put("/api/ai/config", json={
        "use_platform": False, "base_url": "http://169.254.169.254/latest/meta-data/",
        "model": "m", "api_key": "sk-x"})
    assert r.status_code == 400


def test_build_sidecar_ignores_private_own_url():
    """存量私网 own base_url 不再进入 sidecar（user 恒平台）；平台未配 → None。"""
    _reset_config()
    u = user_store.create_user("u@x.com", "password1password1", role="user")
    user_store.set_user_ai_config(u["user_id"], {
        "use_platform": False, "base_url": "http://10.0.0.1/v1",
        "model": "gpt-own", "api_key": "sk-own-secret"})
    cfg = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg is None
    # 平台已配时忽略存量私网 own URL，走平台且不注入 ssrf_guard
    _setup_platform()
    cfg2 = app_mod._build_sidecar_config({"role": "user", "user_id": u["user_id"]})
    assert cfg2 is not None
    assert cfg2["base_url"] == "http://platform/v1"
    assert cfg2.get("ssrf_guard") is not True
