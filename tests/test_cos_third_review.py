# -*- coding: utf-8 -*-
"""review 第三轮（af4ac46）两项问题回归测试（复现用例入仓，2026-09-25）。

覆盖：平台 owner 回落被无条件视为任务归属（实名任务要求精确归属，
平台 owner 等价仅限匿名任务）；归属拒绝时按路径删除 dest 可能误删
并发替换后的他人文件（删除前 (dev, ino) 同一性守卫）。复现脚本由
审查方提供（/tmp/cos-review-740e823/），此处原样入仓仅加本头注释。
"""
import hashlib
import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env
import cos_ingest_worker as w
import ingestion_store as s
import share_store
import share_store_pg


def test_platform_owner_file_not_claimed_by_user(monkeypatch, tmp_path):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner='ordinary-user', role='user')
    claim = s.claim_next_job_for_worker([s.VALIDATING])
    s.worker_persist_commit_intent(job['job_id'], claim['worker_generation'], {
        'target': 'a.svs', 'sha256': hashlib.sha256(payload).hexdigest(),
        'part': w.part_name(job['job_id']), 'declared_size': len(payload)})
    s.release_worker_lease(job['job_id'], claim['worker_lease_token'])
    (tmp_path / 'a.svs').write_bytes(payload)
    monkeypatch.setattr(share_store_pg, '_OWNER_USER_ID', 'platform-owner')
    share_store.set_slide_meta('a.svs', owner_user_id='platform-owner', requester_role='owner')
    w.process_validating(cos=fake, state=st)
    assert s.get_job(job['job_id'])['state'] == s.FAILED

def test_owner_rejection_does_not_delete_replaced_destination(monkeypatch, tmp_path):
    import slide_io
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner='ordinary-user', role='user')
    share_store.set_slide_meta('a.svs', owner_user_id='victim', requester_role='owner')
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    original = share_store.set_slide_meta
    dest = tmp_path / 'a.svs'
    victim_bytes = b'V' * len(payload)
    def concurrent_replace(*args, **kwargs):
        dest.unlink()  # concurrent actor removes just-promoted file
        dest.write_bytes(victim_bytes)  # another actor wins the now-free name
        return original(*args, **kwargs)
    monkeypatch.setattr(share_store, 'set_slide_meta', concurrent_replace)
    w.process_validating(cos=fake, state=st)
    assert dest.read_bytes() == victim_bytes
