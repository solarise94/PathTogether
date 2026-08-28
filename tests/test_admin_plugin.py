# -*- coding: utf-8 -*-
"""PR3（前半）：admin.workspace 宿主 + AdminBridge 框架 + admin 插件骨架测试。

docs/admin-billing-plugin-implementation-plan.md §8 / §14.1（权限/插件行）：
  - ``GET /admin``：匿名 302 /login?next=/admin；user 403；preview subject 403
    （actor 判定不接受 preview effective subject，且预览态直接拒绝）；
    真实 owner 200 + no-store + 严格 CSP；admin 插件不可信（缺失/禁用/未 pin/
    hash 不符/策略文件缺失）→ 平台降级页，不影响 ``/`` Viewer；
  - 信任判定 ``_admin_plugin_trusted`` **永远 fail-closed**：白名单 ① 显式
    sha256 pin ② hash 精确匹配 ③ installation enabled ④ 缺一不可；
  - ``GET /admin/plugin-assets/<id>/<path>``：匿名 401 / 非 owner 403 / owner+
    受信 200；扩展名白名单（.svg/source map 拒绝）；路径穿越/绝对路径/
    symlink 逃逸拒绝；固定 MIME + nosniff + no-store；HTML 加严格 CSP；
  - manifest 校验器：sample 插件（v1.0）仍通过；adminPermissions 枚举外值/
    非法类型拒绝；v1.1.0 admin manifest 通过。

运行：cd 项目根 && python3 -m pytest tests/test_admin_plugin.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_plugin.py -q）
"""
import hashlib
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
import share_store  # noqa: E402
import user_store  # noqa: E402
from plugins.sdk import manifest as M  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_PLUGIN_DIR = REPO_ROOT / "plugins" / "pathtogether-admin"
ADMIN_MANIFEST = ADMIN_PLUGIN_DIR / "manifest.json"
SAMPLE_MANIFEST = REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json"

#: repo 内置 source-policy.json 对 pathtogether-admin 的 pin（信任链锚点）
_REPO_POLICY = json.loads(
    (REPO_ROOT / "plugins" / "source-policy.json").read_text(encoding="utf-8"))
ADMIN_PIN = _REPO_POLICY["pathtogether-admin"]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True + source-policy 缓存复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    # PLUGIN_BUNDLES_DIR 显式指到空目录：仓库 plugins/ 目录（PLUGINS_DIR）仍是
    # pathtogether-admin 的发现来源，且不泄漏本机部署的 bundle
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "no-bundles")
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


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


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _install_admin_plugin(enabled=True):
    """创建 pathtogether-admin 安装行（信任判定的第 ④ 步）。"""
    created = share_store.create_plugin_installation(
        "pathtogether-admin", version="0.1.0")
    installation_id = created["installation_id"]
    if not enabled:
        share_store.set_installation_enabled(installation_id, False)
    return installation_id


def _set_policy(monkeypatch, tmp_path, policy):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(p))
    app_mod._plugin_source_policy.cache_clear()
    return p


# --------------------------------------------------------------------------- #
# 1. GET /admin 权限矩阵（§8.1 / §14.1）
# --------------------------------------------------------------------------- #
def test_anonymous_admin_redirects_to_login():
    _setup_users()
    r = _client().get("/admin")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/admin"


def test_user_gets_403_without_admin_content():
    _owner, usera = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), usera).get("/admin")
    assert r.status_code == 403
    body = r.get_data(as_text=True)
    assert "admin-plugin-frame" not in body          # 不泄露宿主页结构
    assert "/admin/plugin-assets/" not in body       # 不泄露资源路由
    assert r.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in r.headers


