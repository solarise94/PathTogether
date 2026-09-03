# -*- coding: utf-8 -*-
"""账户系统批次 B「登录标识收口」+ 批次 C「物理收口」测试（docs
account-system-simplification-fix-plan.md §4.2 / §6.1 / §11.2 矩阵）。

覆盖（json 默认 + RUN_PG_TESTS=1 双跑）：
  - 用户 dict 单键输出（批次 C）：create_user / get_user /
    get_user_by_login_id / verify_user / list_users / list_enabled_owners /
    create_bootstrap_owner / set_user_password 等读写路径只带 login_id，
    **email 键不再出现**（反向断言）；json 侧物理键即 login_id；
  - 登录只认 login_id：大小写/空白不敏感登录成功；display_name 登录失败；
    两人同 display_name 各自 login_id 均可登录；A 的 display_name == B 的
    login_id 时该字符串只能登进 B；
  - 登录防爆破主体：大小写变体与正确值共用同一账号桶（_auth_subject_hash
    规范化直接断言 + 混合大小写连续失败触发 429 的功能断言）；
  - 统一失败文案「账号或密码错误」，不泄露账号存在性；
  - 管理 API：/api/admin/users 列表/重置/禁用响应无 email 键；旧「创建」
    端点已 410 退役（review R2-F1），创建契约（只接受 login_id，只传 email
    不给 login_id → 400，批次 C 删兼容入参）迁至 POST /api/admin/v1/users
    （响应包 user 键）；
  - 邀请管理 API（仅 PG，registration_invites 为 PG-only 能力）：创建只接受
    login_id 入参，响应只携带 login_id_masked（email_masked 已删除），绑定
    语义为「允许兑换的登录账号」；redeem_invite 返回 login_id 键。

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

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
os.environ["ADMIN_PASSWORD"] = ""
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import conftest  # noqa: E402
from pg_compat import BACKEND, json_only  # noqa: E402
# check()：_pt_helpers 统一带守卫实现；PASS/FAIL 计数仍落在本模块
from _pt_helpers import check, csrf_client, install_json_login_limits, isolate_app # noqa: E402

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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，并清空 users.json。"""
    isolate_app(monkeypatch, DATA_DIR, login_limits=True, clear_stores=True)
    if BACKEND == "postgres":
        # review R2-F2：PG 上 role=user 建号/兑换统一走「维护闸 + 开通锁」
        # 组合原语（闸 fail-closed），conftest TRUNCATE 清掉 0029 种子——
        # 每用例幂等重放（target=window + 闸=false）
        import _billing_helpers as bh
        bh.seed_spend_settings()
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
# 1. 单键输出（docs §4.2 物理收口：email 键不再出现）
# =========================================================================== #
def test_user_dicts_login_id_only_no_email_key():
    """所有返回用户 dict 的路径只带 login_id；email 键不再出现（批次 C）。"""
    owner = make_owner("Owner_One")
    check("create_bootstrap_owner login_id==规范化值",
          owner.get("login_id") == "owner_one")
    check("create_bootstrap_owner 无 email 键", "email" not in owner)
    u = user_store.create_user("Alice@Example.COM", PW, role="user",
                               display_name="Alice")
    check("create_user login_id 规范化值",
          u.get("login_id") == "alice@example.com")
    check("create_user 无 email 键", "email" not in u)

    got = user_store.get_user(u["user_id"])
    check("get_user login_id 命中",
          got.get("login_id") == "alice@example.com")
    check("get_user 无 email 键", "email" not in got)
    by_login = user_store.get_user_by_login_id("alice@example.com")
    check("get_user_by_login_id 命中且无 email 键",
          by_login is not None and by_login.get("login_id")
          == "alice@example.com" and "email" not in by_login)
    v = user_store.verify_user("ALICE@example.com", PW)
    check("verify_user 命中本人且无 email 键",
          v is not None and v["user_id"] == u["user_id"]
          and "email" not in v)
    listed = user_store.list_users()
    check("list_users 每行只有 login_id 键",
          len(listed) == 2 and all(
              x.get("login_id") and "email" not in x for x in listed))
    owners = user_store.list_enabled_owners()
    check("list_enabled_owners 单键",
          len(owners) == 1 and owners[0].get("login_id") == "owner_one"
          and "email" not in owners[0])
    # 写路径返回同样单键（set_user_password 递增 auth_version 后的公共 dict）
    p = user_store.set_user_password(u["user_id"], PW3)
    check("set_user_password 返回单键",
          p is not None and p.get("login_id") == "alice@example.com"
          and "email" not in p)


