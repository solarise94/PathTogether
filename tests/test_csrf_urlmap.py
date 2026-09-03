# -*- coding: utf-8 -*-
"""CSRF 参数化遍历 + 零覆盖路由端点级测试（test-review P1-5 / P1-6）。

P1-5：从 ``app.url_map`` 枚举**全部**非安全方法路由（排除 ``_CSRF_EXEMPT_PREFIXES``：
``/internal/``、``/api/plugin/``、``/api/demo/``），统一断言「有 session 无 token
→ 400 csrf_required」——替代此前 3 端点抽查，任一写端点漏 CSRF 都会被捕获。
注意契约：``/api/*`` 只认 X-CSRF-Token header（不回退 form 域），所以测试不携带
任何 token；``/login`` 表单路径无 token 返回 HTML 400（error_code=csrf）。

P1-6：零覆盖路由端点级——``GET /api/share/rois``（401/按可见性过滤）与
``POST /api/admin/plugins/install``（401/403/400）。

运行：cd 项目根 && python3 -m pytest tests/test_csrf_urlmap.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    yield


# --------------------------------------------------------------------------- #
# P1-5：url_map 参数化遍历
# --------------------------------------------------------------------------- #
#: 按转换器类型给路由参数填样板值（只求命中路由 + 触发 CSRF 层，不进业务）
_ARG_SAMPLES = {
    "default": "probe",
    "string": "probe",
    "int": "1",
    "float": "1.0",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "path": "x",
}


def _rule_methods_unsafe(rule):
    return sorted(rule.methods - app_mod._CSRF_SAFE_METHODS)


def _build_rule_path(rule):
    """为 rule 的路径参数填样板值并构造 URL；无法构造返回 None。"""
    values = {}
    for arg in rule.arguments:
        conv = rule._converters.get(arg)
        # werkzeug 转换器类名：UnicodeConverter/IntegerConverter/FloatConverter/
        # UUIDConverter/PathConverter/AnyConverter —— 按类名子串选样板值
        cls = type(conv).__name__.lower() if conv is not None else ""
        if "integer" in cls:
            val = _ARG_SAMPLES["int"]
        elif "float" in cls:
            val = _ARG_SAMPLES["float"]
        elif "uuid" in cls:
            val = _ARG_SAMPLES["uuid"]
        elif "path" in cls:
            val = _ARG_SAMPLES["path"]
        else:
            val = _ARG_SAMPLES["default"]
        values[arg] = val
    built = app_mod.app.url_map.bind("localhost").build(
        rule.endpoint, values, force_external=False)
    return built if isinstance(built, str) else built[0]


def _collect_cases():
    """[(rule, method, path)]：全部非豁免、非安全方法路由。"""
    cases = []
    for rule in app_mod.app.url_map.iter_rules():
        if rule.rule == "/static/<path:filename>":  # 静态资源（GET-only，防御位）
            continue
        if rule.rule.startswith(app_mod._CSRF_EXEMPT_PREFIXES):
            continue
        for method in _rule_methods_unsafe(rule):
            cases.append((rule, method, rule.rule))
    return cases


#: 有 session、无 token 的裸 client（AUTH_ENABLED=False：_require_auth no-op，
#: 每个写请求都应死在 _csrf_protect）
def _bare_client():
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    client.get("/login")  # 建立 session + 绑定 csrf token（无 token 提交必 400）
    return client


def test_csrf_traversal_collects_nontrivial_routes():
    """遍历面自检：收集到的 (路由×方法) 数量可观且覆盖已知写端点。"""
    cases = _collect_cases()
    paths = {r.rule for r, _m, _p in cases}
    assert len(cases) >= 40, "url_map 遍历收集到的写路由过少：%d" % len(cases)
    for must in ("/api/upload", "/api/uploads", "/api/share/create",
                 "/api/annotation", "/api/ai/run", "/api/admin/v1/users",
                 "/api/admin/preview/start", "/api/admin/preview/stop",
                 "/api/ai/session/<session_id>/archive"):
        assert must in paths, "缺写路由 %s（遍历断言面不完整）" % must
    # 豁免前缀确实被排除
    assert not any(p.startswith(app_mod._CSRF_EXEMPT_PREFIXES)
                   for p in paths)


_CASES = [(rule.rule, method) for rule, method, _ in _collect_cases()]


@pytest.mark.parametrize("rule_path,method", _CASES,
                         ids=["%s %s" % (m, p) for p, m in _CASES])
def test_write_route_without_token_gets_400(rule_path, method):
    """有 session 无 token 的写请求 → 400（csrf_required；/login 为 HTML 表单页）。"""
    rule = next(r for r in app_mod.app.url_map.iter_rules()
                if r.rule == rule_path and method in r.methods)
    path = _build_rule_path(rule)
    if path is None:
        pytest.skip("路由参数含不支持的转换器：%s" % rule_path)
    client = _bare_client()
    r = client.open(path, method=method)
    assert r.status_code == 400, "%s %s -> %s（预期 400 csrf_required）" % (
        method, path, r.status_code)
    if rule_path == "/login":
        # 表单页：HTML 重试页（error_code=csrf），非裸 JSON
        assert b"csrf" in r.data or "csrf" in r.get_data(as_text=True)
    else:
        assert r.get_json().get("error") == "csrf_required", (
            "%s %s 响应体 %r" % (method, path, r.get_data(as_text=True)[:200]))


def test_api_routes_do_not_accept_form_token_fallback():
    """P1-5 契约注意点：/api/* 只认 header——**带 form 域 token 不算通过**。

    对抽查的 /api/upload（multipart 场景）发送 form 域 csrf_token：仍必须 400
    （header-only 契约；否则「无 token 在 body 接收前即拒」不成立）。
    """
    client = _bare_client()
    tok = client.get_cookie("csrf_token", domain="localhost", path="/")
    assert tok, "GET /login 应下发 csrf_token cookie"
    r = client.post("/api/upload", data={
        "csrf_token": tok.value, "file": (b"stub", "a.svs")})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


# --------------------------------------------------------------------------- #
# P1-6：零覆盖路由端点级
# --------------------------------------------------------------------------- #
def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera, userb


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    return name


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


class TestShareRoisEndpoint:
    """GET /api/share/rois：owner 全量；user 按可见切片过滤；未登录 401。"""

    def test_unauthenticated_401(self):
        app_mod.AUTH_ENABLED = True
        client = csrf_client(app_mod.app.test_client())
        r = client.get("/api/share/rois")
        assert r.status_code == 401
        assert r.get_json()["error"] == "auth_required"

    def test_owner_sees_all_user_sees_visible_only(self):
        owner, usera, userb = _setup_users()
        sa = _touch("a.svs")
        sb = _touch("b.svs")
        share_store.set_slide_meta(sa, owner_user_id=usera["user_id"])
        share_store.set_slide_meta(sb, owner_user_id=userb["user_id"])
        share_store.add_roi("admin", sa, "L", type="rect",
                            x=0, y=0, side_px=10, owner_user_id=usera["user_id"],
                            requester_role=user_store.ROLE_OWNER)
        share_store.add_roi("admin", sb, "L", type="rect",
                            x=0, y=0, side_px=10, owner_user_id=userb["user_id"],
                            requester_role=user_store.ROLE_OWNER)

        app_mod.AUTH_ENABLED = True
        oc = _login(csrf_client(app_mod.app.test_client()), owner)
        r = oc.get("/api/share/rois")
        assert r.status_code == 200
        assert {x["slide"] for x in r.get_json()} == {sa, sb}

        ac = _login(csrf_client(app_mod.app.test_client()), usera)
        r = ac.get("/api/share/rois")
        assert r.status_code == 200
        assert {x["slide"] for x in r.get_json()} == {sa}, r.get_json()

        bc = _login(csrf_client(app_mod.app.test_client()), userb)
        r = bc.get("/api/share/rois")
        assert r.status_code == 200
        assert {x["slide"] for x in r.get_json()} == {sb}

    def test_user_without_visible_slides_gets_empty_list(self):
        owner, usera, _b = _setup_users()
        sa = _touch("a.svs")
        share_store.set_slide_meta(sa, owner_user_id=usera["user_id"])
        share_store.add_roi("admin", sa, "L", type="rect",
                            x=0, y=0, side_px=10, owner_user_id=usera["user_id"],
                            requester_role=user_store.ROLE_OWNER)
        # 再造一个与 C 无关的切片 roi
        sc = _touch("c.svs")
        share_store.set_slide_meta(sc, owner_user_id=usera["user_id"])
        share_store.add_roi("admin", sc, "L", type="rect",
                            x=0, y=0, side_px=10, owner_user_id=usera["user_id"],
                            requester_role=user_store.ROLE_OWNER)
        userc = user_store.create_user("c@x.com", "userCpass123456", role="user")
        app_mod.AUTH_ENABLED = True
        cc = _login(csrf_client(app_mod.app.test_client()), userc)
        r = cc.get("/api/share/rois")
        assert r.status_code == 200
        assert r.get_json() == []


class TestAdminPluginsInstallEndpoint:
    """POST /api/admin/plugins/install：owner-only；参数 400；CSRF。"""

    def test_unauthenticated_401(self):
        app_mod.AUTH_ENABLED = True
        client = csrf_client(app_mod.app.test_client())
        r = client.post("/api/admin/plugins/install", json={"plugin": "p"})
        assert r.status_code == 401
        assert r.get_json()["error"] == "auth_required"

    def test_user_403_owner_bad_body_400(self):
        owner, usera, _b = _setup_users()
        app_mod.AUTH_ENABLED = True
        ac = _login(csrf_client(app_mod.app.test_client()), usera)
        r = ac.post("/api/admin/plugins/install", json={"plugin": "p"})
        assert r.status_code == 403
        assert r.get_json()["error"] == "需要 owner 权限"

        oc = _login(csrf_client(app_mod.app.test_client()), owner)
        # owner：缺 plugin 字段 → 400（_require_owner 已过，进到参数校验）
        r = oc.post("/api/admin/plugins/install", json={})
        assert r.status_code == 400
        assert "plugin" in r.get_json()["error"]
        r = oc.post("/api/admin/plugins/install", json={"plugin": "  "})
        assert r.status_code == 400
        # owner：不存在的插件目录 → 400 级错误（安装失败 fail-closed，
        # 不允许 2xx；具体 code 由 install_plugin_bundle 决定）
        r = oc.post("/api/admin/plugins/install",
                    json={"plugin": "no-such-plugin-dir"})
        assert r.status_code == 400, r.get_data(as_text=True)

    def test_missing_token_400(self):
        owner, _a, _b = _setup_users()
        app_mod.AUTH_ENABLED = True
        raw = app_mod.app.test_client()
        with raw.session_transaction() as s:
            s.update({"auth_user": "o", "user_id": owner["user_id"],
                      "role": "owner",
                      "auth_version": owner.get("auth_version", 1)})
        r = raw.post("/api/admin/plugins/install", json={"plugin": "p"})
        assert r.status_code == 400
        assert r.get_json()["error"] == "csrf_required"