def test_preview_subject_gets_403():
    """owner 预览成 user 时 /admin 403（§14.1：preview subject 不能访问 admin）。"""
    owner, usera = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": usera["user_id"]}).status_code == 200
    r = oc.get("/admin")
    assert r.status_code == 403
    assert "admin-plugin-frame" not in r.get_data(as_text=True)
    # actor 判定不被预览替换（actor_identity 永远是真实管理员；详见
    # test_admin_preview.py 的函数级断言）
    with app_mod.app.test_request_context("/api/slides"):
        from flask import session as flask_session
        flask_session["role"] = "owner"
        flask_session["user_id"] = owner["user_id"]
        assert app_mod.actor_identity()["role"] == "owner"


def test_owner_gets_host_page_with_strict_headers():
    owner, _u = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), owner).get("/admin")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-src 'self'" in csp
    body = r.get_data(as_text=True)
    assert 'src="/admin/plugin-assets/pathtogether-admin/ui/index.html"' in body
    assert 'sandbox="allow-scripts"' in body
    assert "referrerpolicy" in body
    # adminPermissions 注入（宿主权限门查表数据）
    assert "admin:overview:read" in body


def test_auth_disabled_owner_normalized_renders_host():
    """AUTH_ENABLED=False（内网归一 owner）不变量：/admin 照常渲染宿主页。"""
    app_mod.AUTH_ENABLED = False
    _setup_users()
    _install_admin_plugin()
    r = _client().get("/admin")
    assert r.status_code == 200
    assert "admin-plugin-frame" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 2. 信任判定 fail-closed（§8.2）
# --------------------------------------------------------------------------- #
def test_trusted_when_all_conditions_hold():
    _setup_users()
    _install_admin_plugin()
    trusted, reason = app_mod._admin_plugin_trusted("pathtogether-admin")
    assert (trusted, reason) == (True, "ok")


def test_untrusted_plugin_id_not_in_whitelist():
    assert app_mod._admin_plugin_trusted("sample-annotator") == \
        (False, "plugin not privileged")
    assert app_mod._admin_plugin_trusted("histopilot") == \
        (False, "plugin not privileged")


def test_untrusted_when_policy_file_missing(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(tmp_path / "nonexistent.json"))
    app_mod._plugin_source_policy.cache_clear()
    # viewer 插件同策略下是 dev 模式全放行（fail-open 兼容不变）……
    assert app_mod.plugin_source_allowed("sample-annotator")[0] is True
    # ……但 admin 永远 fail-closed
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "source policy not configured")


def test_untrusted_when_not_pinned(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    # 策略文件存在但没有 pathtogether-admin 条目（未 pin）
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin plugin not pinned")
    # null 显式放行（viewer 语义）对 admin 同样不可信
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": None})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin plugin not pinned")


def test_untrusted_on_hash_mismatch(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    assert bad != ADMIN_PIN
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "manifest hash mismatch")


def test_untrusted_when_installation_missing_or_disabled():
    _setup_users()
    # 无安装行
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "installation missing or disabled")
    # 安装后被禁用
    _install_admin_plugin(enabled=False)
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "installation missing or disabled")


def test_untrusted_when_plugin_directory_missing(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    # 先钉住 repo 策略文件（PLUGINS_DIR 移走后默认路径失效），再移走插件目录
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(REPO_ROOT / "plugins" / "source-policy.json"))
    app_mod._plugin_source_policy.cache_clear()
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "empty1")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", tmp_path / "empty2")
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "plugin directory missing")


def test_untrusted_on_invalid_manifest(monkeypatch, tmp_path):
    """manifest 结构破坏（校验不过 / 缺 admin.workspace slot）→ 不可信。"""
    _setup_users()
    _install_admin_plugin()
    # 复制 bundle 到临时根并破坏 manifest，pin 指向破坏后的文件
    root = tmp_path / "bundles"
    plugin = root / "pathtogether-admin"
    (plugin / "ui").mkdir(parents=True)
    for name in ("index.html", "main.js", "style.css"):
        (plugin / "ui" / name).write_bytes(
            (ADMIN_PLUGIN_DIR / "ui" / name).read_bytes())
    broken = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    broken["ui"]["slots"] = ["viewer.right-panel"]  # 丢掉 admin.workspace
    (plugin / "manifest.json").write_text(
        json.dumps(broken), encoding="utf-8")
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin.workspace slot missing")

    broken["adminPermissions"] = "admin:overview:read"  # 非法类型
    (plugin / "manifest.json").write_text(
        json.dumps(broken), encoding="utf-8")
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "manifest invalid")


