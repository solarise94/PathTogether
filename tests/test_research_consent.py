# -*- coding: utf-8 -*-
"""P2 可撤回研究授权与账户设置测试（docs/agent-plan-20260921-registration-
consent-research.md §3.5/§6 + §10 P2 验收）。

覆盖：

  - 迁移 0062（research_data_deletion_jobs）：conftest 内嵌 PG 从零应用、
    CHECK 约束（reason/status/epoch）、「每用户至多一条未终态任务」部分
    唯一索引兜底；
  - GET /api/account/agreements：匿名 401、no-store、当前发布文档 + 本人
    接受情况 + 本人研究状态（唯一权威 + 旧选项 historical_only）；
  - POST /api/account/agreements/accept：CSRF 闸、版本必填、版本/hash
    不匹配或未发布 409、成功追加 source=account_reaccept；
  - PUT /api/account/research-consent：enabled 必须真布尔（"false" 字符串
    400）、grant 版本必填、版本/hash/未发布 409、expected_epoch CAS
    （409 epoch_conflict + current_epoch）、撤回后再同意新 epoch、文稿
    下架后仍可撤回；
  - 越权：body 携他人 user_id 不生效（身份只取 session）；owner 预览态
    GET 403 preview_forbidden、写 403（预览写闸）且不改变用户授权；
    不存在管理员代 grant 的 admin 端点（url_map 静态断言）；
  - 旧 test_applications 选项兼容但不作为新采集权威：旧 true 经 HTTP 视图
    仍 granted=False、只显 historical_only 标记；
  - POST /api/account/research-data/deletion：幂等创建（重复请求同
    job_id、created=false）、GET 可查询状态、不改变授权状态；
  - 撤回原子：withdraw 同事务建 reason=withdrawal 删除任务（新 epoch）；
    删除任务插入失败 → 整体回滚（consent/history/job 三者一致）；CAS
    失败无任何写入；
  - §6.1 服务端权威判定 evaluate_research_access 全矩阵：active/预览/
    demo/未授权/旧 epoch/grant 前数据/资源权利/删除任务未清/文档下架/
    采集开关；撤回→再同意后旧 epoch 数据不复活；
  - 研究采集开关仍默认关闭：granted 用户 ingest_allowed=False、视图
    collection_enabled=False（P2 交付态）。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_research_consent.py -q
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import research_consent_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PASSWORD = "longpassword123"
MIGRATION_0062 = "0062_research_data_deletion_jobs.sql"


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """per-test 隔离 + 认证开启 + 采集开关复位（P2 交付态：默认关闭）。"""
    isolate_app(monkeypatch, tmp_path / "share", clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.delenv("RESEARCH_COLLECTION_ENABLED", raising=False)
    # role=user 建号走「维护闸 + 开通锁」组合原语（同 test_admin_preview）
    import _billing_helpers as bh
    bh.seed_spend_settings()
    yield


def _create_user(login_id):
    return user_store.create_user(login_id, PASSWORD)


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _login(client, user):
    """伪造正式 session（与 test_admin_preview 同款；不经登录表单）。"""
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _register_and_publish(*doc_types):
    agreement_store.ensure_builtin_documents()
    for dt in doc_types:
        d = _doc(dt)
        agreement_store.publish_document(dt, d["version"])


def _grant(user_id):
    d = _doc("research_sharing")
    return research_consent_store.grant(
        user_id, document_version=d["version"],
        document_sha256=d["content_sha256"])


def _insert_legacy_application(user_id, share):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO test_applications "
                "(user_id, research_direction, share_research_data, "
                " consent_version) VALUES (%s,'other',%s,%s)",
                (user_id, share, "research-data-20260916-v1"))
        conn.commit()
    finally:
        conn.close()


def _retire_research_doc():
    d = _doc("research_sharing")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agreement_documents SET status='retired' "
                "WHERE document_type='research_sharing' AND version=%s",
                (d["version"],))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 迁移 0062：空库应用 + 约束 + 每用户一条未终态任务
# --------------------------------------------------------------------------- #
def test_migration_0062_applied_and_constraints():
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0062,))
            assert cur.fetchone() is not None, "0062 应已被 ensure_schema 应用"
            cur.execute("SELECT 1 FROM pg_tables WHERE tablename=%s",
                        ("research_data_deletion_jobs",))
            assert cur.fetchone() is not None
            cur.execute(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname='research_data_deletion_jobs_one_active'")
            assert cur.fetchone() is not None, "每用户至多一条未终态任务索引"
        # CHECK 约束：坏 reason / 坏 status / 负 epoch 全部被拒
        user = _create_user("m62chk@x.com")
        for sql, params in (
            ("INSERT INTO research_data_deletion_jobs "
             "(job_id, user_id, consent_epoch, reason) "
             "VALUES ('j1',%s,1,'bogus')", (user["user_id"],)),
            ("INSERT INTO research_data_deletion_jobs "
             "(job_id, user_id, consent_epoch, reason, status) "
             "VALUES ('j2',%s,1,'withdrawal','done')", (user["user_id"],)),
            ("INSERT INTO research_data_deletion_jobs "
             "(job_id, user_id, consent_epoch, reason) "
             "VALUES ('j3',%s,-1,'withdrawal')", (user["user_id"],)),
        ):
            violated = False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                conn.commit()
            except psycopg.errors.CheckViolation:
                violated = True
                conn.rollback()
            assert violated, "约束应拒绝：%s" % sql
        # 部分唯一索引：同一用户第二条未终态任务被拒；终态后可再建
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_data_deletion_jobs "
                "(job_id, user_id, consent_epoch, reason) "
                "VALUES ('ja',%s,1,'withdrawal')", (user["user_id"],))
        conn.commit()
        blocked = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research_data_deletion_jobs "
                    "(job_id, user_id, consent_epoch, reason) "
                    "VALUES ('jb',%s,1,'user_request')", (user["user_id"],))
            conn.commit()
        except psycopg.errors.UniqueViolation:
            blocked = True
            conn.rollback()
        assert blocked, "同用户第二条未终态任务应被索引拒绝"
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='completed', "
                "completed_at=now() WHERE job_id='ja'")
            cur.execute(
                "INSERT INTO research_data_deletion_jobs "
                "(job_id, user_id, consent_epoch, reason) "
                "VALUES ('jc',%s,2,'user_request')", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. GET /api/account/agreements
# --------------------------------------------------------------------------- #
def test_agreements_view_auth_and_payload():
    user = _create_user("view@x.com")
    c = _client()
    # 匿名 → 401（全局鉴权闸）
    assert c.get("/api/account/agreements").status_code == 401
    _login(c, user)
    r = c.get("/api/account/agreements")
    assert r.status_code == 200, r.get_json()
    assert "no-store" in r.headers.get("Cache-Control", "")
    body = r.get_json()
    # 未发布前：三类文稿以内置 draft 回退展示（draft 不可用于接受/授权）
    types = {d["document_type"]: d for d in body["documents"]}
    assert set(types) == set(agreement_store.DOCUMENT_TYPES)
    assert types["research_sharing"]["status"] == "draft"
    assert types["research_sharing"]["version"] == \
        agreement_store.BUILTIN_VERSION
    assert body["research"]["granted"] is False
    assert body["research"]["epoch"] == 0
    assert body["collection_enabled"] is False, "P2 采集开关默认关闭"
    assert body["acceptances"] == []
    assert body["research"]["legacy_test_application"] is None

    # 发布后：published 元数据 + 版本化链接 + 接受/授权情况
    _register_and_publish("user_agreement", "research_sharing")
    research_consent_store.record_terms_acceptance(
        user["user_id"], "user_agreement", _doc("user_agreement")["version"],
        _doc("user_agreement")["content_sha256"], source="register")
    _grant(user["user_id"])
    r2 = c.get("/api/account/agreements")
    body2 = r2.get_json()
    types2 = {d["document_type"]: d for d in body2["documents"]}
    rs = types2["research_sharing"]
    assert rs["status"] == "published"
    assert rs["published_at"] is not None
    assert rs["url"] == "/legal/research-sharing/%s" % rs["version"]
    assert len(body2["acceptances"]) == 1
    assert body2["acceptances"][0]["source"] == "register"
    assert body2["research"]["state"] == "granted"
    assert body2["research"]["epoch"] == 1
    assert body2["research"]["granted_at"] is not None
    assert len(body2["history"]) == 1


# --------------------------------------------------------------------------- #
# 3. POST /api/account/agreements/accept
# --------------------------------------------------------------------------- #
def test_accept_terms_validation_and_success():
    user = _create_user("accept2@x.com")
    d = _doc("user_agreement")
    _register_and_publish("user_agreement")
    c = _login(_client(), user)

    # CSRF：裸 client（无 X-CSRF-Token）→ 400 csrf_required
    raw = app_mod.app.test_client()
    _login(raw, user)
    assert raw.post("/api/account/agreements/accept",
                    json={"version": d["version"]}).status_code == 400

    # 缺版本 → 400
    r = c.post("/api/account/agreements/accept", json={})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_request"
    # 非必选文档类型 → 400
    r = c.post("/api/account/agreements/accept", json={
        "document_type": "research_sharing", "version": d["version"]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "unsupported_document_type"
    # 版本不匹配 → 409 document_not_published
    r = c.post("/api/account/agreements/accept", json={
        "version": "1999-01-01-v0"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "document_not_published"
    # hash 不匹配 → 409
    r = c.post("/api/account/agreements/accept", json={
        "version": d["version"], "content_sha256": "0" * 64})
    assert r.status_code == 409
    # 成功（省略 hash：服务端以注册表为权威）
    r = c.post("/api/account/agreements/accept", json={
        "version": d["version"]})
    assert r.status_code == 200, r.get_json()
    acc = r.get_json()["acceptance"]
    assert acc["source"] == "account_reaccept"
    assert acc["version"] == d["version"]
    # 再次接受 → 追加新凭据（不覆盖）
    r2 = c.post("/api/account/agreements/accept", json={
        "version": d["version"], "content_sha256": d["content_sha256"]})
    assert r2.status_code == 200
    rows = research_consent_store.list_acceptances(
        user["user_id"], "user_agreement")
    assert len(rows) == 2


def test_accept_requires_published_document():
    user = _create_user("accept3@x.com")
    d = _doc("user_agreement")
    agreement_store.ensure_builtin_documents()  # 只登记不发布
    c = _login(_client(), user)
    r = c.post("/api/account/agreements/accept", json={
        "version": d["version"], "content_sha256": d["content_sha256"]})
    assert r.status_code == 409
    assert r.get_json()["code"] == "document_not_published"
    assert research_consent_store.list_acceptances(user["user_id"]) == []


# --------------------------------------------------------------------------- #
# 4. PUT /api/account/research-consent：grant/withdraw + CAS
# --------------------------------------------------------------------------- #
def test_put_consent_grant_validation():
    user = _create_user("grant2@x.com")
    c = _login(_client(), user)
    # 未发布 → 409
    agreement_store.ensure_builtin_documents()
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": _doc("research_sharing")["version"]})
    assert r.status_code == 409
    assert r.get_json()["code"] == "document_not_published"

    _register_and_publish("research_sharing")
    d = _doc("research_sharing")
    # enabled 缺失 / 非 bool（含字符串 "false" truthiness 陷阱）→ 400
    for bad in (None, "false", "true", 1, 0):
        r = c.put("/api/account/research-consent", json={"enabled": bad})
        assert r.status_code == 400, "enabled=%r 应 400" % (bad,)
        assert r.get_json()["code"] == "invalid_request"
    # grant 缺版本 → 400 document_version_required
    r = c.put("/api/account/research-consent", json={"enabled": True})
    assert r.status_code == 400
    assert r.get_json()["code"] == "document_version_required"
    # 版本不匹配 → 409
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": "1999-01-01-v0"})
    assert r.status_code == 409
    # hash 不匹配 → 409
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "document_sha256": "0" * 64})
    assert r.status_code == 409
    # 成功：epoch 1
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["consent"]["state"] == "granted"
    assert body["consent"]["epoch"] == 1
    assert body["changed"] is True
    # 旧 expected_epoch 再 grant → 409 epoch_conflict + current_epoch
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0})
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "epoch_conflict"
    assert j["current_epoch"] == 1
    # expected_epoch 非 int → 400
    r = c.put("/api/account/research-consent", json={
        "enabled": False, "expected_epoch": "1"})
    assert r.status_code == 400


def test_put_consent_withdraw_epoch_cas_and_regrant():
    user = _create_user("wd2@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    c = _login(_client(), user)
    assert c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0}).status_code == 200
    # 撤回：旧 epoch → 409
    r = c.put("/api/account/research-consent", json={
        "enabled": False, "expected_epoch": 99})
    assert r.status_code == 409
    assert r.get_json()["current_epoch"] == 1
    # 正确 epoch → 撤回成功，epoch+1，且响应带删除任务
    r = c.put("/api/account/research-consent", json={
        "enabled": False, "expected_epoch": 1})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["consent"]["state"] == "withdrawn"
    assert body["consent"]["epoch"] == 2
    assert body["deletion_job"] is not None
    assert body["deletion_job"]["reason"] == "withdrawal"
    assert body["deletion_job"]["consent_epoch"] == 2
    assert body["deletion_job"]["status"] == "pending"
    # 再同意：新 epoch，不复活旧语义
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 2})
    assert r.status_code == 200
    assert r.get_json()["consent"]["epoch"] == 3
    history = research_consent_store.list_history(user["user_id"])
    assert [(h["from_state"], h["to_state"], h["epoch"]) for h in history] == [
        ("withdrawn", "granted", 3), ("granted", "withdrawn", 2),
        (None, "granted", 1)]


def test_withdraw_works_after_document_retired_via_http():
    user = _create_user("wd3@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    c = _login(_client(), user)
    assert c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"]}).status_code == 200
    _retire_research_doc()
    # 文稿下架后：grant 拒绝、撤回仍可用（§3.5）
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"]})
    assert r.status_code == 409
    r = c.put("/api/account/research-consent", json={"enabled": False})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["consent"]["state"] == "withdrawn"


# --------------------------------------------------------------------------- #
# 5. 越权与预览
# --------------------------------------------------------------------------- #
def test_put_cannot_target_other_user():
    usera = _create_user("actorA@x.com")
    userb = _create_user("actorB@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    c = _login(_client(), usera)
    # body 携他人 user_id：身份只取 session，不产生任何对 B 的写入
    r = c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0, "user_id": userb["user_id"]})
    assert r.status_code == 200
    assert r.get_json()["consent"]["state"] == "granted"
    assert research_consent_store.get_consent(userb["user_id"]) is None
    assert research_consent_store.get_consent(usera["user_id"])[
        "state"] == "granted"


def test_owner_preview_cannot_view_or_change_user_consent():
    owner = user_store.create_user("pvowner@x.com", PASSWORD, role="owner")
    user = _create_user("pvuser@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": user["user_id"]}).status_code == 200
    # GET：路由 actor 解析显式拒绝预览态
    r = oc.get("/api/account/agreements")
    assert r.status_code == 403
    assert r.get_json()["code"] == "preview_forbidden"
    # 写：预览写硬闸（preview_readonly）在路由前拦截
    for method, path, payload in (
            ("post", "/api/account/agreements/accept",
             {"version": _doc("user_agreement")["version"]}),
            ("put", "/api/account/research-consent",
             {"enabled": False}),
            ("post", "/api/account/research-data/deletion", None)):
        kwargs = {"json": payload} if payload is not None else {}
        r = getattr(oc, method)(path, **kwargs)
        assert r.status_code == 403, "%s %s 预览态应 403" % (method, path)
    # 用户授权未被预览改变
    assert research_consent_store.get_consent(user["user_id"])[
        "state"] == "granted"


def test_no_admin_endpoint_can_grant_research_consent():
    """§3.5/验收：管理员无按钮强制 grant——不存在 admin 面授权写入端点。"""
    admin_rules = [str(r) for r in app_mod.app.url_map.iter_rules()
                   if str(r).startswith("/api/admin")]
    for rule in admin_rules:
        assert "research-consent" not in rule and "research_data" not in rule, \
            "admin 面不得出现研究授权写入端点：%s" % rule
    # store 层：他人代操作被 ActorForbiddenError 拒绝（HTTP 面不可达路径）
    usera = _create_user("advA@x.com")
    userb = _create_user("advB@x.com")
    _register_and_publish("research_sharing")
    with pytest.raises(research_consent_store.ActorForbiddenError):
        research_consent_store.grant(
            usera["user_id"], _doc("research_sharing")["version"],
            _doc("research_sharing")["content_sha256"],
            actor_user_id=userb["user_id"])
    assert research_consent_store.get_consent(usera["user_id"]) is None


# --------------------------------------------------------------------------- #
# 6. 旧 test_applications 选项：兼容但不作为新采集权威
# --------------------------------------------------------------------------- #
def test_legacy_true_not_authoritative_via_http():
    user = _create_user("legacy2@x.com")
    _insert_legacy_application(user["user_id"], True)
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    c = _login(_client(), user)
    body = c.get("/api/account/agreements").get_json()
    research = body["research"]
    assert research["granted"] is False, "旧 true 不得自动 granted"
    assert research["state"] is None
    legacy = research["legacy_test_application"]
    assert legacy["historical_only"] is True
    assert legacy["share_research_data"] is True
    assert legacy["historical_consent_version"] == "research-data-20260916-v1"
    # 旧申请查询端点也带 historical 标记
    tap = c.get("/api/account/test-application").get_json()
    assert tap["share_research_data"] is True
    assert tap["share_research_data_historical"] is True
    # 授权只经新服务：PUT 后才有 granted
    assert c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0}).status_code == 200
    research2 = c.get("/api/account/agreements").get_json()["research"]
    assert research2["granted"] is True
    assert research2["epoch"] == 1


# --------------------------------------------------------------------------- #
# 7. POST /api/account/research-data/deletion：幂等 + 可查询
# --------------------------------------------------------------------------- #
def test_deletion_endpoint_idempotent_and_queryable():
    user = _create_user("del1@x.com")
    d = _doc("research_sharing")
    _register_and_publish("research_sharing")
    c = _login(_client(), user)
    # 未授权也可申请（删除研究副本与当前授权状态是两件事）
    r1 = c.post("/api/account/research-data/deletion")
    assert r1.status_code == 200, r1.get_json()
    b1 = r1.get_json()
    assert b1["created"] is True
    assert b1["job"]["reason"] == "user_request"
    assert b1["job"]["status"] == "pending"
    assert b1["job"]["consent_epoch"] == 0
    # 幂等：重复请求 → 同一 job，created=False
    r2 = c.post("/api/account/research-data/deletion")
    b2 = r2.get_json()
    assert b2["created"] is False
    assert b2["job"]["job_id"] == b1["job"]["job_id"]
    jobs = research_consent_store.list_deletion_jobs(user["user_id"])
    assert len(jobs) == 1, "重复申请不得重复建任务"
    # GET 可查询状态
    g = c.get("/api/account/research-data/deletion")
    assert g.status_code == 200
    assert "no-store" in g.headers.get("Cache-Control", "")
    assert g.get_json()["job"]["job_id"] == b1["job"]["job_id"]
    # 匿名 401
    assert _client().get("/api/account/research-data/deletion") \
        .status_code == 401
    # 申请不改变授权状态；grant 不触碰删除任务（任务只覆盖创建时点之前的
    # 研究副本；撤回才会把任务推进到新 epoch）
    assert c.put("/api/account/research-consent", json={
        "enabled": True, "document_version": d["version"],
        "expected_epoch": 0}).status_code == 200
    g2 = c.get("/api/account/research-data/deletion").get_json()
    assert g2["job"]["consent_epoch"] == 0, \
        "grant 不得改写既有删除任务的 epoch 范围"


# --------------------------------------------------------------------------- #
# 8. 撤回原子：同事务建删除任务；失败整体回滚
# --------------------------------------------------------------------------- #
def test_withdraw_creates_deletion_job_atomically():
    user = _create_user("atomic1@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])
    w = research_consent_store.withdraw(user["user_id"])
    assert w["changed"] is True
    job = w["deletion_job"]
    assert job is not None
    assert job["reason"] == "withdrawal"
    assert job["consent_epoch"] == 2, "任务 epoch = 撤回后的新 epoch"
    # 落库可见（同事务已提交）
    assert research_consent_store.get_active_deletion_job(
        user["user_id"])["job_id"] == job["job_id"]
    # 授权视图带 active_deletion_job
    view = research_consent_store.research_authorization_view(user["user_id"])
    assert view["active_deletion_job"]["job_id"] == job["job_id"]


def test_withdraw_failure_rolls_back_everything(monkeypatch):
    """删除任务插入失败 → consent 状态、epoch、历史、任务全部不变（同事务）。"""
    user = _create_user("atomic2@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])

    def _boom(cur, user_id, consent_epoch, reason):
        raise RuntimeError("deletion job insert failed (test)")

    monkeypatch.setattr(research_consent_store,
                        "_upsert_deletion_job_tx", _boom)
    with pytest.raises(RuntimeError):
        research_consent_store.withdraw(user["user_id"])
    # 回滚后：仍是 granted、epoch 1、仅 1 条历史、无删除任务
    consent = research_consent_store.get_consent(user["user_id"])
    assert consent["state"] == "granted"
    assert consent["epoch"] == 1
    assert len(research_consent_store.list_history(user["user_id"])) == 1
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None


def test_withdraw_epoch_conflict_has_no_side_effects():
    user = _create_user("atomic3@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])
    with pytest.raises(research_consent_store.EpochConflictError):
        research_consent_store.withdraw(user["user_id"], expected_epoch=99)
    assert research_consent_store.get_consent(user["user_id"])[
        "state"] == "granted"
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None


def test_withdraw_without_row_creates_no_job():
    user = _create_user("atomic4@x.com")
    result = research_consent_store.withdraw(user["user_id"])
    assert result["changed"] is False
    assert result["deletion_job"] is None
    assert research_consent_store.list_deletion_jobs(user["user_id"]) == []


def test_concurrent_deletion_requests_create_single_job():
    """并发竞争：两个线程同时申请删除 → 恰好一条任务、同 job_id、一创一幂等。"""
    import threading

    user = _create_user("atomic5@x.com")
    results = []
    errors = []

    def _apply():
        try:
            results.append(research_consent_store.create_deletion_job(
                user["user_id"], reason="user_request"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_apply) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], "并发申请不得抛错：%r" % (errors,)
    assert sorted(r["created"] for r in results) == [False, True]
    assert len({r["job"]["job_id"] for r in results}) == 1
    assert len(research_consent_store.list_deletion_jobs(
        user["user_id"])) == 1


# --------------------------------------------------------------------------- #
# 9. §6.1 服务端权威判定 evaluate_research_access 全矩阵
# --------------------------------------------------------------------------- #
def test_evaluate_research_access_matrix(monkeypatch):
    user = _create_user("gate@x.com")
    other = _create_user("gateother@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])
    granted_at = research_consent_store.get_consent(user["user_id"])[
        "granted_at"]

    # P2 交付态：采集开关默认关闭 → granted 也不允许
    out = research_consent_store.evaluate_research_access(user["user_id"])
    assert out["allowed"] is False
    assert "collection_disabled" in out["reasons"]

    monkeypatch.setenv("RESEARCH_COLLECTION_ENABLED", "1")
    out = research_consent_store.evaluate_research_access(user["user_id"])
    assert out["allowed"] is True and out["reasons"] == []

    # 预览态 / demo 访客
    assert research_consent_store.evaluate_research_access(
        user["user_id"], preview=True)["allowed"] is False
    assert research_consent_store.evaluate_research_access(
        user["user_id"], demo_guest=True)["allowed"] is False

    # 无身份 / 不存在用户
    assert research_consent_store.evaluate_research_access(
        None)["reasons"] == ["no_identity"]
    assert research_consent_store.evaluate_research_access(
        "uid-no-such")["allowed"] is False

    # 账号非 active（pending）/ 禁用
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET activation_state="
                        "'pending_activation' WHERE user_id=%s",
                        (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    out = research_consent_store.evaluate_research_access(user["user_id"])
    assert out["allowed"] is False
    assert "account_not_active" in out["reasons"]
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET activation_state='active', "
                        "disabled=TRUE WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    assert "account_not_found" in research_consent_store \
        .evaluate_research_access(user["user_id"])["reasons"]
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled=FALSE WHERE user_id=%s",
                        (user["user_id"],))
        conn.commit()
    finally:
        conn.close()

    # epoch 一致 / 不一致（旧 grant、撤回前、离线重传）
    ok = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=1)
    assert ok["allowed"] is True
    bad = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=99)
    assert bad["allowed"] is False
    assert "epoch_mismatch" in bad["reasons"]

    # grant 之前产生的数据（历史回填）→ 拒绝；grant 之后 → 允许
    before = research_consent_store.evaluate_research_access(
        user["user_id"],
        data_created_at=granted_at - timedelta(days=1))
    assert before["allowed"] is False
    assert "predates_grant" in before["reasons"]
    after = research_consent_store.evaluate_research_access(
        user["user_id"], data_created_at=granted_at + timedelta(minutes=1))
    assert after["allowed"] is True

    # 资源权利：仅本人拥有的资源可入研究
    assert research_consent_store.evaluate_research_access(
        user["user_id"], resource_owner_id=user["user_id"])["allowed"] is True
    notown = research_consent_store.evaluate_research_access(
        user["user_id"], resource_owner_id=other["user_id"])
    assert notown["allowed"] is False
    assert "resource_not_owned" in notown["reasons"]

    # 未终态删除任务阻断研究使用
    research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    pending = research_consent_store.evaluate_research_access(user["user_id"])
    assert pending["allowed"] is False
    assert "deletion_pending" in pending["reasons"]
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET "
                "status='completed', completed_at=now() "
                "WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    assert research_consent_store.evaluate_research_access(
        user["user_id"])["allowed"] is True

    # 文稿下架（无 published）→ 不能以旧协议继续研究
    _retire_research_doc()
    retired = research_consent_store.evaluate_research_access(user["user_id"])
    assert retired["allowed"] is False
    assert "document_not_published" in retired["reasons"]

    # 撤回后：not_granted
    research_consent_store.withdraw(user["user_id"])
    wd = research_consent_store.evaluate_research_access(user["user_id"])
    assert wd["allowed"] is False
    assert "not_granted" in wd["reasons"]


def test_regrant_does_not_revive_old_epoch_data(monkeypatch):
    """撤回→再同意：旧 epoch 数据继续被拒（备份恢复不复活语义）。"""
    user = _create_user("revive@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])           # epoch 1
    research_consent_store.withdraw(user["user_id"])   # epoch 2（建删除任务）
    conn = _pg()
    try:
        with conn.cursor() as cur:   # 清掉未终态任务以隔离 epoch 判定
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='completed', "
                "completed_at=now() WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    _grant(user["user_id"])           # epoch 3
    monkeypatch.setenv("RESEARCH_COLLECTION_ENABLED", "1")
    old = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=1)
    assert old["allowed"] is False
    assert "epoch_mismatch" in old["reasons"]
    new = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=3)
    assert new["allowed"] is True


def test_legacy_true_user_not_allowed_by_gate(monkeypatch):
    user = _create_user("legacygate@x.com")
    _insert_legacy_application(user["user_id"], True)
    monkeypatch.setenv("RESEARCH_COLLECTION_ENABLED", "1")
    out = research_consent_store.evaluate_research_access(user["user_id"])
    assert out["allowed"] is False
    assert "not_granted" in out["reasons"]


# --------------------------------------------------------------------------- #
# 10. 研究采集开关仍关闭（P2 交付态）
# --------------------------------------------------------------------------- #
def test_collection_switch_off_is_default():
    user = _create_user("swoff@x.com")
    _register_and_publish("research_sharing")
    _grant(user["user_id"])
    assert research_consent_store.collection_enabled() is False
    assert research_consent_store.ingest_allowed(user["user_id"]) is False
    view = research_consent_store.research_authorization_view(user["user_id"])
    assert view["collection_enabled"] is False
    assert view["ingest_allowed"] is False
    assert view["granted"] is True
    c = _login(_client(), user)
    body = c.get("/api/account/agreements").get_json()
    assert body["collection_enabled"] is False


# --------------------------------------------------------------------------- #
# 11. 账户设置「数据共享」页（/app 侧栏入口 + 弹窗骨架）
# --------------------------------------------------------------------------- #
def test_workbench_has_data_sharing_settings_ui():
    user = _create_user("uishell@x.com")
    c = _login(_client(), user)
    r = c.get("/app")
    assert r.status_code == 200, r.get_json() if r.status_code != 200 else ""
    html = r.get_data(as_text=True)
    # 入口按钮（默认 hidden，applyAuthInfo 按登录态/预览态控制）与弹窗骨架
    assert 'id="datashare-btn"' in html
    assert 'id="datashare-mask"' in html
    assert 'id="datashare-check"' in html
    assert 'id="datashare-withdraw"' in html
    assert 'id="datashare-delete"' in html
    # 自愿项不预选：checkbox 无 checked 属性；链接只读协议（新窗口 + noopener）
    check_pos = html.find('id="datashare-check"')
    assert check_pos > 0
    seg = html[html.rfind("<input", 0, check_pos):check_pos]
    assert "checked" not in seg, "自愿研究项不得预勾选"
    assert 'href="/legal/research-sharing"' in html
    assert 'rel="noopener"' in html
    # 按钮默认隐藏（未登录/预览态不可见），JS applyAuthInfo 才展开
    btn_start = html.find('id="datashare-btn"')
    btn_end = html.find("</button>", btn_start)
    btn_block = html[btn_start:btn_end]
    assert "hidden" in btn_block, "数据共享入口默认必须隐藏"
