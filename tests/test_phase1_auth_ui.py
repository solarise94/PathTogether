# -*- coding: utf-8 -*-
"""Phase 1 认证加固 + 入口/登录/注册关闭态 UI 验收测试。

覆盖（docs demo-access-auth-ui-design §3/§6/§7/§8.3/§10.13-14/§11 Phase 1/§12.2）：

1. 统一 CSRF（Cookie 会话写端点）：
   - 缺 token 的 POST/PUT 被拒（400）；带 token 通过；
   - token 绑定 session（跨 client 复制 token 无效）；
   - 同步 cookie 非 HttpOnly + SameSite=Lax；
   - /internal/* 与 /api/plugin/* 通道不受 CSRF 影响；
   - GET 安全（只下发 token）。
2. logout 改 POST + CSRF；GET 短期兼容（记 warning）；登录成功 session.clear()。
3. next 白名单：//host、协议 URL、\\\\host 均回 `/`。
4. 跨 worker 登录锁定：
   - json/dual 后端 POST /login 503 fail-closed（不退化内存计数）；
   - mock 两桶下锁定 → 429 + Retry-After + 页面倒计时；统一「账号或密码错误」；
   - 成功登录 clear 两桶；subject 只存带盐 hash（IP /24、IPv6 /64、账号 lower+strip）。
5. `/` 按认证状态分流：未登录入口页（不 302 /login）、已登录完整应用、
   AUTH_ENABLED=False 保持直接应用。
6. /register 关闭态（GET 状态页 / POST 一律 403）；registration_open 权威读 settings_store。
7. /demo 占位页（公开可达）。
8. 启动期 PUBLIC_DEMO_ENABLED 检查（json/dual → SystemExit）。
9. 中英文案守卫：登录页无 admin-only 措辞；AI 导航助手/平台 AI 配置/我的 AI 设置；
   分享 UI 显式权限选择；前端 CSRF 头 + POST logout。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="svs-p1-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["ADMIN_PASSWORD"] = ""

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

import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import platform_features  # noqa: E402
from pg_compat import json_only, BACKEND  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：临时目录夺回 + 关认证默认（认证用例自行开启）。"""
    data_dir = Path(DATA_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    share_store.set_owner_user_id("")
    for name in ("users.json", "users.json.bak", "shares.json", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    yield


def _raw_client(auth=True):
    """不自动带 CSRF 的裸 client（测 CSRF 拒绝语义用）。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _client(auth=True):
    """自动带 CSRF 的 client（正常流用）。"""
    return csrf_client(_raw_client(auth))


def _token_from(client):
    c = client.get_cookie("csrf_token", domain="localhost", path="/")
    return c.value if c is not None else None


def _setup_owner_and_user():
    owner = user_store.create_user("owner@x.com", "ownerpass1", role="owner")
    user = user_store.create_user("u@x.com", "userpass1", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, user


def _login_ok(client, username="owner@x.com", password="ownerpass1", **extra):
    return client.post("/login", data={
        "username": username, "password": password, **extra})


# =========================================================================== #
# 1. 统一 CSRF（Cookie 会话写端点）
# =========================================================================== #
def test_csrf_login_page_issues_token_cookie():
    app_mod.AUTH_ENABLED = True
    client = _raw_client()
    r = client.get("/login")
    assert r.status_code == 200
    # 表单隐藏域携带 token
    assert 'name="csrf_token"' in r.get_data(as_text=True)
    # 同步 cookie：非 HttpOnly + SameSite=Lax（前端 JS 可读）
    tok = _token_from(client)
    assert tok and len(tok) >= 32
    cookie_header = r.headers.getlist("Set-Cookie")
    csrf_cookie = [h for h in cookie_header if h.startswith("csrf_token=")]
    assert csrf_cookie, "未下发 csrf_token cookie"
    assert "HttpOnly" not in csrf_cookie[0]
    assert "SameSite=Lax" in csrf_cookie[0]


def test_csrf_missing_token_post_login_rejected():
    """缺 token 的 POST /login 被拒（400，可重试的 HTML 错误）。"""
    app_mod.AUTH_ENABLED = True
    user_store.create_user("o@x.com", "ownerpass1", role="owner")
    client = _raw_client()
    client.get("/login")  # 取得 session/cookie，但提交不带 token
    r = client.post("/login", data={"username": "o@x.com", "password": "ownerpass1"})
    assert r.status_code == 400
    # 未建立登录态
    with client.session_transaction() as s:
        assert not s.get("auth_user")


def test_csrf_login_with_token_passes(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _client()
    r = _login_ok(client)
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert s.get("auth_user")


def test_csrf_api_write_endpoints_enforced():
    """PUT /api/ai/config：无 session token → 400；注入 session 后带 token 通过。"""
    app_mod.AUTH_ENABLED = True
    owner, user = _setup_owner_and_user()
    # 无 token（甚至无 session）：先 401 认证（auth 先于 CSRF）
    raw = _raw_client()
    assert raw.put("/api/ai/config", json={"model": "m"}).status_code == 401
    # 有 session 无 token → 400 csrf_required
    authed = _raw_client()
    with authed.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    r = authed.put("/api/ai/config", json={"model": "m"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # 带 token → 通过 CSRF 层（业务层正常处理）
    client = _client()
    client.get("/login")  # 下发 token
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    tok = _token_from(client)
    assert tok
    r2 = client.put("/api/ai/config", json={"model": "m"},
                    headers={"X-CSRF-Token": tok})
    assert r2.status_code != 400, r2.get_data(as_text=True)


def test_csrf_admin_users_post_enforced():
    """POST /api/admin/users（Cookie 会话写端点）纳入 CSRF。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    # 先摘掉 wrapper 注入：直接用底层 client 发（有 session、无 token）
    r = client._base.post("/api/admin/users",
                          json={"email": "n@x.com", "password": "password1"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # wrapper 自动带 token → 通过 CSRF（业务 200/400 由参数决定）
    r2 = client.post("/api/admin/users",
                     json={"email": "n@x.com", "password": "password1"})
    assert r2.status_code == 200, r2.get_data(as_text=True)


def test_csrf_token_bound_to_session():
    """token 与 session 绑定：复制他人 token 到另一 session 无效。"""
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    a = _client()
    a.get("/login")
    stolen = _token_from(a)
    b = _raw_client()
    b.get("/login")  # b 有自己的 session/token
    r = b.post("/login", data={"username": "owner@x.com", "password": "ownerpass1"},
               headers={"X-CSRF-Token": stolen})
    assert r.status_code == 400
    with b.session_transaction() as s:
        assert not s.get("auth_user")


def test_csrf_exempt_internal_and_plugin_channels():
    """/internal/* 与 /api/plugin/* 不套 Cookie CSRF（各自非 Cookie 鉴权）。"""
    app_mod.AUTH_ENABLED = True
    client = _raw_client()
    # internal：无 CSRF 拦截（401 来自 internal token 鉴权，而非 csrf_required）
    r = client.post("/internal/ai/annotate", json={})
    assert r.status_code == 401
    assert (r.get_json() or {}).get("error") == "invalid_internal_token"
    # plugin v1 auth/token：无 session、无 CSRF token，靠自身 secret 校验（400/401）
    r2 = client.post("/api/plugin/v1/auth/token", json={"installation_id": "x"})
    assert r2.status_code in (400, 401, 404)
    if r2.get_json(silent=True):
        assert r2.get_json().get("error") != "csrf_required"


def test_csrf_get_methods_safe():
    """GET 只下发 token，不校验（安全方法）。"""
    client = _raw_client(auth=False)
    r = client.get("/api/auth/info")
    assert r.status_code == 200
    assert _token_from(client)


# =========================================================================== #
# 2. logout 改 POST + 登录清 session
# =========================================================================== #
def test_logout_post_with_csrf_clears_session(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client)
    r = client.post("/logout")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")
    with client.session_transaction() as s:
        assert not s.get("auth_user")


def test_logout_post_without_csrf_rejected():
    """已登录 session 下 POST /logout 缺 token → 400（未登录时先被 auth 302）。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _raw_client()
    client.get("/login")  # 取得 session token
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    r = client.post("/logout")
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # session 未被清除（登出没发生）
    with client.session_transaction() as s:
        assert s.get("auth_user") == "o"


def test_logout_get_short_term_compat_with_warning(caplog):
    """GET /logout 短期兼容仍可退出（已登录），但记录 warning（docs §10.14）。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _raw_client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    with caplog.at_level("WARNING"):
        r = client.get("/logout")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")
    assert any("GET /logout" in rec.getMessage() for rec in caplog.records)
    with client.session_transaction() as s:
        assert not s.get("auth_user")


def test_login_success_clears_old_session(monkeypatch):
    """登录成功前 session.clear()：预置的旧键不残留（防 fixation）。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    with client.session_transaction() as s:
        s["poison"] = "old-session-data"
        s["role"] = "user"  # 伪造的旧身份
    old_token = _token_from(client)
    r = _login_ok(client)
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert s.get("poison") is None
        assert s.get("role") == "owner"
        assert s.get("auth_user")
        # CSRF token 已轮换（身份切换）
        assert s.get("csrf_token") != old_token


# =========================================================================== #
# 3. next 白名单
# =========================================================================== #
@pytest.mark.parametrize("bad_next", [
    "//evil.com", "https://evil.com", "http://evil.com/x",
    "\\\\evil.com", "/\\evil.com", "javascript:alert(1)", "evil.com",
])
def test_login_next_rejects_external(monkeypatch, bad_next):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = _login_ok(client, next=bad_next)
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_login_next_allows_site_absolute(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = _login_ok(client, next="/api/slides")
    assert r.status_code == 302
    assert r.headers["Location"] == "/api/slides"


def test_safe_next_path_unit():
    f = app_mod._safe_next_path
    assert f("/ok/path") == "/ok/path"
    assert f("//host") == "/"
    assert f("/\\host") == "/"
    assert f("https://x") == "/"
    assert f("\\\\host") == "/"
    assert f("") == "/"
    assert f(None) == "/"


# =========================================================================== #
# 4. 跨 worker 登录锁定
# =========================================================================== #
@json_only  # json 后端生产行为：503 fail-closed（PG 走真实 store，见下方 pg 用例）
def test_json_backend_login_fails_closed_without_store():
    """json/dual 后端 POST /login 直接 503（不退化内存计数，docs §6.3）。"""
    app_mod.AUTH_ENABLED = True
    user_store.create_user("o@x.com", "ownerpass1", role="owner")
    client = _client()
    r = _login_ok(client, "o@x.com", "ownerpass1")
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert "PostgreSQL" in body
    with client.session_transaction() as s:
        assert not s.get("auth_user")


@json_only  # mock 两桶语义在 json 下验证；PG 下用真实 store 断言（见 pg 用例）
def test_login_lock_two_buckets_mock_429_with_retry_after(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    # 首次失败：401 + 统一文案（不泄露账号是否存在）
    r = _login_ok(client, "owner@x.com", "wrongpass")
    assert r.status_code == 401
    assert "账号或密码错误" in r.get_data(as_text=True)
    # IP 前缀桶（5 次/窗）打满 → 锁定
    for _ in range(4):
        _login_ok(client, "owner@x.com", "wrongpass")
    r2 = _login_ok(client, "owner@x.com", "ownerpass1")
    assert r2.status_code == 429
    assert int(r2.headers.get("Retry-After") or 0) > 0
    # 页面含服务端权威倒计时
    body = r2.get_data(as_text=True)
    assert "尝试过于频繁" in body
    assert 'data-retry-seconds=' in body


@json_only
def test_login_success_clears_failure_buckets(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client, "owner@x.com", "wrongpass")  # 1 次失败
    r = _login_ok(client, "owner@x.com", "ownerpass1")  # 成功清桶
    assert r.status_code == 302
    # 清桶后可继续正常登录失败计数（未锁）
    r2 = _login_ok(client, "owner@x.com", "wrongpass")
    assert r2.status_code == 401


def test_login_error_message_no_account_enumeration(monkeypatch):
    """不存在账号与错误密码文案一致（不泄露账号是否存在）。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r1 = _login_ok(client, "ghost@x.com", "whatever1")
    r2 = _login_ok(client, "owner@x.com", "wrongpass")
    assert r1.status_code == r2.status_code == 401
    assert "账号或密码错误" in r1.get_data(as_text=True)
    assert "账号或密码错误" in r2.get_data(as_text=True)


def test_ip_prefix_normalization_and_hashing():
    # IPv4 /24
    assert app_mod._ip_prefix("203.0.113.9") == "203.0.113.0"
    assert app_mod._ip_prefix("203.0.113.200") == "203.0.113.0"
    # IPv6 /64
    assert app_mod._ip_prefix("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::"
    # 解析失败：原样返回（哈希仍可计算）
    assert app_mod._ip_prefix("not-an-ip") == "not-an-ip"
    assert app_mod._ip_prefix("") == ""
    # 带盐哈希：不含明文 IP；同前缀同 hash、不同前缀不同 hash
    h1 = app_mod._ip_prefix_hash("203.0.113.9")
    h2 = app_mod._ip_prefix_hash("203.0.113.200")
    h3 = app_mod._ip_prefix_hash("203.0.114.1")
    assert h1 == h2 and h1 != h3
    assert "203.0.113" not in h1


def test_account_hash_normalized():
    """账号 hash：lower+strip 规范化（大小写/空白不产生新桶）。"""
    h1 = app_mod._auth_subject_hash("  Alice@X.COM ")
    h2 = app_mod._auth_subject_hash("alice@x.com")
    assert h1 == h2
    assert h1 != app_mod._auth_subject_hash("bob@x.com")
    assert "alice" not in h1  # 不含明文


# =========================================================================== #
# 5. `/` 按认证状态分流
# =========================================================================== #
def test_index_unauthenticated_renders_entry_page(monkeypatch):
    """AUTH_ENABLED=True 未登录：渲染入口页，不 302 /login（docs §3.1）。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "直接体验 Demo" in body
    assert 'href="/demo"' in body
    assert 'href="/login"' in body
    # 底部研究/教学声明（docs §3.2）
    assert "仅用于研究、教学和软件演示" in body
    # 不是完整应用
    assert 'id="viewer"' not in body


def test_index_authenticated_renders_full_app(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client)
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="viewer"' in body


def test_index_auth_disabled_keeps_current_behavior():
    """AUTH_ENABLED=False：直接渲染完整应用（不变成入口页，保本地开发与测试）。"""
    app_mod.AUTH_ENABLED = False
    client = _client(auth=False)
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="viewer"' in body
    assert "直接体验 Demo" not in body


def test_login_get_redirects_when_authenticated(monkeypatch):
    """已登录访问 /login：302 到安全 next 或 /（docs §3.1）。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client)
    r = client.get("/login")
    assert r.status_code == 302
    assert r.headers["Location"] == "/"
    # 外部 next 仍拒绝
    r2 = client.get("/login?next=//evil.com")
    assert r2.headers["Location"] == "/"


# =========================================================================== #
# 6. /register 关闭态
# =========================================================================== #
def test_register_get_closed_state_page():
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "当前采用邀请注册" in body
    assert 'href="/login"' in body
    assert 'href="/demo"' in body
    # 不是 404、没有可提交的注册表单
    assert "<form" not in body


def test_register_post_always_rejected_phase1():
    app_mod.AUTH_ENABLED = True
    client = _client()
    r = client.post("/register", json={
        "email": "n@x.com", "password": "password1"})
    assert r.status_code == 403
    assert "邀请注册" in (r.get_json() or {}).get("error", "")
    # PG 后端 registration_open=true 时也一律 403（第一阶段）
    r2 = client.post("/register", json={
        "email": "n@x.com", "password": "password1"},
        headers={"X-Registration-Open": "1"})
    assert r2.status_code == 403


def test_registration_mode_reads_settings_store(monkeypatch):
    """/api/admin/users 的 registration_mode 来自 settings_store（PG 权威）。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    # json 后端下打开前置条件闸（PG 运行时三条件真实满足）
    monkeypatch.setattr(app_mod, "_registration_precondition_failures",
                        lambda environ=None: [])
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "invite_only")
    body = client.get("/api/admin/users").get_json()
    assert body["registration_mode"] == "invite_only"
    # 旧 UI 兼容字段
    assert body["registration_open"] is True
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "closed")
    body2 = client.get("/api/admin/users").get_json()
    assert body2["registration_mode"] == "closed"
    assert body2["registration_open"] is False


def test_registration_mode_fail_closed_on_error(monkeypatch):
    def boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode", boom)
    assert app_mod._registration_mode_stored() == "closed"
    assert app_mod._effective_registration_mode() == "closed"


# =========================================================================== #
# 7. /demo 占位页
# =========================================================================== #
def test_demo_landing_placeholder_public():
    """/demo 公开可达（免登录），Phase 1 为占位页。"""
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Demo" in body
    assert 'href="/login"' in body
    assert "仅用于研究、教学和软件演示" in body


@json_only  # json 后端：占位页明确提示不满足 PG 前置条件
def test_demo_landing_json_backend_shows_pg_prerequisite():
    app_mod.AUTH_ENABLED = True
    client = _client()
    body = client.get("/demo").get_data(as_text=True)
    assert "PostgreSQL" in body


# =========================================================================== #
# 8. 启动期 PUBLIC_DEMO_ENABLED 检查（docs §4.3）
# =========================================================================== #
def test_public_demo_env_non_pg_refuses_to_start(monkeypatch):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "json")
    with pytest.raises(SystemExit):
        app_mod._check_public_demo_backend_or_exit(
            {"PUBLIC_DEMO_ENABLED": "1"})
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "dual")
    with pytest.raises(SystemExit):
        app_mod._check_public_demo_backend_or_exit(
            {"PUBLIC_DEMO_ENABLED": "true"})


def test_public_demo_env_pg_or_disabled_passes(monkeypatch):
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "postgres")
    # 不抛
    app_mod._check_public_demo_backend_or_exit({"PUBLIC_DEMO_ENABLED": "1"})
    # 未开启：任何后端都放行
    monkeypatch.setattr(platform_features, "STORAGE_BACKEND", "json")
    app_mod._check_public_demo_backend_or_exit({"PUBLIC_DEMO_ENABLED": "0"})
    app_mod._check_public_demo_backend_or_exit({})


# =========================================================================== #
# 9. 前端文案 / UI 守卫（§8.3 / §12.2）
# =========================================================================== #
def test_i18n_no_admin_only_wording_left():
    text = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for banned in ("管理员登录", "请输入管理员账号", "Admin Login",
                   "Enter admin credentials", "AI 读片助手（管理员）",
                   "AI reading assistant (admin)", "AI 服务配置",
                   "AI service config"):
        assert banned not in text, "i18n.js 仍含旧措辞：%r" % banned
    for required in ("登录 PathTogether", "Log in to PathTogether",
                     "登录后继续查看、测试 AI 和协作",
                     "AI 导航助手", "AI navigation assistant",
                     "平台 AI 配置", "AI 服务（平台统一提供）",
                     "Platform AI config", "AI service (platform-provided)",
                     "只能分享你拥有的切片", "允许标注", "允许下载"):
        assert required in text, "i18n.js 缺新文案：%r" % required


def test_login_template_phase1_requirements():
    text = (REPO_ROOT / "templates" / "login.html").read_text(encoding="utf-8")
    # 次入口：注册方式 + Demo；找回提示为纯文本（非链接，docs §6.1）
    assert 'href="/register"' in text and "login.register" in text
    assert 'href="/demo"' in text and "login.demo" in text
    assert '<p class="forgot" data-i18n="login.forgot">' in text
    assert 'class="forgot" href=' not in text
    # 密码显示/隐藏按钮带可访问名称
    assert "pwd-toggle" in text and "login.pwd.show.aria" in text
    # 提交中状态 + CSRF 隐藏域
    assert "login.submitting" in text
    assert 'name="csrf_token"' in text


def test_index_template_share_permissions_and_logout():
    text = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    shell = (REPO_ROOT / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    # 分享权限显式选择（docs §8.3）——写操作入口在共享外壳的正式版分支
    assert '{% include "_app_shell.html" %}' in text
    assert 'id="share-perm-view"' in shell
    assert 'id="share-perm-annotate"' in shell
    assert 'id="share-perm-download"' in shell
    assert 'id="share-perm-hint"' in shell
    # logout 不再是 GET 链接
    assert 'href="/logout"' not in text and 'href="/logout"' not in shell
    assert 'id="logout-btn"' in shell


def test_appjs_csrf_header_and_post_logout():
    text = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in text, "app.js 未附带 CSRF 头"
    assert '"/logout"' in text and '"POST"' in text, "app.js 未改 POST /logout"
    assert "resp.ok" in text
    assert "toast.logout.fail" in text
    assert "window.HP_AUTH" in text
    # 分享创建携带显式 permissions
    assert "getSharePermissions" in text
    assert "permissions: permissions" in text
    # 角色注入 AI 配置标题
    assert "setRole" in text


def test_share_create_with_view_only_permissions(monkeypatch):
    """端到端：显式仅查看权限的分享不默认带 annotate（UI 语义后端已支持）。"""
    app_mod.AUTH_ENABLED = True
    owner, user = _setup_owner_and_user()
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", Path(UPLOAD_DIR))
    slide = "p1.svs"
    (Path(UPLOAD_DIR) / slide).write_bytes(b"stub")
    share_store.set_slide_meta(slide, owner_user_id=owner["user_id"])
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner"})
    r = client.post("/api/share/create", json={
        "slides": [slide], "expires_hours": 1, "permissions": ["view"]})
    assert r.status_code == 200
    assert r.get_json()["permissions"] == ["view"]


# =========================================================================== #
# 10. PG 后端：跨 worker 登录锁定（真实 auth_rate_limits）
# =========================================================================== #
pg_only = pytest.mark.skipif(BACKEND != "postgres",
                             reason="跨 worker 登录锁定需 PG（RUN_PG_TESTS=1）")


@pg_only
class TestPgLoginLockout:
    def _mk_users(self, n):
        return [user_store.create_user("u%d@x.com" % i, "password1", role="user")
                for i in range(n)]

    def test_single_ip_many_accounts_locks_ip_prefix_bucket(self):
        """单 IP 撞多账号：IP 前缀桶（5）先达阈值被锁（§12.2）。"""
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(4)
        client = _client()
        for u in users:
            r = _login_ok(client, u["email"], "wrongpass")
            assert r.status_code == 401
        # 第 5 次失败（仍同 IP）触发锁定 → 本次响应即 429
        r = _login_ok(client, "ghost@x.com", "wrongpass")
        assert r.status_code == 429
        assert int(r.headers.get("Retry-After") or 0) > 0
        # 锁定期内正确密码也 429
        r2 = _login_ok(client, users[0]["email"], "password1")
        assert r2.status_code == 429

    def test_single_account_many_ips_locks_account_bucket(self):
        """多 IP 撞单账号：IP 桶每条 fresh，账号桶（10）累计到阈值被锁（§12.2）。"""
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(1)
        client = _client()
        target = users[0]["email"]
        # 每次失败来自不同 /24（IP 桶按前缀聚合，docs §9.5：IPv4 → /24）
        for i in range(9):
            r = client.post("/login", data={"username": target, "password": "wrongpass"},
                            environ_overrides={"REMOTE_ADDR": "198.51.%d.1" % (i + 1)})
            assert r.status_code == 401, r.status_code
        # 第 10 次失败：账号桶达阈值（10）→ 429
        r = client.post("/login", data={"username": target, "password": "wrongpass"},
                        environ_overrides={"REMOTE_ADDR": "198.51.100.100"})
        assert r.status_code == 429
        # 换全新 /24（IP 桶 fresh）也仍被账号桶锁住
        r2 = client.post("/login", data={"username": target, "password": "password1"},
                         environ_overrides={"REMOTE_ADDR": "203.0.113.77"})
        assert r2.status_code == 429


    def test_success_clears_lock_state(self):
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(1)
        client = _client()
        _login_ok(client, users[0]["email"], "wrongpass")
        r = _login_ok(client, users[0]["email"], "password1")
        assert r.status_code == 302
        # 清桶后失败不立即 429
        r2 = _login_ok(client, users[0]["email"], "wrongpass")
        assert r2.status_code == 401


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