# --------------------------------------------------------------------------- #
# 3. 降级页（不影响 / Viewer）
# --------------------------------------------------------------------------- #
def _assert_degraded(resp, reason):
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "管理插件当前不可用" in body
    assert reason in body
    assert "admin-plugin-frame" not in body
    assert resp.headers["Cache-Control"] == "no-store"


def test_admin_degrades_when_installation_missing():
    owner, _u = _setup_users()
    r = _login(_client(), owner).get("/admin")
    _assert_degraded(r, "installation missing or disabled")


def test_admin_degrades_when_disabled_or_unpinned_or_hash_mismatch(
        monkeypatch, tmp_path):
    owner, _u = _setup_users()
    oc = _login(_client(), owner)

    _install_admin_plugin(enabled=False)
    _assert_degraded(oc.get("/admin"), "installation missing or disabled")

    _install_admin_plugin()
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    _assert_degraded(oc.get("/admin"), "admin plugin not pinned")

    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    _assert_degraded(oc.get("/admin"), "manifest hash mismatch")


def test_viewer_unaffected_by_admin_degradation():
    """admin 不可用时 / Viewer 与公开插件路由照常。"""
    owner, _u = _setup_users()
    oc = _login(_client(), owner)
    assert oc.get("/admin").status_code == 200  # 降级页
    r = oc.get("/")
    assert r.status_code == 200
    assert "admin-plugin-frame" not in r.get_data(as_text=True)
    # 旧公开插件路由语义不变
    assert oc.get("/plugins/sample-annotator/ui/main.js").status_code == 200


def test_public_plugin_route_hides_admin_bundle():
    """公开 /plugins/<id>/ui/* 绝不服务 admin bundle（§8.3，PR3 fix）。

    匿名与登录 owner 均 404（不是 403——不向匿名访问者暴露 bundle 存在性）；
    sample 插件（普通 viewer 插件）不受影响。
    """
    owner, _u = _setup_users()
    _install_admin_plugin()
    c = _client()  # 匿名（公开路由本就免登录）
    for path in ("/plugins/pathtogether-admin/ui/index.html",
                 "/plugins/pathtogether-admin/ui/main.js",
                 "/plugins/pathtogether-admin/ui/style.css",
                 "/plugins/pathtogether-admin/ui/missing.js"):
        assert c.get(path).status_code == 404, path
        body = c.get(path).get_data(as_text=True)
        assert "pathtogether-admin" not in body.lower() or "404" in body
    oc = _login(_client(), owner)
    for path in ("/plugins/pathtogether-admin/ui/index.html",
                 "/plugins/pathtogether-admin/ui/main.js"):
        assert oc.get(path).status_code == 404, path  # owner 走 owner-only 路由
    # 普通插件公开路由不受影响
    assert c.get("/plugins/sample-annotator/ui/main.js").status_code == 200
    assert c.get("/plugins/sample-annotator/ui/index.html").status_code == 200


# --------------------------------------------------------------------------- #
# 4. /admin/plugin-assets/<id>/<path>（§8.3）
# --------------------------------------------------------------------------- #
ASSET_BASE = "/admin/plugin-assets/pathtogether-admin"


def test_asset_anonymous_gets_401():
    _setup_users()
    r = _client().get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 401
    assert r.get_json()["error"] == "auth_required"


def test_asset_non_owner_gets_403():
    _owner, usera = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), usera).get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 403


