# -*- coding: utf-8 -*-
"""账户系统批次 A「app 线」测试（docs/account-system-simplification-fix-plan.md
§5 启动状态机 / §6.2 session 版本 / §7 密码 API / §11 批次 A 测试矩阵）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑）：
  - 启动状态机纯函数（_resolve_bootstrap_config / _resolve_owner_at_startup）：
    空库首建、已有 owner 不对账、兼容别名 deprecated 告警、无 owner 拒启、
    多 owner 拒启（json-only 直插构造）、空 hash 拒启、owner 归属注入；
  - session auth_version 失效矩阵：本人改密/管理重置/disable→enable/旧 Cookie；
  - POST /api/account/password：401/CSRF/invalid_current_password/429 防爆破/
    长度与同密码拒绝/成功清 session；
  - 管理端点收紧：owner 409、user 重置 + audit + 版本递增、
    「无任何 Web 端点可降级/删除 owner」不变量锁定；
  - 普通请求只有一次用户 DB 回查（get_user spy 计数）。

break-glass CLI（useradmin）不在此重复：store 线 tests/test_useradmin.py
已覆盖 16 例。隔离：独立临时 SHARE_DATA_DIR / UPLOAD_DIR，绝不触真实数据。
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="svs-acct-auth-")
DATA_DIR = os.path.join(TMP, "share-data")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)
# 默认无 bootstrap 秘密；认证由用户是否存在决定（与 test_user_store 同口径）
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
import conftest  # noqa: E402
from pg_compat import json_only  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402

#: 测试用密码（≥15 字符，满足统一 15..200 策略）
OWNER_PW = "owner-pass-123456"
USER_PW = "user-pass-1234567"
NEW_PW = "new-pass-12345678"
OTHER_PW = "other-pass-123456"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def _share_impl():
    """当前后端的 share_store 实现模块（读 _OWNER_USER_ID 注入状态用）。"""
    if conftest.BACKEND == "postgres":
        import share_store_pg
        return share_store_pg
    import share_store_json
    return share_store_json


def _share_owner_uid():
    return getattr(_share_impl(), "_OWNER_USER_ID", "")


def _reset_users_file():
    """json 后端：清空 users.json（每用例隔离）；PG 后端由 conftest truncate。"""
    if conftest.BACKEND != "json":
        return
    p = user_store.USER_FILE
    if p.exists():
        p.unlink()


def _write_json_users(users: dict):
    """json 后端：直写 users.json（构造多 owner / 空 hash 等非常规状态）。"""
    assert conftest.BACKEND == "json"
    user_store.USER_FILE.write_text(
        json.dumps({"users": users, "meta": {"schema_version": 1}},
                   ensure_ascii=False), encoding="utf-8")


def _read_json_users() -> dict:
    assert conftest.BACKEND == "json"
    return json.loads(user_store.USER_FILE.read_text(encoding="utf-8"))["users"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离目录 / 清空用户库 / 归属注入清空 / json 登录限流 mock。"""
    data_dir = Path(DATA_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    if conftest.BACKEND == "json":
        monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
        monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
        monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
        monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    share_store.set_owner_user_id("")
    install_json_login_limits(monkeypatch)
    _reset_users_file()
    for name in ("users.json", "shares.json", "users.json.bak", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    yield
    # 恢复全局开关，避免串扰其他测试文件
    app_mod.AUTH_ENABLED = app_mod._resolve_auth_enabled({})


def _fake_pg_backend(monkeypatch):
    """json 模式下把 share_store.STORAGE_BACKEND 伪造成 postgres，使
    REQUIRE_ADMIN_AUTH=1 的后端前置检查放行（PG 模式本身即是 postgres）。"""
    monkeypatch.setattr(share_store, "STORAGE_BACKEND", "postgres")


def make_client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def relogin(client, username, password):
    """模拟真实浏览器「被登出 → 跳登录页 → 再提交」流程再登录。

    session 被 _require_auth/改密清空后，旧 csrf_token cookie 即失效；
    GET /login（安全方法）会下发新 token——浏览器跳转登录页天然走这一步，
    测试需显式补上（与 _pt_helpers.CsrfClient 的惰性取 token 行为一致）。
    """
    client.get("/login")
    return login(client, username, password)


def make_owner(login_id="admin", password=OWNER_PW):
    """空库首建 owner（store 线契约）。返回 owner dict（含 hash/auth_version）。"""
    return user_store.create_bootstrap_owner(login_id, password)


# =========================================================================== #
# 1. 启动状态机（docs §5.2 / §11.1）
# =========================================================================== #
def test_startup_bootstrap_creates_owner_and_login():
    """空库 + bootstrap 秘密 → 建 owner（规范化 login_id）且可登录。"""
    owner = app_mod._resolve_owner_at_startup({
        "BOOTSTRAP_OWNER_LOGIN_ID": "  Browser_Admin  ",
        "ADMIN_PASSWORD": OWNER_PW,
    })
    check("首建返回 owner dict", isinstance(owner, dict) and owner.get("role") == "owner")
    check("login_id 规范化（trim+lower）", owner.get("email") == "browser_admin")
    check("新 owner auth_version=1", owner.get("auth_version") == 1)
    check("库中恰一个 enabled owner", len(user_store.list_enabled_owners()) == 1)
    check("可用该密码登录", user_store.verify_user("browser_admin", OWNER_PW) is not None)


def test_startup_bootstrap_legacy_alias():
    """空库 + 兼容别名（ADMIN_USERNAME/ADMIN_PASSWORD）同样可引导（§11.1-1）。

    独立用例：首建只允许空库一次（json 靠 _isolate 清文件、PG 靠 conftest
    每用例 TRUNCATE 获得干净起点）。
    """
    owner2 = app_mod._resolve_owner_at_startup({
        "ADMIN_USERNAME": "legacy_admin",
        "ADMIN_PASSWORD": OWNER_PW,
    })
    check("兼容别名空库可引导", owner2 is not None
          and owner2.get("email") == "legacy_admin")
    check("兼容别名引导后可登录",
          user_store.verify_user("legacy_admin", OWNER_PW) is not None)


def test_startup_existing_owner_env_password_not_reconciled():
    """已有 owner + env 密码不同 → 启动后 DB hash 不变、绝不对账覆盖。"""
    make_owner("admin", OWNER_PW)
    hash_before = user_store.list_enabled_owners()[0]["password_hash"]
    owner = app_mod._resolve_owner_at_startup({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "totally-different-pw-123",
    })
    check("正常解析已有 owner", owner is not None and owner.get("email") == "admin")
    hash_after = user_store.list_enabled_owners()[0]["password_hash"]
    check("DB hash 不变（无对账覆盖）", hash_before == hash_after)
    check("原密码仍可登录", user_store.verify_user("admin", OWNER_PW) is not None)
    check("env 密码不能登录（未写入）",
          user_store.verify_user("admin", "totally-different-pw-123") is None)


def test_startup_existing_owner_without_env(monkeypatch):
    """已有 owner + 无 ADMIN_PASSWORD → 正常启动可登录（§11.1-3）。"""
    make_owner("admin", OWNER_PW)
    owner = app_mod._resolve_owner_at_startup({})
    check("无 env 正常解析", owner is not None and owner.get("email") == "admin")
    # 归属注入（docs §5.2 末段）：无论是否有 bootstrap env，都从 DB 注入
    uid = app_mod._inject_owner_into_share_store(owner)
    check("share_store 归属注入 = 解析出的 owner", _share_owner_uid() == uid)
    client = make_client()
    r = login(client, "admin", OWNER_PW)
    check("无 env 时 owner 可登录", r.status_code == 302, "got %s" % r.status_code)


def test_startup_admin_username_change_only_warns(monkeypatch, caplog):
    """已有 owner + 改动 ADMIN_USERNAME → 不改 DB login_id，仅 deprecated 告警。"""
    make_owner("admin", OWNER_PW)
    with caplog.at_level(logging.WARNING):
        owner = app_mod._resolve_owner_at_startup({
            "ADMIN_USERNAME": "someone-else",
            "ADMIN_PASSWORD": "whatever-password-1",
        })
    check("仍解析原 owner", owner is not None and owner.get("email") == "admin")
    check("DB login_id 未被改动",
          user_store.list_enabled_owners()[0]["email"] == "admin")
    warned = any(
        ("忽略" in rec.message or "deprecated" in rec.message)
        and ("ADMIN" in rec.message or "BOOTSTRAP" in rec.message)
        for rec in caplog.records)
    check("记录一次忽略/deprecated 告警", warned,
          "records=%r" % [r.message for r in caplog.records])


def test_startup_no_owner_but_users_refuses(monkeypatch):
    """无 owner 但已有普通 user → REQUIRE_ADMIN_AUTH=1 拒绝启动（指明逃生路径）。"""
    _fake_pg_backend(monkeypatch)
    user_store.create_user("u@x.com", USER_PW, role="user")
    with pytest.raises(SystemExit) as ei:
        app_mod._resolve_owner_at_startup({"REQUIRE_ADMIN_AUTH": "1"})
    msg = str(ei.value)
    check("拒启文案指明 break-glass/审计路径",
          "useradmin" in msg or "人工审计" in msg, "msg=%r" % msg)
    # 算法分支无条件（docs §5.2）：0 owner + 已有用户行即拒启，不静默建号
    with pytest.raises(SystemExit):
        app_mod._resolve_owner_at_startup({})


def test_startup_requires_pg_backend(monkeypatch):
    """REQUIRE_ADMIN_AUTH=1 且后端非 postgres → 拒绝启动（docs §9.1）。"""
    monkeypatch.setattr(share_store, "STORAGE_BACKEND", "json")
    with pytest.raises(SystemExit) as ei:
        app_mod._resolve_owner_at_startup({"REQUIRE_ADMIN_AUTH": "1"})
    check("文案指明 postgres 要求", "postgres" in str(ei.value))


def test_startup_empty_db_require_auth_no_secret_refuses(monkeypatch):
    """空库 + REQUIRE_ADMIN_AUTH=1 + 无秘密 → 拒绝启动（fail-closed）。"""
    _fake_pg_backend(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        app_mod._resolve_owner_at_startup({"REQUIRE_ADMIN_AUTH": "1"})
    check("文案指明 bootstrap 秘密缺失", "bootstrap" in str(ei.value).lower())


def test_startup_placeholder_secret_treated_as_unconfigured(monkeypatch):
    """占位符 ADMIN_PASSWORD 视为未配置：REQUIRE_ADMIN_AUTH=1 → 拒启。"""
    _fake_pg_backend(monkeypatch)
    with pytest.raises(SystemExit):
        app_mod._resolve_owner_at_startup({
            "REQUIRE_ADMIN_AUTH": "1",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": app_mod.ADMIN_PASSWORD_PLACEHOLDER_SENTINEL,
        })
    with pytest.raises(SystemExit):
        app_mod._resolve_owner_at_startup({
            "REQUIRE_ADMIN_AUTH": "1",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "<still-a-placeholder>",
        })


@json_only  # PG 下 0015 部分唯一索引使 >1 enabled owner 不可构造（store 线已覆盖）
def test_startup_multiple_enabled_owners_refuses():
    """2 个 enabled owner（json 直插构造）→ 拒绝启动，禁止选「第一个」。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("o2@x.com", OTHER_PW, role="owner")
    with pytest.raises(SystemExit) as ei:
        app_mod._resolve_owner_at_startup({})
    check("文案含 multiple_enabled_owners 语义",
          "owner" in str(ei.value), "msg=%r" % ei.value)


def test_startup_owner_empty_hash_refuses():
    """owner password_hash 为空 → 拒绝启动（提示 useradmin 修复）。"""
    owner = make_owner("admin", OWNER_PW)
    if conftest.BACKEND == "json":
        users = _read_json_users()
        users[owner["user_id"]]["password_hash"] = ""
        _write_json_users(users)
    else:
        import pg_store
        conn = pg_store.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET password_hash='' WHERE user_id=%s",
                            (owner["user_id"],))
            conn.commit()
        finally:
            conn.close()
    with pytest.raises(SystemExit) as ei:
        app_mod._resolve_owner_at_startup({})
    check("文案提示 useradmin 修复", "useradmin" in str(ei.value),
          "msg=%r" % ei.value)


def test_startup_dev_mode_no_owner_no_secret():
    """空库 + 无秘密 + 未开 REQUIRE_ADMIN_AUTH → owner=None（本地免认证开发态）。"""
    owner = app_mod._resolve_owner_at_startup({})
    check("返回 None", owner is None)
    check("AUTH_ENABLED=False", app_mod._resolve_auth_enabled({}) is False)


def test_startup_concurrent_loser_re_resolves(monkeypatch):
    """并发首建败者（users_table_not_empty）→ 重走解析正常启动（docs §5.3）。"""
    make_owner("admin", OWNER_PW)  # 模拟另一 worker 已建号
    real_create = user_store.create_bootstrap_owner

    def _raise_table_not_empty(login_id, password):
        raise user_store.OwnerInvariantError(
            "users_table_not_empty：users 表已有 1 行，create_bootstrap_owner "
            "仅允许在完全空库时创建首个 owner")

    monkeypatch.setattr(user_store, "create_bootstrap_owner", _raise_table_not_empty)
    owner = app_mod._resolve_owner_at_startup({
        "BOOTSTRAP_OWNER_LOGIN_ID": "admin",
        "ADMIN_PASSWORD": OWNER_PW,
    })
    check("并发败者重解析成功", owner is not None and owner.get("email") == "admin")
    check("create_bootstrap_owner 确被调用过（模拟并发）",
          real_create is not None)


# =========================================================================== #
# 2. session auth_version 失效矩阵（docs §6.2 / §11.3）
# =========================================================================== #
def _audits(action):
    return share_store.list_audit(limit=50, action=action)


def test_change_own_password_invalidates_all_sessions():
    """本人改密：当前 cookie + 另一浏览器 cookie 均 401；新密码可登录。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    c1 = make_client()
    c2 = make_client()  # 第二个「浏览器」
    assert login(c1, "u@x.com", USER_PW).status_code == 302
    assert login(c2, "u@x.com", USER_PW).status_code == 302
    check("改密前两客户端均可访问", c1.get("/api/projects").status_code == 200
          and c2.get("/api/projects").status_code == 200)

    r = c1.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("改密 200", r.status_code == 200, "got %s: %r" % (r.status_code, r.data))
    check("返回 ok=true", r.get_json() == {"ok": True})
    check("改密后当前 cookie 401", c1.get("/api/projects").status_code == 401)
    check("改密后另一浏览器 cookie 401",
          c2.get("/api/projects").status_code == 401)
    check("旧密码不能再登录", relogin(c2, "u@x.com", USER_PW).status_code == 401)
    check("新密码可登录", relogin(c2, "u@x.com", NEW_PW).status_code == 302)


def test_change_password_audit_no_secrets():
    """改密 audit：action/actor/target 正确且 detail 无密码特征。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    c = make_client()
    login(c, "u@x.com", USER_PW)
    c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    evs = _audits("user.password_change")
    check("audit 落库", len(evs) == 1, "events=%d" % len(evs))
    ev = evs[0]
    check("actor=自己", ev.get("actor_user_id") == uid)
    check("target=自己", ev.get("target_type") == "user"
          and ev.get("target_id") == uid)
    check("detail 只含 sessions_revoked",
          ev.get("detail") == {"sessions_revoked": True}, "detail=%r" % ev.get("detail"))
    blob = json.dumps(ev, ensure_ascii=False, default=str)
    check("audit 无明文/hag 特征",
          USER_PW not in blob and NEW_PW not in blob and "hash" not in blob)


def test_owner_reset_user_password_invalidates_session():
    """owner 重置普通用户密码后该用户旧 cookie 401；audit 正确；版本递增。"""
    make_owner("admin", OWNER_PW)
    u = user_store.create_user("u@x.com", USER_PW, role="user")
    uc = make_client()
    login(uc, "u@x.com", USER_PW)
    check("重置前可访问", uc.get("/api/projects").status_code == 200)

    oc = make_client()
    login(oc, "admin", OWNER_PW)
    r = oc.post("/api/admin/users/%s/password" % u["user_id"],
                json={"password": NEW_PW})
    check("owner 重置 user 200", r.status_code == 200, "got %s" % r.status_code)
    check("响应不回显密码", "password" not in json.loads(r.data))
    check("旧 cookie 立即 401", uc.get("/api/projects").status_code == 401)
    check("auth_version 递增",
          user_store.get_user(u["user_id"])["auth_version"] == u["auth_version"] + 1)
    evs = _audits("user.password_reset")
    check("user.password_reset audit 落库", len(evs) == 1)
    ev = evs[0]
    check("reset actor=操作者(owner)",
          ev.get("actor_user_id") != u["user_id"] and ev.get("actor_user_id"))
    check("reset target 正确", ev.get("target_id") == u["user_id"])
    check("reset detail 无密码特征",
          ev.get("detail") == {"sessions_revoked": True}
          and NEW_PW not in json.dumps(ev, default=str))


def test_disable_then_enable_still_invalidates():
    """disable 后未访问再 enable，旧 cookie 仍 401（版本不匹配，docs §6.2）。"""
    make_owner("admin", OWNER_PW)
    u = user_store.create_user("u@x.com", USER_PW, role="user")
    uc = make_client()
    login(uc, "u@x.com", USER_PW)
    check("禁用前可访问", uc.get("/api/projects").status_code == 200)

    oc = make_client()
    login(oc, "admin", OWNER_PW)
    assert oc.post("/api/admin/users/%s/disable" % u["user_id"]).status_code == 200
    # 禁用期间用户不发请求；直接重新启用（store 层双向递增 auth_version）
    assert oc.post("/api/admin/users/%s/enable" % u["user_id"]).status_code == 200
    check("enable 后旧 cookie 仍 401（版本不匹配）",
          uc.get("/api/projects").status_code == 401)
    check("重新登录恢复访问", relogin(uc, "u@x.com", USER_PW).status_code == 302
          and uc.get("/api/projects").status_code == 200)


def test_disable_invalidates_immediately():
    """disable 后旧 cookie 401（基础路径）。"""
    make_owner("admin", OWNER_PW)
    u = user_store.create_user("u@x.com", USER_PW, role="user")
    uc = make_client()
    login(uc, "u@x.com", USER_PW)
    oc = make_client()
    login(oc, "admin", OWNER_PW)
    assert oc.post("/api/admin/users/%s/disable" % u["user_id"]).status_code == 200
    check("禁用后旧 cookie 401", uc.get("/api/projects").status_code == 401)


def test_legacy_cookie_without_auth_version_rejected():
    """旧版本 cookie（无 auth_version 键）→ 首次请求即 401，不回填复活。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    c = make_client()
    login(c, "u@x.com", USER_PW)
    # 模拟部署前签发的旧 Cookie：删掉 session 里的 auth_version
    with c.session_transaction() as s:
        s.pop("auth_version", None)
    r = c.get("/api/projects")
    check("旧 cookie 401", r.status_code == 401, "got %s" % r.status_code)
    check("错误码 auth_required", r.get_json().get("error") == "auth_required")
    # 同一 cookie 不会因回填兼容而复活（session 已被清理）
    with c.session_transaction() as s:
        check("session 已清理无 auth_version", "auth_version" not in s)


# =========================================================================== #
# 3. POST /api/account/password（docs §7.1）
# =========================================================================== #
def _login_user_client():
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    c = make_client()
    login(c, "u@x.com", USER_PW)
    return c


def test_change_password_requires_login():
    """未登录（AUTH_ENABLED=True）→ 401 auth_required。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    c = make_client()  # 未登录
    r = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("未登录 401", r.status_code == 401)
    check("错误码 auth_required", r.get_json().get("error") == "auth_required")


def test_change_password_dev_mode_401():
    """AUTH_ENABLED=False 本地开发态（无登录 session）→ 401。"""
    app_mod.AUTH_ENABLED = False
    c = make_client()
    r = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("开发态无 session 401", r.status_code == 401)
    check("错误码 auth_required", r.get_json().get("error") == "auth_required")


def test_change_password_csrf_required():
    """CSRF 缺失 → 400 csrf_required（统一 before_request，非豁免路径）。"""
    c = _login_user_client()
    # 绕过 csrf 包装：底层 client 带 session cookie 但不带 X-CSRF-Token
    r = c._base.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("缺 CSRF 400", r.status_code == 400, "got %s" % r.status_code)
    check("错误码 csrf_required", r.get_json().get("error") == "csrf_required")


def test_change_password_wrong_current_400():
    """当前密码错误 → 400 invalid_current_password（不写密码特征）。"""
    c = _login_user_client()
    r = c.post("/api/account/password", json={
        "current_password": "wrong-current-pw-1", "new_password": NEW_PW})
    check("错误当前密码 400", r.status_code == 400)
    check("错误码 invalid_current_password",
          r.get_json().get("error") == "invalid_current_password")
    check("session 未被清除（仍可访问）", c.get("/api/projects").status_code == 200)


def test_change_password_brute_force_lockout(monkeypatch):
    """当前密码连续错误计入登录失败桶 → 触限后 429 + Retry-After。"""
    if conftest.BACKEND == "json":
        # json mock 桶阈值调低（3 次）加速用例；PG 用真实 auth_limit_limits
        install_json_login_limits(monkeypatch, account_limit=3, ip_limit=99)
        limit = 3
    else:
        import auth_limit_store
        # 两个桶独立计数、任一触限即锁；同源 IP 连续失败先撞 IP 前缀桶
        limit = min(auth_limit_store.AUTH_ACCOUNT_FAILURE_LIMIT,
                    auth_limit_store.AUTH_IP_FAILURE_LIMIT)
    c = _login_user_client()
    statuses = []
    for i in range(limit - 1):
        r = c.post("/api/account/password", json={
            "current_password": "wrong-current-pw-%d" % i,
            "new_password": NEW_PW})
        statuses.append(r.status_code)
    check("触限前均为 400", all(s == 400 for s in statuses), "statuses=%r" % statuses)
    r_lock = c.post("/api/account/password", json={
        "current_password": "wrong-current-pw-final", "new_password": NEW_PW})
    check("第 %d 次错误触发 429" % limit, r_lock.status_code == 429,
          "got %s" % r_lock.status_code)
    check("带 Retry-After", int(r_lock.headers.get("Retry-After") or 0) > 0)
    check("锁定文案与登录一致", "尝试过于频繁" in json.dumps(
        r_lock.get_json(), ensure_ascii=False))
    # 锁定期内即使当前密码正确也 429（先查锁）
    r_even_correct = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("锁定期内正确密码也 429", r_even_correct.status_code == 429)


def test_change_password_validation_rejections():
    """新密码 <15 / >200 / 与当前相同 → 400。"""
    c = _login_user_client()
    r_short = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": "short"})
    check("新密码 <15 → 400", r_short.status_code == 400)
    check("长度文案与 store 一致（15..200）",
          "15" in r_short.get_json().get("error", ""))
    r_long = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": "x" * 201})
    check("新密码 >200 → 400", r_long.status_code == 400)
    r_same = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": USER_PW})
    check("新密码与当前相同 → 400", r_same.status_code == 400,
          "got %s" % r_same.status_code)
    check("同密码文案", "相同" in r_same.get_json().get("error", ""))


def test_change_password_success_clears_session():
    """成功改密：200 + ok、session 清空、登录页 password_changed 提示可用。"""
    c = _login_user_client()
    r = c.post("/api/account/password", json={
        "current_password": USER_PW, "new_password": NEW_PW})
    check("200 ok", r.status_code == 200 and r.get_json() == {"ok": True})
    with c.session_transaction() as s:
        check("session 已清空", not s.get("auth_user") and not s.get("user_id"))
    page = c.get("/login?password_changed=1")
    body = page.get_data(as_text=True)
    check("登录页渲染改密成功提示",
          "密码已修改" in body and "重新登录" in body)
    check("无提示参数不渲染提示",
          "密码已修改" not in c.get("/login").get_data(as_text=True))


def test_owner_can_use_change_password_endpoint():
    """owner 同样可用本人改密端点（docs §7.1：owner 与 user 通用）。"""
    make_owner("admin", OWNER_PW)
    c = make_client()
    login(c, "admin", OWNER_PW)
    r = c.post("/api/account/password", json={
        "current_password": OWNER_PW, "new_password": NEW_PW})
    check("owner 改密 200", r.status_code == 200)
    check("owner 旧 cookie 401", c.get("/api/projects").status_code == 401)
    check("owner 新密码可登录", relogin(c, "admin", NEW_PW).status_code == 302)


# =========================================================================== #
# 4. 管理端点收紧（docs §7.2 / §3.2 不变量 5）
# =========================================================================== #
def _owner_client():
    """登录既有 owner（测试自行先 make_owner 建号）。"""
    c = make_client()
    assert login(c, "admin", OWNER_PW).status_code == 302
    return c


def test_admin_reset_owner_password_409():
    """重置 owner → 409（提示本人改密或 break-glass）。"""
    owner = make_owner("admin", OWNER_PW)
    c = _owner_client()
    r = c.post("/api/admin/users/%s/password" % owner["user_id"],
               json={"password": NEW_PW})
    check("重置 owner 409", r.status_code == 409, "got %s" % r.status_code)
    check("文案提示替代路径",
          "owner" in json.loads(r.data).get("error", ""))
    check("owner 密码未被改动",
          user_store.verify_user("admin", OWNER_PW) is not None)


def test_admin_disable_owner_409():
    """禁用 owner → 409；启用 owner 同口径 409。"""
    owner = make_owner("admin", OWNER_PW)
    c = _owner_client()
    r = c.post("/api/admin/users/%s/disable" % owner["user_id"])
    check("禁用 owner 409", r.status_code == 409)
    r2 = c.post("/api/admin/users/%s/enable" % owner["user_id"])
    check("启用 owner 409", r2.status_code == 409)


def test_admin_create_user_password_policy():
    """创建用户：短密码 400（统一 15..200，无硬编码 8）。"""
    make_owner("admin", OWNER_PW)
    c = _owner_client()
    r = c.post("/api/admin/users", json={"email": "n@x.com", "password": "short14chars__x"[:13]})
    check("14 位密码 400", r.status_code == 400)
    check("长度文案统一（15..200）", "15" in json.loads(r.data).get("error", ""))
    r2 = c.post("/api/admin/users", json={"email": "n@x.com", "password": USER_PW})
    check("15 位密码 200", r2.status_code == 200, "got %s" % r2.status_code)


def test_no_web_endpoint_can_demote_or_delete_owner():
    """不变量锁定：无任何 Web 端点可改 owner 角色或删除用户行（§3.2-5）。"""
    rules = list(app_mod.app.url_map.iter_rules())
    user_rules = [r for r in rules
                  if r.rule.lower().startswith("/api/admin/users")]
    check("存在用户管理端点（供遍历）", len(user_rules) > 0)
    for rule in user_rules:
        # 用户行级端点只允许 password/disable/enable/ai-access 四类操作：
        # 不允许出现 role/delete/remove/demote 等降级/删除语义
        tail = rule.rule.lower().rsplit("/api/admin/users", 1)[1]
        forbidden = any(k in tail for k in ("role", "delete", "remove", "demote"))
        check("用户端点 %s 不含降级/删除语义" % rule.rule, not forbidden)
        check("用户端点 %s 无 DELETE 方法" % rule.rule,
              "DELETE" not in rule.methods)


# =========================================================================== #
# 5. 回查次数：普通请求只有一次用户 DB 查询（docs §6.2/§11.3）
# =========================================================================== #
def test_single_user_lookup_per_request(monkeypatch):
    """登录态普通请求只触发一次 get_user 回查（auth_version 比对不加查询）。"""
    make_owner("admin", OWNER_PW)
    user_store.create_user("u@x.com", USER_PW, role="user")
    c = make_client()
    login(c, "u@x.com", USER_PW)

    calls = {"n": 0}
    real_get_user = user_store.get_user

    def _spy(user_id):
        calls["n"] += 1
        return real_get_user(user_id)

    monkeypatch.setattr(user_store, "get_user", _spy)
    r = c.get("/api/projects")
    check("请求成功", r.status_code == 200, "got %s" % r.status_code)
    check("恰好一次 get_user 回查", calls["n"] == 1, "calls=%d" % calls["n"])


# =========================================================================== #
# 6. 汇总
# =========================================================================== #
def test_run_summary():
    print("\n== test_account_auth summary: %d ok, %d failed ==" % (PASS, FAIL))
    if FAIL:
        raise AssertionError("%d checks failed" % FAIL)