@json_only  # 直读 users.json 物理文件；PG 后端无该文件
def test_login_id_is_physical_key_in_json_store():
    """批次 C：json 物理记录键即 login_id（不再落 email 键、不双写）。"""
    u = user_store.create_user("persist@x.com", PW, role="user")
    raw = json.loads(user_store.USER_FILE.read_text(encoding="utf-8"))
    rec = raw["users"][u["user_id"]]
    check("物理记录 login_id 键", rec.get("login_id") == "persist@x.com")
    check("物理记录不落 email 键", "email" not in rec)
    got = user_store.get_user(u["user_id"])
    check("读出口同值", got.get("login_id") == "persist@x.com")


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
# 4. 管理 API 单键输出与 login_id-only 入参（docs §4.2/§8.1，批次 C）
# =========================================================================== #
def test_admin_users_api_login_id_only(monkeypatch):
    """列表/重置/禁用响应无 email 键；创建只接受 login_id（email 入参
    已删除——只传 email 不给 login_id 一律 400）。旧建号端点已 410 退役
    （review R2-F1），创建契约改在 POST /api/admin/v1/users 上验证。"""
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    make_owner()
    user_store.create_user("u@x.com", PW2, role="user")
    client = make_client()
    check("owner 登录 302", login(client, "admin", OWNER_PW).status_code == 302)

    # 列表：v1 掩码视图（旧 /api/admin/users 列表已随 R3 wave1 删除）
    r = client.get("/api/admin/v1/users")
    check("GET /api/admin/v1/users 200", r.status_code == 200)
    users = r.get_json().get("items") or []
    check("列表非空", len(users) == 2)
    check("列表每行 login_id_masked 且无 hash 无 email 键",
          all(u.get("login_id_masked") and "password_hash" not in u
              and "email" not in u for u in users))

    # 创建：login_id 入参（旧建号端点已 410 退役，review R2-F1；契约在 v1）
    r2 = client.post("/api/admin/v1/users",
                     json={"login_id": "New@X.com", "password": PW})
    check("login_id 入参创建 200", r2.status_code == 200,
          "got %s %s" % (r2.status_code, r2.get_data(as_text=True)))
    body2 = r2.get_json().get("user") or {}
    check("创建响应 login_id==规范化值",
          body2.get("login_id") == "new@x.com")
    check("创建响应无 email 键", "email" not in body2)

    # 批次 C：email 兼容入参已删除——只传 email 不给 login_id → 400
    r3 = client.post("/api/admin/v1/users",
                     json={"email": "legacy@x.com", "password": PW})
    check("email 入参不再被接受（400）", r3.status_code == 400,
          "got %s" % r3.status_code)
    check("错误文案为缺登录账号",
          "登录账号" in r3.get_json()["error"].get("message", ""))

    # 两个都没给 → 400（登录账号缺失）
    r4 = client.post("/api/admin/v1/users", json={"password": PW})
    check("缺登录账号 400", r4.status_code == 400)
    check("缺登录账号文案",
          "登录账号" in r4.get_json()["error"].get("message", ""))

    # 重置密码 / 禁用 / 启用响应同样单键
    uid = user_store.get_user_by_login_id("u@x.com")["user_id"]
    r5 = client.post("/api/admin/v1/users/%s/password-reset" % uid,
                     json={"password": PW3})
    check("重置密码 200", r5.status_code == 200)
    b5 = r5.get_json().get("user") or {}
    check("重置响应单键", b5.get("login_id") == "u@x.com"
          and "email" not in b5)
    r6 = client.post("/api/admin/v1/users/%s/disable" % uid)
    check("禁用 200", r6.status_code == 200)
    check("禁用响应单键", "email" not in r6.get_json().get("user", {}))
    r7 = client.post("/api/admin/v1/users/%s/enable" % uid)
    check("启用响应单键", "email" not in r7.get_json().get("user", {}))