def test_asset_owner_trusted_serves_files_with_hard_headers():
    owner, _u = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)

    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/html")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    # HTML CSP：**显式 origin**而非 'self'——sandbox（无 allow-same-origin）使
    # iframe 文档 origin 为 opaque，CSP 'self' 在真实浏览器会把同源 .js/.css
    # 一并拒绝（见 app._admin_asset_html_csp 注释）。测试 client 的 host 是
    # http://localhost。
    assert r.headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src http://localhost; "
        "style-src http://localhost; img-src http://localhost; "
        "frame-ancestors 'self'")

    r = oc.get(ASSET_BASE + "/ui/main.js")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/javascript")
    assert "Content-Security-Policy" not in r.headers  # CSP 只加在 HTML 上
    assert r.headers["X-Content-Type-Options"] == "nosniff"

    r = oc.get(ASSET_BASE + "/ui/style.css")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/css")

    assert oc.get(ASSET_BASE + "/missing.js").status_code == 404


def test_asset_rejected_when_plugin_untrusted():
    """owner 也不允许拉取不可信 admin 插件的资源（无安装行 → 403）。"""
    owner, _u = _setup_users()
    r = _login(_client(), owner).get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 403
    assert r.get_json()["reason"] == "installation missing or disabled"


def test_asset_unknown_plugin_rejected():
    owner, _u = _setup_users()
    oc = _login(_client(), owner)
    assert oc.get("/admin/plugin-assets/sample-annotator/ui/main.js").status_code == 403
    assert oc.get("/admin/plugin-assets/../../etc/passwd").status_code in (403, 404)


def test_asset_traversal_and_absolute_paths_rejected():
    owner, _u = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)
    probes = [
        ASSET_BASE + "/ui/../manifest.json",           # .. 穿越
        ASSET_BASE + "/ui/../../source-policy.json",   # .. 出插件根
        ASSET_BASE + "/ui/%2e%2e/manifest.json",       # 编码 ..
        ASSET_BASE + "/%2fetc%2fpasswd",               # 绝对路径（编码 /）
        ASSET_BASE + "/ui/..%2f..%2fapp.py",           # 混合编码穿越
    ]
    for url in probes:
        # follow_redirects：werkzeug 对 %2f 形态可能先回 308 合并斜杠重定向，
        # 追到底后必须仍是 403/404（绝不能 200 吐出插件根外内容）
        r = oc.get(url, follow_redirects=True)
        assert r.status_code in (403, 404), "%s -> %s" % (url, r.status_code)
        assert b"top-secret" not in r.data
        assert b"SECRET_KEY" not in r.data


def test_asset_symlink_escape_and_bad_extensions_rejected(tmp_path, monkeypatch):
    """symlink 逃逸出插件根 → 403；.svg / source map → 403。"""
    owner, _u = _setup_users()
    _install_admin_plugin()

    # 独立 bundle 根：复制 admin bundle + 植入恶意文件/symlink，pin 指向其 manifest
    root = tmp_path / "bundles"
    plugin = root / "pathtogether-admin"
    (plugin / "ui").mkdir(parents=True)
    for name in ("index.html", "main.js", "style.css"):
        (plugin / "ui" / name).write_bytes(
            (ADMIN_PLUGIN_DIR / "ui" / name).read_bytes())
    (plugin / "manifest.json").write_bytes(ADMIN_MANIFEST.read_bytes())
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    os.symlink(secret, plugin / "ui" / "evil.js")          # symlink 逃逸
    (plugin / "ui" / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (plugin / "ui" / "main.js.map").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})

    oc = _login(_client(), owner)
    # 正常文件仍可服务（该 bundle 受信）
    assert oc.get(ASSET_BASE + "/ui/index.html").status_code == 200
    # symlink 逃逸 / .svg / source map 一律拒绝
    assert oc.get(ASSET_BASE + "/ui/evil.js").status_code == 403
    assert oc.get(ASSET_BASE + "/ui/icon.svg").status_code == 403
    assert oc.get(ASSET_BASE + "/ui/main.js.map").status_code == 403
    # 拒绝响应同样带 nosniff
    r = oc.get(ASSET_BASE + "/ui/icon.svg")
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# --------------------------------------------------------------------------- #
# 5. 安装引导（幂等 bootstrap，PR3 fix）
# --------------------------------------------------------------------------- #
def _admin_installation_rows():
    return [i for i in share_store.list_plugin_installations()
            if i.get("plugin_id") == "pathtogether-admin"]


