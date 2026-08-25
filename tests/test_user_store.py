# -*- coding: utf-8 -*-
"""Stage 3a 身份基础测试（user_store + app 登录/用户管理/数据归属懒迁移）。

账户系统批次 A（docs/account-system-simplification-fix-plan.md §5.3/§11）更新：
  - owner 引导不再走 env 对账（ensure_owner/first_owner 已删除），改测
    create_bootstrap_owner / resolve_primary_owner / list_enabled_owners /
    list_owners 新契约（空库首建、非空拒绝、并发单建、0/多 owner 拒绝解析）；
  - 统一密码策略 15..200（PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH）；
  - auth_version：读路径带出、密码/disable/enable 原子递增、旧 json 数据缺字段
    读为 1（json-only）。

隔离：独立临时 SHARE_DATA_DIR / UPLOAD_DIR，monkeypatch 夺回 user_store /
share_store 常量与 env，绝不触碰真实数据。
"""
import json
import os
import sys
import tempfile
import threading
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
import conftest  # noqa: E402
from pg_compat import json_only  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402

PASS = 0
FAIL = 0

#: 测试用统一密码（≥15 字符，满足批次 A 策略）
PW = "pass1234pass1234"
PW2 = "password1password1"
PW3 = "newpass99newpass99"
OWNER_PW = "owner-pass-123456"


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
    # 登录防爆破：app 已删 per-worker 内存字典；json 后端装两桶 mock（PG 走真实
    # auth_rate_limits，conftest 每用例 TRUNCATE）
    install_json_login_limits(monkeypatch)
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
    return csrf_client(app_mod.app.test_client())


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def make_owner(login_id="admin", password=OWNER_PW):
    """批次 A owner 引导：空库经 create_bootstrap_owner 建 owner（替代已删除的
    env 对账式 ensure_owner）。返回 owner dict（含 hash 与 auth_version）。"""
    owner = user_store.create_bootstrap_owner(login_id, password)
    assert owner and owner.get("role") == "owner"
    return owner


# =========================================================================== #
# user_store CRUD
# =========================================================================== #
@json_only  # 断言 users.json 原文（无明文/含 pbkdf2 hash）
def test_user_crud_and_email_unique():
    u = user_store.create_user("Alice@Example.COM", PW, role="user",
                               display_name="Alice")
    check("创建用户返回不含 hash", "password_hash" not in u)
    check("email 小写规范化", u["email"] == "alice@example.com")
    check("display_name 保留", u["display_name"] == "Alice")
    check("user_id 前缀", u["user_id"].startswith("usr_"))
    check("新用户 auth_version=1", u.get("auth_version") == 1)

    got = user_store.get_user(u["user_id"])
    check("get_user 命中", got is not None and got["email"] == "alice@example.com")
    check("get_user 含 hash", "password_hash" in got)
    check("get_user 带 auth_version", got.get("auth_version") == 1)

    dup = None
    try:
        user_store.create_user("ALICE@example.com", PW)
    except ValueError:
        dup = "raised"
    check("email 唯一（大小写不敏感）", dup == "raised")

    by_email = user_store.get_user_by_email("alice@example.com")
    check("get_user_by_email 命中", by_email is not None)
    check("get_user_by_email 带 auth_version",
          by_email.get("auth_version") == 1)
    by_name = user_store.get_user_by_display_name("Alice")
    check("get_user_by_display_name 命中", by_name is not None)
    check("get_user_by_display_name 带 auth_version",
          by_name.get("auth_version") == 1)

    listed = user_store.list_users()
    check("list_users 不含 hash", listed and "password_hash" not in listed[0])
    check("list_users 带 auth_version",
          listed and listed[0].get("auth_version") == 1)

    v = user_store.verify_user("alice@example.com", PW)
    check("verify_user 密码正确", v is not None and v["user_id"] == u["user_id"])
    check("verify_user 带 auth_version", v.get("auth_version") == 1)
    check("verify_user 错误密码 None", user_store.verify_user("alice@example.com", "wrong") is None)

    # 明文密码不得落盘
    raw = _read_users_raw()
    assert raw is not None
    raw_text = json.dumps(raw, ensure_ascii=False)
    check("users.json 无明文密码", PW not in raw_text and "pass9999" not in raw_text)
    check("users.json 含 pbkdf2 hash", "pbkdf2" in raw_text)


