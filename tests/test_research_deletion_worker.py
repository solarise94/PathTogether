# -*- coding: utf-8 -*-
"""研究副本删除执行器测试（docs/agent-plan-20260921-registration-consent-
research.md §6.3/§8；P1 修复：0062 只落任务，本批补执行器与 90 天清理）。

覆盖：

  - 迁移 0064（attempts/next_retry_at）随 ensure_schema 应用；
  - 撤回（withdrawal）→ drain_once 实际删除研究副本：events → conversation
    items → sessions → subjects 伪名映射，任务 pending→running→completed，
    online/exports cleared；业务数据（slides/rois/users/consent/历史/接受
    凭据）一概未动；
  - 执行期间阻断：删除义务未了结（pending/running、退避重试中的 failed
    **以及达上限的终态 failed**）时 evaluate_research_access 带
    deletion_pending、建研究会话 403——终态 failed 只停止 worker 自动
    重试，不解除研究阻断（P1 修复：停止重试与解除限制是两件事）；
  - completed 后解除阻断：用户可再授权（新 epoch）并正常采集，新 subject
    伪名、旧 epoch 数据不复活；
  - user_request 任务同样执行（epoch=申请时当前 epoch=全部现有副本）；
  - 失败有界退避重试：error_code 只含异常类名（安全错误码）；退避期满
    重试成功落 completed；持续失败达 MAX_DELETION_ATTEMPTS 后 failed 为
    终态不再领取，但研究侧仍阻断、任务以「待人工处置」真实状态可见；
  - running 超租约回收（重启安全：worker 崩溃后任务可被重领）；
  - 撤回推进/复活退避中的 failed 任务（新 epoch + pending 立即可领取）；
  - 90 天到期清理（§8）：expires_at 到期的 events/conversation items/
    sessions 删除，未到期保留；会话删除级联残余事件。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_research_deletion_worker.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import research_consent_store  # noqa: E402
import research_deletion_worker  # noqa: E402
import research_store  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PASSWORD = "longpassword123"
MIGRATION_0064 = "0064_research_deletion_execution.sql"
SLIDE = "p1-del-slide.ndpi"
SWITCH = "RESEARCH_COLLECTION_ENABLED"


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
    research_store._PSEUDONYM_SALT_CACHE["value"] = None
    import _billing_helpers as bh
    bh.seed_spend_settings()
    yield


def _create_user(login_id):
    return user_store.create_user(login_id, PASSWORD)


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


def _doc(doc_type):
    for d in agreement_store.builtin_documents():
        if d["document_type"] == doc_type:
            return d
    raise AssertionError("missing builtin doc %s" % doc_type)


def _publish_and_grant(user_id):
    agreement_store.ensure_builtin_documents()
    for dt in agreement_store.DOCUMENT_TYPES:
        agreement_store.publish_document(dt, _doc(dt)["version"])
    d = _doc("research_sharing")
    return research_consent_store.grant(
        user_id, document_version=d["version"],
        document_sha256=d["content_sha256"])["consent"]


def _open_collection(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")


def _own_slide(user, name=SLIDE):
    share_store.set_slide_meta(name, owner_user_id=user["user_id"])
    return name


def _zoom(seq):
    return {
        "event_id": "evt_%012d" % seq,
        "seq": seq,
        "action": "zoom_in",
        "schema_version": research_store.SCHEMA_VERSION,
        "payload": {
            "bbox_before": [0.0, 0.0, 1.0, 1.0],
            "bbox_after": [0.25, 0.25, 0.5, 0.5],
            "image_zoom_ratio": 1.5,
            "input_kind": "wheel",
            "changed_center": True,
        },
    }


def _collect(monkeypatch, user, login, n_events=2, slide=SLIDE):
    """授权 + 本人切片 + 建研究会话 + 写 n 条事件；返回 (client, sid, epoch)。"""
    _open_collection(monkeypatch)
    _publish_and_grant(user["user_id"])
    _own_slide(user, slide)
    client = _login(_client(), user)
    resp = client.post("/api/research/viewing-sessions", json={"slide": slide})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    sid, epoch = body["viewing_session_id"], body["consent_epoch"]
    resp = client.post("/api/research/viewer-events", json={
        "viewing_session_id": sid, "consent_epoch": epoch,
        "events": [_zoom(i) for i in range(1, n_events + 1)]})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["accepted"] == n_events
    return client, sid, epoch


def _insert_business_roi(slide, owner_user_id, annotation_id):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rois (id, token, slide, annotation_id, label, "
                "type, geom, owner_user_id) VALUES "
                "(%s,'tok-test',%s,%s,'业务标注','rect','{}'::jsonb,%s)",
                ("roi_" + annotation_id, slide, annotation_id, owner_user_id))
        conn.commit()
    finally:
        conn.close()


def _job_row(user_id):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, reason, status, consent_epoch, attempts, "
                "online_cleared, exports_cleared, backups_pending, "
                "error_code, completed_at, next_retry_at "
                "FROM research_data_deletion_jobs WHERE user_id=%s "
                "ORDER BY created_at DESC LIMIT 1", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _count(sql, params):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _research_rows(user_id):
    """该 user 的研究副本行数（subjects/sessions/events）。"""
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM research_subjects "
                "         WHERE user_id=%s) AS subjects,"
                " (SELECT count(*) FROM research_viewing_sessions s "
                "    JOIN research_subjects sub ON sub.subject_id=s.subject_id"
                "    WHERE sub.user_id=%s) AS sessions,"
                " (SELECT count(*) FROM research_viewer_events e "
                "    JOIN research_viewing_sessions s ON s.session_id=e.session_id"
                "    JOIN research_subjects sub ON sub.subject_id=s.subject_id"
                "    WHERE sub.user_id=%s) AS events,"
                " (SELECT count(*) FROM research_conversation_items i "
                "    JOIN research_subjects sub ON sub.subject_id=i.subject_id"
                "    WHERE sub.user_id=%s) AS items",
                (user_id, user_id, user_id, user_id))
            return cur.fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 迁移 0064
# --------------------------------------------------------------------------- #
def test_migration_0064_applied_and_defaults():
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (MIGRATION_0064,))
            assert cur.fetchone() is not None, "0064 应已被 ensure_schema 应用"
            cur.execute(
                "SELECT column_name, column_default FROM information_schema"
                ".columns WHERE table_name='research_data_deletion_jobs' "
                "AND column_name IN ('attempts','next_retry_at')")
            cols = {r["column_name"]: r["column_default"] for r in cur.fetchall()}
            assert set(cols) == {"attempts", "next_retry_at"}
            cur.execute(
                "SELECT 1 FROM pg_indexes WHERE indexname="
                "'idx_research_data_deletion_jobs_due'")
            assert cur.fetchone() is not None
        user = _create_user("m64@x.com")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_data_deletion_jobs "
                "(job_id, user_id, consent_epoch, reason) VALUES "
                "('jd64',%s,0,'user_request')", (user["user_id"],))
            cur.execute(
                "SELECT attempts, next_retry_at FROM research_data_deletion_jobs"
                " WHERE job_id='jd64'")
            row = cur.fetchone()
            assert row["attempts"] == 0 and row["next_retry_at"] is not None
            # 负 attempts 被 CHECK 拒绝
            try:
                cur.execute(
                    "UPDATE research_data_deletion_jobs SET attempts=-1 "
                    "WHERE job_id='jd64'")
                conn.commit()
                raise AssertionError("负 attempts 应被 CHECK 拒绝")
            except psycopg.errors.CheckViolation:
                conn.rollback()
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. 撤回 → 执行器实际删除研究副本；业务数据不动
# --------------------------------------------------------------------------- #
def test_withdrawal_job_executes_and_deletes_research_copies(monkeypatch):
    user = _create_user("wdel@x.com")
    client, sid, epoch = _collect(monkeypatch, user, "wdel@x.com")
    _insert_business_roi(SLIDE, user["user_id"], "ann_del0001")
    assert _research_rows(user["user_id"]) == {
        "subjects": 1, "sessions": 1, "events": 2, "items": 0}

    # 撤回前无删除任务：不阻断（deletion_pending 只来自删除任务）
    ok = research_consent_store.evaluate_research_access(
        user["user_id"], environ={SWITCH: "1"})
    assert ok["allowed"] is True and ok["reasons"] == []
    research_consent_store.withdraw(user["user_id"], expected_epoch=epoch)
    pending = research_consent_store.evaluate_research_access(
        user["user_id"], environ={SWITCH: "1"})
    assert "deletion_pending" in pending["reasons"]
    assert client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE}).status_code == 403

    # 执行时任务状态应为 running（领取事务已提交、删除尚未落终态）
    seen = {}
    real_delete = research_deletion_worker._delete_research_copies

    def _spy(conn, user_id, consent_epoch):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, attempts FROM research_data_deletion_jobs "
                "WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            seen["status"], seen["attempts"] = row["status"], row["attempts"]
        return real_delete(conn, user_id, consent_epoch)

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _spy)
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1 and summary["failed"] == 0
    assert seen == {"status": "running", "attempts": 1}, \
        "执行窗口内任务应为 running（pending→running→completed）"

    job = _job_row(user["user_id"])
    assert job["status"] == "completed"
    assert job["reason"] == "withdrawal"
    assert job["online_cleared"] is True and job["exports_cleared"] is True
    assert job["error_code"] is None and job["completed_at"] is not None

    # 研究副本全链删除（events → sessions → subjects）
    assert _research_rows(user["user_id"]) == {
        "subjects": 0, "sessions": 0, "events": 0, "items": 0}
    assert research_store.get_viewing_session(sid) is None

    # 业务数据一概未动（§3.5：不删业务切片/标注/临床记录）
    assert _count("SELECT count(*) AS n FROM slides WHERE slide_id=%s "
                  "OR legacy_filename=%s", (SLIDE, SLIDE)) == 1
    assert _count("SELECT count(*) AS n FROM rois WHERE slide=%s "
                  "AND annotation_id='ann_del0001'", (SLIDE,)) == 1
    assert user_store.get_user(user["user_id"]) is not None
    consent = research_consent_store.get_consent(user["user_id"])
    assert consent["state"] == "withdrawn" and consent["epoch"] == epoch + 1
    assert len(research_consent_store.list_history(user["user_id"])) == 2

    # 幂等：再跑一轮无任务可领（不重复、不报错）
    again = research_deletion_worker.drain_once()
    assert again["completed"] == 0 and again["failed"] == 0


def test_deletion_blocked_while_failed_retryable(monkeypatch):
    """退避重试中的 failed 也阻断（删除未完成不得恢复采集/使用）。"""
    user = _create_user("failblock@x.com")
    _collect(monkeypatch, user, "failblock@x.com")
    research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("storage down (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    research_deletion_worker.drain_once(limit=1)
    job = _job_row(user["user_id"])
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["error_code"] == "deletion_failed_RuntimeError", \
        "错误码只含异常类名（安全错误码，不落消息/堆栈）"
    out = research_consent_store.evaluate_research_access(
        user["user_id"], environ={SWITCH: "1"})
    assert out["allowed"] is False
    assert "deletion_pending" in out["reasons"], "退避窗口内仍阻断"
    # 数据未删
    assert _research_rows(user["user_id"])["events"] == 2


# --------------------------------------------------------------------------- #
# 3. completed 后解除阻断：再授权、正常采集、新伪名、旧 epoch 不复活
# --------------------------------------------------------------------------- #
def test_completed_job_unblocks_regrant_and_collection(monkeypatch):
    user = _create_user("regrant@x.com")
    client, sid, old_epoch = _collect(monkeypatch, user, "regrant@x.com")
    old_subject = research_store.subject_for_user(user["user_id"])
    research_consent_store.withdraw(user["user_id"],
                                    expected_epoch=old_epoch)
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None, "completed 后无未终态任务"

    # 再授权：新 epoch，evaluate 无 deletion_pending，可正常采集
    d = _doc("research_sharing")
    new_consent = research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])["consent"]
    assert new_consent["epoch"] == old_epoch + 2
    out = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=new_consent["epoch"],
        environ={SWITCH: "1"})
    assert out["allowed"] is True and out["reasons"] == []
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 200, resp.get_json()
    sid2 = resp.get_json()["viewing_session_id"]
    resp = client.post("/api/research/viewer-events", json={
        "viewing_session_id": sid2,
        "consent_epoch": new_consent["epoch"],
        "events": [_zoom(1)]})
    assert resp.status_code == 200

    # 新 subject 伪名（旧伪名已删，不复活）；旧 epoch 数据被拒（不复活）
    new_subject = research_store.subject_for_user(user["user_id"])
    assert new_subject["subject_id"] != old_subject["subject_id"]
    stale = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=old_epoch,
        environ={SWITCH: "1"})
    assert stale["allowed"] is False and "epoch_mismatch" in stale["reasons"]
    # worker 再跑一轮（无任务）不动新副本
    research_deletion_worker.drain_once()
    assert _research_rows(user["user_id"]) == {
        "subjects": 1, "sessions": 1, "events": 1, "items": 0}


def test_user_request_job_deletes_all_current_copies(monkeypatch):
    """user_request 任务（不撤回也可申请）：epoch=申请时当前 epoch → 全删。"""
    user = _create_user("ureq@x.com")
    client, sid, epoch = _collect(monkeypatch, user, "ureq@x.com")
    research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    job = _job_row(user["user_id"])
    assert job["consent_epoch"] == epoch, "任务 epoch = 申请时当前授权 epoch"
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1
    assert _research_rows(user["user_id"]) == {
        "subjects": 0, "sessions": 0, "events": 0, "items": 0}
    # 授权状态不变（删除副本与授权状态是两件事，§3.5）
    assert research_consent_store.get_consent(user["user_id"])[
        "state"] == "granted"


# --------------------------------------------------------------------------- #
# 4. 失败重试与终态
# --------------------------------------------------------------------------- #
def test_failure_backoff_retry_then_success(monkeypatch):
    user = _create_user("retry@x.com")
    _collect(monkeypatch, user, "retry@x.com")
    research_consent_store.withdraw(user["user_id"])
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    real_delete = research_deletion_worker._delete_research_copies
    calls = {"n": 0}

    def _flaky(conn, user_id, consent_epoch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.OperationalError("connection reset (test)")
        return real_delete(conn, user_id, consent_epoch)

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _flaky)
    first = research_deletion_worker.drain_once(limit=1)
    assert first["failed"] == 1 and first["completed"] == 0
    job = _job_row(user["user_id"])
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["error_code"] == "deletion_failed_OperationalError"
    assert _research_rows(user["user_id"])["events"] == 2, "失败不删数据"

    second = research_deletion_worker.drain_once(limit=1)
    assert second["completed"] == 1 and second["failed"] == 0
    job = _job_row(user["user_id"])
    assert job["status"] == "completed" and job["attempts"] == 2
    assert job["error_code"] is None, "成功后清除错误码"
    assert _research_rows(user["user_id"]) == {
        "subjects": 0, "sessions": 0, "events": 0, "items": 0}


def test_failure_exhausts_to_terminal_failed(monkeypatch):
    """持续失败达 MAX_DELETION_ATTEMPTS：failed 为终态，worker 不再领取。"""
    user = _create_user("termfail@x.com")
    _collect(monkeypatch, user, "termfail@x.com")
    research_consent_store.withdraw(user["user_id"])
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(research_consent_store.MAX_DELETION_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    job = _job_row(user["user_id"])
    assert job["status"] == "failed"
    assert job["attempts"] == research_consent_store.MAX_DELETION_ATTEMPTS
    # 终态：worker 不再领取、不再计数（停止自动重试）
    final = research_deletion_worker.drain_once()
    assert final == {"completed": 0, "failed": 0, "deleted": {}}
    assert _job_row(user["user_id"])["attempts"] == \
        research_consent_store.MAX_DELETION_ATTEMPTS
    # 终态 failed 仍是未了结删除义务：任务可见（待人工处置），重复申请
    # 幂等返回同一条任务（created=False，不自动换新任务重试）
    pending_manual = research_consent_store.get_active_deletion_job(
        user["user_id"])
    assert pending_manual is not None
    assert pending_manual["status"] == "failed"
    assert pending_manual["attempts"] == \
        research_consent_store.MAX_DELETION_ATTEMPTS
    created = research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    assert created["created"] is False
    assert created["job"]["job_id"] == pending_manual["job_id"]
    assert len(research_consent_store.list_deletion_jobs(
        user["user_id"])) == 1


def test_terminal_failed_user_request_still_blocks_research(monkeypatch):
    """P1 修复：user_request 删除失败达上限后，研究读取/采集仍被阻断。

    授权与 epoch 不变（用户单独申请删除）——统一权限检查不得因任务终态
    failed 重新放行，尚未删除的旧研究副本也不得通过研究读取检查；
    业务读片/标注不受任何影响（阻断只作用于研究侧）。
    """
    user = _create_user("termblk@x.com")
    client, sid, epoch = _collect(monkeypatch, user, "termblk@x.com")
    _insert_business_roi(SLIDE, user["user_id"], "ann_termblk")
    research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(research_consent_store.MAX_DELETION_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    job = _job_row(user["user_id"])
    assert job["status"] == "failed"
    assert job["attempts"] == research_consent_store.MAX_DELETION_ATTEMPTS
    research_deletion_worker.drain_once()  # 终态：worker 不再领取

    # 研究读取（evaluate_research_access）：授权仍 granted、epoch 未变，
    # 唯一阻断理由是 deletion_pending（不得重新放行）
    out = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=epoch, environ={SWITCH: "1"})
    assert out["allowed"] is False
    assert out["reasons"] == ["deletion_pending"]
    # 研究采集（建会话 / 旧会话续写）均 403 deletion_pending
    resp = client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "deletion_pending"
    resp = client.post("/api/research/viewer-events", json={
        "viewing_session_id": sid, "consent_epoch": epoch,
        "events": [_zoom(99)]})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "deletion_pending"
    # 尚未删除的旧研究副本仍在（删除义务未了结）
    assert _research_rows(user["user_id"])["events"] == 2

    # 业务读片不受影响：业务标注读取正常、业务数据原样（§3.5）
    ann = client.get("/api/annotations?slide=%s" % SLIDE)
    assert ann.status_code == 200, ann.get_data(as_text=True)
    assert _count("SELECT count(*) AS n FROM slides WHERE slide_id=%s "
                  "OR legacy_filename=%s", (SLIDE, SLIDE)) == 1
    assert _count("SELECT count(*) AS n FROM rois WHERE slide=%s "
                  "AND annotation_id='ann_termblk'", (SLIDE,)) == 1
    assert user_store.get_user(user["user_id"]) is not None


def test_terminal_failed_unblocks_only_after_completed(monkeypatch):
    """completed 才解除阻断：终态 failed 经人工处置落 completed 后恢复。"""
    user = _create_user("termok@x.com")
    client, sid, epoch = _collect(monkeypatch, user, "termok@x.com")
    research_consent_store.create_deletion_job(
        user["user_id"], reason="user_request")
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(research_consent_store.MAX_DELETION_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    assert _job_row(user["user_id"])["status"] == "failed"
    assert client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE}).status_code == 403

    # 人工处置完成（排障后重放清理 → completed）：阻断解除
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='completed', "
                "completed_at=now() WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None, "completed 后未了结任务清空"
    out = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=epoch, environ={SWITCH: "1"})
    assert out["allowed"] is True and out["reasons"] == []
    resp = client.post("/api/research/viewing-sessions", json={"slide": SLIDE})
    assert resp.status_code == 200, resp.get_json()


def test_terminal_failed_withdrawal_blocks_until_handled(monkeypatch):
    """撤回场景：删除失败达上限后研究使用持续阻断（授权已撤也仍阻断）。"""
    user = _create_user("termwd@x.com")
    client, sid, epoch = _collect(monkeypatch, user, "termwd@x.com")
    research_consent_store.withdraw(user["user_id"], expected_epoch=epoch)
    monkeypatch.setenv("RESEARCH_DELETION_RETRY_BASE_SECONDS", "0")
    real_delete = research_deletion_worker._delete_research_copies

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("always fails (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    for _ in range(research_consent_store.MAX_DELETION_ATTEMPTS):
        research_deletion_worker.drain_once(limit=1)
    assert _job_row(user["user_id"])["status"] == "failed"
    research_deletion_worker.drain_once()  # 终态：worker 不再领取

    # 已撤回（not_granted）本就不该采集；终态 failed 删除义务仍未了结，
    # deletion_pending 同样在列（阻断语义对撤回场景同样适用）
    out = research_consent_store.evaluate_research_access(
        user["user_id"], environ={SWITCH: "1"})
    assert out["allowed"] is False
    assert "not_granted" in out["reasons"]
    assert "deletion_pending" in out["reasons"]
    assert _research_rows(user["user_id"])["events"] == 2, "旧副本未删"
    assert client.post("/api/research/viewing-sessions",
                       json={"slide": SLIDE}).status_code == 403

    # 再同意 → 再撤回（新删除义务）：终态 failed 任务被复活为 pending 并
    # 推进到最新撤回 epoch（每次新撤回至多换一轮尝试），worker 完成后
    # completed 解除阻断
    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        real_delete)
    d = _doc("research_sharing")
    research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])
    research_consent_store.withdraw(user["user_id"])
    job = _job_row(user["user_id"])
    assert job["status"] == "pending"
    assert job["consent_epoch"] == epoch + 3, "清理目标推进到最新撤回 epoch"
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1
    assert _research_rows(user["user_id"]) == {
        "subjects": 0, "sessions": 0, "events": 0, "items": 0}
    assert research_consent_store.get_active_deletion_job(
        user["user_id"]) is None

    # completed 后：再同意即可正常采集（deletion_pending 不再出现）
    new_consent = research_consent_store.grant(
        user["user_id"], document_version=d["version"],
        document_sha256=d["content_sha256"])["consent"]
    out = research_consent_store.evaluate_research_access(
        user["user_id"], data_consent_epoch=new_consent["epoch"],
        environ={SWITCH: "1"})
    assert out["allowed"] is True and out["reasons"] == []


def test_backoff_window_defers_reclaim(monkeypatch):
    """默认退避基数下，failed 任务的 next_retry_at 在未来 → 本轮不领取。"""
    user = _create_user("backoff@x.com")
    _collect(monkeypatch, user, "backoff@x.com")
    research_consent_store.withdraw(user["user_id"])

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("boom (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    research_deletion_worker.drain_once(limit=1)  # 默认 base=30s
    job = _job_row(user["user_id"])
    assert job["status"] == "failed"
    assert job["next_retry_at"] is not None
    # 退避未满：立即再跑一轮领取不到（不重复失败刷 attempts）
    summary = research_deletion_worker.drain_once(limit=1)
    assert summary["completed"] == 0 and summary["failed"] == 0
    assert _job_row(user["user_id"])["attempts"] == 1


# --------------------------------------------------------------------------- #
# 5. 重启安全：running 超租约回收；撤回推进/复活退避中的 failed 任务
# --------------------------------------------------------------------------- #
def test_stale_running_job_reclaimed(monkeypatch):
    """worker 崩溃在 running：超租约未更新 → 可回收重领并完成（幂等）。"""
    user = _create_user("stale@x.com")
    _collect(monkeypatch, user, "stale@x.com")
    research_consent_store.withdraw(user["user_id"])
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='running', "
                "attempts=1, updated_at=now() - interval '2 hours' "
                "WHERE user_id=%s", (user["user_id"],))
        conn.commit()
    finally:
        conn.close()
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1
    job = _job_row(user["user_id"])
    assert job["status"] == "completed" and job["attempts"] == 2
    assert _research_rows(user["user_id"])["events"] == 0

    # 反向：running 且租约未到 → 本轮不回收（避免与在跑 worker 抢活）
    user2 = _create_user("freshrun@x.com")
    _collect(monkeypatch, user2, "freshrun@x.com",
             slide="p1-del-slide-fresh.ndpi")
    research_consent_store.withdraw(user2["user_id"])
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='running', "
                "attempts=1, updated_at=now() WHERE user_id=%s",
                (user2["user_id"],))
        conn.commit()
    finally:
        conn.close()
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 0, "租约内的 running 不得被回收"
    assert _research_rows(user2["user_id"])["events"] == 2


def test_new_withdraw_reactivates_failed_job(monkeypatch):
    user = _create_user("react@x.com")
    _collect(monkeypatch, user, "react@x.com")
    research_consent_store.withdraw(user["user_id"])
    real_delete = research_deletion_worker._delete_research_copies

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("boom (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    failed = research_deletion_worker.drain_once(limit=1)
    assert failed["failed"] == 1
    job = _job_row(user["user_id"])
    assert job["status"] == "failed"

    # 再同意（新 epoch）后再次撤回：既有 failed 任务被推进到新 epoch 并
    # 复活为 pending（新撤回 = 新删除义务，不等旧退避时钟）
    d = _doc("research_sharing")
    research_consent_store.grant(user["user_id"],
                                 document_version=d["version"],
                                 document_sha256=d["content_sha256"])
    research_consent_store.withdraw(user["user_id"])
    job = _job_row(user["user_id"])
    assert job["status"] == "pending"
    assert job["consent_epoch"] == 4, "清理目标推进到最新撤回 epoch"

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        real_delete)
    summary = research_deletion_worker.drain_once()
    assert summary["completed"] == 1
    assert _research_rows(user["user_id"]) == {
        "subjects": 0, "sessions": 0, "events": 0, "items": 0}


# --------------------------------------------------------------------------- #
# 6. 90 天到期清理（§8）
# --------------------------------------------------------------------------- #
def test_purge_expired_research_once(monkeypatch):
    user = _create_user("purge@x.com")
    user2 = _create_user("purge2@x.com")
    _open_collection(monkeypatch)
    _publish_and_grant(user["user_id"])
    _publish_and_grant(user2["user_id"])
    _own_slide(user)
    _own_slide(user2, "p1-del-slide2.ndpi")
    c1 = _login(_client(), user)
    c2 = _login(_client(), user2)
    sids = []
    for c, slide in ((c1, SLIDE), (c2, "p1-del-slide2.ndpi")):
        resp = c.post("/api/research/viewing-sessions", json={"slide": slide})
        assert resp.status_code == 200, resp.get_json()
        sid = resp.get_json()["viewing_session_id"]
        sids.append(sid)
        assert c.post("/api/research/viewer-events", json={
            "viewing_session_id": sid, "consent_epoch": 1,
            "events": [_zoom(1)]}).status_code == 200
    # user2 追加一条对话研究副本（P3 只落表；直接 SQL 造行）
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subject_id FROM research_subjects WHERE user_id=%s",
                (user2["user_id"],))
            subj2 = cur.fetchone()["subject_id"]
            cur.execute(
                "INSERT INTO research_conversation_items "
                "(item_id, subject_id, consent_epoch, source_kind, "
                " source_ref, origin, content, admission, expires_at) "
                "VALUES ('rci_keep',%s,1,'user_message','src1','user',"
                "'去标识内容','admitted', now() + interval '90 days'),"
                "('rci_exp',%s,1,'user_message','src2','user',"
                "'去标识内容','admitted', now() - interval '1 day')",
                (subj2, subj2))
            # user2 的副本整体改到已过期（模拟 90 天到期）
            cur.execute(
                "UPDATE research_viewing_sessions SET expires_at="
                "now() - interval '1 day', status='expired' "
                "WHERE session_id=%s", (sids[1],))
            cur.execute(
                "UPDATE research_viewer_events SET expires_at="
                "now() - interval '1 day' WHERE session_id=%s", (sids[1],))
        conn.commit()
    finally:
        conn.close()

    counts = research_deletion_worker.purge_expired_research_once()
    assert counts["events"] >= 1 and counts["sessions"] >= 1
    assert counts["conversation_items"] == 1
    # 未到期的 user1 副本与 rci_keep 保留
    assert _research_rows(user["user_id"]) == {
        "subjects": 1, "sessions": 1, "events": 1, "items": 0}
    assert _count("SELECT count(*) AS n FROM research_conversation_items "
                  "WHERE item_id='rci_keep'", ()) == 1
    assert _count("SELECT count(*) AS n FROM research_conversation_items "
                  "WHERE item_id='rci_exp'", ()) == 0
    assert research_store.get_viewing_session(sids[0]) is not None
    # 幂等：再跑一轮删不到东西
    again = research_deletion_worker.purge_expired_research_once()
    assert again == {"events": 0, "conversation_items": 0, "sessions": 0}


def test_purge_session_cascades_leftover_events(monkeypatch):
    """会话到期删除时级联其残余（未满 90 天的）事件——「最多 90 天」是上限。"""
    user = _create_user("cascade@x.com")
    _collect(monkeypatch, user, "cascade@x.com")
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_viewing_sessions SET expires_at="
                "now() - interval '1 hour' WHERE subject_id IN "
                "(SELECT subject_id FROM research_subjects WHERE user_id=%s)",
                (user["user_id"],))
            # 事件本身未到期（expires_at 在未来）
            cur.execute(
                "UPDATE research_viewer_events SET expires_at="
                "now() + interval '30 days'")
        conn.commit()
    finally:
        conn.close()
    counts = research_deletion_worker.purge_expired_research_once()
    assert counts["sessions"] == 1
    # 事件随会话级联删除；subjects 伪名映射保留（无引用但未列入到期清理
    # 谓词——它是身份映射而非带 expires_at 的研究数据，随下次授权复用）
    rows = _research_rows(user["user_id"])
    assert rows["sessions"] == 0 and rows["events"] == 0
    assert rows["subjects"] == 1


# --------------------------------------------------------------------------- #
# 7. 账户设置视图带执行进度字段（attempts/next_retry_at）
# --------------------------------------------------------------------------- #
def test_deletion_endpoint_reports_attempts(monkeypatch):
    user = _create_user("wire@x.com")
    _collect(monkeypatch, user, "wire@x.com")
    research_consent_store.withdraw(user["user_id"])

    def _boom(conn, user_id, consent_epoch):
        raise RuntimeError("boom (test)")

    monkeypatch.setattr(research_deletion_worker, "_delete_research_copies",
                        _boom)
    research_deletion_worker.drain_once(limit=1)
    c = _login(_client(), user)
    body = c.get("/api/account/research-data/deletion").get_json()
    job = body["job"]
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["next_retry_at"] is not None
    assert "deletion_failed_" in job["error_code"]
    agreements = c.get("/api/account/agreements").get_json()
    wire_jobs = agreements["deletion_jobs"]
    assert wire_jobs and wire_jobs[0]["attempts"] == 1 \
        and wire_jobs[0]["next_retry_at"]


# --------------------------------------------------------------------------- #
# 8. CLI --once（部署钩子/测试单轮：drain + purge）
# --------------------------------------------------------------------------- #
def test_cli_once_runs_drain_and_purge(monkeypatch, capsys):
    user = _create_user("clionce@x.com")
    _collect(monkeypatch, user, "clionce@x.com")
    research_consent_store.withdraw(user["user_id"])
    rc = research_deletion_worker.main(["--once"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "drain_completed=1" in out and "drain_failed=0" in out
    assert "purge=" in out
    assert _job_row(user["user_id"])["status"] == "completed"
    assert _research_rows(user["user_id"])["events"] == 0
