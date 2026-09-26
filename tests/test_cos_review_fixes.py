# -*- coding: utf-8 -*-
"""review 740e823 五项问题回归测试（P1/P2 三项原样保留；P4-b 两项断言换新，
2026-09-26）。

原样保留（与 P4-b 无关）：
  - Complete 后 HEAD 失败/崩溃的恢复（P1-1）——完成阶段；
  - 对账计入未完成 multipart 分块占用（P1-4）；
  - 全局活跃上限并发准入原子守卫（P2）。

断言换新（P4 合同 §5/§8：场景意义保留、废弃断言换新）：
  - P1-2「提交恢复不凭大小认领目标文件」：name_unavailable/adopted 族拆除
    后，恢复只按任务绑定的 slide_id 判定——他人同大小文件与目标
    objects/<slide_id>/ 零关联，本任务照常发布；
  - P1-3「取消与本地提交互斥」：intent 落库后取消被 CommitInProgress 拒
    （提交先赢且不可撤销）；intent 前取消仍可行（staging 清理 + 无包）。
"""
import concurrent.futures
import hashlib
import os
import threading

import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_ingest_worker as w
import cos_client
import cos_config
import cos_pool_store
import ingestion_store as s
import share_store
import slide_io
import slide_storage
import slide_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def test_complete_recovers_after_head_timeout(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._prepare(fake, st)
    h._put_plan_parts(fake, job, payload)
    s.request_upload_complete(job['job_id'])
    original_head = fake.head_object
    def timeout(*args):
        raise cos_client.CosClientError('HEAD transient timeout')
    monkeypatch.setattr(fake, 'head_object', timeout)
    w.process_completing(cos=fake, state=st)
    monkeypatch.setattr(fake, 'head_object', original_head)
    original_list = fake.list_parts
    def realistic_list(key, upload_id):
        if upload_id not in fake.uploads.get(key, {}):
            raise cos_client.CosClientError('HTTP 404 NoSuchUpload')
        return original_list(key, upload_id)
    monkeypatch.setattr(fake, 'list_parts', realistic_list)
    for _ in range(3):
        w.process_completing(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.QUEUED


def test_intent_recovery_ignores_other_users_same_size_file(monkeypatch):
    """P1-2 场景换新：他人同大小文件与统一发布零关联（不再按名认领/冲突）。

    旧断言：dest 已被 victim 同大小文件占用 → job FAILED（name_unavailable）。
    新断言：恢复发布进自己的 slide_id；victim 文件/行原样保留。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='uploader', role='user')
    sha = hashlib.sha256(payload).hexdigest()
    # 模拟崩溃点：下载代已收尾、intent 持久化、发布未发生
    claim = s.claim_next_job_for_worker([s.VALIDATING])
    s.worker_persist_commit_intent(
        job['job_id'], claim['worker_generation'], {
            'task_ref': job['job_id'],
            'generation': claim['worker_generation'] - 1,  # 下载代目录
            'commit_token': str(claim['worker_generation'] - 1),
            'slide_id': job['slide_id'],
            'owner_user_id': 'uploader',
            'sha256': sha, 'accounted_bytes': len(payload),
            'declared_size': len(payload)})
    s.release_worker_lease(job['job_id'], claim['worker_lease_token'])
    victim_path = os.path.join(h._env_dir(), 'a.svs')
    with open(victim_path, 'wb') as fh:  # 他人同大小文件（旧 dest 位）
        fh.write(b'x' * len(payload))
    share_store.set_slide_meta('a.svs', owner_user_id='victim',
                               requester_role='owner')
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    try:
        assert w.process_validating(cos=fake, state=st) == job['job_id']
        out = s.get_job(job['job_id'])
        assert out['state'] == s.READY  # 同大小他人文件不构成冲突/认领依据
        with open(victim_path, 'rb') as fh:
            assert fh.read() == b'x' * len(payload)  # victim 不动
        with open(h._bundle_data(out['slide_id']), 'rb') as fh:
            assert fh.read() == payload
        assert out['sha256_actual'] == sha

        def owner(cur):
            cur.execute("SELECT owner_user_id FROM slides WHERE "
                        "legacy_filename='a.svs'")
            return cur.fetchone()['owner_user_id']
        assert h._sql(owner) == 'victim'
    finally:
        try:
            os.unlink(victim_path)
        except OSError:
            pass


def test_cancel_vs_commit_boundary_has_clear_winner(monkeypatch):
    """P1-3 场景换新：取消与统一发布的边界竞争有明确胜者。

    - intent 持久化后取消 → CommitInProgress 拒（提交先赢且不可撤销），
      发布完成资产归 owner（无「无主可见文件」）；
    - intent 前（水位暂停窗口）取消 → 成功：暂存树清理、无 objects 包、
      资产行 failed。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(
        fake, st, owner='uploader', role='user')
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    # 屏障：intent 落库后、发布前插入取消尝试——必须被拒
    real_persist = s.worker_persist_commit_intent

    def cancel_then_persist(job_id, generation, intent):
        out = real_persist(job_id, generation, intent)
        with pytest.raises(s.CommitInProgress):
            s.cancel_job(job_id)
        return out

    monkeypatch.setattr(s, 'worker_persist_commit_intent', cancel_then_persist)
    assert w.process_validating(cos=fake, state=st) == job['job_id']
    out = s.get_job(job['job_id'])
    assert out['state'] == s.READY  # 提交先赢：完成发布与结算
    assert slide_store.authorize_read(out['slide_id'],
                                      actor_user_id='uploader')  # 归 owner 可读
    # 落库后取消同样被拒（删除走切片删除合同）
    with pytest.raises(s.IngestionStateError):
        s.cancel_job(job['job_id'])

    # intent 前取消（水位暂停窗口）→ 取消先赢：清理彻底、无包
    fake2, st2 = h.FakeCos(), {}
    job2, payload2 = h._drive_to_validating(
        fake2, st2, owner='uploader2', role='user')
    monkeypatch.setattr(h.upload_guard, 'UPLOAD_RESERVED_FREE_BYTES', 10 ** 15)
    w.process_validating(cos=fake2, state=st2)  # 水位不过：保持 validating
    assert s.get_job(job2['job_id'])['state'] == s.VALIDATING
    cancelled = s.cancel_job(job2['job_id'])
    assert cancelled['state'] == s.CANCELLED
    assert not slide_storage.staging_task_dir(
        job2['job_id'], root=h._env_dir()).exists()  # 暂存树已清
    assert not slide_storage.bundle_dir(
        job2['slide_id'], root=h._env_dir()).exists()  # 从未发布
    assert slide_store.resolve_slide_id(
        job2['slide_id']).asset_state == 'failed'


def test_parallel_admission_respects_global_limit(monkeypatch):
    monkeypatch.setattr(cos_config, 'COS_MAX_ACTIVE_UPLOADS_GLOBAL', 1)
    jobs = [h._mkjob(owner='owner%d' % i) for i in range(2)]
    original_counts = s._active_counts
    barrier = threading.Barrier(2)
    def counts(*args):
        result = original_counts(*args)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(s, '_active_counts', counts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda j: s.try_admit_job(j['job_id']), jobs))
    assert sum(r['outcome'] == 'admitted' for r in results) == 1


def test_reconcile_counts_unfinished_parts():
    fake = h.FakeCos()
    key = 'incoming/orphan/inj_unknown/random'
    uid = fake.initiate_multipart(key)
    fake.put_part(key, uid, 1, b'x' * 1_200_000)
    w.reconcile_tick(cos=fake, state={}, force=True)
    assert cos_pool_store.get_pool_state()['reconcile_status'] == \
        'reconcile_required'
