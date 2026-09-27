# -*- coding: utf-8 -*-
"""R12 验收矩阵（修复方案 §6）——文件锁协议的确定性并发与互斥用例。

覆盖行：
  - COS claim 后、首次建目录前取消（晚到 writer 重验退出）→ r12 反例 1；
  - **已 open / 网络读取中取消**：两线程 + Event——writer 在临界区内，
    另一连接完成终止状态提交（不等待文件锁），确认预约仍保留，放行
    writer；清理等待 writer 退出后删树收口；期间责任不释放；最终无残留
    （§6 并发用例调整规则的指定形态）；
  - writer 自身失败触发清理：无递归 flock 自锁（延迟收口）；
  - V2 PUT 分片 × DELETE 取消：共用稳定 inode 任务锁，清理后无写入；
  - 锁竞争超时不是删除成功；锁文件 inode 跨暂存树删除稳定（运行期不删）；
  - V2 清理 DB 收口失败：pending 与容量仍在，恢复后恰一次收口。
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

import ingestion_store as ist
import slide_storage
import task_storage_lock
import upload_guard
import upload_task_store
from _pt_helpers import csrf_client  # noqa: E402

import app as app_mod  # noqa: E402  # 收集期导入（空表 owner 检查放行）
from test_cos_ingest_worker import _env  # noqa: E402,F401  # fixture 注册

PG_URI = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
def _cos_env(_env, monkeypatch):
    """COS worker 套件环境（池状态/水位；_mk_uploading 依赖）。"""
    import cos_config
    import cos_pool_store
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    monkeypatch.setattr(cos_config, "COS_CLEANUP_RETRY_BASE_SECONDS", 0)
    cos_pool_store.ensure_pool_state()
    yield


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from _pt_helpers import clear_upload_dir, isolate_app
    import share_store
    import user_store
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    share_store.set_owner_user_id(
        user_store.create_user("r12-acc-owner@x.com", "localownerpass12345",
                               role="user")["user_id"])
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client_user(tag):
    import user_store
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client = csrf_client(app_mod.app.test_client())
    uid = user_store.create_user(
        "r12-acc-%s@example.com" % tag, "pass1234pass1234", role="user"
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


def _q(uid):
    with psycopg.connect(PG_URI) as db:
        r = db.execute("SELECT used_bytes, reserved_bytes FROM "
                       "upload_user_quotas WHERE user_id=%s",
                       (uid,)).fetchone()
    return int(r[0]), int(r[1])


def _create(client, name, size):
    r = client.post("/api/uploads", json={
        "filename": name, "declared_size": size,
        "sha256_expected": hashlib.sha256(b"x" * size).hexdigest()})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["upload_id"]


# --------------------------------------------------------------------------- #
# §6 行 2：已 open / 网络读取中取消——两线程 + Event 的确定性时序
# --------------------------------------------------------------------------- #
def test_cancel_while_writer_in_critical_section_waits_then_cleans(
        monkeypatch):
    """writer 持锁在网络读中；另一连接只做**终止短事务**（不等待文件锁）
    并确认预约仍保留；放行 writer（写文件 + 迟到 checkpoint 被拒）；清理
    等待 writer 退出后删树收口——期间责任不释放，最终无残留。"""
    import test_cos_ingest_worker as h
    from test_capacity_lifecycle_cos import _mk_uploading
    job, rid, uid = _mk_uploading("acc_io")
    jid = job["job_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='downloading',"
                   " object_key='k', cos_version_id='v' WHERE job_id=%s",
                   (jid,))
    worker_entered = threading.Event()
    allow_writer = threading.Event()
    terminate_done = threading.Event()
    errors = []

    original_fetch = h.ciw._fetch_range if hasattr(h, "ciw") else None
    import cos_ingest_worker as worker

    def _hooked_fetch(cos, key, version_id, start, end, declared, **kw):
        worker_entered.set()  # writer 已在锁内、I/O 进行中
        assert allow_writer.wait(30)
        return b"y" * (end - start + 1), end - start + 1

    monkeypatch.setattr(worker, "_fetch_range", _hooked_fetch)

    def _writer():
        try:
            worker.process_downloading(cos=object(), state={})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _terminator():
        assert worker_entered.wait(30)
        # 只做终止短事务（不带文件清理——清理会等待 writer 的锁）
        out, _pending = ist._terminate_cancel_tx(jid)
        assert out["state"] == ist.CANCELLED
        # 期间容量责任不释放（writer 尚在临界区）
        assert _q(uid) == (0, 500)
        terminate_done.set()
        allow_writer.set()  # 放行 writer（不等清理返回）

    tw = threading.Thread(target=_writer)
    tt = threading.Thread(target=_terminator)
    tw.start()
    tt.start()
    tw.join(timeout=60)
    tt.join(timeout=60)
    assert not tw.is_alive() and not tt.is_alive(), "并发场景死锁"
    assert errors == [] or all(
        isinstance(e, (ist.IngestionStateError, ist.StaleLease))
        for e in errors), errors
    assert terminate_done.is_set()
    # writer 退出临界区（迟到 checkpoint 被拒/静默）；文件可能已写——清理
    # 在锁内等待后删除并收口
    assert ist._local_cleanup_finish(jid) is True
    after = ist.get_job(jid)
    assert after["state"] == ist.CANCELLED
    assert after["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert not slide_storage.staging_task_dir(jid).exists()  # 无残留
    assert upload_guard.get_reservation(rid)["state"] == "released"
    assert _q(uid) == (0, 0)


# --------------------------------------------------------------------------- #
# §6 行 3：writer 自身失败触发清理——无递归 flock 自锁
# --------------------------------------------------------------------------- #
def test_writer_self_failure_cleanup_no_recursive_lock(monkeypatch):
    from test_capacity_lifecycle_cos import _mk_uploading
    import cos_ingest_worker as worker
    job, rid, uid = _mk_uploading("acc_self")
    jid = job["job_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='downloading',"
                   " object_key='k', cos_version_id='v' WHERE job_id=%s",
                   (jid,))
        # 让下载预算立即烧尽 → 锁内 _fail（终态短事务）→ 锁外延迟清理
        db.execute("UPDATE ingestion_jobs SET wire_download_bytes=%s,"
                   " download_checkpoint_json='{\"next_offset\":0}'"
                   " WHERE job_id=%s", (10 ** 9, jid))
    done = threading.Event()

    def _run():
        worker.process_downloading(cos=object(), state={})
        done.set()

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=60)
    assert done.is_set(), "writer 自身失败路径自锁（flock 递归等待）"
    out = ist.get_job(jid)
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "download_budget_exceeded"
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert not slide_storage.staging_task_dir(jid).exists()
    assert _q(uid) == (0, 0)


# --------------------------------------------------------------------------- #
# §6 行 4：V2 PUT 分片 × DELETE 取消共用稳定锁
# --------------------------------------------------------------------------- #
def test_v2_put_chunk_vs_delete_cancel_mutual_exclusion(monkeypatch):
    client, uid = _client_user("putdel")
    upload_id = _create(client, "pd.svs", 1000)
    rid = upload_task_store.get_task(upload_id)["reservation_id"]
    put_in_section = threading.Event()
    allow_put = threading.Event()
    errors = []
    real_pwrite = os.pwrite

    def _hooked_pwrite(fd, data, offset):
        if offset == 0:
            put_in_section.set()
            assert allow_put.wait(30)
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", _hooked_pwrite)

    def _put():
        try:
            r = client.put(
                "/api/uploads/%s/chunk?offset=0&sha256=%s"
                % (upload_id, hashlib.sha256(b"p" * 100).hexdigest()),
                data=b"p" * 100,
                content_type="application/octet-stream")
            errors.append(("put", r.status_code))
        except Exception as exc:  # noqa: BLE001
            errors.append(("put", exc))

    def _delete():
        assert put_in_section.wait(30)
        # DELETE 的清理段等待 writer 退出任务锁；期间责任保留
        r = client.delete("/api/uploads/%s" % upload_id)
        errors.append(("delete", r.status_code))
        allow_put.set()

    tp = threading.Thread(target=_put)
    td = threading.Thread(target=_delete)
    tp.start()
    td.start()
    tp.join(timeout=60)
    td.join(timeout=60)
    assert not tp.is_alive() and not td.is_alive(), "PUT×DELETE 死锁"
    statuses = dict(errors)
    assert statuses.get("delete") == 200
    task = upload_task_store.get_task(upload_id)
    assert task["state"] == upload_task_store.STATE_CANCELLED
    assert not slide_storage.staging_task_dir(upload_id,
                                              root=UPLOAD_DIR).exists()
    assert upload_guard.get_reservation(rid)["state"] == "released"
    assert _q(uid) == (0, 0)
    # 清理后无写入：再次 PUT 被状态拒绝，不产生新文件
    r2 = client.put(
        "/api/uploads/%s/chunk?offset=0&sha256=%s"
        % (upload_id, hashlib.sha256(b"p" * 100).hexdigest()),
        data=b"p" * 100, content_type="application/octet-stream")
    assert r2.status_code in (403, 409)
    assert not slide_storage.staging_task_dir(upload_id,
                                              root=UPLOAD_DIR).exists()


# --------------------------------------------------------------------------- #
# §6 行 5：锁竞争/超时/稳定 inode
# --------------------------------------------------------------------------- #
def test_lock_contention_timeout_is_not_success_and_inode_stable():
    tid = "upt_acc_lock1"
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with task_storage_lock.task_storage_lock("upload_task", tid):
            held.set()
            assert release.wait(30)

    t = threading.Thread(target=_holder)
    t.start()
    assert held.wait(30)
    # 竞争者超时：TaskStorageLockTimeout——不得视为获得写入/清理权
    with pytest.raises(task_storage_lock.TaskStorageLockTimeout):
        with task_storage_lock.task_storage_lock("upload_task", tid,
                                                 timeout=0.2):
            raise AssertionError("不应获得锁")
    lock_path = task_storage_lock.task_lock_path("upload_task", tid)
    inode_before = os.stat(lock_path).st_ino
    # 删除暂存树（含整树清理）不影响锁 inode（锁在树外，运行期不删）
    staging = slide_storage.staging_task_dir(tid, root=UPLOAD_DIR)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "x").write_bytes(b"1")
    slide_storage.remove_staging_tree(tid, root=UPLOAD_DIR)
    assert os.stat(lock_path).st_ino == inode_before
    release.set()
    t.join(timeout=30)
    assert not t.is_alive()
    # 释放后可再获（晚到 writer/清理）
    with task_storage_lock.task_storage_lock("upload_task", tid):
        pass


def test_invalid_lock_keys_rejected():
    with pytest.raises(task_storage_lock.TaskStorageLockError):
        task_storage_lock.task_lock_path("workflow", "x")
    with pytest.raises(ValueError):
        task_storage_lock.task_lock_path("upload_task", "../escape")


# --------------------------------------------------------------------------- #
# §6 行 6：V2 清理 DB 收口失败——pending 与容量仍在，恢复后恰一次收口
# --------------------------------------------------------------------------- #
def test_cleanup_db_closeout_failure_keeps_pending_then_single_release(
        monkeypatch):
    client, uid = _client_user("dbfail")
    upload_id = _create(client, "db.svs", 1000)
    rid = upload_task_store.get_task(upload_id)["reservation_id"]
    r = client.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
                   % (upload_id, hashlib.sha256(b"d" * 100).hexdigest()),
                   data=b"d" * 100,
                   content_type="application/octet-stream")
    assert r.status_code == 200
    rd = client.delete("/api/uploads/%s" % upload_id)
    assert rd.status_code == 200
    assert upload_task_store.get_cleanup_pending(upload_id) is None
    assert upload_guard.get_reservation(rid)["state"] == "released"
    assert _q(uid) == (0, 0)
    # 构造收口失败：第二个任务清理成功、确认事务内释放抛错 → pending 保留
    upload2 = _create(client, "db2.svs", 1000)
    rid2 = upload_task_store.get_task(upload2)["reservation_id"]
    client.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
               % (upload2, hashlib.sha256(b"e" * 100).hexdigest()),
               data=b"e" * 100, content_type="application/octet-stream")
    real_release = upload_guard.release_reservation_locked

    def _flaky(cur, reservation_id, **kw):
        raise RuntimeError("db down at closeout")

    monkeypatch.setattr(upload_guard, "release_reservation_locked", _flaky)
    rd2 = client.delete("/api/uploads/%s" % upload2)
    assert rd2.status_code == 503  # cleanup_retryable
    assert upload_task_store.get_cleanup_pending(upload2) is not None
    assert upload_guard.get_reservation(rid2)["state"] == "reserved"
    assert _q(uid)[1] == 1000  # 容量仍在（账本一致）
    # 恢复后恰一次收口
    monkeypatch.setattr(upload_guard, "release_reservation_locked",
                        real_release)
    rd3 = client.delete("/api/uploads/%s" % upload2)
    assert rd3.status_code == 200
    assert upload_task_store.get_cleanup_pending(upload2) is None
    assert upload_guard.get_reservation(rid2)["state"] == "released"
    assert _q(uid) == (0, 0)
    rd4 = client.delete("/api/uploads/%s" % upload2)  # 幂等：不重复释放
    assert _q(uid) == (0, 0)
