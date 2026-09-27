# -*- coding: utf-8 -*-
"""R12 复核（2026-09-27）反例处置——最终验收形态（docs/review-evidence/r12/）。

原始失败证据：docs/review-evidence/r12/results.txt（4 failed）。按修复方案
§6 的用例调整规则，本文件保留四个场景的最终断言：

  1. R12-1（物理写入与清理互斥）：审查反例原样语义——claim 后、首次建
     目录前另一事务取消。文件锁协议下：取消清理先获锁完成（空树
     cleaned+released），晚到 writer 获锁后**锁内重验**退出、不建文件
     ——cleaned+released 时无残留。（旧行为=writer 照写形成残留。）
  2. R12-2（补记恢复）：**合同变更**（§3.4 取消恢复执行）——新断言：
     已补责任、任务保持停止、随后清理恰一次释放；CLI 走
     --plan-out/--apply --repair-residuals。旧反例要求恢复 uploading 的
     断言不保留（原始证据见 review-evidence，不声称原样保留）。
  3. R12-3（终态 pending 迁移）：冻结计划补绑定后 TTL 不再回收；新准入
     50 时旧 100 保留（总账 150），清理确认后才降 50。
  4. R12-4（超额补记）：额度 50、已有 100——维护补记如实成功（0,100），
     走新 CLI。

两线程 + Event 的「writer 在临界区内取消」确定性时序见
tests/test_r12_lifecycle_acceptance.py（§6 并发用例调整规则——同步 hook
在文件锁下会等待自己，不在此重复）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import ingestion_store as ist
import upload_guard as guard
import slide_storage
import cos_ingest_worker as worker
import importlib.util
import user_store
from test_cos_ingest_worker import _env  # noqa: E402,F401
from test_capacity_lifecycle_cos import _worker_env  # noqa: E402,F401
from test_capacity_lifecycle_cos import _mk_uploading  # noqa: E402
from test_reconcile_upload_capacity import _q  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
_SPEC = importlib.util.spec_from_file_location(
    "recon", os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts",
        "reconcile_upload_capacity.py"))
recon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recon)


def _freeze_apply(upload_dir, extra=()):
    plan = "/tmp/r12-review-plan.json"
    rc = recon.main(["--database-url", PG_URI, "--upload-dir",
                     str(upload_dir), "--plan-out", plan] + list(extra))
    assert rc == 0, open(plan).read()
    return recon.main(["--database-url", PG_URI, "--upload-dir",
                       str(upload_dir), "--apply", "--plan", plan]
                      + list(extra))


def test_cancel_does_not_release_before_claimed_writer_stops(monkeypatch,
                                                             pg_uri):
    job, rid, uid = _mk_uploading('r12_writer')
    jid = job['job_id']
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='downloading',"
                   " object_key='k', cos_version_id='v' WHERE job_id=%s",
                   (jid,))
    original = worker.staging_data_path
    paths = []

    def cancel_after_claim(*args, **kwargs):
        p = original(*args, **kwargs)
        paths.append(p)
        # Real separate DB transaction after the worker has claimed its job,
        # before that worker creates its first file. No fake DB state/clock.
        ist.cancel_job(jid)
        return p

    monkeypatch.setattr(worker, 'staging_data_path', cancel_after_claim)
    monkeypatch.setattr(worker, '_fetch_range',
                        lambda cos, key, version, start, end, total, **kw:
                        (b'x' * (end - start + 1), end - start + 1))
    try:
        worker.process_downloading(cos=object(), state={})
    except ist.IngestionStateError:
        # DB correctly rejects late progress, but the filesystem write has
        # already happened. Verify the physical/accounting invariant below.
        pass
    after = ist.get_job(jid)
    assert after['state'] == ist.CANCELLED
    assert not (paths[0].exists()
                and guard.get_reservation(rid)['state'] == 'released'), \
        (str(paths[0]), after['local_cleanup_status'], _q(uid))


def test_reattach_replaced_by_stop_and_repair_then_single_release(pg_uri,
                                                                  tmp_path):
    """R12-2 最终合同（替代旧「恢复 uploading」断言，§6 调整规则）：核账
    只补责任——任务停止 + 清理 pending + 绑定新责任；随后本地清理恰一次
    释放，绝无「重试器删除刚恢复数据」路径。"""
    job, rid, uid = _mk_uploading('r12_reattach')
    jid = job['job_id']
    guard.release_reservation(rid, expect_holder=('ingestion_job', jid))
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute('UPDATE ingestion_jobs SET local_reservation_id=NULL '
                   'WHERE job_id=%s', (jid,))
    data = slide_storage.staging_dir(jid, '1') / 'data.svs'
    data.parent.mkdir(parents=True)
    data.write_bytes(b'x' * 100)
    assert _freeze_apply(tmp_path, extra=["--repair-residuals"]) == 0
    stopped = ist.get_job(jid)
    assert stopped['state'] == ist.FAILED  # 不恢复执行
    assert stopped['local_cleanup_status'] == ist.LOCAL_CLEANUP_PENDING
    new_rid = stopped['local_reservation_id']
    assert new_rid and new_rid != rid
    assert guard.reservation_holds_capacity(
        guard.get_reservation(new_rid))  # 责任已补（origin=reconcile）
    assert _q(uid) == (0, 100)
    assert data.exists()  # 核账本身不删数据
    # 随后清理恰一次释放
    ist.retry_local_cleanups()
    after = ist.get_job(jid)
    assert after['local_cleanup_status'] == ist.LOCAL_CLEANUP_CLEANED
    assert not data.exists()
    assert _q(uid) == (0, 0)
    assert guard.get_reservation(new_rid)['state'] == 'released'
    # 幂等：重复清理重试不再动作
    assert ist.retry_local_cleanups() == []
    assert _q(uid) == (0, 0)


def test_migration_binds_terminal_pending_responsibility(pg_uri, tmp_path):
    uid = user_store.create_user('rec-r12-terminal@example.com',
                                 'pass1234pass1234', role='user')['user_id']
    rid = guard.reserve_upload(uid, 100)['reservation_id']
    tid = 'upt_r12_terminal'
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute(
            "INSERT INTO upload_tasks (upload_id, owner_user_id, filename,"
            " safe_name, declared_size, chunk_size, confirmed_offset, state,"
            " expires_at, created_at, updated_at, reservation_id)"
            " VALUES (%s,%s,'a.svs','a.svs',100,0,100,'failed',"
            " now()+interval '1 hour', now(), now(), %s)", (tid, uid, rid))
        data = slide_storage.staging_dir(tid, 'transfer') / 'data.svs'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'x' * 100)
        db.execute("INSERT INTO upload_cleanup_pending(upload_id,"
                   " reservation_id) VALUES(%s,%s)", (tid, rid))
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now()-interval '1 second' WHERE reservation_id=%s",
                   (rid,))
    rc = _freeze_apply(tmp_path)
    assert rc == 0
    guard.reserve_upload(uid, 50)
    assert guard.get_reservation(rid)['state'] == 'reserved', (
        data.exists(), _q(uid))
    assert _q(uid) == (0, 150)
    # 清理确认后才降为 50
    slide_storage.remove_staging_tree(tid, root=tmp_path)
    import upload_task_store
    upload_task_store.confirm_cleanup_and_release(tid)
    assert _q(uid) == (0, 50)


def test_reconcile_existing_bytes_can_exceed_quota(pg_uri, tmp_path):
    uid = user_store.create_user('rec-r12-over@example.com',
                                 'pass1234pass1234', role='user')['user_id']
    tid = 'upt_r12_over'
    guard.get_quota_row(uid)
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute('UPDATE upload_user_quotas SET quota_bytes=50 '
                   'WHERE user_id=%s', (uid,))
        db.execute(
            "INSERT INTO upload_tasks (upload_id, owner_user_id, filename,"
            " safe_name, declared_size, chunk_size, confirmed_offset, state,"
            " expires_at, created_at, updated_at, reservation_id)"
            " VALUES (%s,%s,'a.svs','a.svs',100,0,100,'active',"
            " now()+interval '1 hour', now(), now(), NULL)", (tid, uid))
        data = slide_storage.staging_dir(tid, 'transfer') / 'data.svs'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'x' * 100)
    assert _freeze_apply(tmp_path, extra=["--repair-residuals"]) == 0
    assert _q(uid) == (0, 100)