def test_bootstrap_creates_enabled_installation_idempotently():
    """bundle + pin 正确：引导创建一条 enabled 安装行，重复调用不重复建行。"""
    _setup_users()
    assert _admin_installation_rows() == []  # 每用例隔离后的空存储
    first = app_mod._bootstrap_admin_plugin_installation()
    assert first is not None
    assert first.get("plugin_id") == "pathtogether-admin"
    assert first.get("enabled") is True
    assert "secret" not in first  # admin 插件无凭证（不走插件 v1 JWT 通道）
    rows = _admin_installation_rows()
    assert len(rows) == 1
    # 幂等：再次引导不新建
    second = app_mod._bootstrap_admin_plugin_installation()
    assert second is not None
    assert second["installation_id"] == rows[0]["installation_id"]
    assert len(_admin_installation_rows()) == 1
    # 引导后 /admin 直接可用（信任判定四项全满足）
    owner, _u = user_store.create_user("o2@x.com", "ownerpass123456", role="owner"), None
    r = _login(_client(), owner).get("/admin")
    assert r.status_code == 200
    assert "admin-plugin-frame" in r.get_data(as_text=True)


def test_bootstrap_skips_when_hash_or_pin_invalid(monkeypatch, tmp_path):
    """hash 被篡改 / 未 pin / 策略缺失 → 不创建任何安装行（fail-closed）。"""
    _setup_users()
    # pin 与磁盘 manifest 不符
    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []
    # 未 pin（策略文件存在但无该条目）
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []
    # 策略文件缺失（dev 模式空表）
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(tmp_path / "nonexistent.json"))
    app_mod._plugin_source_policy.cache_clear()
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []


def test_bootstrap_never_touches_existing_row():
    """已有行（如被 owner 禁用）不被引导自动启用；hash 失配时也不自动禁用。"""
    _setup_users()
    # 已有禁用行 + pin 正确：原样返回，不自动启用
    disabled_id = _install_admin_plugin(enabled=False)
    row = app_mod._bootstrap_admin_plugin_installation()
    assert row is not None
    assert row["installation_id"] == disabled_id
    assert share_store.get_plugin_installation(disabled_id)["enabled"] is False
    assert len(_admin_installation_rows()) == 1  # 也不重复建行


def test_bootstrap_skips_when_bundle_missing(monkeypatch, tmp_path):
    _setup_users()
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(REPO_ROOT / "plugins" / "source-policy.json"))
    app_mod._plugin_source_policy.cache_clear()
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "empty1")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", tmp_path / "empty2")
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []


# --------------------------------------------------------------------------- #
# 6. manifest 校验器回归（Manifest v1.1）
# --------------------------------------------------------------------------- #
def test_sample_manifest_still_validates_v1_0():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []          # 旧 manifest 零迁移
    assert "adminPermissions" not in data


def test_admin_manifest_v1_1_validates():
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []
    assert M.check_manifest_schema_supported(data["manifestSchemaVersion"]) is True
    assert sorted(data["adminPermissions"]) == sorted(M.MANIFEST_ADMIN_PERMISSIONS)


def test_admin_permissions_enum_and_type_enforced():
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(data))
    bad["adminPermissions"] = ["admin:overview:read", "admin:users:delete"]
    errs = M.validate_manifest(bad)
    assert errs and any("adminPermissions[1]" in e for e in errs)

    bad["adminPermissions"] = "admin:overview:read"  # 非法类型
    errs = M.validate_manifest(bad)
    assert errs and any(e.startswith("adminPermissions 需为数组") for e in errs)

    bad["adminPermissions"] = [123]
    errs = M.validate_manifest(bad)
    assert errs and any("adminPermissions[0] 需为字符串" in e for e in errs)