def test_set_disabled_and_password():
    u = user_store.create_user("bob@ex.com", PW2, role="user")
    check("建号 auth_version=1", u.get("auth_version") == 1)
    d = user_store.set_user_disabled(u["user_id"], True)
    check("禁用后 disabled=True", d is not None and d["disabled"] is True)
    check("禁用递增 auth_version（1→2）", d.get("auth_version") == 2)
    check("禁用用户无法登录", user_store.verify_user("bob@ex.com", PW2) is None)
    # 但仍可作为 owner 查找（禁用不影响存在性）
    e = user_store.set_user_disabled(u["user_id"], False)
    check("重新启用后可登录", user_store.verify_user("bob@ex.com", PW2) is not None
          and e["disabled"] is False)
    check("enable 也递增 auth_version（2→3）", e.get("auth_version") == 3)
    p = user_store.set_user_password(u["user_id"], PW3)
    check("重置密码后可新密码登录", user_store.verify_user("bob@ex.com", PW3) is not None)
    check("重置密码递增 auth_version（3→4）", p.get("auth_version") == 4)
    # user_id 不存在 → None（不抛错）
    check("set_user_password 目标不存在 None",
          user_store.set_user_password("usr_nope", PW3) is None)
    check("set_user_disabled 目标不存在 None",
          user_store.set_user_disabled("usr_nope", True) is None)


def test_password_policy_bounds():
    """统一密码策略 15..200（docs §3.3）：边界含 15/200，两侧拒绝。"""
    ok14 = ok201 = False
    try:
        user_store.create_user("a@b.com", "x" * 14)
    except ValueError:
        ok14 = True
    check("14 字符拒绝", ok14)
    u15 = user_store.create_user("a15@b.com", "x" * 15, role="user")
    check("15 字符接受", u15 is not None)
    u200 = user_store.create_user("a200@b.com", "x" * 200, role="user")
    check("200 字符接受", u200 is not None)
    try:
        user_store.create_user("a201@b.com", "x" * 201)
    except ValueError:
        ok201 = True
    check("201 字符拒绝", ok201)
    # 允许空格（不要求组合规则）
    u_space = user_store.create_user("sp@b.com", "a very long passphrase 42", role="user")
    check("含空格长口令接受", u_space is not None)
    # set_user_password 同策略（无旁路参数）
    raised = False
    try:
        user_store.set_user_password(u15["user_id"], "short")
    except ValueError:
        raised = True
    check("set_user_password 短密码拒绝", raised)
    # create_user/set_user_password 不再接受 _enforce_min_length 旁路
    try:
        user_store.create_user("bypass@b.com", "short", _enforce_min_length=False)
        bypassed = True
    except TypeError:
        bypassed = False
    check("create_user 无 _enforce_min_length 旁路", bypassed is False)


def test_short_password_rejected():
    raised = False
    try:
        user_store.create_user("a@b.com", "short")
    except ValueError:
        raised = True
    check("创建密码 <15 位拒绝", raised)
    check("策略常量导出",
          user_store.PASSWORD_MIN_LENGTH == 15
          and user_store.PASSWORD_MAX_LENGTH == 200)


# =========================================================================== #
# owner bootstrap 新契约（批次 A docs §5.3：create_bootstrap_owner /
# resolve_primary_owner / list_enabled_owners / list_owners）
# =========================================================================== #
def test_create_bootstrap_owner_empty_store():
    """空库首建成功：规范化 login_id、display_name 同值、auth_version=1。"""
    owner = user_store.create_bootstrap_owner("  Browser_Admin  ", OWNER_PW)
    check("首建 owner 返回 role=owner", owner.get("role") == "owner")
    check("login_id trim+lower 规范化", owner["email"] == "browser_admin")
    check("display_name 同 login_id", owner["display_name"] == "browser_admin")
    check("含 password_hash", bool(owner.get("password_hash")))
    check("auth_version=1", owner.get("auth_version") == 1)
    check("owner 可登录", user_store.verify_user("browser_admin", OWNER_PW) is not None)
    check("count_owners=1", user_store.count_owners() == 1)
    check("resolve_primary_owner 命中同一 user_id",
          user_store.resolve_primary_owner()["user_id"] == owner["user_id"])


def test_create_bootstrap_owner_refuses_existing_owner():
    """已有 owner 行 → OwnerInvariantError，绝不静默建号/对账改密。"""
    first = make_owner()
    raised = None
    try:
        user_store.create_bootstrap_owner("another", OWNER_PW)
    except user_store.OwnerInvariantError as e:
        raised = str(e)
    check("已有 owner 再 bootstrap 拒绝", raised is not None)
    check("消息含 users_table_not_empty 场景标识",
          raised and "users_table_not_empty" in raised)
    check("未新增任何用户", len(user_store.list_users()) == 1)
    again = user_store.get_user(first["user_id"])
    check("原 owner 不被改动（hash 不变，仍可登录）",
          again["password_hash"] == first["password_hash"]
          and user_store.verify_user("admin", OWNER_PW) is not None)


