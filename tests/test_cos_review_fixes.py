# -*- coding: utf-8 -*-
"""review 740e823 五项问题的回归测试（复现用例入仓，2026-09-25）。

覆盖：Complete 后 HEAD 失败/崩溃的恢复（P1-1）；提交恢复不凭大小认领
目标文件（P1-2）；取消与本地提交互斥（P1-3）；对账计入未完成 multipart
分块占用（P1-4）；全局活跃上限并发准入原子守卫（P2）。修复实现见
ingestion_store.py / cos_ingest_worker.py / app.py 同日注释。
原复现脚本由审查方提供（/tmp/cos-review-740e823/），此处原样入仓仅加
本头注释——用例语义与断言未改动。
"""
import os
import hashlib
import concurrent.futures
import threading
import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_ingest_worker as w
import cos_client
import cos_config
import cos_pool_store
import ingestion_store as s
import share_store
import slide_io


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


def test_intent_does_not_adopt_other_users_same_size_file(tmp_path):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st)
    claim = s.claim_next_job_for_worker([s.VALIDATING])
    s.worker_persist_commit_intent(job['job_id'], claim['worker_generation'], {
        'target': 'a.svs', 'sha256': hashlib.sha256(payload).hexdigest(),
        'part': w.part_name(job['job_id']), 'declared_size': len(payload)})
    s.release_worker_lease(job['job_id'], claim['worker_lease_token'])
    (tmp_path / 'a.svs').write_bytes(b'x' * len(payload))
    share_store.set_slide_meta('a.svs', owner_user_id='victim', requester_role='owner')
    w.process_validating(cos=fake, state=st)
    def owner(cur):
        cur.execute("SELECT owner_user_id FROM slides WHERE legacy_filename='a.svs'")
        return cur.fetchone()['owner_user_id']
    assert h._sql(owner) == 'victim'
    assert s.get_job(job['job_id'])['state'] == s.FAILED


def test_cancel_at_commit_boundary_does_not_leave_visible_file(monkeypatch, tmp_path):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st)
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    promote = w._promote_no_clobber
    def cancel_then_promote(src, dest):
        s.cancel_job(job['job_id'])
        return promote(src, dest)
    monkeypatch.setattr(w, '_promote_no_clobber', cancel_then_promote)
    try:
        w.process_validating(cos=fake, state=st)
    except s.IngestionStateError:
        pass
    w.process_cleanup(cos=fake, state=st)
    assert not (tmp_path / 'a.svs').exists()


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
    assert cos_pool_store.get_pool_state()['reconcile_status'] == 'reconcile_required'
