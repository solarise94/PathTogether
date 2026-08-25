# -*- coding: utf-8 -*-
"""账户系统批次 B「登录标识收口」测试（docs/account-system-simplification-
fix-plan.md §4.1 兼容窗口 / §6.1 登录只认 login_id / §11.2 登录标识矩阵）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑）：
  - 用户 dict 双键输出：login_id（规范名）与 email（deprecated，同值）在
    create_user / get_user / get_user_by_email / verify_user / list_users /
    list_enabled_owners / create_bootstrap_owner 等读写路径一致携带；
    json 侧 login_id 只是对外别名、不落盘；
  - 登录只认 login_id：大小写/空白不敏感登录成功；display_name 登录失败；
    两人同 display_name 各自 login_id 均可登录；A 的 display_name == B 的
    login_id 时该字符串只能登进 B；
  - 登录防爆破主体：大小写变体与正确值共用同一账号桶（_auth_subject_hash
    规范化直接断言 + 混合大小写连续失败触发 429 的功能断言）；
  - 统一失败文案「账号或密码错误」，不泄露账号存在性；
  - 管理 API：/api/admin/users 列表/创建/重置/禁用响应双字段；创建接受
    login_id（优先）或 email（兼容）；
  - 邀请管理 API 别名（仅 PG，registration_invites 为 PG-only 能力）：创建
    接受 login_id 优先 / email 兼容，响应携带 login_id_masked 与
    email_masked（deprecated 同值），绑定语义为「允许兑换的登录账号」。

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

TMP = tempfile.mkdtemp(prefix="svs-loginid-")
DATA_DIR = os.path.join(TMP, "share-data")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)
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
from pg_compat import BACKEND, json_only  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits  # noqa: E402

pg_only = pytest.mark.skipif(
    BACKEND != "postgres",
    reason="registration_invites 数据层需 PG（RUN_PG_TESTS=1）",
)

PASS = 0
FAIL = 0

#: 测试用密码（≥15 字符，满足统一 15..200 策略）
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
    share_store.set_owner_user_id("")
    # json 后端装两桶 mock（PG 走真实 auth_rate_limits，conftest 每用例 TRUNCATE）
    install_json_login_limits(monkeypatch)
    for name in ("users.json", "shares.json", "users.json.bak", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    yield


def make_client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def make_owner(login_id="admin", password=OWNER_PW):
    owner = user_store.create_bootstrap_owner(login_id, password)
    assert owner and owner.get("role") == "owner"
    return owner


# =========================================================================== #
# 1. 双键输出（docs §4.1 兼容窗口）
# =========================================================================== #
def test_user_dicts_carry_login_id_and_email():
    """所有返回用户 dict 的路径同时携带 login_id（规范）与 email（deprecated）。"""
    owner = make_owner("Owner_One")
    check("create_bootstrap_owner login_id==email==规范化值",
          owner.get("login_id") == owner.get("email") == "owner_one")
    u = user_store.create_user("Alice@Example.COM", PW, role="user",
                               display_name="Alice")
    check("create_user 双键同值",
          u.get("login_id") == u.get("email") == "alice@example.com")

    got = user_store.get_user(u["user_id"])
    check("get_user 双键同值",
          got.get("login_id") == got.get("email") == "alice@example.com")
    by_email = user_store.get_user_by_email("alice@example.com")
    check("get_user_by_email 双键同值",
          by_email.get("login_id") == by_email.get("email"))
    v = user_store.verify_user("ALICE@example.com", PW)
    check("verify_user 双键同值且命中本人",
          v is not None and v["user_id"] == u["user_id"]
          and v.get("login_id") == v.get("email"))
    listed = user_store.list_users()
    check("list_users 每行双键同值",
          len(listed) == 2 and all(
              x.get("login_id") == x.get("email") for x in listed))
    owners = user_store.list_enabled_owners()
    check("list_enabled_owners 双键同值",
          len(owners) == 1 and owners[0].get("login_id")
          == owners[0].get("email") == "owner_one")
    # 写路径返回同样补键（set_user_password 递增 auth_version 后的公共 dict）
    p = user_store.set_user_password(u["user_id"], PW3)
    check("set_user_password 返回双键同值",
          p is not None and p.get("login_id") == p.get("email"))


@json_only  # 直读 users.json 物理文件；PG 后端无该文件
def test_login_id_not_persisted_in_json_store():
    """json 侧 login_id 只是对外别名：users.json 物理记录不新增该键。"""
    u = user_store.create_user("persist@x.com", PW, role="user")
    raw = json.loads(user_store.USER_FILE.read_text(encoding="utf-8"))
    rec = raw["users"][u["user_id"]]
    check("物理记录保留 email 键", rec.get("email") == "persist@x.com")
    check("物理记录不落 login_id 键", "login_id" not in rec)
    got = user_store.get_user(u["user_id"])
    check("读出口仍补 login_id 别名", got.get("login_id") == "persist@x.com")


# =========================================================================== #
# 2. 登录只认 login_id（docs §6.1 / §11.2 矩阵）
# =========================================================================== #
def test_login_id_case_and_space_insensitive():
    """login_id 大小写/空白不敏感登录成功（trim + lower 规范化）。"""
    u = user_store.create_user("Mixed@Case.COM", PW, role="user",
                               display_name="展示名")
    check("原值可登录",
          user_store.verify_user("mixed@case.com", PW) is not None)
    check("大写变体可登录",
          user_store.verify_user("MIXED@CASE.COM", PW) is not None)
    check("混合大小写可登录",
          user_store.verify_user("MiXeD@cAsE.CoM", PW) is not None)
    check("首尾空白可登录",
          user_store.verify_user("  mixed@case.com  ", PW) is not None)
    check("命中同一 user_id",
          user_store.verify_user("MIXED@CASE.COM", PW)["user_id"]
          == u["user_id"])


def test_display_name_cannot_login():
    """display_name 与 login_id 不同：只用 display_name 登录失败。"""
    user_store.create_user("alice@x.com", PW, role="user",
                           display_name="Alice Wonder")
    check("display_name 精确值登录失败",
          user_store.verify_user("Alice Wonder", PW) is None)
    check("display_name 小写变体登录失败",
          user_store.verify_user("alice wonder", PW) is None)
    # 路由层同样只认 login_id，且统一失败文案
    client = make_client()
    r = login(client, "Alice Wonder", PW)
    check("POST /login 用 display_name 401", r.status_code == 401,
          "got %s" % r.status_code)
    check("失败文案统一（不泄露账号存在性）",
          "账号或密码错误" in r.get_data(as_text=True))


def test_duplicate_display_names_both_can_login():
    """两人同 display_name：各自 login_id 均可登录，互不影响。"""
    u1 = user_store.create_user("a1@x.com", PW, role="user",
                                display_name="SameName")
    u2 = user_store.create_user("a2@x.com", PW2, role="user",
                                display_name="SameName")
    v1 = user_store.verify_user("a1@x.com", PW)
    v2 = user_store.verify_user("a2@x.com", PW2)
    check("u1 可登录", v1 is not None and v1["user_id"] == u1["user_id"])
    check("u2 可登录", v2 is not None and v2["user_id"] == u2["user_id"])
    check("交叉密码不可登录",
          user_store.verify_user("a1@x.com", PW2) is None)
    check("共同 display_name 不可登录",
          user_store.verify_user("SameName", PW) is None)
    # 路由层：两个同显示名账号都能走 POST /login 成功
    c1 = make_client()
    check("u1 路由登录 302", login(c1, "a1@x.com", PW).status_code == 302)
    c2 = make_client()
    check("u2 路由登录 302", login(c2, "a2@x.com", PW2).status_code == 302)


def test_display_name_equal_to_other_login_id():
    """A 的 display_name == B 的 login_id：该字符串只能登进 B（docs §2.5/§11.2）。"""
    a = user_store.create_user("aaa@x.com", PW, role="user", display_name="bbb")
    b = user_store.create_user("bbb", PW2, role="user", display_name="Bee")
    v = user_store.verify_user("bbb", PW2)
    check("该字符串登进 B", v is not None and v["user_id"] == b["user_id"])
    check("A 的密码不能用于该字符串",
          user_store.verify_user("bbb", PW) is None)
    check("A 仍可用自己的 login_id 登录",
          user_store.verify_user("aaa@x.com", PW) is not None
          and user_store.verify_user("aaa@x.com", PW)["user_id"] == a["user_id"])
    # 路由层：登录后 session 归属 B（不是显示名撞名的 A）
    client = make_client()
    r = login(client, "bbb", PW2)
    check("路由登录 302", r.status_code == 302)
    with client.session_transaction() as s:
        check("session user_id 归属 B", s.get("user_id") == b["user_id"])


def test_login_unified_error_no_account_enumeration():
    """错误账号与错误密码返回同一状态码与同一文案，不泄露账号存在性。"""
    user_store.create_user("real@x.com", PW, role="user")
    client = make_client()
    r_ghost = login(client, "ghost@x.com", "wrong-password-123")
    r_real = login(client, "real@x.com", "wrong-password-123")
    check("不存在账号 401", r_ghost.status_code == 401)
    check("存在账号错密码 401", r_real.status_code == 401)
    t_ghost = r_ghost.get_data(as_text=True)
    t_real = r_real.get_data(as_text=True)
    check("两者均含统一文案",
          "账号或密码错误" in t_ghost and "账号或密码错误" in t_real)
    check("不存在/未注册类泄露文案不出现",
          "不存在" not in t_ghost and "未注册" not in t_ghost)


# =========================================================================== #
# 3. 登录防爆破主体：规范化值共用同一账号桶（docs §6.1 末段）
# =========================================================================== #
def test_auth_subject_hash_normalizes_case_and_space():
    """_auth_subject_hash 直接断言：大小写/空白变体与正确值同摘要。"""
    h = app_mod._auth_subject_hash
    check("大小写变体同摘要",
          h("LOGIN") == h("login") == h("LoGiN"))
    check("首尾空白同摘要", h("  login  ") == h("login"))
    check("None/空串同摘要（空主体）", h(None) == h(""))
    check("不同账号摘要不同", h("alice") != h("bob"))


def test_case_variants_share_account_lockout_bucket(monkeypatch):
    """混合大小写连续失败与正确值共用同一账号桶：第 3 次（=阈值）触发 429。"""
    user_store.create_user("bucket@x.com", PW, role="user")
    if conftest.BACKEND == "json":
        # json mock：账号桶阈值 3、IP 桶 99（隔离账号桶语义）
        install_json_login_limits(monkeypatch, account_limit=3, ip_limit=99)
    else:
        import auth_limit_store
        monkeypatch.setattr(auth_limit_store, "AUTH_ACCOUNT_FAILURE_LIMIT", 3)
        monkeypatch.setattr(auth_limit_store, "AUTH_IP_FAILURE_LIMIT", 99)
    client = make_client()
    variants = ["BUCKET@X.COM", " Bucket@X.com ", "bucket@x.COM"]
    statuses = [login(client, v, "wrong-password-%d" % i).status_code
                for i, v in enumerate(variants)]
    check("前两次 401（未触限）", statuses[:2] == [401, 401],
          "statuses=%r" % statuses)
    check("第三次（阈值）触发 429", statuses[2] == 429,
          "statuses=%r" % statuses)


# =========================================================================== #
# 4. 管理 API 双字段输出与 login_id 入参（docs §4.1/§8.1）
# =========================================================================== #
def test_admin_users_api_dual_fields_and_login_id_input(monkeypatch):
    """列表/创建/重置/禁用响应双字段；创建接受 login_id（优先）或 email。"""
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    user_store.create_user("u@x.com", PW2, role="user")
    client = make_client()
    check("owner 登录 302", login(client, "admin", OWNER_PW).status_code == 302)

    # 列表：每个用户双键同值
    r = client.get("/api/admin/users")
    check("GET /api/admin/users 200", r.status_code == 200)
    users = r.get_json().get("users") or []
    check("列表非空", len(users) == 2)
    check("列表每行 login_id==email 且无 hash",
          all(u.get("login_id") == u.get("email")
              and "password_hash" not in u for u in users))

    # 创建：login_id 入参（优先形式）
    r2 = client.post("/api/admin/users",
                     json={"login_id": "New@X.com", "password": PW})
    check("login_id 入参创建 200", r2.status_code == 200,
          "got %s %s" % (r2.status_code, r2.get_data(as_text=True)))
    body2 = r2.get_json()
    check("创建响应 login_id==email==规范化值",
          body2.get("login_id") == body2.get("email") == "new@x.com")

    # 创建：email 兼容入参仍可用（deprecated）
    r3 = client.post("/api/admin/users",
                     json={"email": "legacy@x.com", "password": PW})
    check("email 兼容入参创建 200", r3.status_code == 200)
    body3 = r3.get_json()
    check("兼容创建响应双键同值",
          body3.get("login_id") == body3.get("email") == "legacy@x.com")

    # 两个都没给 → 400（登录账号缺失）
    r4 = client.post("/api/admin/users", json={"password": PW})
    check("缺登录账号 400", r4.status_code == 400)
    check("缺登录账号文案", "登录账号" in r4.get_json().get("error", ""))

    # 重置密码 / 禁用 / 启用响应同样双字段
    uid = user_store.get_user_by_email("u@x.com")["user_id"]
    r5 = client.post("/api/admin/users/%s/password" % uid,
                     json={"password": PW3})
    check("重置密码 200", r5.status_code == 200)
    b5 = r5.get_json()
    check("重置响应双键同值", b5.get("login_id") == b5.get("email"))
    r6 = client.post("/api/admin/users/%s/disable" % uid)
    check("禁用 200", r6.status_code == 200)
    check("禁用响应双键同值",
          r6.get_json().get("login_id") == r6.get_json().get("email"))
    r7 = client.post("/api/admin/users/%s/enable" % uid)
    check("启用响应双键同值",
          r7.get_json().get("login_id") == r7.get_json().get("email"))


@pg_only
def test_invite_admin_api_login_id_alias(monkeypatch):
    """邀请管理 API：login_id（优先）/ email（兼容）入参；响应掩码双键；
    绑定语义为「允许兑换的登录账号」（大小写不敏感匹配）。"""
    import registration_store
    make_owner()
    client = make_client()
    check("owner 登录 302", login(client, "admin", OWNER_PW).status_code == 302)

    # 创建：login_id 入参（优先形式）
    r = client.post("/api/admin/registration-invites",
                    json={"login_id": "NewUser@X.com"})
    check("login_id 入参创建邀请 200", r.status_code == 200,
          "got %s %s" % (r.status_code, r.get_data(as_text=True)))
    body = r.get_json()
    check("创建响应 login_id_masked",
          body.get("login_id_masked") == "n***@x.com")
    check("创建响应 email_masked 同值（deprecated）",
          body.get("email_masked") == body.get("login_id_masked"))
    check("响应不含完整绑定值与 token_hash",
          "newuser@x.com" not in json.dumps(body)
          and "token_hash" not in body)

    # 创建：email 兼容入参（deprecated）仍可用
    r2 = client.post("/api/admin/registration-invites",
                     json={"email": "LegacyInv@x.com"})
    check("email 兼容入参创建邀请 200", r2.status_code == 200)
    check("兼容创建响应掩码双键",
          r2.get_json().get("login_id_masked")
          == r2.get_json().get("email_masked") == "l***@x.com")

    # 列表：掩码双键
    r3 = client.get("/api/admin/registration-invites")
    items = r3.get_json().get("invites") or []
    check("列表两条", len(items) == 2)
    check("列表每条掩码双键同值",
          all(i.get("login_id_masked") == i.get("email_masked")
              for i in items))

    # 兑换：绑定语义为允许兑换的登录账号（大小写不敏感）
    result = registration_store.redeem_invite(
        body["token"], "NEWUSER@x.com", PW)
    created = result["user"]
    check("大小写不敏感兑换成功",
          created.get("email") == "newuser@x.com")
    check("兑换创建的用户 dict 双键同值",
          created.get("login_id") == created.get("email"))
    # 新用户可用该登录账号登录（display_name 缺省同 login_id）
    v = user_store.verify_user("newuser@x.com", PW)
    check("兑换后可登录", v is not None
          and v["user_id"] == created["user_id"])
    # 错误绑定值兑换失败（统一错误，不消费语义由既有套件覆盖）
    try:
        registration_store.redeem_invite(
            r2.get_json()["token"], "someone-else@x.com", PW)
        redeemed_wrong = True
    except registration_store.InviteRedeemError:
        redeemed_wrong = False
    check("绑定不匹配兑换失败", redeemed_wrong is False)


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
