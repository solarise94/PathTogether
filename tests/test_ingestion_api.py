# -*- coding: utf-8 -*-
"""COS 直传 Phase 3 控制 API 测试（docs/cos-direct-upload-audit-plan.md §4/§6.1）。

覆盖：capability off 不可见；422 大小合同（invalid_declared_size /
cos_exceeds_admission 不建行不占预约）；格式白名单；池不足 202 等待 + 排队
位置；parts/sign 唯一授权接口（绑定长度/计划内/批量/速率/no-store）；
upload-complete 幂等；cancel 幂等与已入库拒绝；ownership 403；
capability payload 下发（off 时零 DB、manual_only）。
"""

import pytest

import app as app_mod
import cos_config
import cos_pool_store
import ingestion_store as ist
import upload_guard
from _pt_helpers import csrf_client


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 100)
    cos_pool_store.ensure_pool_state()
    yield


def _mkuser(user_id, role):
    """建真实 users 行（会话身份校验在全量套件语境下要求用户存在；
    伪造 session 在单文件跑时侥幸通过，不能依赖）。"""
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


@pytest.fixture()
def owner_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("owner-1", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-1@x"
        s["user_id"] = "owner-1"
        s["role"] = "owner"
        s["auth_version"] = 1
    return client


def _create(client, filename="cosapi-a.svs", size=500_000, **extra):
    return client.post("/api/ingestions", json=dict(
        filename=filename, declared_size=size, **extra))


def _mk_uploading(client, size=500_000):
    """创建并推进到 uploading（直改 store，绕过 worker）。"""
    r = _create(client, filename="cosapi-%s.svs" % size, size=size)
    assert r.status_code == 202, r.get_json()
    job_id = r.get_json()["job_id"]
    if ist.get_job(job_id)["state"] != ist.PREPARING:
        out = ist.try_admit_job(job_id)
        assert out["outcome"] == "admitted", out["reason"]
    claim = ist.claim_next_job_for_worker([ist.PREPARING])
    part = cos_config.COS_PART_BYTES
    plan = []
    offset = 0
    n = 1
    while offset < size:
        length = min(part, size - offset)
        plan.append({"part_number": n, "offset": offset, "length": length})
        offset += length
        n += 1
    ist.worker_begin_uploading(
        job_id, claim["worker_generation"], bucket="bucket-appid",
        object_key="incoming/owner-1/%s/abc" % job_id, upload_id="up-1",
        part_plan=plan)
    ist.release_worker_lease(job_id, claim["worker_lease_token"])
    return job_id, plan


# --------------------------------------------------------------------------- #
# 可用性与 422 大小合同
# --------------------------------------------------------------------------- #
def test_capability_off_returns_404(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "off")
    r = _create(owner_client)
    assert r.status_code == 404
    assert r.get_json()["code"] == "cos_unavailable"


def test_invalid_declared_size_422(owner_client):
    for bad in (0, -5, "abc", None):
        r = _create(owner_client, size=bad if bad is not None else 0)
        r2 = owner_client.post("/api/ingestions", json={
            "filename": "a.svs", "declared_size": bad})
        assert r2.status_code == 422, bad
        assert r2.get_json()["code"] == "invalid_declared_size"
    assert ist.waiting_and_holding_counts()["waiting"] == 0


def test_exceeds_admission_422_no_side_effects(owner_client):
    r = _create(owner_client, size=900_001)
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "cos_exceeds_admission"
    assert body["max_size_bytes"] == 900_000
    assert body["fallback_transport"] == "v2"
    # 不建行、不占预约、不进等待
    assert ist.waiting_and_holding_counts()["waiting"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_exact_admission_boundary_creates(owner_client):
    r = _create(owner_client, size=900_000)
    assert r.status_code == 202


def test_format_whitelist(owner_client):
    for name in ("cosapi-a.zip", "cosapi-a.mrxs", "cosapi-a.kfb", "cosapi-a.bmp", "cosapinoext"):
        r = _create(owner_client, filename=name)
        assert r.status_code == 422, name
        assert r.get_json()["code"] == "cos_format_unsupported"
    for name in ("cosapi-b.svs", "cosapi-b.tif", "cosapi-c.ndpi",
                 "cosapi-d.bif"):
        r = _create(owner_client, filename=name, size=100_000)
        assert r.status_code == 202, name
        # 每身份 1 active + 1 waiting：逐个取消腾位再验下一格式
        owner_client.post("/api/ingestions/%s/cancel"
                          % r.get_json()["job_id"])


# --------------------------------------------------------------------------- #
# 创建 / 等待 / 幂等
# --------------------------------------------------------------------------- #
def test_create_admits_and_responds_state(owner_client):
    r = _create(owner_client, idempotency_key="K1")
    assert r.status_code == 202
    body = r.get_json()
    assert body["state"] == ist.PREPARING
    assert body["stage"] == "uploading"
    assert body["policy_version"] if "policy_version" in body else True
    # 幂等重放返回同一任务
    r2 = _create(owner_client, idempotency_key="K1")
    assert r2.get_json()["job_id"] == body["job_id"]


def test_pool_exhausted_waits_with_position(owner_client):
    a = _create(owner_client, filename="cosapi-e.svs", idempotency_key="A")
    assert a.get_json()["state"] == ist.PREPARING
    b = _create(owner_client, filename="cosapi-f.svs", idempotency_key="B")
    assert b.status_code == 202
    body = b.get_json()
    assert body["state"] == ist.WAITING
    assert body["code"] == "cos_waiting_capacity"
    assert body["queue_position"] == 0
    assert "eta_seconds" not in body  # 不承诺预计秒数
    assert "urls" not in body and "upload_id" not in body  # 等待不发凭证
    # 状态接口同口径
    s = owner_client.get("/api/ingestions/%s" % body["job_id"])
    assert s.get_json()["stage"] == "waiting_space"


def test_second_waiting_rejected(owner_client):
    a = _create(owner_client, filename="cosapi-e.svs", idempotency_key="A")
    assert a.get_json()["state"] == ist.PREPARING
    b = _create(owner_client, filename="cosapi-f.svs", idempotency_key="B")
    assert b.get_json()["state"] == ist.WAITING
    # 同一身份（owner-1）已有一条 waiting → 409
    c = _create(owner_client, filename="cosapi-g.svs", idempotency_key="C")
    assert c.status_code == 409
    assert c.get_json()["code"] == "cos_waiting_limit"


# --------------------------------------------------------------------------- #
# parts/sign
# --------------------------------------------------------------------------- #
def test_parts_sign_issues_bound_urls(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 200_000)
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    nums = [p["part_number"] for p in plan[:2]]
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": nums})
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["transport"] == "presign_parts"
    assert len(body["urls"]) == 2
    for item, n in zip(body["urls"], nums):
        assert item["part_number"] == n
        assert item["content_length"] == plan[n - 1]["length"]
        url = item["url"]
        assert "partNumber=%d" % n in url
        assert "uploadId=up-1" in url
        assert "q-signature=" in url
        assert url.startswith("https://bucket-appid.cos.ap-shanghai"
                              ".myqcloud.com/incoming/")


def test_parts_sign_validates_state_and_plan(owner_client):
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    # 计划外编号
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [999]})
    assert r.status_code == 400
    # 空数组 / 非整数
    for bad in ([], ["1"], [1.5]):
        r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                              json={"part_numbers": bad})
        assert r.status_code == 400, bad
    # 非 uploading 态：先 complete
    assert owner_client.post(
        "/api/ingestions/%s/upload-complete" % job_id).status_code == 202
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1]})
    assert r.status_code == 409


