# -*- coding: utf-8 -*-
"""S4 管理员只读身份预览测试（HistoPilot/docs/session-isolation-fix-plan.md §3）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑）：
  - actor/subject 非破坏分离：current_identity() 返回 effective subject，
    actor_identity() 永远是真实管理员；_require_owner() 查 actor（预览骗不过）；
  - 预览态 subject 权限生效：切片列表按 subject 过滤（user 只见自己的），
    AI 会话过滤按 subject；
  - preview write guard：预览态一切非安全方法 403 preview_readonly（CSRF 之后），
    白名单仅退出预览 + GET；
  - 进入/退出：owner-only（user 403）、CSRF（无 token 400）、审计 start/stop
    各一条（actor + subject 字段）；
  - TTL 过期自动退出（默认 15 分钟，monkeypatch 缩短）；
  - 禁用用户不可作 subject（start 400；预览中被禁用 → 自动退出回 actor）；
  - AUTH_ENABLED=False 不变量不破：start 明确 400、无 preview、
    current_identity 仍归一 owner。

运行：cd 项目根 && python3 -m pytest tests/test_admin_preview.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_admin_preview.py -q）
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app, FakeRequests # noqa: E402
from pg_compat import BACKEND  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例独立存储 + 归一环境（AUTH_ENABLED=True + 无预览）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_mod, "PREVIEW_TTL_SECONDS", 15 * 60)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    if BACKEND == "postgres":
        # review R2-F2：PG 上 role=user 建号统一走「维护闸 + 开通锁」组合
        # 原语（闸 fail-closed），conftest TRUNCATE 清掉 0029 种子——每用例
        # 幂等重放（target=window + 闸=false）
        import _billing_helpers as bh
        bh.seed_spend_settings()
    yield


# --------------------------------------------------------------------------- #
# 基建：fake sidecar（path/stream 等代理端点不被本文件触发，仅归属查询用）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_sidecar(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


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
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera, userb


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    return name


# --------------------------------------------------------------------------- #
# 1. actor / subject 分离（§3.2）
# --------------------------------------------------------------------------- #
def test_current_identity_returns_subject_actor_stays_admin():
    """预览态 current_identity = subject；actor_identity 永远是管理员。"""
    owner, usera, _b = _setup_users()
    c = _login(_client(), owner)
    r = c.post("/api/admin/preview/start", json={"user_id": usera["user_id"]})
    assert r.status_code == 200, r.get_json()
    with c.session_transaction() as s:
        pv = dict(s[app_mod.PREVIEW_SESSION_KEY])
    assert pv["subject_user_id"] == usera["user_id"]
    assert pv["actor_user_id"] == owner["user_id"]
    assert pv["expires_at"] > time.time()

    # 函数级：把同一预览态灌进请求上下文，验证 identity 两个视角
    from flask import session as flask_session
    with app_mod.app.test_request_context("/api/slides"):
        flask_session["role"] = "owner"
        flask_session["user_id"] = owner["user_id"]
        flask_session[app_mod.PREVIEW_SESSION_KEY] = dict(pv)
        assert app_mod.current_identity() == {
            "role": "user", "user_id": usera["user_id"]}
        assert app_mod.actor_identity() == {
            "role": "owner", "user_id": owner["user_id"]}
        # 预览态结构无效（expires_at 非法）→ 自动退出，回 actor
        flask_session[app_mod.PREVIEW_SESSION_KEY] = {"subject_user_id": "x",
                                                      "expires_at": "bad"}
        assert app_mod.current_identity()["user_id"] == owner["user_id"]
        assert app_mod.PREVIEW_SESSION_KEY not in flask_session


def test_subject_visibility_applies_during_preview():
    """预览态切片可见性按 subject（user 只见自己的，owner 全量）。"""
    owner, usera, userb = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    share_store.set_slide_meta(sa, owner_user_id=usera["user_id"])
    share_store.set_slide_meta(sb, owner_user_id=userb["user_id"])

    oc = _login(_client(), owner)
    r = oc.get("/api/slides")
    assert {s["name"] for s in r.get_json()} == {sa, sb}

    r = oc.post("/api/admin/preview/start", json={"user_id": userb["user_id"]})
    assert r.status_code == 200, r.get_json()
    # 预览态：owner 以 subject(b) 视角看列表 → 只见 b.svs
    r = oc.get("/api/slides")
    assert r.status_code == 200
    assert {s["name"] for s in r.get_json()} == {sb}, r.get_json()
    # GET 放行（写 guard 只拦非安全方法）
    r = oc.get("/api/share/rois")
    assert r.status_code == 200


def test_require_owner_checks_actor_not_subject():
    """预览态 owner 守卫查 actor：GET 管理端点仍可用（只读浏览）。"""
    owner, usera, _b = _setup_users()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": usera["user_id"]}).status_code == 200
    # actor 仍是 owner → 管理端点 GET 不因预览被拒
    r = oc.get("/api/admin/users")
    assert r.status_code == 200, r.get_json()
    # 但写（POST .../disable，写探针端点——旧建号端点已 410 退役，review
    # R2-F1）被 preview write guard 拦
    r = oc.post("/api/admin/users/%s/disable" % _b["user_id"])
    assert r.status_code == 403
    assert r.get_json().get("code") == "preview_readonly"


# --------------------------------------------------------------------------- #
# 2. preview write guard（§3.3）
# --------------------------------------------------------------------------- #
def test_write_guard_blocks_all_unsafe_methods():
    """预览态所有写端点 403 preview_readonly（抽查多路由，含 CSRF 有效的）。"""
    owner, _a, userb = _setup_users()
    sa = _touch("coop.svs")
    share_store.set_slide_meta(sa, owner_user_id=userb["user_id"])
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    probes = [
        ("POST", "/api/upload", None),
        ("POST", "/api/annotation", {"slide": sa, "items": []}),
        ("POST", "/api/share/create", {"slides": [sa], "expires_hours": 1}),
        ("DELETE", "/api/slide/%s" % sa, None),
        ("PUT", "/api/ai/config", {"model": "m"}),
        ("POST", "/api/ai/run", {"slide": sa}),
        ("POST", "/logout", None),
    ]
    for method, path, body in probes:
        r = oc.open(path, method=method, json=body)
        assert r.status_code == 403, "%s %s -> %s" % (method, path, r.status_code)
        assert r.get_json().get("code") == "preview_readonly", path


def test_write_guard_allows_stop_and_get():
    """白名单：POST /api/admin/preview/stop 放行；GET 一律放行。"""
    owner, _a, userb = _setup_users()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    assert oc.get("/api/slides").status_code == 200
    r = oc.post("/api/admin/preview/stop")
    assert r.status_code == 200, r.get_json()
    # 退出后写恢复（owner 身份）。写探针端点用 disable（旧建号端点已 410
    # 退役，review R2-F1；disable 幂等返回 200，与后端无关）
    r = oc.post("/api/admin/users/%s/disable" % userb["user_id"])
    assert r.status_code == 200, r.get_json()


def test_write_guard_after_csrf_missing_token_gets_csrf_400():
    """写 guard 挂在 CSRF 之后：无 token 的写在 CSRF 层先 400 csrf_required。"""
    owner, _a, userb = _setup_users()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    r = oc._base.post("/api/annotation", json={"slide": "x.svs", "items": []})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


# --------------------------------------------------------------------------- #
# 3. 进入/退出权限与 CSRF（§3.4）
# --------------------------------------------------------------------------- #
def test_start_requires_owner_and_csrf():
    """user 调 start → 403；owner 无 token → 400 csrf_required；user_id 缺失 → 400。"""
    owner, _a, userb = _setup_users()
    bc = _login(_client(), userb)
    r = bc.post("/api/admin/preview/start", json={"user_id": owner["user_id"]})
    assert r.status_code == 403

    oc = _login(_client(), owner)
    r = oc._base.post("/api/admin/preview/start", json={"user_id": userb["user_id"]})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"

    r = oc.post("/api/admin/preview/start", json={})
    assert r.status_code == 400
    r = oc.post("/api/admin/preview/start", json={"user_id": "no-such-user"})
    assert r.status_code == 404


def test_stop_requires_owner():
    """user 调 stop → 403（即便真有 owner 预览态也无法被 user 关闭）。"""
    owner, _a, userb = _setup_users()
    bc = _login(_client(), userb)
    assert bc.post("/api/admin/preview/stop").status_code == 403


def test_cannot_preview_disabled_user():
    """禁用用户不可作 subject：start → 400；预览中被禁用 → 自动退出回 actor。"""
    owner, _a, userb = _setup_users()
    user_store.set_user_disabled(userb["user_id"], True)
    oc = _login(_client(), owner)
    r = oc.post("/api/admin/preview/start", json={"user_id": userb["user_id"]})
    assert r.status_code == 400
    assert r.get_json().get("code") == "subject_disabled"

    # 预览生效后 subject 被禁用：每请求重新解析 → 自动退出
    user_store.set_user_disabled(userb["user_id"], False)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    user_store.set_user_disabled(userb["user_id"], True)
    sb = _touch("b.svs")
    share_store.set_slide_meta(sb, owner_user_id=userb["user_id"])
    # 预览已自动退出 → owner 视角全量可见
    r = oc.get("/api/slides")
    assert r.status_code == 200
    assert {s["name"] for s in r.get_json()} == {sb}
    with oc.session_transaction() as s:
        assert app_mod.PREVIEW_SESSION_KEY not in s


# --------------------------------------------------------------------------- #
# 4. TTL（§3.4）
# --------------------------------------------------------------------------- #
def test_preview_ttl_expires_and_auto_exits():
    """TTL 过期自动退出：过期后写不再被 guard 拦（回到 actor），GET 回 owner 视角。"""
    owner, _a, userb = _setup_users()
    sb = _touch("b.svs")
    _touch("o.svs")
    share_store.set_slide_meta(sb, owner_user_id=userb["user_id"])
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    # 手工把 expires_at 拨到过去（等价 TTL 到期；嵌套 dict 需整体重赋值，
    # werkzeug session 只跟踪顶层 __setitem__ 的 modified 标记）
    with oc.session_transaction() as s:
        pv = dict(s[app_mod.PREVIEW_SESSION_KEY])
        pv["expires_at"] = time.time() - 1
        s[app_mod.PREVIEW_SESSION_KEY] = pv
    # 写探针端点用 disable（旧建号端点已 410 退役，review R2-F1；disable
    # 幂等返回 200，与后端无关）——过期自动退出 → 写放行
    r = oc.post("/api/admin/users/%s/disable" % userb["user_id"])
    assert r.status_code == 200, r.get_json()
    with oc.session_transaction() as s:
        assert app_mod.PREVIEW_SESSION_KEY not in s


def test_preview_ttl_configurable(monkeypatch):
    """PREVIEW_TTL_SECONDS 影响 start 响应与 session 的 expires_at。"""
    owner, _a, userb = _setup_users()
    monkeypatch.setattr(app_mod, "PREVIEW_TTL_SECONDS", 60)
    oc = _login(_client(), owner)
    before = time.time()
    r = oc.post("/api/admin/preview/start", json={"user_id": userb["user_id"]})
    assert r.status_code == 200
    expires = r.get_json()["preview"]["expires_at"]
    assert before + 55 <= expires <= before + 70


# --------------------------------------------------------------------------- #
# 5. 审计（§3.4）
# --------------------------------------------------------------------------- #
def test_preview_start_stop_audited_with_actor_and_subject():
    """start/stop 各记一条：actor=管理员，detail 带 subject_user_id/preview。"""
    owner, _a, userb = _setup_users()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    assert oc.post("/api/admin/preview/stop").status_code == 200
    events = [e for e in share_store.list_audit(limit=50)
              if e["action"].startswith("preview.")]
    actions = [e["action"] for e in events]
    assert "preview.start" in actions and "preview.stop" in actions
    for ev in events:
        assert ev["actor_user_id"] == owner["user_id"], ev
        assert ev["actor_role"] == "owner", ev
    start_ev = next(e for e in events if e["action"] == "preview.start")
    assert start_ev["detail"].get("subject_user_id") == userb["user_id"]
    assert start_ev["detail"].get("preview") is True


def test_preview_state_in_auth_info_or_identity_only():
    """/api/auth/info：顶层 role/user_id 为 effective subject，actor 仍是管理员。"""
    owner, _a, userb = _setup_users()
    oc = _login(_client(), owner)
    before = oc.get("/api/auth/info").get_json()
    assert before["role"] == "owner"
    assert before["user_id"] == owner["user_id"]
    assert before.get("preview") is None
    assert before["actor"]["role"] == "owner"
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    r = oc.get("/api/auth/info")
    assert r.status_code == 200
    info = r.get_json()
    assert info["role"] == "user"
    assert info["user_id"] == userb["user_id"]
    assert info["username"] == userb["login_id"]
    assert info["actor"]["role"] == "owner"
    assert info["actor"]["user_id"] == owner["user_id"]
    assert info["preview"]["subject_user_id"] == userb["user_id"]
    assert info["preview"]["subject_role"] == "user"
    assert info["preview"]["expires_at"] > time.time()


# --------------------------------------------------------------------------- #
# 6. AUTH_ENABLED=False 不变量（§3.6 / 测试计划）
# --------------------------------------------------------------------------- #
def test_auth_disabled_preview_start_rejected_and_identity_owner():
    """AUTH_ENABLED=False：start 400；无 preview 时 current_identity 归一 owner。"""
    app_mod.AUTH_ENABLED = False
    owner, _a, userb = _setup_users()
    c = _client()
    r = c.post("/api/admin/preview/start", json={"user_id": userb["user_id"]})
    assert r.status_code == 400, r.get_json()
    with c.session_transaction() as s:
        assert app_mod.PREVIEW_SESSION_KEY not in s
    with app_mod.app.test_request_context("/api/slides"):
        from flask import session as flask_session
        flask_session.clear()
        ident = app_mod.current_identity()
        assert ident["role"] == "owner"
        assert app_mod.actor_identity()["role"] == "owner"


# --------------------------------------------------------------------------- #
# 7. S4×AI 会话过滤：预览态 session 过滤断言（用 fake sidecar 的 owner 注入）
# --------------------------------------------------------------------------- #
def test_ai_session_filter_uses_subject_during_preview(fake_sidecar):
    """/api/ai/sessions 的 owner 过滤参数 = subject（预览态自动生效）。"""
    owner, _a, userb = _setup_users()
    sa = _touch("coop.svs")
    share_store.set_slide_meta(sa, owner_user_id=userb["user_id"])
    fake_sidecar.register_json(
        "GET", "/sessions", body=[{"session_id": "s1"}])
    oc = _login(_client(), owner)
    # 非预览：owner → 不注入 owner query（全量）
    assert oc.get("/api/ai/sessions?slide=%s" % sa).status_code == 200
    assert fake_sidecar.calls[-1]["query"].get("owner") is None
    # 预览 subject=b：过滤按 subject 注入
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": userb["user_id"]}).status_code == 200
    assert oc.get("/api/ai/sessions?slide=%s" % sa).status_code == 200
    assert fake_sidecar.calls[-1]["query"].get("owner") == userb["user_id"]
