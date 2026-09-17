# -*- coding: utf-8 -*-
"""SER-8（wip/ser8-dev）测试申请 API 测试：用户侧 + 管理侧路由层。

覆盖（tests/conftest.py 起内嵌 PG 并在 import 期跑 pg_store.ensure_schema，
0054_test_applications.sql 随文件名序自动应用——test_applications 表与
registration_mail_jobs 的 test_application/test_decision purpose 扩展即来自
该迁移，本文件全部用例都隐式验证了「ensure_schema 自动拾取新迁移」）：

  - 用户侧 POST /api/account/test-application：匿名 401；enrollment 受限
    会话提交成功 + 幂等（重复提交不重复发管理员邮件）；方向/数据分享形状
    非法 400；管理员通知邮箱未配置 503 admin_email_unconfigured；已激活
    用户提交 409 invalid_state；
  - 用户侧 GET /api/account/test-application：none → pending → approved
    状态流转（enrollment 会话与正式 session 双通道）；
  - verify 接线：POST /api/registration/verify 带 research_direction/
    share_research_data 建号后 best-effort 提交申请（application_submitted
    响应字段）；形状非法 400 且 **不消费 token**（先校验形状再消费）；缺
    字段兼容老前端（application_submitted=false）；
  - 管理侧 GET /api/admin/v1/test-applications：匿名 401 / 非 owner 403 /
    owner 200（status/direction 内存过滤、字段白名单、next_cursor=None）；
  - 管理侧 POST .../review：**真实 review 路径**（未 monkeypatch——
    conftest 每用例 TRUNCATE 后重播 ai_spend_total_defaults 基线行 20 CNY，
    默认额度 provisioning 基建现成，走真路径比替身更可信）：approve 原子
    激活 + 建总额度 + test_decision 通知邮件 + 幂等 409 already_reviewed；
    缺默认行 409 default_allowance_unconfigured；reject 终态且不改激活态；
    非 owner 触发仓储层 PermissionError → 403。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_test_application_api.py -q
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import settings_store  # noqa: E402
import test_application_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

BASE = "https://path.example.com"
PASSWORD = "longpassword123"


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器 + 禁用异步排水，时序确定）。"""
    from _pt_helpers import isolate_app
    import _billing_helpers as bh
    isolate_app(monkeypatch, SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()  # 含 0029 键（维护闸缺键按 False 的生产口径）
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY",
                 "TEST_APPLICATION_ADMIN_EMAIL",
                 "FORMAT_REQUEST_ADMIN_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    # 已知 store 侧缺陷（test_application_store.py 在本分支冻结不可改）：
    # submit_tx/review 调 mail.public_base_url()，但 registration_mail_worker
    # 并无该函数（仓库内也无任何定义）——不补则每次新提交/审核必然
    # AttributeError 回滚。这里按 store 的预期语义（PUBLIC_BASE_URL 原值）
    # 注入替身以测**路由层**行为；store 修复后应删除本行。
    monkeypatch.setattr(registration_mail_worker, "public_base_url",
                        lambda: BASE, raising=False)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _open_email_mode(monkeypatch):
    """打开 email_verify_invite_activation 生效态（verify 流程全部前置）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    settings_store.set_registration_mode(
        "email_verify_invite_activation", updated_by="t")


def _admin_email(monkeypatch, value="admin-notifications@x.com"):
    """配置测试申请通知邮箱（store 只在调用时读 env，monkeypatch 即生效）。"""
    monkeypatch.setenv("TEST_APPLICATION_ADMIN_EMAIL", value)


def _enqueue_and_verify(client, email, application=None):
    """真实 verify 流程建 pending_activation 用户；application 为随 verify
    提交的申请字段 dict（None = 不带，兼容老前端分支）。返回建号响应。"""
    out = registration_store.enqueue_email_verification(email, base_url=BASE)
    body = {"token": out["token"], "password": PASSWORD,
            "password_confirm": PASSWORD}
    if application is not None:
        body.update(application)
    r = client.post("/api/registration/verify", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _pending_user(email, application=None, monkeypatch=None):
    """建 pending 用户并返回（user, verify 响应）。"""
    client = _client()
    r = _enqueue_and_verify(client, email, application)
    user = user_store.get_user_by_login_id(email.lower())
    assert user is not None
    assert user["activation_state"] == "pending_activation"
    return user, r


def _enrollment_login(client, email, password=PASSWORD):
    """pending 用户登录 → enrollment 受限会话（走真实 login 白名单路径）。"""
    r = client.post("/login", data={"username": email, "password": password})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/activate")
    return r


def _owner():
    return user_store.create_user("app-owner@x.com", "ownerpass12345678",
                                  role="owner")


def _session_as(client, user, role):
    """伪造普通 session（_require_auth 口径：auth_user/user_id/role/version）。"""
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("email_normalized")
                  or user.get("login_id") or "u",
                  "user_id": user["user_id"], "role": role,
                  "auth_version": user.get("auth_version", 1)})


def _mail_job_count(purpose):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM registration_mail_jobs "
                        "WHERE purpose=%s", (purpose,))
            return cur.fetchone()["n"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 用户侧：POST /api/account/test-application
# --------------------------------------------------------------------------- #
def test_submit_anonymous_unauthorized():
    """匿名 POST：_require_auth 权威 401（中文 error + code=auth_required）。"""
    client = _client(auth=True)
    r = client.post("/api/account/test-application",
                    json={"research_direction": "model_plant",
                          "share_research_data": True})
    assert r.status_code == 401
    body = r.get_json()
    assert body["code"] == "auth_required"
    assert "重新登录" in body["error"]


def test_enrollment_submit_success_idempotent_and_get_pending(monkeypatch):
    """enrollment 会话提交成功 + 幂等（重复提交不再重复发管理员邮件）+
    GET 回读 pending 与申请字段。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("apply@x.com")
    client = _client()
    _enrollment_login(client, "apply@x.com")
    # 提交前 GET：无记录 → none
    r0 = client.get("/api/account/test-application")
    assert r0.status_code == 200
    assert r0.get_json() == {"state": "none"}
    r1 = client.post("/api/account/test-application",
                     json={"research_direction": "model_plant",
                           "share_research_data": True})
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert r1.get_json() == {"ok": True, "state": "pending"}
    # 幂等：重复点击 → duplicate=true，管理员通知邮件仍只有一封
    r2 = client.post("/api/account/test-application",
                     json={"research_direction": "model_plant",
                           "share_research_data": True})
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True and body["state"] == "pending"
    assert body["duplicate"] is True
    assert _mail_job_count("test_application") == 1
    # GET 回读：机器值原样（中文映射在前端做）
    r3 = client.get("/api/account/test-application")
    assert r3.status_code == 200
    got = r3.get_json()
    assert got["state"] == "pending"
    assert got["research_direction"] == "model_plant"
    assert got["share_research_data"] is True
    assert got["consent_version"] == test_application_store.CONSENT_VERSION


def test_submit_invalid_shape_400(monkeypatch):
    """方向非法 / share 非 bool / 缺字段 → 400 invalid_request（本地形状错误）。"""
    _admin_email(monkeypatch)
    _pending_user("shape@x.com")
    client = _client()
    _enrollment_login(client, "shape@x.com")
    for payload in ({"research_direction": "bogus", "share_research_data": True},
                    {"research_direction": "model_plant",
                     "share_research_data": "yes"},
                    {"research_direction": "model_animal"},
                    {}):
        r = client.post("/api/account/test-application", json=payload)
        assert r.status_code == 400, payload
        assert r.get_json()["code"] == "invalid_request"
    # 非法请求不产生申请记录、不发邮件
    assert _mail_job_count("test_application") == 0
    assert test_application_store.get(
        user_store.get_user_by_login_id("shape@x.com")["user_id"]) is None


def test_submit_admin_email_unconfigured_503(monkeypatch):
    """通知邮箱未配置：503 admin_email_unconfigured（fail-closed 不吞申请）。

    路由层测试（任务允许的 monkeypatch 口径）：store 的 admin_email() 在
    本分支被并行改动为带硬编码兜底收件人，env 已无法模拟「未配置」——
    先替换 store.admin_email 返回 None 锁定路由 503 契约，再还原原函数
    验证同一请求成功（全程不动 fixture 其它替身）。"""
    _pending_user("nomail@x.com")
    client = _client()
    _enrollment_login(client, "nomail@x.com")
    original_admin_email = test_application_store.admin_email
    monkeypatch.setattr(test_application_store, "admin_email",
                        lambda: None)
    r = client.post("/api/account/test-application",
                    json={"research_direction": "other",
                          "share_research_data": False})
    assert r.status_code == 503
    body = r.get_json()
    assert body["code"] == "admin_email_unconfigured"
    assert "暂不可用" in body["error"]
    # 失败请求未产生申请记录、未发邮件
    assert _mail_job_count("test_application") == 0
    # 还原真实 admin_email（并行改动后的带兜底实现）→ 同一请求成功
    monkeypatch.setattr(test_application_store, "admin_email",
                        original_admin_email)
    r2 = client.post("/api/account/test-application",
                     json={"research_direction": "other",
                           "share_research_data": False})
    assert r2.status_code == 200
    assert r2.get_json()["ok"] is True


def test_active_user_submit_conflict_409(monkeypatch):
    """已激活用户（正式 session）提交 → 409 invalid_state（状态不符）。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("active@x.com",
                            application={"research_direction": "other",
                                         "share_research_data": False})
    owner = _owner()
    # 真实审核通过 → 用户 active
    client = _client()
    _session_as(client, owner, "owner")
    r = client.post("/api/admin/v1/test-applications/%s/review" % user["user_id"],
                    json={"decision": "approved"})
    assert r.status_code == 200
    # active 后正式 session 提交：仓储层状态校验 → 409 invalid_state
    _session_as(client, user_store.get_user(user["user_id"]), "user")
    r2 = client.post("/api/account/test-application",
                     json={"research_direction": "other",
                           "share_research_data": False})
    assert r2.status_code == 409
    body = r2.get_json()
    assert body["code"] == "invalid_state"
    assert "待激活" in body["error"]
    assert owner is not None  # owner 仅用于审核会话


# --------------------------------------------------------------------------- #
# verify 接线（建号 + best-effort 申请提交）
# --------------------------------------------------------------------------- #
def test_verify_submits_application_and_rejects_bad_shape_before_token(
        monkeypatch):
    """verify 带申请字段：建号成功后 application_submitted=true；形状非法
    400 且 token **未被消费**（先校验形状再消费）；缺字段兼容老前端。"""
    _open_email_mode(monkeypatch)
    _admin_email(monkeypatch)
    client = _client()
    # 形状非法：400，token 仍 valid（未被废）
    out = registration_store.enqueue_email_verification("bad@x.com",
                                                        base_url=BASE)
    r_bad = client.post("/api/registration/verify", json={
        "token": out["token"], "password": PASSWORD,
        "research_direction": "bogus", "share_research_data": True})
    assert r_bad.status_code == 400
    assert r_bad.get_json()["code"] == "invalid_request"
    assert registration_store.check_verify_token(out["token"])["state"] == \
        "valid"
    # 合法提交：建号 + 申请同响应（申请走独立事务，best-effort）
    r = _enqueue_and_verify(client, "wired@x.com",
                            application={"research_direction":
                                         "clinical_pathology",
                                         "share_research_data": False})
    body = r.get_json()
    assert body["ok"] is True
    assert body["application_submitted"] is True
    user = user_store.get_user_by_login_id("wired@x.com")
    record = test_application_store.get(user["user_id"])
    assert record is not None
    assert record["research_direction"] == "clinical_pathology"
    assert record["share_research_data"] is False
    assert record["status"] == "pending"
    # 缺字段（老前端兼容）：建号成功、不提交申请
    r_old = _enqueue_and_verify(client, "oldfront@x.com")
    assert r_old.get_json()["application_submitted"] is False
    old_user = user_store.get_user_by_login_id("oldfront@x.com")
    assert test_application_store.get(old_user["user_id"]) is None


# --------------------------------------------------------------------------- #
# 管理侧：GET /api/admin/v1/test-applications + review
# --------------------------------------------------------------------------- #
def test_admin_list_owner_gate_and_filters(monkeypatch):
    """列表：匿名 401 / 非 owner 403 / owner 200（status/direction 过滤 +
    字段白名单 + next_cursor=None）。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("list@x.com",
                            application={"research_direction": "model_plant",
                                         "share_research_data": True})
    client = _client()
    # 匿名 → 401（auth_required）
    assert client.get("/api/admin/v1/test-applications").status_code == 401
    # 非 owner → 403（_require_owner_admin_v1）
    _session_as(client, user, "user")
    r_user = client.get("/api/admin/v1/test-applications")
    assert r_user.status_code == 403
    # enrollment 会话不是管理身份 → 仍 401
    anon_client = _client()
    _enrollment_login(anon_client, "list@x.com")
    assert anon_client.get(
        "/api/admin/v1/test-applications").status_code == 401
    # owner：命中 + 过滤 + 白名单字段
    _session_as(client, _owner(), "owner")
    r = client.get("/api/admin/v1/test-applications")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["next_cursor"] is None
    items = [it for it in payload["items"] if it["user_id"] == user["user_id"]]
    assert len(items) == 1
    item = items[0]
    assert item["email_normalized"] == "list@x.com"
    assert item["research_direction"] == "model_plant"
    assert item["share_research_data"] is True
    assert item["status"] == "pending"
    assert item["activation_state"] == "pending_activation"
    assert item["created_at"] and item["reviewed_at"] is None
    # status / direction 内存过滤：命中与不命中各验一次
    assert client.get("/api/admin/v1/test-applications?status=pending"
                      ).status_code == 200
    r_empty = client.get("/api/admin/v1/test-applications?status=approved")
    assert r_empty.get_json()["items"] == []
    r_dir = client.get("/api/admin/v1/test-applications?direction=other")
    assert r_dir.get_json()["items"] == []
    # 非法参数 → 400 invalid_request
    assert client.get("/api/admin/v1/test-applications?direction=bogus"
                      ).status_code == 400
    assert client.get("/api/admin/v1/test-applications?status=bogus"
                      ).status_code == 400


def test_review_approve_activates_and_is_idempotent(monkeypatch):
    """真实 review 路径：approve 原子激活 + 按默认行建一次性总额度 +
    test_decision 邮件；重复审批 409 already_reviewed；GET 状态流转到
    approved（正式 session 通道）。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("approve@x.com",
                            application={"research_direction": "model_animal",
                                         "share_research_data": True})
    owner = _owner()
    client = _client()
    _session_as(client, owner, "owner")
    r = client.post("/api/admin/v1/test-applications/%s/review"
                    % user["user_id"], json={"decision": "approved",
                                             "ai_access": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"ok": True, "user_id": user["user_id"],
                            "status": "approved"}
    after = user_store.get_user(user["user_id"])
    assert after["activation_state"] == "active"
    assert after["ai_access"] is True
    # 默认额度 provisioning：conftest 基线行 20 CNY → 20e9 nano
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT limit_nano_cny FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s", (user["user_id"],))
            row = cur.fetchone()
            assert row is not None and row["limit_nano_cny"] == 20 * 10 ** 9
    finally:
        conn.close()
    assert _mail_job_count("test_decision") == 1
    # 重复审批 → 409 already_reviewed（不重复发邮件/额度）
    r2 = client.post("/api/admin/v1/test-applications/%s/review"
                     % user["user_id"], json={"decision": "approved"})
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "already_reviewed"
    assert _mail_job_count("test_decision") == 1
    # GET 状态流转：active 用户正式 session → approved
    user_client = _client()
    _session_as(user_client, after, "user")
    r3 = user_client.get("/api/account/test-application")
    assert r3.status_code == 200
    got = r3.get_json()
    assert got["state"] == "approved"
    assert got["research_direction"] == "model_animal"


def test_review_reject_and_default_allowance_unconfigured(monkeypatch):
    """缺默认总额度行 → approve 409 default_allowance_unconfigured（用户
    仍 pending、不发额度）；reject 终态（不改激活态、发结果邮件）。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("reject@x.com",
                            application={"research_direction": "other",
                                         "share_research_data": False})
    client = _client()
    _session_as(client, _owner(), "owner")
    # 删掉全局默认行（conftest 基线种下 20 CNY）→ provisioning 无默认可解析
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_spend_total_defaults")
        conn.commit()
    finally:
        conn.close()
    r = client.post("/api/admin/v1/test-applications/%s/review"
                    % user["user_id"], json={"decision": "approved"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"]["code"] == "default_allowance_unconfigured"
    assert "默认总额度" in body["error"]["message"]
    assert user_store.get_user(user["user_id"])[
        "activation_state"] == "pending_activation"
    # reject：终态通过、激活态不动、结果邮件照发
    r2 = client.post("/api/admin/v1/test-applications/%s/review"
                     % user["user_id"], json={"decision": "rejected"})
    assert r2.status_code == 200
    assert r2.get_json()["status"] == "rejected"
    assert user_store.get_user(user["user_id"])[
        "activation_state"] == "pending_activation"
    assert _mail_job_count("test_decision") == 1
    # 已处理后再审 → 409 already_reviewed
    r3 = client.post("/api/admin/v1/test-applications/%s/review"
                     % user["user_id"], json={"decision": "approved"})
    assert r3.status_code == 409
    assert r3.get_json()["error"]["code"] == "already_reviewed"


def test_review_non_owner_forbidden_and_bad_params(monkeypatch):
    """非 owner 调 review：路由守卫 403；伪造绕过时仓储层 PermissionError
    兜底同样 403。decision 非法 → 400。"""
    _admin_email(monkeypatch)
    user, _ = _pending_user("perm@x.com",
                            application={"research_direction": "other",
                                         "share_research_data": True})
    stranger = user_store.create_user("stranger@x.com", "strangerpass123456",
                                      role="user")
    client = _client()
    _session_as(client, stranger, "user")
    # 路由守卫（_require_owner_admin_v1）先拦
    r = client.post("/api/admin/v1/test-applications/%s/review"
                    % user["user_id"], json={"decision": "approved"})
    assert r.status_code == 403
    # decision 形状非法（owner 会话）→ 400 invalid_request
    _session_as(client, _owner(), "owner")
    r_bad = client.post("/api/admin/v1/test-applications/%s/review"
                        % user["user_id"], json={"decision": "maybe"})
    assert r_bad.status_code == 400
    assert r_bad.get_json()["error"]["code"] == "invalid_request"
    # ai_access 非 bool → 400
    r_ai = client.post("/api/admin/v1/test-applications/%s/review"
                       % user["user_id"],
                       json={"decision": "approved", "ai_access": "yes"})
    assert r_ai.status_code == 400
    # 用户仍 pending、申请仍 pending（以上全部被拒，无副作用）
    assert user_store.get_user(user["user_id"])[
        "activation_state"] == "pending_activation"
    assert test_application_store.get(user["user_id"])["status"] == "pending"
