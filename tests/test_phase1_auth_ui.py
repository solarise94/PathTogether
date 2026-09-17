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
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：临时目录夺回 + 关认证默认（认证用例自行开启）。"""
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
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
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    user = user_store.create_user("u@x.com", "userpass1234567", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, user

def _login_ok(client, username="owner@x.com", password="ownerpass123456", **extra):
    return client.post("/login", data={
        "username": username, "password": password, **extra})

def _dialog_tag(body):
    """取页面里登录弹窗 <dialog id="login-dialog"> 的起始标签（无则 None）。"""
    m = re.search(r'<dialog\b[^>]*id="login-dialog"[^>]*>', body)
    return m.group(0) if m else None

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
    user_store.create_user("o@x.com", "ownerpass123456", role="owner")
    client = _raw_client()
    client.get("/login")  # 取得 session/cookie，但提交不带 token
    r = client.post("/login", data={"username": "o@x.com", "password": "ownerpass123456"})
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
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = authed.put("/api/ai/config", json={"model": "m"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # 带 token → 通过 CSRF 层（业务层正常处理）
    client = _client()
    client.get("/login")  # 下发 token
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    tok = _token_from(client)
    assert tok
    r2 = client.put("/api/ai/config", json={"model": "m"},
                    headers={"X-CSRF-Token": tok})
    assert r2.status_code != 400, r2.get_data(as_text=True)

def test_csrf_admin_users_post_enforced():
    """POST /api/admin/v1/users（Cookie 会话写端点）纳入 CSRF。

    旧 POST /api/admin/users 已 410 退役（review R2-F1），CSRF 探针换到
    v1 建号端点（同为 Cookie 会话写端点，闸层一致）。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    # 先摘掉 wrapper 注入：直接用底层 client 发（有 session、无 token）
    r = client._base.post("/api/admin/v1/users",
                          json={"login_id": "n@x.com", "password": "password1password1"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # wrapper 自动带 token → 通过 CSRF（业务 200/400 由参数决定）
    r2 = client.post("/api/admin/v1/users",
                     json={"login_id": "n@x.com", "password": "password1password1"})
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
    r = b.post("/login", data={"username": "owner@x.com", "password": "ownerpass123456"},
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
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.post("/logout")
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"
    # session 未被清除（登出没发生）
    with client.session_transaction() as s:
        assert s.get("auth_user") == "o"

def test_logout_get_rejected_405(caplog):
    """GET /logout 已随 R3 wave1 物理删除（CSRF 加固，docs §10.14）：405。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _raw_client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.get("/logout")
    assert r.status_code == 405
    # session 未被 GET 触碰（登出只能经 POST + CSRF）
    with client.session_transaction() as s:
        assert s.get("auth_user") == "o"

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
    r2 = _login_ok(client, "owner@x.com", "ownerpass123456")
    assert r2.status_code == 429
    assert int(r2.headers.get("Retry-After") or 0) > 0
    # 页面含服务端权威倒计时（弹窗内 span，entry-auth.js 据此禁用提交按钮）
    body = r2.get_data(as_text=True)
    assert "尝试过于频繁" in body
    assert 'data-retry-seconds=' in body
    assert 'id="login-dialog-countdown"' in body
    tag = _dialog_tag(body)
    assert tag and re.search(r"\bopen\b", tag)

def test_login_success_clears_failure_buckets(monkeypatch):
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client, "owner@x.com", "wrongpass")  # 1 次失败
    r = _login_ok(client, "owner@x.com", "ownerpass123456")  # 成功清桶
    assert r.status_code == 302
    # 清桶后可继续正常登录失败计数（未锁）
    r2 = _login_ok(client, "owner@x.com", "wrongpass")
    assert r2.status_code == 401

def test_login_error_message_no_account_enumeration(monkeypatch):
    """不存在账号与错误密码文案一致（不泄露账号是否存在）；错误页弹窗直开。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r1 = _login_ok(client, "ghost@x.com", "whatever1")
    r2 = _login_ok(client, "owner@x.com", "wrongpass")
    assert r1.status_code == r2.status_code == 401
    body1 = r1.get_data(as_text=True)
    body2 = r2.get_data(as_text=True)
    # 错误凭据返回的页面同样渲染 entry.html + 打开的登录弹窗 + 错误条
    for body in (body1, body2):
        tag = _dialog_tag(body)
        assert tag and re.search(r"\bopen\b", tag)
        assert "账号或密码错误" in body

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
    # 底部功能定位
    assert "协助研究者更快开展病理研究" in body
    # 不是完整应用
    assert 'id="viewer"' not in body

def test_index_entry_landing_page_content(monkeypatch):
    """未登录 / 的产品介绍页内容（histopilot-com-landing-page.md §5/§6）。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 新深色样式与结构锚点
    assert "entry.css?v=" in body
    assert 'id="product"' in body
    assert 'id="capabilities"' in body
    assert 'id="suite"' in body
    # 中文默认文案（测试锁定，勿改写）
    assert body.count('href="/login"') == 1
    assert 'href="#principle"' not in body
    assert "Demo 无需登录，可查看示例切片并体验 AI 导航" in body
    assert "不用于临床诊断" not in body
    assert "与 AI 一起，观察病理切片。" in body
    assert "可疑病理区域" in body and "计量分析" in body
    # 三个 GitHub 仓库链接（顶栏 / 套件卡 / 页脚）
    for repo in ("HistoPilot", "PathTogether", "HistoPilot-DSH"):
        assert 'https://github.com/solarise94/%s' % repo in body
    assert 'href="https://me.solarise94.fun"' in body
    assert 'href="mailto:solarise94@gmail.com"' in body
    # i18n 锚点（语言切换覆盖导航 / hero / mock / 卡片）
    for key in ("entry.nav.login", "entry.hero.title", "entry.mock.step1",
                "entry.cap.1.title", "entry.suite.github", "entry.principle.title"):
        assert 'data-i18n="%s"' % key in body
    # 不加载完整应用资源（介绍页只做营销与分流）
    assert 'id="viewer"' not in body
    assert "app.js" not in body
    assert "openseadragon" not in body
    assert "style.css" not in body
    # 空 favicon（data URI，避免未登录撞 /favicon.ico 鉴权）
    assert 'href="data:,"' in body
    # 不编造尚不存在的条款/隐私路由
    assert 'href="/terms"' not in body
    assert 'href="/privacy"' not in body
    # 无外部字体 / CDN；真实 TCGA 图从本站静态资源读取
    assert "fonts.googleapis" not in body
    assert 'src="http' not in body
    js_src = (REPO_ROOT / "static" / "entry.js").read_text(encoding="utf-8")
    assert "HP_EntryTissue" in js_src
    assert 'id="tissue"' in body and 'id="hero-tissue"' in body
    assert "entry-media/" not in body
    assert 'id="note-body"' in body
    assert 'id="review-a"' in body and 'id="review-b"' in body
    # 无内联脚本（CSP script-src 'self'）；标题由 i18n.js 按 data-page=entry 同步
    assert body.count("<script") == 3
    assert 'src="/static/i18n.js' in body
    assert 'src="/static/entry.js' in body
    assert 'src="/static/entry-auth.js' in body
    assert 'data-page="entry"' in body
    assert 'id="principle"' in body
    assert "受控 Demo" not in body
    assert "测量与计量分析" in body
    # 登录弹窗内嵌在主页且默认关闭（GET / 不直出 open 属性，登录链接才打开）
    tag = _dialog_tag(body)
    assert tag, "介绍页未渲染登录弹窗"
    assert not re.search(r"\bopen\b", tag), "介绍页登录弹窗不应默认打开"
    assert "Content-Security-Policy" in r.headers
    assert "unsafe-inline" not in r.headers.get("Content-Security-Policy", "")
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert r.headers.get("X-Frame-Options") == "DENY"

def test_entry_landing_source_guards():
    """介绍页源码守卫：深色主题、减少动画、语义结构与 i18n 键。"""
    html = (REPO_ROOT / "templates" / "entry.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "entry.css").read_text(encoding="utf-8")
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    # 深色 ZCode 风格 + 尊重 prefers-reduced-motion
    assert "#161616" in css
    assert "prefers-reduced-motion" in css
    assert "position: sticky" in css
    js = (REPO_ROOT / "static" / "entry.js").read_text(encoding="utf-8")
    assert "data-hp-stage" in js
    assert "prefers-reduced-motion" in js
    assert "IntersectionObserver" in js
    assert "HP_EntryTissue" in js
    assert "viewBox" in js and "streaming" in js
    assert 'id="review-a"' in html and 'id="review-b"' in html
    assert 'id="note-body"' in html and 'id="toggle"' in html
    assert "#007aff" in css.lower()
    # 语义结构：header + main + footer；锚点导航与跳转链接
    assert "<header" in html and "<main" in html and "<footer" in html
    assert "<nav" in html
    assert 'href="#top"' in html
    assert 'class="skip-link"' in html
    # 语言切换沿用 .lang-toggle
    assert 'class="lang-toggle"' in html
    assert html.count("<script") == 3
    assert 'src="/static/i18n.js' in html
    assert 'src="/static/entry.js' in html
    assert 'src="/static/entry-auth.js' in html
    assert 'data-page="entry"' in html
    assert 'id="principle"' in html
    # 登录弹窗：entry.html include _login_dialog.html（login.html 已删除）
    assert '{% include "_login_dialog.html" %}' in html
    assert ".login-dialog::backdrop" in css
    auth_js = (REPO_ROOT / "static" / "entry-auth.js").read_text(encoding="utf-8")
    assert "showModal" in auth_js
    assert "login-dialog-countdown" in auth_js
    assert "受控 Demo" not in html
    # i18n 新键 zh/en 双语成对存在（histopilot-com-landing-page.md §4）
    new_keys = (
        "entry.skip", "lang.toggle.aria", "app.doc.title.entry",
        "entry.nav.product", "entry.nav.principle", "entry.nav.capabilities",
        "entry.nav.suite",
        "entry.nav.github", "entry.nav.home", "entry.nav.email",
        "entry.nav.login", "entry.nav.workbench", "entry.nav.logout",
        "entry.workbench", "entry.signed.hint",
        "entry.cta.title.signed", "entry.cta.body.signed",
        "entry.badge",
        "entry.hero.title", "entry.hero.lead",
        "entry.mock.title", "entry.mock.step1", "entry.mock.step2",
        "entry.mock.step3", "entry.mock.step4",
        "entry.how.kicker", "entry.how.title",
        "entry.how.s1.title", "entry.how.s1.body",
        "entry.how.s2.title", "entry.how.s2.body",
        "entry.how.s3.title", "entry.how.s3.body",
        "entry.cap.kicker", "entry.cap.title",
        "entry.cap.1.title", "entry.cap.1.body",
        "entry.cap.2.title", "entry.cap.2.body",
        "entry.cap.3.title", "entry.cap.3.body",
        "entry.principle.title", "entry.principle.review.a", "entry.principle.review.b",
        "entry.principle.pin.a", "entry.principle.pin.b",
        "entry.principle.loop.hint", "entry.principle.nav.s5",
        "entry.principle.status.nav.7",
        "entry.suite.kicker", "entry.suite.title",
        "entry.suite.hp.body", "entry.suite.pt.body", "entry.suite.dsh.body",
        "entry.suite.github", "entry.cta.title", "entry.cta.body",
        "entry.footer.copy",
    )
    zh_block = i18n[i18n.index("zh: {"):i18n.index("en: {")]
    en_block = i18n[i18n.index("en: {"):]
    for key in new_keys:
        assert '"%s"' % key in zh_block, "i18n.js zh 缺新键：%r" % key
        assert '"%s"' % key in en_block, "i18n.js en 缺新键：%r" % key
    # 现有 entry.* 中文默认不回退（其他页面共用）
    for text in ("直接体验 Demo", "登录测试与协作",
                 "Demo 无需登录，可查看示例切片并体验 AI 导航",
                 "与 AI 一起，观察病理切片。", "计量分析"):
        assert text in zh_block

def test_index_authenticated_stays_on_landing(monkeypatch):
    """已登录访问 / 仍是介绍主页：头像 + 进入工作台，不进 Viewer。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    _login_ok(client)
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="viewer"' not in body
    assert "进入工作台" in body
    assert body.count('href="/app"') == 1
    assert '<span class="avatar"' in body
    assert 'class="avatar"' in body
    assert "登录测试与协作" not in body


def test_workbench_requires_login_and_renders_app(monkeypatch):
    """/app 未登录 302 /login?next=/app；已登录渲染完整工作台。"""
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    anon = _client()
    r = anon.get("/app")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    assert "next=/app" in r.headers["Location"]
    client = _client()
    _login_ok(client)
    r2 = client.get("/app")
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
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

def test_login_get_renders_entry_with_open_dialog(monkeypatch):
    """GET /login 复用介绍页模板：entry.html + 直出已打开的登录弹窗。

    login.html 已删除（登录页并入主页弹窗）：返回 200、含 id="login-dialog"
    且弹窗带 open 属性（login_open=True，无 JS 时也可见）。
    """
    install_json_login_limits(monkeypatch)
    app_mod.AUTH_ENABLED = True
    _setup_owner_and_user()
    client = _client()
    r = client.get("/login")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    tag = _dialog_tag(body)
    assert tag, "GET /login 未渲染登录弹窗"
    assert re.search(r"\bopen\b", tag), "login_open=True 时弹窗应带 open 属性"
    # 弹窗表单（CSRF + next）随 entry.html 一起下发
    assert 'id="login-dialog-form"' in body
    assert 'name="csrf_token"' in body
    assert 'name="next"' in body
    # 本人改密成功后跳 /login?password_changed=1：弹窗内提示（docs §7.1-7）
    r2 = client.get("/login?password_changed=1")
    assert r2.status_code == 200
    assert "密码已修改，请使用新密码重新登录" in r2.get_data(as_text=True)

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
        "login_id": "n@x.com", "password": "password1password1"})
    assert r.status_code == 403
    assert "邀请注册" in (r.get_json() or {}).get("error", "")
    # PG 后端 registration_open=true 时也一律 403（第一阶段）
    r2 = client.post("/register", json={
        "login_id": "n@x.com", "password": "password1password1"},
        headers={"X-Registration-Open": "1"})
    assert r2.status_code == 403

def test_registration_mode_reads_settings_store(monkeypatch):
    """v1 settings 聚合 registration 段的 mode 来自 settings_store（PG 权威）。"""
    app_mod.AUTH_ENABLED = True
    owner, _u = _setup_owner_and_user()
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    # json 后端下打开前置条件闸（PG 运行时三条件真实满足）
    monkeypatch.setattr(app_mod, "_registration_precondition_failures",
                        lambda *a, **k: [])
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "invite_only")
    body = client.get("/api/admin/v1/settings").get_json()["registration"]
    assert body["mode"] == "invite_only"
    assert body["registration_open"] is True
    monkeypatch.setattr(app_mod.settings_store, "get_registration_mode",
                        lambda: "closed")
    body2 = client.get("/api/admin/v1/settings").get_json()["registration"]
    assert body2["mode"] == "closed"
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
    for required in ("登录 HistoPilot", "Log in to HistoPilot",
                     "登录后继续查看、测试 AI 和协作",
                     "AI 导航助手", "AI navigation assistant",
                     "平台 AI 配置", "AI 服务（平台统一提供）",
                     "Platform AI config", "AI service (platform-provided)",
                     "只能分享你拥有的切片", "允许标注", "允许下载"):
        assert required in text, "i18n.js 缺新文案：%r" % required

