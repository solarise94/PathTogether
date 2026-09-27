# -*- coding: utf-8 -*-
"""0072 生命周期——V1/V2/ZIP/百度/转换通道收口测试（plan §6-C/§7 矩阵）。

覆盖：
  - V2 创建即绑定（native / convert-required 两分支）；
  - V1 受理前失败：清理确认后释放（清理失败 → pending 保留容量）；
  - V2 DELETE 取消 / TTL 维护：清理失败不释放，重试确认后恰一次释放；
  - 百度建批即绑定（holder=baidu_batch）+ 闭班收口与终态同事务
    （配额收口失败 → 批次不落终态，无「终态已落、结算未发」窗口）；
  - 转换结算走 upload_guard 唯一财务原语（失败整体回滚）；
  - 确定性并发：COS 新准入先赢（与 r10 并发 / r11 续租先赢互补）；
    V2 取消×新准入×清理失败（责任持续计入、确认后恰一次释放）。
"""
import hashlib
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (csrf_client, clear_upload_dir,  # noqa: E402
                         isolate_app)

PG_URI = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """同 test_upload_v2：独立存储 + 登录限制 mock + 水印 0 + owner 配置。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    share_store.set_owner_user_id(
        user_store.create_user("life-ch-owner@x.com", "localownerpass12345",
                               role="user")["user_id"])
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client_user(tag):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    uid = user_store.create_user(
        "life-ch-%s@example.com" % tag, "pass1234pass1234", role="user"
    )["user_id"]
    with client.session_transaction() as s:
        s["auth_user"] = True
        s["user_id"] = uid
        s["role"] = "user"
        s["auth_version"] = 1
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_user_quotas SET quota_bytes=%s "
                   "WHERE user_id=%s", (10 ** 7, uid))
    return client, uid


def _rid_row(rid):
    with psycopg.connect(PG_URI) as db:
        return db.execute(
            "SELECT state, holder_kind, holder_id FROM upload_reservations "
            "WHERE reservation_id=%s", (rid,)).fetchone()


def _quota(uid):
    with psycopg.connect(PG_URI) as db:
        r = db.execute("SELECT used_bytes, reserved_bytes FROM "
                       "upload_user_quotas WHERE user_id=%s",
                       (uid,)).fetchone()
    return int(r[0]), int(r[1])


def _ledger_ok(uid):
    with psycopg.connect(PG_URI) as db:
        q, s = db.execute(
            "SELECT (SELECT reserved_bytes FROM upload_user_quotas "
            "WHERE user_id=%s), COALESCE((SELECT SUM(reserved_bytes) FROM "
            "upload_reservations WHERE user_id=%s AND state='reserved'),0)",
            (uid, uid)).fetchone()
    return int(q) == int(s), int(q), int(s)


def _create(client, name, size):
    r = client.post("/api/uploads", json={
        "filename": name, "declared_size": size,
        "sha256_expected": hashlib.sha256(b"x" * size).hexdigest()})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["upload_id"]


# --------------------------------------------------------------------------- #
# V2：创建即绑定
# --------------------------------------------------------------------------- #
def test_v2_create_binds_reservation_native():
    client, uid = _client_user("v2n")
    upload_id = _create(client, "n.svs", 1000)
    task = upload_task_store.get_task(upload_id)
    assert _rid_row(task["reservation_id"]) == \
        ("reserved", "upload_task", upload_id)


def test_v2_create_binds_reservation_convert():
    client, uid = _client_user("v2k")
    upload_id = _create(client, "n.kfb", 1000)
    task = upload_task_store.get_task(upload_id)
    assert _rid_row(task["reservation_id"]) == \
        ("reserved", "upload_task", upload_id)


# --------------------------------------------------------------------------- #
# V1：受理前失败的清理确认后释放
# --------------------------------------------------------------------------- #
def test_v1_preaccept_abort_cleanup_failure_keeps_reservation(monkeypatch):
    client, uid = _client_user("v1a")
    real_remove = slide_storage.remove_staging_tree
    seen = {"rid": None}

    def _fail_remove(task_id, *, root=None):
        if task_id and str(task_id).startswith("upt_"):
            raise OSError("boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    # 非法内容（validation 失败）→ 受理前 abort → 清理失败 → pending 保留
    r = client.post("/api/upload", data={
        "file": (open(os.devnull, "rb"), "bad.svs")}, content_type=
        "multipart/form-data")
    assert r.status_code == 400
    monkeypatch.setattr(slide_storage, "remove_staging_tree", real_remove)
    # 预约仍持有（无任务行——责任挂在 pending 行，重试经 DELETE/admin）
    with psycopg.connect(PG_URI) as db:
        row = db.execute(
            "SELECT upload_id, reservation_id FROM upload_cleanup_pending "
            "LIMIT 1").fetchone()
        reserved = db.execute(
            "SELECT COALESCE(SUM(reserved_bytes),0) FROM upload_reservations "
            "WHERE user_id=%s AND state='reserved'", (uid,)).fetchone()[0]
    assert row, "清理失败必须留下 pending 行"
    assert int(reserved) > 0  # 容量责任保留
    task_id, rid = row[0], row[1]
    # 清理确认（重试成功）→ 按持有者释放
    slide_storage.remove_staging_tree(task_id, root=UPLOAD_DIR)
    out = upload_task_store.clear_cleanup_pending(task_id)
    assert out == rid
    upload_guard.release_reservation(rid, expect_holder=("upload_task",
                                                         task_id))
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)


def test_v1_preaccept_abort_cleanup_success_releases():
    client, uid = _client_user("v1b")
    r = client.post("/api/upload", data={
        "file": (open(os.devnull, "rb"), "bad.svs")},
        content_type="multipart/form-data")
    assert r.status_code == 400
    assert _quota(uid) == (0, 0)  # 清理成功 → 确认后释放


# --------------------------------------------------------------------------- #
# V2：取消 / TTL 维护的清理确认门
# --------------------------------------------------------------------------- #
def test_v2_delete_cancel_cleanup_failure_keeps_then_retry_releases(
        monkeypatch):
    client, uid = _client_user("v2c")
    upload_id = _create(client, "c.svs", 1000)
    rid = upload_task_store.get_task(upload_id)["reservation_id"]
    # 写入分片（产生 staging 树）
    r = client.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
                   % (upload_id, hashlib.sha256(b"x" * 200).hexdigest()),
                   data=b"x" * 200, content_type="application/octet-stream")
    assert r.status_code == 200, r.get_data(as_text=True)
    real_remove = slide_storage.remove_staging_tree

    def _fail_remove(task_id, *, root=None):
        if task_id == upload_id:
            raise OSError("boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    r = client.delete("/api/uploads/%s" % upload_id)
    assert r.status_code == 503
    assert r.get_json()["code"] == "cleanup_retryable"
    assert _rid_row(rid) == ("reserved", "upload_task", upload_id)
    assert _quota(uid)[1] == 1000  # 责任持续计入
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
    # 重试（清理恢复）→ 确认后恰一次释放
    monkeypatch.setattr(slide_storage, "remove_staging_tree", real_remove)
    r2 = client.delete("/api/uploads/%s" % upload_id)
    assert r2.status_code == 200
    assert _rid_row(rid)[0] == "released"
    assert _quota(uid) == (0, 0)
    # 幂等：再删不重复释放
    client.delete("/api/uploads/%s" % upload_id)
    assert _quota(uid) == (0, 0)


def test_v2_maintain_expire_cleanup_failure_keeps_reservation(monkeypatch):
    client, uid = _client_user("v2m")
    upload_id = _create(client, "m.svs", 1000)
    rid = upload_task_store.get_task(upload_id)["reservation_id"]
    # 任务过期 + 清理失败 → 维护路径不释放（此前为无条件释放）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_tasks SET expires_at="
                   "now() - interval '10 seconds' WHERE upload_id=%s",
                   (upload_id,))
    real_remove = slide_storage.remove_staging_tree

    def _fail_remove(task_id, *, root=None):
        if task_id == upload_id:
            raise OSError("boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    r = client.get("/api/uploads/%s" % upload_id)
    assert r.status_code == 200
    task = upload_task_store.get_task(upload_id)
    assert task["state"] == upload_task_store.STATE_EXPIRED
    assert _rid_row(rid) == ("reserved", "upload_task", upload_id)
    assert upload_task_store.get_cleanup_pending(upload_id) is not None
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)


# --------------------------------------------------------------------------- #
# 百度：建批即绑定 + 闭班收口与终态同事务
# --------------------------------------------------------------------------- #
def test_baidu_batch_budget_bound_and_closure_atomic(monkeypatch, tmp_path):
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-life-secret-2026-09")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "up"))
    (tmp_path / "up").mkdir()
    from _baidu_helpers import install_fake, make_ready_enumeration
    import baidu_import_store as store

    from _tiff_fixtures import make_tiff_bytes
    tif = make_tiff_bytes()
    entries = [{"path": "/a.tif", "size": len(tif), "content": tif}]
    uid = user_store.create_user(
        "life-ch-baidu@example.com", "pass1234pass1234", role="user"
    )["user_id"]
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=uid, entries=entries)

    def hook(user_id, nbytes):
        return upload_guard.reserve_upload(user_id, nbytes)["reservation_id"]

    batch = store.create_import(uid, enum_id, [by_path["a.tif"]["id"]],
                                quota_hook=hook, idempotency_key="life1")
    with psycopg.connect(PG_URI) as db:
        rid = db.execute(
            "SELECT quota_reservation_id FROM baidu_import_batches "
            "WHERE id=%s", (batch["id"],)).fetchone()[0]
    assert rid
    assert _rid_row(rid) == ("reserved", "baidu_batch", batch["id"])

    # 闭班收口与终态同事务：consume 原语失败（非预约态业务异常）→
    # 整体回滚，批次不落终态（无「终态已落、结算未发」窗口）
    def _boom(cur, reservation_id, batch_id, actual_bytes):
        raise RuntimeError("settle infra down")

    monkeypatch.setattr(store, "_consume_reservation", _boom)
    with pytest.raises(RuntimeError):
        store.run_batch(batch["id"], fake, staging_root=tmp_path)
    with psycopg.connect(PG_URI) as db:
        state = db.execute("SELECT state FROM baidu_import_batches "
                           "WHERE id=%s", (batch["id"],)).fetchone()[0]
    assert state == "running"  # 未落终态（事务整体回滚）
    assert _rid_row(rid)[0] == "reserved"  # 责任仍在，未无结算落终态
    # 恢复后重跑：闭班收口与终态同事务落定，consume 恰一次
    def _real_consume(cur, reservation_id, batch_id, actual_bytes):
        upload_guard.consume_reservation_locked(
            cur, reservation_id, int(actual_bytes),
            expect_holder=("baidu_batch", batch_id))
    monkeypatch.setattr(store, "_consume_reservation", _real_consume)
    import time as _time
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE baidu_import_batches SET lease_expires_at="
                   "now() - interval '1 second' WHERE id=%s", (batch["id"],))
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                           worker_id="w2")
    assert view["state"] == "succeeded"
    used, reserved = _quota(uid)
    assert used == len(tif) and reserved == 0


# --------------------------------------------------------------------------- #
# 转换：结算走唯一财务原语，失败整体回滚
# --------------------------------------------------------------------------- #
def test_conversion_settle_uses_guard_primitive_and_rolls_back(monkeypatch):
    import conversion_store
    uid = user_store.create_user(
        "life-ch-conv@example.com", "pass1234pass1234", role="user"
    )["user_id"]
    # 直接构造一个 validating 转换任务（走 store 原语，绕过 FS/worker）
    job = conversion_store.create_job(
        owner_user_id=uid, upload_id="upt-life-conv",
        source_name="a.kfb", source_sha256="sha" + "0" * 61,
        source_format="kfb", canonical_name="a.svs")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE conversion_jobs SET state='validating', "
                   "lease_owner='w1', lease_expires_at=now() + "
                   "interval '1 hour' WHERE id=%s", (job["id"],))
    gen = job.get("attempt") or 0
    # 提交恢复栅栏：先持久化 intent（generation=commit_token=attempt）
    conversion_store.persist_commit_intent(
        job["id"], "w1", {"task_ref": job["id"], "generation": str(gen),
                          "commit_token": str(gen),
                          "slide_id": job["slide_id"],
                          "owner_user_id": uid,
                          "manifest": {"entry": "data.svs", "files": []},
                          "sha256": "f" * 64, "accounted_bytes": 100})
    real_add = upload_guard.add_used_bytes_locked

    def _boom(cur, user_id, nbytes):
        raise RuntimeError("quota write down")

    monkeypatch.setattr(upload_guard, "add_used_bytes_locked", _boom)
    gen = job.get("attempt") or 0
    with pytest.raises(RuntimeError):
        conversion_store.worker_settle_ready(
            job["id"], "w1", gen, slide_id=job["slide_id"],
            canonical_name="a.svs", sha256="f" * 64, settle_bytes=100)
    out = conversion_store.get_job(job["id"])
    assert out["state"] != "ready"  # 整体回滚（结算未落）
    q = upload_guard.get_quota_row(uid)  # 惰性建行后读（回滚后行可不存在）
    assert (q["used_bytes"], q["reserved_bytes"]) == (0, 0)
    # 恢复后正常结算（唯一原语 + used_bytes 一次入账）
    monkeypatch.setattr(upload_guard, "add_used_bytes_locked", real_add)
    # 重置 generation 计数（上一 settle 未落 generation 不变）
    conversion_store.worker_settle_ready(
        job["id"], "w1", gen, slide_id=job["slide_id"],
        canonical_name="a.svs", sha256="f" * 64, settle_bytes=100)
    out = conversion_store.get_job(job["id"])
    assert out["state"] == "ready"
    assert _quota(uid) == (100, 0)


# --------------------------------------------------------------------------- #
# 确定性并发（§7 矩阵补集）
# --------------------------------------------------------------------------- #
def test_cos_admission_first_then_renewal_keeps_capacity():
    """§7 行 1 的准入先赢确定性顺序（r10 并发起跑 / r11 续租先赢互补）：
    新准入先回收不到绑定预约 → 账本 150；随后续租同一 rid 重发租约。"""
    import ingestion_store as ist
    uid = user_store.create_user(
        "life-ch-cos@example.com", "pass1234pass1234", role="user"
    )["user_id"]
    job, _ = ist.create_waiting_job(uid, "user", "c.svs", "c.svs", "svs", 100)
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind="ingestion_job", holder_id=job["job_id"],
        purpose="ingest_local")["reservation_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='uploading', "
                   "local_reservation_id=%s, capacity_admitted_at=now(), "
                   "pool_reserved_bytes=100 WHERE job_id=%s",
                   (rid, job["job_id"]))
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now() - interval '1 second' WHERE reservation_id=%s",
                   (rid,))
    # 顺序固定：准入先（回收不到绑定责任）→ 续租后
    upload_guard.reserve_upload(uid, 50)
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "renewed"
    after = ist.get_job(job["job_id"])
    assert after["state"] == "uploading"
    assert after["local_reservation_id"] == rid
    res = upload_guard.get_reservation(rid)
    assert res["state"] == "reserved"
    assert upload_guard.reservation_is_active(res)
    assert _quota(uid)[1] == 150
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)


def test_cancel_vs_admission_with_cleanup_failure_keeps_duty(monkeypatch):
    """§7 行「取消/超期 × 新准入 × 删除失败」：屏障同时起跑 DELETE 取消
    （清理失败注入）与同用户新建任务——旧责任持续计入，重试清理确认后
    恰一次释放；全程账本一致。"""
    client, uid = _client_user("v2x")
    upload_id = _create(client, "x.svs", 1000)
    rid = upload_task_store.get_task(upload_id)["reservation_id"]
    r = client.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
                   % (upload_id, hashlib.sha256(b"x" * 100).hexdigest()),
                   data=b"x" * 100, content_type="application/octet-stream")
    assert r.status_code == 200
    real_remove = slide_storage.remove_staging_tree

    def _fail_remove(task_id, *, root=None):
        if task_id == upload_id:
            raise OSError("boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    barrier = threading.Barrier(2)
    errors = [None, None]

    def _cancel():
        barrier.wait(timeout=30)
        try:
            client.delete("/api/uploads/%s" % upload_id)
        except Exception as exc:  # noqa: BLE001
            errors[0] = exc

    def _create_new():
        barrier.wait(timeout=30)
        try:
            _create(client, "y.svs", 3000)
        except Exception as exc:  # noqa: BLE001
            errors[1] = exc

    ts = [threading.Thread(target=_cancel), threading.Thread(target=_create_new)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
        assert not t.is_alive(), "并发场景死锁"
    assert errors == [None, None], errors
    # 清理失败的旧责任 + 新任务责任都在账上
    assert _rid_row(rid) == ("reserved", "upload_task", upload_id)
    assert _quota(uid)[1] == 4000
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
    # 重试清理确认 → 旧责任恰一次释放
    monkeypatch.setattr(slide_storage, "remove_staging_tree", real_remove)
    r2 = client.delete("/api/uploads/%s" % upload_id)
    assert r2.status_code == 200
    assert _rid_row(rid)[0] == "released"
    assert _quota(uid)[1] == 3000
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