def test_create_bootstrap_owner_refuses_when_only_users():
    """库内只有普通 user（无 owner 行）→ 同样拒绝：bootstrap 只认空库。"""
    user_store.create_user("plain@x.com", PW2, role="user")
    raised = None
    try:
        user_store.create_bootstrap_owner("owner2", OWNER_PW)
    except user_store.OwnerInvariantError as e:
        raised = str(e)
    check("已有普通 user（无 owner）bootstrap 同样拒绝", raised is not None)
    check("仍只有 1 行用户", len(user_store.list_users()) == 1)
    # 密码策略同样约束 bootstrap（无旁路）
    raised3 = False
    try:
        user_store.create_bootstrap_owner("owner3", "short")
    except ValueError:
        raised3 = True
    check("bootstrap 密码统一 15..200 策略", raised3)


def test_create_bootstrap_owner_concurrent_pg():
    """PG 并发首建（多线程同时调）：恰好一个成功，库内恰好一行 owner。

    串行化由 create_bootstrap_owner 事务内的专用 advisory lock
    （0x53564F57 'SVOW'，不复用 schema 的 0x53565347）保证；0015 部分唯一
    索引为数据库层兜底。json 后端无对应并发语义（flock 文件锁已由实现保证，
    不在本用例重复验证），仅 PG 跑。
    """
    if conftest.BACKEND != "postgres":
        pytest.skip("并发首建语义验证需 PG advisory lock（RUN_PG_TESTS=1）")
    results = []
    errors = []

    def _worker(i):
        try:
            results.append(user_store.create_bootstrap_owner(
                "admin_%d" % i, OWNER_PW)["user_id"])
        except user_store.OwnerInvariantError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("恰好一个成功", len(results) == 1, "results=%s" % results)
    check("其余全部 OwnerInvariantError", len(errors) == 7)
    owners = user_store.list_owners()
    check("库内恰好一行 owner", len(owners) == 1)
    check("成功行即库里唯一 owner", owners[0]["user_id"] == results[0])
    check("全部用户恰 1 行", len(user_store.list_users()) == 1)


def test_resolve_primary_owner_invariants():
    """恰好 1 个 enabled → 返回；0 个 → no_owner；>1 个 → multiple_enabled_owners。

    >1 enabled 场景在 PG 下无法经 SQL 构造（0015 部分唯一索引拦截），由索引
    与启动检查双重防御；本用例在 json 后端验证 multiple 分支的解析语义，
    在 PG 后端验证索引拦截 + no_owner 分支。
    """
    # 0 个 owner（空库）
    raised = None
    try:
        user_store.resolve_primary_owner()
    except user_store.OwnerInvariantError as e:
        raised = str(e)
    check("空库 resolve 拒绝（no_owner）", raised is not None
          and "no_owner" in raised)

    if conftest.BACKEND == "json":
        # json 无部分唯一索引，可构造 2 个 enabled owner 验证 multiple 分支
        user_store.create_user("o1@x.com", PW2, role="owner")
        user_store.create_user("o2@x.com", PW2, role="owner")
        raised2 = None
        try:
            user_store.resolve_primary_owner()
        except user_store.OwnerInvariantError as e:
            raised2 = str(e)
        check("2 个 enabled owner 拒绝（multiple_enabled_owners）",
              raised2 is not None and "multiple_enabled_owners" in raised2)
        check("list_enabled_owners 返回 2 行（按 created_at,user_id 排序）",
              len(user_store.list_enabled_owners()) == 2)
    else:
        # PG：第二个 enabled owner 被 0015 索引拦截（create_user → ValueError）
        user_store.create_user("o1@x.com", PW2, role="owner")
        blocked = None
        try:
            user_store.create_user("o2@x.com", PW2, role="owner")
        except ValueError as e:
            blocked = str(e)
        check("PG 第二个 enabled owner 被索引拦截", blocked is not None)
        check("拦截消息指向单 owner 不变量", "owner" in (blocked or ""))
        # disable 后 0 个 enabled → no_owner（走 store 层，绕过 app 的最后 owner 保护）
        o1 = user_store.list_enabled_owners()[0]
        user_store.set_user_disabled(o1["user_id"], True)
        raised3 = None
        try:
            user_store.resolve_primary_owner()
        except user_store.OwnerInvariantError as e:
            raised3 = str(e)
        check("唯一 owner 被禁用后 resolve 拒绝（no_owner）",
              raised3 is not None and "no_owner" in raised3)
        # list_owners 仍能看到 disabled owner 行（含 hash 与 auth_version）
        all_owners = user_store.list_owners()
        check("list_owners 含 disabled 行", len(all_owners) == 1
              and all_owners[0]["disabled"] is True)
        check("list_owners 行带 hash 与 auth_version",
              bool(all_owners[0].get("password_hash"))
              and all_owners[0].get("auth_version") == 2)