def test_login_dialog_template_phase1_requirements():
    """登录并入主页弹窗（_login_dialog.html）后的 Phase 1 语义守卫。

    login.html 已删除：GET/POST /login 由 app._login_page 渲染 entry.html +
    login_open=True，登录表单完全来自 _login_dialog.html（登录行为测试见上）。
    """
    assert not (REPO_ROOT / "templates" / "login.html").exists()
    entry_html = (REPO_ROOT / "templates" / "entry.html").read_text(encoding="utf-8")
    assert '{% include "_login_dialog.html" %}' in entry_html
    text = (REPO_ROOT / "templates" / "_login_dialog.html").read_text(encoding="utf-8")
    # 次入口：注册方式 + Demo；找回提示为纯文本（非链接，docs §6.1）
    assert 'href="/register"' in text and "login.register" in text
    assert 'href="/demo"' in text and "login.demo" in text
    assert '<p class="login-dialog-forgot" data-i18n="login.forgot">' in text
    assert 'class="forgot" href=' not in text
    # 弹窗表单：提交按钮 + CSRF 隐藏域 + next 透传
    assert 'type="submit"' in text
    assert 'name="csrf_token"' in text
    assert 'name="next"' in text
    # 服务端锁定倒计时挂点（entry-auth.js 读取 data-retry-seconds 禁用提交）
    assert 'id="login-dialog-countdown"' in text
    assert 'data-retry-seconds' in text

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
    """源码子串断言（脆弱，test-review P3-17 收敛说明）：

    「X-CSRF-Token 出现在 app.js 全文」已删——那是虚假信心断言：上传 CSRF 的
    **行为**测试在 tests/js/upload-csrf.test.ts（stub fetch 断言头真实附带）与
    tests/test_upload_csrf.py（后端 header-only 契约）。此处仅保留无行为测试
    覆盖的零散锚点，改动 app.js 时允许同步更新。
    """
    text = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
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
        s.update({"auth_user": "o", "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})
    r = client.post("/api/share/create", json={
        "slides": [slide], "expires_hours": 1, "permissions": ["view"]})
    assert r.status_code == 200
    assert r.get_json()["permissions"] == ["view"]

# =========================================================================== #
# 10. PG 后端：跨 worker 登录锁定（真实 auth_rate_limits）
# =========================================================================== #

class TestPgLoginLockout:
    def _mk_users(self, n):
        return [user_store.create_user("u%d@x.com" % i, "password1password1", role="user")
                for i in range(n)]

    def test_single_ip_many_accounts_locks_ip_prefix_bucket(self):
        """单 IP 撞多账号：IP 前缀桶（5）先达阈值被锁（§12.2）。"""
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(4)
        client = _client()
        for u in users:
            r = _login_ok(client, u["login_id"], "wrongpass")
            assert r.status_code == 401
        # 第 5 次失败（仍同 IP）触发锁定 → 本次响应即 429
        r = _login_ok(client, "ghost@x.com", "wrongpass")
        assert r.status_code == 429
        assert int(r.headers.get("Retry-After") or 0) > 0
        # 锁定期内正确密码也 429
        r2 = _login_ok(client, users[0]["login_id"], "password1password1")
        assert r2.status_code == 429

    def test_single_account_many_ips_locks_account_bucket(self):
        """多 IP 撞单账号：IP 桶每条 fresh，账号桶（10）累计到阈值被锁（§12.2）。"""
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(1)
        client = _client()
        target = users[0]["login_id"]
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
        r2 = client.post("/login", data={"username": target, "password": "password1password1"},
                         environ_overrides={"REMOTE_ADDR": "203.0.113.77"})
        assert r2.status_code == 429

    def test_success_clears_lock_state(self):
        app_mod.AUTH_ENABLED = True
        users = self._mk_users(1)
        client = _client()
        _login_ok(client, users[0]["login_id"], "wrongpass")
        r = _login_ok(client, users[0]["login_id"], "password1password1")
        assert r.status_code == 302
        # 清桶后失败不立即 429
        r2 = _login_ok(client, users[0]["login_id"], "wrongpass")
        assert r2.status_code == 401

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