def test_parts_sign_batch_and_rate_limits(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCH_MAX_PARTS", 2)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 200_000)
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1, 2, 3]})
    assert r.status_code == 400
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCH_MAX_PARTS", 8)
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 1)
    assert owner_client.post(
        "/api/ingestions/%s/parts/sign" % job_id,
        json={"part_numbers": [1]}).status_code == 200
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [2]})
    assert r.status_code == 429
    assert r.get_json()["code"] == "cos_sign_rate_limited"


def test_parts_sign_blocked_when_reconcile_paused(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cos_pool_store.record_observation(cur, 10_000_000, True)
    finally:
        conn.close()
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1]})
    assert r.status_code == 503
    assert r.get_json()["code"] == "cos_capacity_reconcile_required"


# --------------------------------------------------------------------------- #
# upload-complete / cancel / ownership
# --------------------------------------------------------------------------- #
def test_upload_complete_idempotent(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    r1 = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    r2 = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r1.status_code == r2.status_code == 202
    assert r1.get_json()["state"] == r2.get_json()["state"] == ist.COMPLETING
    assert r1.get_json()["stage"] == "awaiting_server"
    # resume：completing → uploading
    r3 = owner_client.post("/api/ingestions/%s/resume" % job_id)
    assert r3.get_json()["state"] == ist.UPLOADING


def test_cancel_idempotent(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    r1 = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    r2 = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    assert r1.status_code == r2.status_code == 202
    assert r1.get_json()["state"] == ist.CANCELLED


def test_cancel_after_ready_rejected(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=1000)
    # 缩小计划直推到 ready（复用 phase1 手法）
    c = ist.claim_next_job_for_worker([ist.UPLOADING])
    tok = c["worker_lease_token"]
    ist.request_upload_complete(job_id)
    c = ist.claim_next_job_for_worker([ist.COMPLETING], holding_token=tok)
    tok = c["worker_lease_token"]
    ist.worker_pin_source(job_id, c["worker_generation"], version_id="v",
                          etag="e", size_bytes=1000)
    c = ist.claim_next_job_for_worker([ist.QUEUED], holding_token=tok)
    tok = c["worker_lease_token"]
    ist.worker_begin_download(job_id, c["worker_generation"])
    c = ist.claim_next_job_for_worker([ist.DOWNLOADING], holding_token=tok)
    ist.worker_begin_validating(job_id, c["worker_generation"])
    ist.worker_persist_commit_intent(
        job_id, c["worker_generation"],
        {"target": "cosapi.svs", "version": "v", "sha256": "x" * 64})
    ist.worker_settle_ready(job_id, c["worker_generation"],
                            slide_canonical_name="cosapi.svs",
                            sha256_actual="x" * 64, settle_bytes=1000)
    r = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    assert r.status_code == 409
    assert r.get_json()["code"] == "already_committed"


def test_ownership_isolated(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    _mkuser("user-2", "user")
    other = csrf_client(app_mod.app.test_client())
    with other.session_transaction() as s:
        s["auth_user"] = "user-2@x"
        s["user_id"] = "user-2"
        s["role"] = "user"
        s["auth_version"] = 1
    for method, path in (
            ("get", "/api/ingestions/%s" % job_id),
            ("post", "/api/ingestions/%s/upload-complete" % job_id),
            ("post", "/api/ingestions/%s/cancel" % job_id),
            ("post", "/api/ingestions/%s/parts/sign" % job_id)):
        r = getattr(other, method)(path, json={"part_numbers": [1]}
                                   if method == "post" and "sign" in path
                                   else None)
        assert r.status_code == 403, (method, path)
    # 不存在同样 403（不泄露存在性）
    assert other.get("/api/ingestions/inj_nope").status_code == 403


# --------------------------------------------------------------------------- #
# capability payload
# --------------------------------------------------------------------------- #
def test_capability_payload_shapes(monkeypatch):
    # 钉死身份（全量套件语境下 ambient auth 状态不可依赖——其余测试改过
    # AUTH_ENABLED/会话；单文件跑通过是侥幸，见 run-2/3 教训）
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": "owner-1"})
    with app_mod.app.test_request_context("/"):
        # off：零 DB 静态不可用
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "off")
        p = app_mod._cos_upload_capability_payload(demo=False)
        assert p["available"] is False and p["manual_only"] is True
        # on：可用且含参数
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
        p = app_mod._cos_upload_capability_payload(demo=False)
        assert p["available"] is True
        assert p["max_size_bytes"] == 900_000
        assert p["policy_version"] == "v1-manual"
        assert "svs" in p["formats"]
        # demo 恒不可用
        assert app_mod._cos_upload_capability_payload(
            demo=True)["available"] is False
        # internal：仅 owner（current_identity 已钉为 owner）
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "internal")
        assert app_mod._cos_upload_capability_payload(demo=False)[
            "available"] is True
        # internal 下 user 不可用
        monkeypatch.setattr(app_mod, "current_identity",
                            lambda: {"role": "user", "user_id": "u9"})
        assert app_mod._cos_upload_capability_payload(demo=False)[
            "available"] is False