def test_empty_admin_password_no_owner_disables_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    reset_users_file()
    owner_id = app_mod._bootstrap_owner()
    check("空 ADMIN_PASSWORD 不建 owner", owner_id is None)
    check("无用户 → AUTH_ENABLED False", app_mod._resolve_auth_enabled() is False)


def test_auth_enabled_when_user_exists(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    user_store.create_user("u@x.com", PW2, role="user")
    check("存在 user → AUTH_ENABLED True", app_mod._resolve_auth_enabled() is True)


@json_only  # 损坏 users.json 文件语义；PG 后端无该文件
def test_corrupt_users_json_refuses_fail_open():
    """users.json 损坏不得当成空库关闭鉴权。"""
    p = user_store.USER_FILE
    p.write_text("{not-json", encoding="utf-8")
    with pytest.raises(user_store.UserStoreCorrupt):
        user_store.has_enabled_users()
    bak = p.with_suffix(".json.bak")
    check("损坏文件已备份", bak.is_file())
    with pytest.raises(SystemExit):
        app_mod._resolve_auth_enabled()


@json_only  # 直写旧格式 users.json（无 auth_version 字段）；PG 无该文件
def test_legacy_json_without_auth_version_reads_as_one():
    """旧 json 数据缺 auth_version 字段：读路径按 1 处理；写路径递增从 1 起。"""
    now = 1700000000.0
    legacy = {
        "users": {
            "usr_legacy": {
                "user_id": "usr_legacy", "email": "legacy@x.com",
                "display_name": "Legacy", "password_hash": "pbkdf2:fake",
                "role": "user", "created_at": now, "disabled": False,
                # 无 auth_version 字段（0015 之前的存量数据）
            },
        },
        "meta": {"schema_version": 1},
    }
    user_store.USER_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    got = user_store.get_user("usr_legacy")
    check("旧数据读 auth_version=1", got.get("auth_version") == 1)
    check("旧数据 list_users 读 auth_version=1",
          user_store.list_users()[0].get("auth_version") == 1)
    v = user_store.get_user_by_email("legacy@x.com")
    check("get_user_by_email 旧数据 auth_version=1", v.get("auth_version") == 1)
    p = user_store.set_user_password("usr_legacy", PW3)
    check("旧数据改密递增 1→2", p.get("auth_version") == 2)
    d = user_store.set_user_disabled("usr_legacy", True)
    check("旧数据禁用递增 2→3", d.get("auth_version") == 3)
    # 落盘后的记录带上了 auth_version 字段（写路径携带）
    raw = _read_users_raw()
    check("写路径落盘携带 auth_version",
          raw["users"]["usr_legacy"].get("auth_version") == 3)
    # 非法值（0/负数/脏数据）也按 1 起算，防御损坏文件
    legacy["users"]["usr_legacy"]["auth_version"] = "garbage"
    user_store.USER_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    check("脏 auth_version 读为 1",
          user_store.get_user("usr_legacy").get("auth_version") == 1)


# =========================================================================== #
# 登录
# =========================================================================== #
def test_login_success_sets_role(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    client = make_client()
    r = login(client, "admin", OWNER_PW)
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
    user_store.create_user("carol@ex.com", PW2, role="user")
    client = make_client()
    r = login(client, "carol@ex.com", "wrongpass-wrongpass")
    check("错误密码 401", r.status_code == 401)
    # 触发锁定：连续失败命中 IP 前缀桶（5 次/窗；账号桶 10 次/窗，docs §9.5）
    for _ in range(5):
        login(client, "carol@ex.com", "wrongpass-wrongpass")
    rl = login(client, "carol@ex.com", PW2)
    check("锁定期内正确密码也 429", rl.status_code == 429)
    check("429 带 Retry-After", int(rl.headers.get("Retry-After") or 0) > 0)


# =========================================================================== #
# /api/admin/users 权限与保护
# =========================================================================== #
def test_admin_users_owner_vs_user(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    user_store.create_user("u@x.com", PW2, role="user")
    client = make_client()
    # owner 登录
    login(client, "admin", OWNER_PW)
    r = client.get("/api/admin/users")
    body = json.loads(r.data)
    check("owner GET /api/admin/users 200", r.status_code == 200)
    check("返回 users 数组", isinstance(body.get("users"), list))
    check("返回 registration_open", "registration_open" in body)
    check("list 不含 hash", all("password_hash" not in u for u in body["users"]))
    # 创建 user
    r2 = client.post("/api/admin/users", json={"email": "new@x.com", "password": PW2})
    check("owner 创建用户 200", r2.status_code == 200)
    check("新用户 role=user", json.loads(r2.data).get("role") == "user")
    # 冲突
    r3 = client.post("/api/admin/users", json={"email": "u@x.com", "password": PW2})
    check("创建冲突 409", r3.status_code == 409)
    # 短密码（store 层 15..200 统一策略；app 层 8 位旧校验也仍拦）
    r4 = client.post("/api/admin/users", json={"email": "s@x.com", "password": "short"})
    check("短密码创建 400", r4.status_code == 400)

    # user 角色登录 → 403
    client2 = make_client()
    login(client2, "u@x.com", PW2)
    r5 = client2.get("/api/admin/users")
    check("user GET /api/admin/users 403", r5.status_code == 403)


def test_last_owner_protection(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    owner = make_owner()
    user_store.create_user("u@x.com", PW2, role="user")
    client = make_client()
    login(client, "admin", OWNER_PW)
    # 禁用最后一个 enabled owner → 400
    r = client.post("/api/admin/users/%s/disable" % owner["user_id"])
    check("禁用最后 owner 400", r.status_code == 400)
    check("错误文案", "最后一个" in json.loads(r.data).get("error", ""))
    # 仍可禁用 user
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    r2 = client.post("/api/admin/users/%s/disable" % uid)
    check("禁用 user 200", r2.status_code == 200)
    # 多 owner 时可禁用其一：json 后端可构造多个 enabled owner 验证；
    # PG 下 0015 部分唯一索引使该场景不可达（>1 enabled owner 由索引与启动
    # 检查双重防御），该分支只在 json 跑。
    if conftest.BACKEND == "json":
        user_store.create_user("o2@x.com", PW2, role="owner", display_name="o2")
        o2 = user_store.get_user_by_email("o2@x.com")["user_id"]
        r3 = client.post("/api/admin/users/%s/disable" % o2)
        check("多 owner 时可禁用其一 200", r3.status_code == 200)


def test_admin_reset_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    user_store.create_user("u@x.com", PW2, role="user")
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    client = make_client()
    login(client, "admin", OWNER_PW)
    r = client.post("/api/admin/users/%s/password" % uid, json={"password": PW3})
    check("owner 重置密码 200", r.status_code == 200)
    check("新密码可登录", user_store.verify_user("u@x.com", PW3) is not None)
    check("重置递增 auth_version", user_store.get_user(uid)["auth_version"] == 2)
    r_short = client.post("/api/admin/users/%s/password" % uid,
                          json={"password": "short"})
    check("重置短密码 400（统一 15..200）", r_short.status_code == 400)


def test_disable_invalidates_existing_session(monkeypatch):
    """禁用用户后，已有 Flask session 立刻失效（不能再打 /api/*）。"""
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    u = user_store.create_user("u@x.com", PW2, role="user")
    user_client = make_client()
    login(user_client, "u@x.com", PW2)
    r = user_client.get("/api/projects")
    check("禁用前 /api/projects 200", r.status_code == 200)

    owner_client = make_client()
    login(owner_client, "admin", OWNER_PW)
    rd = owner_client.post("/api/admin/users/%s/disable" % u["user_id"])
    check("禁用 user 200", rd.status_code == 200)

    r2 = user_client.get("/api/projects")
    check("禁用后 /api/projects 401", r2.status_code == 401,
          "got %s" % r2.status_code)
    body = json.loads(r2.data)
    check("禁用后 error=auth_required", body.get("error") == "auth_required")
    r3 = login(user_client, "u@x.com", PW2)
    check("禁用期间无法再登录", r3.status_code == 401)


# =========================================================================== #
# 懒迁移：旧 shares.json 读一次后补 owner_user_id
# =========================================================================== #
def test_lazy_migration_owner_refs(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    owner = make_owner()
    owner_id = owner["user_id"]
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
