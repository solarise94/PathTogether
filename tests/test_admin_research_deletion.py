# -*- coding: utf-8 -*-
"""研究删除任务「管理员最小处置入口」测试（owner admin API；docs/agent-plan-
20260921-registration-consent-research.md §6.3-5 终态 failed 的人工处置）。

覆盖：

  - 门控：匿名 401 / 普通 user 403 / owner 预览态 403（GET 列表与 POST retry
    与全部 admin v1 端点同口径 _require_owner_admin_v1）；
  - 列表：处置诊断字段出线（status / error_code / attempts / next_retry_at /
    user_id + identity / consent_epoch / 创建与更新时间）+ terminal_failed
    标记；?status= 过滤与非法状态 400；
  - 终态 failed 复活：POST retry → pending、attempts 重置 0、next_retry_at
    立即可领取；worker drain_once 真实领取并完成（completed 由 worker 清理
    成功产生——复活是可真实执行的，不是只改状态）；审计同事务落条
    （操作者 / 动作 research.deletion_job.retry / 任务 id / previous_attempts）；
    复活后（pending）再次 retry → 409（不再是终态 failed）；
  - completed 不可经该入口改动：retry → 409，任务保持 completed
    （completed_at / attempts 不变）；
  - 非 终态一律拒绝：pending / 退避重试中的 failed（attempts < 上限）均 409
    且行不变；
  - 红线（无「直接置 completed」路径）：admin v1 路由表里 research-deletion
    只有列表 + retry 两个端点；app.py 不直接 UPDATE 删除任务表；
    research_consent_store 无 status='completed' 写入——删除任务的
    completed 只能由 research_deletion_worker 落库。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_admin_research_deletion.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import research_consent_store  # noqa: E402
import research_deletion_worker  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PASSWORD = "longpassword123"
SWITCH = "RESEARCH_COLLECTION_ENABLED"
LIST_URL = "/api/admin/v1/research-deletion-jobs"
MAX_ATTEMPTS = research_consent_store.MAX_DELETION_ATTEMPTS
# 真实清理实现（模块导入期留存：_run_to_terminal_failed 打桩后恢复用）
_REAL_DELETE_COPIES = research_deletion_worker._delete_research_copies


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """per-test 隔离 + 认证开启 + 采集开关/退避 env 复位。"""
    isolate_app(monkeypatch, tmp_path / "share", clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", raising=False)
    monkeypatch.delenv("RESEARCH_DELETION_LEASE_SECONDS", raising=False)
    yield


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


def _create_owner():
    return user_store.create_user("owner@x.com", PASSWORD, role="owner")


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _grant(user_id):
    """发布协议并 grant（epoch=1），让任务带真实 consent_epoch。"""
    agreement_store.ensure_builtin_documents()
    for dt in agreement_store.DOCUMENT_TYPES:
        agreement_store.publish_document(dt, _doc(dt)["version"])
    d = _doc("research_sharing")
    return research_consent_store.grant(
        user_id, document_version=d["version"],
        document_sha256=d["content_sha256"])["consent"]


def _make_job(user, granted=False):
    """建一条 user_request 删除任务；granted=True 先 grant（epoch=1）。"""
    epoch = _grant(user["user_id"])["epoch"] if granted else 0
    created = research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request", actor_user_id=user["user_id"])
    assert created["created"] is True
    assert created["job"]["consent_epoch"] == epoch
    return created["job"]


def _run_to_terminal_failed(monkeypatch, user):
    """把该用户任务打到达上限的终态 failed（worker 停止自动重试）。

    调用前须已有未了结任务（_make_job / withdraw）。
    """
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(MAX_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    job = _job_row(user["user_id"])
    assert job is not None and job["status"] == "failed"
    assert job["attempts"] == MAX_ATTEMPTS
    return job


def _job_row(user_id):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, user_id, reason, status, consent_epoch, "
                "attempts, online_cleared, exports_cleared, error_code, "
                "completed_at, next_retry_at, created_at, updated_at "
                "FROM research_data_deletion_jobs WHERE user_id=%s "
                "ORDER BY created_at DESC LIMIT 1", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _retry_url(job_id):
    return "%s/%s/retry" % (LIST_URL, job_id)


# --------------------------------------------------------------------------- #
# 1. 门控：匿名 / 普通 user / owner 预览态
# --------------------------------------------------------------------------- #
def test_gatekeeping_anonymous_user_preview(monkeypatch):
    owner = _create_owner()
    user = user_store.create_user("gate@x.com", PASSWORD)
    _make_job(user)
    _run_to_terminal_failed(monkeypatch, user)
    job_id = _job_row(user["user_id"])["job_id"]

    # 匿名：认证闸 401
    anon = _client()
    assert anon.get(LIST_URL).status_code == 401
    assert anon.get(LIST_URL).get_json()["code"] == "auth_required"
    assert anon.post(_retry_url(job_id)).status_code == 401

    # 普通 user：403（无 owner 权限）
    uc = _login(_client(), user)
    assert uc.get(LIST_URL).status_code == 403
    assert uc.post(_retry_url(job_id)).status_code == 403

    # owner 预览态：管理 API 一律 403（§14.1 与其余 admin v1 端点同口径）
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": user["user_id"]}).status_code == 200
    r = oc.get(LIST_URL)
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] in ("preview_forbidden",
                                             "preview_readonly")
    assert oc.post(_retry_url(job_id)).status_code == 403

    # owner 正常会话：两入口可用（预览不残留——新 client 新 session）
    ok = _login(_client(), owner)
    assert ok.get(LIST_URL).status_code == 200
    assert ok.post(_retry_url(job_id)).status_code == 200


# --------------------------------------------------------------------------- #
# 2. 列表：处置诊断字段出线 + 状态过滤
# --------------------------------------------------------------------------- #
def test_list_reports_diagnostic_fields(monkeypatch):
    owner = _create_owner()
    user = user_store.create_user("rdel@x.com", PASSWORD)
    job = _make_job(user, granted=True)
    _run_to_terminal_failed(monkeypatch, user)

    c = _login(_client(), owner)
    resp = c.get(LIST_URL)
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.get_json()
    items = body["items"]
    assert len(items) == 1
    item = items[0]
    # 字段白名单（不多不少）
    assert set(item.keys()) == {
        "job_id", "user_id", "identity", "reason", "status",
        "terminal_failed", "error_code", "attempts", "next_retry_at",
        "consent_epoch", "created_at", "updated_at", "completed_at"}
    assert item["job_id"] == job["job_id"]
    assert item["user_id"] == user["user_id"]
    assert item["identity"] == "rdel@x.com"  # 身份主列映射（邮箱用户名）
    assert item["reason"] == "user_request"
    assert item["status"] == "failed"
    assert item["terminal_failed"] is True
    assert item["error_code"] == "deletion_failed_RuntimeError"
    assert item["attempts"] == MAX_ATTEMPTS
    assert item["consent_epoch"] == 1
    for key in ("next_retry_at", "created_at", "updated_at"):
        assert isinstance(item[key], str) and item[key].endswith("Z")
    assert item["completed_at"] is None

    # 状态过滤 + 非法状态
    assert c.get(LIST_URL + "?status=completed").get_json()["items"] == []
    assert len(c.get(LIST_URL + "?status=failed")
               .get_json()["items"]) == 1
    r = c.get(LIST_URL + "?status=bogus")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# 3. 终态 failed → retry 复活为 pending + 审计 + worker 真实完成
# --------------------------------------------------------------------------- #
def test_retry_revives_terminal_failed_then_worker_completes(monkeypatch):
    owner = _create_owner()
    user = user_store.create_user("revive@x.com", PASSWORD)
    job = _make_job(user)
    _run_to_terminal_failed(monkeypatch, user)
    job_id = job["job_id"]
    c = _login(_client(), owner)

    r = c.post(_retry_url(job_id))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["previous_attempts"] == MAX_ATTEMPTS
    assert body["job"]["status"] == "pending"
    assert body["job"]["attempts"] == 0

    # DB：pending / attempts=0 / next_retry_at 已到期（worker 可立即领取）
    row = _job_row(user["user_id"])
    assert row["status"] == "pending" and row["attempts"] == 0
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT next_retry_at <= now() AS due, error_code "
                "FROM research_data_deletion_jobs WHERE job_id=%s", (job_id,))
            due = cur.fetchone()
    finally:
        conn.close()
    assert due["due"] is True
    assert due["error_code"] == "deletion_failed_RuntimeError", \
        "上一轮错误码保留为诊断线索"

    # 审计同事务落条：操作者 / 动作 / 任务 id / previous_attempts
    events = share_store.list_audit(action="research.deletion_job.retry")
    assert len(events) == 1
    ev = events[0]
    assert ev["actor_user_id"] == owner["user_id"]
    assert ev["target_type"] == "research_deletion_job"
    assert ev["target_id"] == job_id
    assert ev["detail"]["previous_attempts"] == MAX_ATTEMPTS
    assert ev["detail"]["attempts_reset_to"] == 0

    # 复活后（pending）不再是终态 failed：再次 retry → 409，行不变
    again = c.post(_retry_url(job_id))
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "deletion_job_not_terminal"
    assert _job_row(user["user_id"])["status"] == "pending"

    # worker 真实领取并完成：completed 由清理成功产生（不是管理入口写的）
    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _REAL_DELETE_COPIES)
    monkeypatch.delenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", raising=False)
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1 and summary["failed"] == 0
    final = _job_row(user["user_id"])
    assert final["status"] == "completed"
    assert final["attempts"] == 1  # 重置后新的一轮（复活 → 领取 +1）
    assert final["completed_at"] is not None
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None, "completed 后未了结任务清空（解除阻断）"


# --------------------------------------------------------------------------- #
# 4. completed 任务不能被该入口改动
# --------------------------------------------------------------------------- #
def test_completed_job_cannot_be_modified(monkeypatch):
    owner = _create_owner()
    user = user_store.create_user("done@x.com", PASSWORD)
    _make_job(user)
    summary = research_deletion_worker.drain_once()  # 真实清理 → completed
    assert summary["completed"] == 1
    before = _job_row(user["user_id"])
    assert before["status"] == "completed"

    c = _login(_client(), owner)
    r = c.post(_retry_url(before["job_id"]))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "deletion_job_not_terminal"

    after = _job_row(user["user_id"])
    assert after["status"] == "completed"
    assert after["attempts"] == before["attempts"]
    assert after["completed_at"] == before["completed_at"]
    # 不写任何审计（处置未发生）
    assert share_store.list_audit(action="research.deletion_job.retry") == []


# --------------------------------------------------------------------------- #
# 5. 非终态（pending / 退避重试中的 failed）拒绝
# --------------------------------------------------------------------------- #
def test_non_terminal_states_rejected(monkeypatch):
    owner = _create_owner()
    user = user_store.create_user("nonterm@x.com", PASSWORD)
    job = _make_job(user)
    c = _login(_client(), owner)

    # pending：本就会被 worker 领取，不允许人工复活
    r = c.post(_retry_url(job["job_id"]))
    assert r.status_code == 409
    row = _job_row(user["user_id"])
    assert row["status"] == "pending" and row["attempts"] == 0

    # 退避重试中的 failed（attempts=1 < 上限）：仍会自动重试，不允许
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("boom (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    research_deletion_worker.drain_once(limit=1)
    assert _job_row(user["user_id"])["attempts"] == 1
    r = c.post(_retry_url(job["job_id"]))
    assert r.status_code == 409
    assert _job_row(user["user_id"])["attempts"] == 1, \
        "退避中的 failed 不被重置（保留自动重试节奏）"

    # 不存在的任务：404
    r = c.post(_retry_url("rdj_does_not_exist"))
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "deletion_job_not_found"


# --------------------------------------------------------------------------- #
# 6. 红线：不存在「直接置 completed」的管理路径
# --------------------------------------------------------------------------- #
def test_no_direct_completed_admin_path():
    """admin v1 只有列表 + retry 两个 research-deletion 端点；completed 只能
    由 research_deletion_worker 清理成功产生（源级防漂移守卫）。"""
    rules = sorted(str(rule) for rule in app_mod.app.url_map.iter_rules()
                   if "research-deletion" in str(rule))
    assert rules == [
        "/api/admin/v1/research-deletion-jobs",
        "/api/admin/v1/research-deletion-jobs/<job_id>/retry",
    ], "研究删除管理入口只允许「列表 + 重新执行」两个端点"
    for rule in rules:
        assert "complete" not in rule, "不允许出现 complete 类端点"

    repo = Path(app_mod.__file__).resolve().parent
    app_src = (repo / "app.py").read_text(encoding="utf-8")
    store_src = (repo / "research_consent_store.py").read_text(encoding="utf-8")
    worker_src = (repo / "research_deletion_worker.py").read_text(
        encoding="utf-8")
    # 路由层不直接写删除任务表（一切写经 store / worker）
    assert "UPDATE research_data_deletion_jobs" not in app_src
    # store 层唯一的任务状态赋值是复活为 'pending'（admin_retry_deletion_job）
    assert "status='completed'" not in store_src
    # completed 的唯一写入方：worker 清理成功后的 _finalize_success
    assert "SET status='completed'" in worker_src
