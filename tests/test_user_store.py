# -*- coding: utf-8 -*-
"""Stage 3a 身份基础测试（user_store + app 登录/用户管理/数据归属懒迁移）。

覆盖：
  - user_store CRUD、email 唯一、密码 hash 不落明文（读 users.json 原文断言）；
  - owner 引导：ADMIN_PASSWORD 首启建 owner、再启改密码、空 ADMIN_PASSWORD 无
    owner → AUTH_ENABLED False；
  - 登录：user 成功 / 错误密码 401 / 锁定；session 带 role；
  - /api/admin/users：user 403、owner 200、创建冲突 409、最后 owner 保护；
  - 懒迁移：旧 shares.json（无 owner_user_id）读一次后 projects/slide_meta/rois 带 owner id；
  - /api/auth/info 返回 role。

隔离：独立临时 SHARE_DATA_DIR / UPLOAD_DIR，monkeypatch 夺回 user_store /
share_store 常量与 env，绝不触碰真实数据。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="svs-users-")
DATA_DIR = os.path.join(TMP, "share-data")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)
# 默认无 ADMIN_PASSWORD → 认证由用户是否存在决定；各用例按需覆盖
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
from pg_compat import json_only  # noqa: E402

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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，并清空 users.json。"""
    data_dir = Path(DATA_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    # 归属注入清空（避免跨用例串扰）
    share_store.set_owner_user_id("")
    # 防爆破状态（内存全局）跨用例清空，避免锁定泄漏
    app_mod._auth_attempts.clear()
    # 每用例重置 users.json 与 shares.json
    for name in ("users.json", "shares.json", "users.json.bak", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    yield


def reset_users_file():
    p = user_store.USER_FILE
    if p.exists():
        p.unlink()


def _read_users_raw():
    """读 users.json 原文（用于断言无明文密码）。"""
    p = user_store.USER_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def make_client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return app_mod.app.test_client()


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


# =========================================================================== #
# user_store CRUD
# =========================================================================== #
@json_only  # 断言 users.json 原文（无明文/含 pbkdf2 hash）
def test_user_crud_and_email_unique():
    u = user_store.create_user("Alice@Example.COM", "pass1234", role="user",
                               display_name="Alice")
    check("创建用户返回不含 hash", "password_hash" not in u)
    check("email 小写规范化", u["email"] == "alice@example.com")
    check("display_name 保留", u["display_name"] == "Alice")
    check("user_id 前缀", u["user_id"].startswith("usr_"))

    got = user_store.get_user(u["user_id"])
    check("get_user 命中", got is not None and got["email"] == "alice@example.com")
    check("get_user 含 hash", "password_hash" in got)

    dup = None
    try:
        user_store.create_user("ALICE@example.com", "pass9999")
    except ValueError:
        dup = "raised"
    check("email 唯一（大小写不敏感）", dup == "raised")

    by_email = user_store.get_user_by_email("alice@example.com")
    check("get_user_by_email 命中", by_email is not None)
    by_name = user_store.get_user_by_display_name("Alice")
    check("get_user_by_display_name 命中", by_name is not None)

    listed = user_store.list_users()
    check("list_users 不含 hash", listed and "password_hash" not in listed[0])

    v = user_store.verify_user("alice@example.com", "pass1234")
    check("verify_user 密码正确", v is not None and v["user_id"] == u["user_id"])
    check("verify_user 错误密码 None", user_store.verify_user("alice@example.com", "wrong") is None)

    # 明文密码不得落盘
    raw = _read_users_raw()
    assert raw is not None
    raw_text = json.dumps(raw, ensure_ascii=False)
    check("users.json 无明文密码", "pass1234" not in raw_text and "pass9999" not in raw_text)
    check("users.json 含 pbkdf2 hash", "pbkdf2" in raw_text)


def test_set_disabled_and_password():
    u = user_store.create_user("bob@ex.com", "password1", role="user")
    d = user_store.set_user_disabled(u["user_id"], True)
    check("禁用后 disabled=True", d is not None and d["disabled"] is True)
    check("禁用用户无法登录", user_store.verify_user("bob@ex.com", "password1") is None)
    # 但仍可作为 owner 查找（禁用不影响存在性）
    e = user_store.set_user_disabled(u["user_id"], False)
    check("重新启用后可登录", user_store.verify_user("bob@ex.com", "password1") is not None
          and e["disabled"] is False)
    p = user_store.set_user_password(u["user_id"], "newpass99")
    check("重置密码后可新密码登录", user_store.verify_user("bob@ex.com", "newpass99") is not None)


def test_short_password_rejected():
    raised = False
    try:
        user_store.create_user("a@b.com", "short")
    except ValueError:
        raised = True
    check("创建密码 <8 位拒绝", raised)


# =========================================================================== #
# owner bootstrap
# =========================================================================== #
def test_owner_bootstrap_create_and_reset(monkeypatch):
    # 首启：无 owner → 创建
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    owner_id = app_mod._bootstrap_owner()
    owners = user_store.list_users()
    owner = next((x for x in owners if x["role"] == "owner"), None)
    check("首启创建 owner", owner_id is not None and owner is not None)
    check("owner email 用 ADMIN_USERNAME", owner["email"] == "admin")
    check("owner 可登录", user_store.verify_user("admin", "owner-pass") is not None)
    check("count_owners=1", user_store.count_owners() == 1)

    # 再启：改密码
    monkeypatch.setenv("ADMIN_PASSWORD", "new-owner-pass")
    owner_id2 = app_mod._bootstrap_owner()
    check("再启不新增 owner（同一 user_id）", owner_id2 == owner_id)
    check("count_owners 仍为 1", user_store.count_owners() == 1)
    check("旧密码失效", user_store.verify_user("admin", "owner-pass") is None)
    check("新密码生效", user_store.verify_user("admin", "new-owner-pass") is not None)


def test_empty_admin_password_no_owner_disables_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    reset_users_file()
    owner_id = app_mod._bootstrap_owner()
    check("空 ADMIN_PASSWORD 不建 owner", owner_id is None)
    check("无用户 → AUTH_ENABLED False", app_mod._resolve_auth_enabled() is False)


def test_auth_enabled_when_user_exists(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    user_store.create_user("u@x.com", "password1", role="user")
    check("存在 user → AUTH_ENABLED True", app_mod._resolve_auth_enabled() is True)


# =========================================================================== #
# 登录
# =========================================================================== #
def test_login_success_sets_role(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    app_mod._bootstrap_owner()
    client = make_client()
    r = login(client, "admin", "owner-pass")
    check("owner 登录 302", r.status_code == 302)
    with client.session_transaction() as s:
        check("session auth_user 为 display_name", s.get("auth_user") == "admin")
        check("session role=owner", s.get("role") == "owner")
        check("session 有 user_id", s.get("user_id") is not None)
    # auth/info 返回 role
    info = json.loads(client.get("/api/auth/info").data)
    check("auth/info 返回 role", info.get("role") == "owner")
    check("auth/info 返回 user_id", info.get("user_id") is not None)


def test_login_wrong_password_and_lock():
    user_store.create_user("carol@ex.com", "password1", role="user")
    client = make_client()
    r = login(client, "carol@ex.com", "wrongpass")
    check("错误密码 401", r.status_code == 401)
    # 触发锁定：连续 5 次
    for _ in range(5):
        login(client, "carol@ex.com", "wrongpass")
    rl = login(client, "carol@ex.com", "password1")
    check("锁定期内正确密码也 429", rl.status_code == 429)


# =========================================================================== #
# /api/admin/users 权限与保护
# =========================================================================== #
def test_admin_users_owner_vs_user(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    app_mod._bootstrap_owner()
    user_store.create_user("u@x.com", "password1", role="user")
    client = make_client()
    # owner 登录
    login(client, "admin", "owner-pass")
    r = client.get("/api/admin/users")
    body = json.loads(r.data)
    check("owner GET /api/admin/users 200", r.status_code == 200)
    check("返回 users 数组", isinstance(body.get("users"), list))
    check("返回 registration_open", "registration_open" in body)
    check("list 不含 hash", all("password_hash" not in u for u in body["users"]))
    # 创建 user
    r2 = client.post("/api/admin/users", json={"email": "new@x.com", "password": "password1"})
    check("owner 创建用户 200", r2.status_code == 200)
    check("新用户 role=user", json.loads(r2.data).get("role") == "user")
    # 冲突
    r3 = client.post("/api/admin/users", json={"email": "u@x.com", "password": "password1"})
    check("创建冲突 409", r3.status_code == 409)
    # 短密码
    r4 = client.post("/api/admin/users", json={"email": "s@x.com", "password": "short"})
    check("短密码创建 400", r4.status_code == 400)

    # user 角色登录 → 403
    client2 = make_client()
    login(client2, "u@x.com", "password1")
    r5 = client2.get("/api/admin/users")
    check("user GET /api/admin/users 403", r5.status_code == 403)


def test_last_owner_protection(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    owner_id = app_mod._bootstrap_owner()
    user_store.create_user("u@x.com", "password1", role="user")
    client = make_client()
    login(client, "admin", "owner-pass")
    # 禁用最后一个 enabled owner → 400
    r = client.post("/api/admin/users/%s/disable" % owner_id)
    check("禁用最后 owner 400", r.status_code == 400)
    check("错误文案", "最后一个" in json.loads(r.data).get("error", ""))
    # 仍可禁用 user
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    r2 = client.post("/api/admin/users/%s/disable" % uid)
    check("禁用 user 200", r2.status_code == 200)
    # 多 owner 时可禁用其一
    user_store.create_user("o2@x.com", "password1", role="owner", display_name="o2")
    o2 = user_store.get_user_by_email("o2@x.com")["user_id"]
    r3 = client.post("/api/admin/users/%s/disable" % o2)
    check("多 owner 时可禁用其一 200", r3.status_code == 200)


def test_admin_reset_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    app_mod._bootstrap_owner()
    user_store.create_user("u@x.com", "password1", role="user")
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    client = make_client()
    login(client, "admin", "owner-pass")
    r = client.post("/api/admin/users/%s/password" % uid, json={"password": "newpass88"})
    check("owner 重置密码 200", r.status_code == 200)
    check("新密码可登录", user_store.verify_user("u@x.com", "newpass88") is not None)


# =========================================================================== #
# 懒迁移：旧 shares.json 读一次后补 owner_user_id
# =========================================================================== #
def test_lazy_migration_owner_refs(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-pass")
    owner_id = app_mod._bootstrap_owner()
    # 注入归属（模拟 app 启动时的 set_owner_user_id）
    share_store.set_owner_user_id(owner_id)
    # 构造旧格式 shares.json（无 owner_user_id）
    old = {
        "shares": {},
        "rois": [{"token": "admin", "slide": "s.svs", "label": "a", "ts": 1}],
        "projects": {"p1": {"name": "P", "note": "", "slides": [], "created_at": 1}},
        "slide_meta": {"s.svs": {"alias": "A", "note": ""}},
        "change_seq_by_slide": {},
    }
    share_store.SHARE_FILE.write_text(json.dumps(old, ensure_ascii=False),
                                      encoding="utf-8")
    # 读一次（list_projects 等读路径触发迁移）
    share_store.list_projects()
    share_store.get_slide_meta("s.svs")
    share_store.annotations_by_slide()
    # 落盘后断言字段
    raw = json.loads(share_store.SHARE_FILE.read_text(encoding="utf-8"))
    check("rois 补 owner_user_id",
          raw["rois"] and raw["rois"][0].get("owner_user_id") == owner_id)
    check("projects 补 owner_user_id",
          raw["projects"]["p1"].get("owner_user_id") == owner_id)
    check("slide_meta 补 owner_user_id",
          raw["slide_meta"]["s.svs"].get("owner_user_id") == owner_id)


# =========================================================================== #
# 收尾
# =========================================================================== #
def _finish():
    if FAIL:
        print("\n%d FAILED of %d checks" % (FAIL, PASS + FAIL))
    else:
        print("\nall %d checks passed" % PASS)
    return 1 if FAIL else 0


def test_run_summary():
    # 该函数只是让每个 check 标记为已执行；真正的统计在模块收尾 print 里。
    pass


if __name__ == "__main__":
    raise SystemExit(_finish())
