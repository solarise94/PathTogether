# -*- coding: utf-8 -*-
"""用户研究数据视图（2026-10-20 评审 §三：「研究删除」入口简化，执行链保留）。

冻结的合同：

  - 「研究删除」不再是独立一级页：研究授权状态与删除任务进度并入用户详情
    抽屉的「研究数据」折叠区，数据源 = GET /api/admin/v1/users/<user_id>/research
    （owner-only、只读、no-store）；旧 #research-deletion 深链过渡到「用户」页；
    旧 API/桥（/api/admin/v1/research-deletion-jobs 与
    admin.researchDeletionJobs.list/retry）**保留兼容**；
  - 视图只读：没有代用户授权/撤回的入口；管理员唯一写动作仍是
    POST .../research-deletion-jobs/<job_id>/retry（仅终态 failed）；
  - 采集总开关（RESEARCH_COLLECTION_ENABLED）与个人授权分开出线：
    collection_enabled 关着时 ingest_allowed 恒 False（即使已授权）；
  - 删除任务出线含 online_cleared/exports_cleared/**backups_pending**——
    completed + backups_pending=true 是「在线副本已清理，备份等待轮换」
    文案的唯一依据（不宣称备份已删除）；
  - 概览只在有**异常**删除任务（重试耗尽的终态 failed）时给计数
    （research.abnormal_deletion_jobs），点入即按 research=abnormal 筛选用户；
  - 执行链不变：撤回同事务建删除任务、未了结任务持续阻断研究采集、
    completed 只能由 worker 清理成功产生（本文件同时守卫这一点）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub

import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import research_consent_store  # noqa: E402
import research_deletion_worker  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
from _pt_helpers import make_client as _client  # noqa: E402

PASSWORD = "longpassword123"
SWITCH = "RESEARCH_COLLECTION_ENABLED"
MAX_ATTEMPTS = research_consent_store.MAX_DELETION_ATTEMPTS
DETAIL_URL = "/api/admin/v1/users/%s/research"
LIST_URL = "/api/admin/v1/research-deletion-jobs"
OVERVIEW_URL = "/api/admin/v1/overview"
USERS_URL = "/api/admin/v1/users"
PLUGIN_DIR = Path(app_mod.__file__).resolve().parent / "plugins" \
    / "pathtogether-admin" / "ui"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path / "share", clear_stores=True)
    app_mod.AUTH_ENABLED = True
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", raising=False)
    yield


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _owner_client():
    owner = user_store.create_user("owner@x.com", PASSWORD, role="owner")
    return owner, _login(_client(), owner)


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _publish_docs():
    agreement_store.ensure_builtin_documents()
    for dt in agreement_store.DOCUMENT_TYPES:
        agreement_store.publish_document(dt, _doc(dt)["version"])


def _grant(user):
    _publish_docs()
    d = _doc("research_sharing")
    return research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])["consent"]


def _terminal_failed_job(monkeypatch, user):
    """把该用户的删除任务打到达上限的终态 failed（worker 停止自动重试）。"""
    created = research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request", actor_user_id=user["user_id"])
    assert created["created"] is True
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(MAX_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    job = research_consent_store.get_active_deletion_job(user["user_id"])
    assert job is not None and job["status"] == "failed"
    assert job["attempts"] == MAX_ATTEMPTS
    return job


# --------------------------------------------------------------------------- #
# 1. 门控：匿名 401 / 普通 user 403 / owner 预览态 403
# --------------------------------------------------------------------------- #
def test_gatekeeping_anonymous_user_preview():
    owner, oc = _owner_client()
    user = user_store.create_user("gate@x.com", PASSWORD)

    anon = _client()
    r = anon.get(DETAIL_URL % user["user_id"])
    assert r.status_code == 401
    assert r.get_json()["code"] == "auth_required"

    uc = _login(_client(), user)
    assert uc.get(DETAIL_URL % user["user_id"]).status_code == 403

    assert oc.post("/api/admin/preview/start",
                   json={"user_id": user["user_id"]}).status_code == 200
    r = oc.get(DETAIL_URL % user["user_id"])
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] in ("preview_forbidden",
                                            "preview_readonly")


# --------------------------------------------------------------------------- #
# 2. 视图内容：授权状态/版本/时间 + 采集总开关 + 删除任务 + no-store
# --------------------------------------------------------------------------- #
def test_detail_reports_consent_and_collection_switch():
    owner, oc = _owner_client()
    user = user_store.create_user("view@x.com", PASSWORD)
    consent = _grant(user)

    r = oc.get(DETAIL_URL % user["user_id"])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_json()
    assert body["user_id"] == user["user_id"]
    assert body["identity"] == "view@x.com"
    assert body["consent"]["state"] == "granted"
    assert body["consent"]["granted"] is True
    assert body["consent"]["document_version"] == consent["document_version"]
    assert body["consent"]["epoch"] == consent["epoch"]
    assert body["consent"]["granted_at"].endswith("Z")
    assert body["consent"]["withdrawn_at"] is None
    # 采集总开关与个人授权分开：默认关闭 → 即使已授权也不采集
    assert body["collection_enabled"] is False
    assert body["ingest_allowed"] is False
    assert body["deletion_jobs"] == []
    assert body["active_deletion_job"] is None

    # 开关打开（平台级）后已授权用户才 ingest_allowed
    os.environ[SWITCH] = "1"
    try:
        body2 = oc.get(DETAIL_URL % user["user_id"]).get_json()
        assert body2["collection_enabled"] is True
        assert body2["ingest_allowed"] is True
    finally:
        os.environ.pop(SWITCH, None)

    # 未授权用户：state/版本/时间均为空，不伪造「已授权」
    plain = user_store.create_user("plain@x.com", PASSWORD)
    b3 = oc.get(DETAIL_URL % plain["user_id"]).get_json()
    assert b3["consent"]["state"] is None
    assert b3["consent"]["granted"] is False
    assert b3["consent"]["document_version"] is None
    assert b3["consent"]["granted_at"] is None
    assert b3["consent"]["epoch"] == 0


def test_detail_rejects_unknown_user_and_bad_id():
    owner, oc = _owner_client()
    r = oc.get(DETAIL_URL % "usr_nope")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "user_not_found"
    r = oc.get("/api/admin/v1/users/%s/research" % ("x" * 200))
    assert r.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# 3. completed + backups_pending：文案依据出线（在线已清、备份待轮换）
# --------------------------------------------------------------------------- #
def test_completed_job_exposes_backups_pending_wording_source():
    owner, oc = _owner_client()
    user = user_store.create_user("done@x.com", PASSWORD)
    _grant(user)
    created = research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request", actor_user_id=user["user_id"])
    summary = research_deletion_worker.drain_once()  # 真实清理 → completed
    assert summary["completed"] == 1

    body = oc.get(DETAIL_URL % user["user_id"]).get_json()
    assert len(body["deletion_jobs"]) == 1
    job = body["deletion_jobs"][0]
    assert job["job_id"] == created["job"]["job_id"]
    assert job["status"] == "completed"
    assert job["terminal_failed"] is False
    assert job["online_cleared"] is True
    assert job["exports_cleared"] is True
    # 「在线副本已清理，备份等待轮换」的唯一依据（不做备份已删的宣称）
    assert job["backups_pending"] is True
    assert job["completed_at"].endswith("Z")
    assert body["active_deletion_job"] is None, "completed 后未了结任务清空"


# --------------------------------------------------------------------------- #
# 4. 终态 failed：terminal_failed 出线 + 唯一写动作仍是 retry（仅终态）
# --------------------------------------------------------------------------- #
def test_terminal_failed_exposed_and_retry_only_for_terminal(monkeypatch):
    owner, oc = _owner_client()
    user = user_store.create_user("failed@x.com", PASSWORD)
    job = _terminal_failed_job(monkeypatch, user)

    body = oc.get(DETAIL_URL % user["user_id"]).get_json()
    row = body["deletion_jobs"][0]
    assert row["status"] == "failed"
    assert row["terminal_failed"] is True
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["error_code"] == "deletion_failed_RuntimeError"
    assert body["active_deletion_job"]["job_id"] == row["job_id"], \
        "未了结（failed）任务即 active——研究侧持续阻断"

    # 概览：异常任务计数（点入筛选的依据）
    ov = oc.get(OVERVIEW_URL).get_json()
    assert ov["research"]["available"] is True
    assert ov["research"]["abnormal_deletion_jobs"] == 1

    # 用户列表 research=abnormal 只出有异常任务的用户
    ul = oc.get(USERS_URL + "?kind=all&research=abnormal").get_json()
    assert [u["user_id"] for u in ul["items"]] == [user["user_id"]]
    # 非法取值 400（不静默忽略）
    bad = oc.get(USERS_URL + "?research=whatever")
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_request"

    # 唯一写动作：retry（既有端点，鉴权/幂等/审计/worker 全沿用）
    r = oc.post("%s/%s/retry" % (LIST_URL, row["job_id"]))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["previous_attempts"] == MAX_ATTEMPTS
    # 复活后不再是终态：视图同步（terminal_failed=False，仍是未了结任务）
    body2 = oc.get(DETAIL_URL % user["user_id"]).get_json()
    row2 = body2["deletion_jobs"][0]
    assert row2["status"] == "pending" and row2["attempts"] == 0
    assert row2["terminal_failed"] is False
    assert body2["active_deletion_job"] is not None
    # 再次 retry → 409（仅终态 failed 可重新执行）
    again = oc.post("%s/%s/retry" % (LIST_URL, row["job_id"]))
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "deletion_job_not_terminal"

    # 无异常任务后概览计数回落 0（UI 整块隐藏）
    ov2 = oc.get(OVERVIEW_URL).get_json()
    assert ov2["research"]["abnormal_deletion_jobs"] == 0
    assert oc.get(USERS_URL + "?kind=all&research=abnormal"
                  ).get_json()["items"] == []


# --------------------------------------------------------------------------- #
# 5. 撤回 → 同事务建删除任务（执行链保留） + 阻断语义不变
# --------------------------------------------------------------------------- #
def test_withdrawal_still_creates_deletion_job_and_blocks_ingest():
    owner, oc = _owner_client()
    user = user_store.create_user("withdraw@x.com", PASSWORD)
    consent = _grant(user)
    os.environ[SWITCH] = "1"
    try:
        assert research_consent_store.ingest_allowed(user["user_id"]) is True
        result = research_consent_store.withdraw(
            user["user_id"], expected_epoch=consent["epoch"],
            actor_user_id=user["user_id"])
        job = result["deletion_job"]
        assert job is not None and job["reason"] == "withdrawal", \
            "撤回与删除任务同事务创建（执行链不因入口简化而下线"
        assert job["status"] == "pending"
        # 未了结删除任务持续阻断研究采集
        assert research_consent_store.ingest_allowed(user["user_id"]) is False
        body = oc.get(DETAIL_URL % user["user_id"]).get_json()
        assert body["consent"]["state"] == "withdrawn"
        assert body["consent"]["withdrawn_at"].endswith("Z")
        assert body["active_deletion_job"]["job_id"] == job["job_id"]
        # worker 清理成功 → completed（解除阻断的唯一终态）：
        # 未了结任务清空 → 研究判定不再报 deletion_pending
        assert research_deletion_worker.drain_once()["completed"] == 1
        assert research_consent_store.get_active_deletion_job(
            user["user_id"]) is None
        verdict = research_consent_store.evaluate_research_access(
            user["user_id"])
        assert "deletion_pending" not in verdict["reasons"]
        assert "not_granted" in verdict["reasons"], \
            "撤回后仍是未授权状态（撤回不等于重新授权）"
    finally:
        os.environ.pop(SWITCH, None)


# --------------------------------------------------------------------------- #
# 6. 红线：视图只读；无「代用户授权/撤回」或「直接置 completed」入口
# --------------------------------------------------------------------------- #
def test_view_is_read_only_and_no_admin_grant_path():
    owner, oc = _owner_client()
    user = user_store.create_user("ro@x.com", PASSWORD)
    detail = DETAIL_URL % user["user_id"]
    # 只允许 GET：POST/PUT/DELETE 一律 405（无任何写语义）
    for method in ("post", "put", "delete", "patch"):
        r = getattr(oc, method)(detail)
        assert r.status_code in (404, 405), method
    # 路由表里没有管理侧研究授权写端点（grant/withdraw 只有用户侧
    # /api/account/*；管理侧唯一写动作是既有 retry）
    rules = [str(rule) for rule in app_mod.app.url_map.iter_rules()]
    assert not [r for r in rules if r.startswith("/api/admin") and
                ("research" in r and ("grant" in r or "withdraw" in r))]
    # 源级守卫：本视图函数体内不出现任何 consent/删除任务写调用
    src = (Path(app_mod.__file__).resolve().parent / "app.py").read_text(
        encoding="utf-8")
    body = src.split("def admin_v1_user_research(")[1].split(
        "\n@app.route")[0]
    for forbidden in ("research_consent_store.grant(",
                      "research_consent_store.withdraw(",
                      "research_consent_store.create_deletion_job(",
                      "admin_retry_deletion_job(",
                      "UPDATE research_data_deletion_jobs"):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------- #
# 7. 深链与入口（源级守卫）：旧 #research-deletion 过渡到用户页，旧 API 保留
# --------------------------------------------------------------------------- #
def test_deep_link_alias_and_retired_page_source_guards():
    main_js = (PLUGIN_DIR / "main.js").read_text(encoding="utf-8")
    html = (PLUGIN_DIR / "index.html").read_text(encoding="utf-8")
    # 旧深链过渡到新区域（用户页），不是回概览
    assert 'HASH_PAGE_ALIASES = { "research-deletion": "users" }' in main_js
    # 一级导航与页面骨架移除
    assert 'data-page="research-deletion"' not in html
    assert 'id="adm-page-research-deletion"' not in html
    assert '"research-deletion": "研究删除"' not in main_js
    # 旧 API/桥保留兼容（执行链与处置入口不被下线）
    host_js = (Path(app_mod.__file__).resolve().parent / "static"
               / "admin-host.js").read_text(encoding="utf-8")
    assert '"admin.researchDeletionJobs.list": "admin:users:read"' in host_js
    assert '"admin.researchDeletionJobs.retry": "admin:users:write"' in host_js
    assert '@app.route("/api/admin/v1/research-deletion-jobs"' in \
        (Path(app_mod.__file__).resolve().parent / "app.py").read_text(
            encoding="utf-8")
    # 新的用户研究视图桥存在，且只读（users:read）
    assert '"admin.users.research": "admin:users:read"' in host_js
    assert '"admin.users.research"' in main_js
    # 抽屉里的状态节点是 <p>（不是 select）：错误/重试提示写文案，绝不
    # 清空任何 select 的 option（2026-10-20 §一.2 重复 DOM ID 修复）
    assert 'statusEl.id = "adm-drawer-research-status";' in main_js
    assert 'id="adm-drawer-research-status"' not in html  # 动态节点（无重复 ID）