@pg_only
def test_invite_admin_api_login_id_only(monkeypatch):
    """邀请管理 API：只接受 login_id 入参（email 兼容入参已删除）；响应只带
    login_id_masked（email_masked 已删除）；redeem_invite 返回 login_id 键；
    绑定语义为「允许兑换的登录账号」（大小写不敏感匹配）。"""
    import registration_store
    make_owner()
    client = make_client()
    check("owner 登录 302", login(client, "admin", OWNER_PW).status_code == 302)

    # 创建：login_id 入参（v1；旧 /api/admin/registration-invites 已删除）
    r = client.post("/api/admin/v1/invites",
                    json={"login_id": "NewUser@X.com"})
    check("login_id 入参创建邀请 200", r.status_code == 200,
          "got %s %s" % (r.status_code, r.get_data(as_text=True)))
    body = r.get_json().get("invite") or {}
    check("创建响应 login_id_masked",
          body.get("login_id_masked") == "n***@x.com")
    check("创建响应无 email_masked 键（批次 C）",
          "email_masked" not in body)
    check("响应不含完整绑定值与 token_hash",
          "newuser@x.com" not in json.dumps(body)
          and "token_hash" not in body)

    # 批次 C：email 兼容入参已删除——body 仍带 email 键说明是旧客户端，
    # 显式 400（绝不静默降级为不绑定邀请这一高风险形态）。
    r2 = client.post("/api/admin/v1/invites",
                     json={"email": "LegacyInv@x.com"})
    check("email 入参显式 400", r2.status_code == 400,
          "got %s" % r2.status_code)
    check("400 文案指引 login_id",
          "login_id" in json.dumps((r2.get_json() or {}).get("error"),
                                   ensure_ascii=False))
    # 显式不绑定（不带 email/login_id）仍可创建（owner 高风险选项语义保持）
    r2u = client.post("/api/admin/v1/invites", json={})
    check("显式不绑定创建 200", r2u.status_code == 200)
    b2 = r2u.get_json().get("invite") or {}
    check("不绑定邀请 login_id_masked 为空串",
          b2.get("login_id_masked") == "", "got %r" % b2.get("login_id_masked"))

    # 列表：只带 login_id_masked
    r3 = client.get("/api/admin/v1/invites")
    items = r3.get_json().get("invites") or []
    check("列表两条", len(items) == 2)
    check("列表每条无 email_masked 键",
          all("email_masked" not in i for i in items))

    # 兑换：绑定语义为允许兑换的登录账号（大小写不敏感）
    result = registration_store.redeem_invite(
        body["token"], "NEWUSER@x.com", PW)
    created = result["user"]
    check("大小写不敏感兑换成功",
          created.get("login_id") == "newuser@x.com")
    check("redeem_invite 返回 login_id 键（批次 C）",
          result.get("login_id") == "newuser@x.com")
    check("redeem_invite 返回无 email 键", "email" not in result)
    check("兑换创建的用户 dict 单键",
          created.get("login_id") == "newuser@x.com"
          and "email" not in created)
    # 新用户可用该登录账号登录（display_name 缺省同 login_id）
    v = user_store.verify_user("newuser@x.com", PW)
    check("兑换后可登录", v is not None
          and v["user_id"] == created["user_id"])
    # 绑定邀请 + 错误绑定值兑换失败（统一错误；不消费语义由既有套件覆盖）
    r_bound = client.post("/api/admin/v1/invites",
                          json={"login_id": "BoundUser@X.com"})
    check("绑定邀请创建 200", r_bound.status_code == 200)
    try:
        registration_store.redeem_invite(
            r_bound.get_json()["token"], "someone-else@x.com", PW)
        redeemed_wrong = True
    except registration_store.InviteRedeemError:
        redeemed_wrong = False
    check("绑定不匹配兑换失败", redeemed_wrong is False)
    # 不绑定邀请（r2u）任意登录账号可兑换（高风险选项语义保持）
    out_unbound = registration_store.redeem_invite(
        b2["token"], "FreePick@x.com", PW)
    check("不绑定邀请任意登录账号兑换成功",
          out_unbound.get("login_id") == "freepick@x.com")


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
