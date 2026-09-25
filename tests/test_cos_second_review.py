# -*- coding: utf-8 -*-
"""review 第二轮（8e1e2db）两项问题回归测试（复现用例入仓，2026-09-25）。

覆盖：Complete 响应丢失后恢复路径首次 HEAD 瞬态超时被误判永久失败
（_RecoveryTransient 保持 completing 重试）；同内容（同 sha）他人文件
仍被提交恢复认领（set_slide_meta 返回 owner 终检）。复现脚本由审查方
提供（/tmp/cos-review-740e823/），此处原样入仓仅加本头注释。
"""
import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_client
import cos_ingest_worker as w
import ingestion_store as s


def test_complete_response_lost_then_head_transient_recovers(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._prepare(fake, st)
    h._put_plan_parts(fake, job, payload)
    s.request_upload_complete(job['job_id'])
    original = fake.complete_multipart
    def completes_but_response_lost(*args):
        original(*args)
        raise cos_client.CosClientError('response lost')
    monkeypatch.setattr(fake, 'complete_multipart', completes_but_response_lost)
    w.process_completing(cos=fake, state=st)
    monkeypatch.setattr(fake, 'complete_multipart', original)
    def no_such_upload(*args):
        raise cos_client.CosClientError('HTTP 404 NoSuchUpload')
    monkeypatch.setattr(fake, 'list_parts', no_such_upload)
    saved_head = fake.head_object
    calls = [0]
    def temporary_head(*args):
        calls[0] += 1
        if calls[0] == 1:
            raise cos_client.CosClientError('HEAD timeout')
        return saved_head(*args)
    monkeypatch.setattr(fake, 'head_object', temporary_head)
    w.process_completing(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.COMPLETING
    w.process_completing(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.QUEUED

def test_same_content_other_owner_not_claimed(monkeypatch, tmp_path):
    import hashlib
    import os
    import share_store
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st)
    claim = s.claim_next_job_for_worker([s.VALIDATING])
    s.worker_persist_commit_intent(job['job_id'], claim['worker_generation'], {
        'target': 'a.svs', 'sha256': hashlib.sha256(payload).hexdigest(),
        'part': w.part_name(job['job_id']), 'declared_size': len(payload)})
    s.release_worker_lease(job['job_id'], claim['worker_lease_token'])
    (tmp_path / 'a.svs').write_bytes(payload)
    share_store.set_slide_meta('a.svs', owner_user_id='victim', requester_role='owner')
    w.process_validating(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.FAILED
