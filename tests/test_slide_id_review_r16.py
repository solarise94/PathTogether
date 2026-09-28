# -*- coding: utf-8 -*-
"""R16 独立审查反例（/tmp/slide-id-review-r16/test_r16.py 原样入仓，断言未改）。

1. ZIP 解压前必须补占（写入前 actual <= reserved）；
2. KFB 在 intent 前取消不得留下可执行子任务与源文件；
3. 排空 report 不得因存在任务行放行终态残留；
5. ZIP intent 前失败重试不得重复分配无绑定资产。
（R16 第 4 项检查点 A 随用户裁决不保留 A 而退役，无运行反例。）
"""
from pathlib import Path
import pytest
import test_cos_ingestion_kinds as h
from test_cos_ingestion_kinds import _env, _fake_probe
import ingestion_store as ist
import cos_ingest_worker as ciw
import upload_content
import upload_guard
import slide_io
import slide_storage
import conversion_store
import upload_task_store
from test_upload_drain import _drain_tool, _mk_task


def test_zip_must_reserve_before_expansion(monkeypatch, tmp_path):
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    payload, _ = h._zip_bytes({'a.tif': h._payload(20000)})
    fake, st = h.FakeCos(), {}
    job = h._drive_to_validating(fake, st, payload, owner='r16_zip', role='user')
    observations = []
    real = upload_guard.topup_reservation
    def check(rid, extra, *args, **kw):
        root = slide_storage.staging_task_dir(job['job_id'], root=tmp_path)
        actual = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        reserved = upload_guard.get_reservation(rid)['reserved_bytes']
        observations.append((actual, reserved))
        return real(rid, extra, *args, **kw)
    monkeypatch.setattr(upload_guard, 'topup_reservation', check)
    ciw.process_validating(cos=fake, state=st)
    assert observations
    assert all(actual <= reserved for actual, reserved in observations), observations


def test_conversion_cancel_before_intent_does_not_leave_live_child(monkeypatch, tmp_path, _fake_probe):
    fake, st = h.FakeCos(), {}
    job = h._drive_kfb(fake, st, owner='r16_cancel')
    jid = job['job_id']
    real = ist.worker_persist_commit_intent
    def cancel_then_persist(*args, **kw):
        # Actual cancellation DB phase commits while the writer holds FS lock.
        # Cleanup waits for writer; run that real phase after the writer exits.
        ist._terminate_cancel_tx(jid, reason_code='cancelled_by_user')
        return real(*args, **kw)
    monkeypatch.setattr(ist, 'worker_persist_commit_intent', cancel_then_persist)
    ciw.process_validating(cos=fake, state=st)
    ist._local_cleanup_finish(jid)
    child = conversion_store.get_job_by_upload_id(jid)
    assert ist.get_job(jid)['state'] == ist.CANCELLED
    files = [str(p) for p in slide_storage.staging_task_dir(child['id'], root=tmp_path).rglob('*') if p.is_file()]
    assert child['state'] not in ('queued', 'converting', 'ready'), (child['state'], h._quota('r16_cancel'), files)
    assert not files


def test_drain_rejects_known_terminal_residue(tmp_path):
    task = _mk_task('r16_old.tif')
    upload_task_store.cancel_task(task['upload_id'])
    p = slide_storage.staging_dir(task['upload_id'], 'transfer', root=tmp_path) / 'data.tif'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'x' * 100)
    assert _drain_tool().main(['report', '--upload-dir', str(tmp_path)]) == 3


def test_zip_retry_before_intent_does_not_allocate_unbound_slides(monkeypatch, tmp_path):
    monkeypatch.setattr(slide_io, 'open_slide', h._ok_open_slide)
    payload, _ = h._zip_bytes({'a.tif': h._payload(200)})
    fake, st = h.FakeCos(), {}
    job = h._drive_to_validating(fake, st, payload)
    real = ist.worker_persist_commit_intent
    def interrupted(*a, **kw):
        raise ist.IngestionStateError('injected intent transaction failure')
    monkeypatch.setattr(ist, 'worker_persist_commit_intent', interrupted)
    ciw.process_validating(cos=fake, state=st)
    original = ist.list_ingestion_job_items(job['job_id'])
    assert len(original) == 1
    monkeypatch.setattr(ist, 'worker_persist_commit_intent', real)
    assert ciw.process_validating(cos=fake, state=st) == job['job_id']
    def rows(cur):
        cur.execute('SELECT slide_id,asset_state FROM slides WHERE owner_user_id=%s', ('own',))
        return cur.fetchall()
    assets = h._sql(rows)
    assert len(assets) == 1, assets
