import os
import json
import psycopg
import pytest

pytestmark = pytest.mark.skip(reason=
    "U5（检查点 B）：本文件反例 2 的被测面（V1 _api_upload_native_single 早退分支）已按 docs/cos-only-upload-agent-plan-20260928.md §6 明确删除；断言按处置约定原样保留，仅整文件跳过（历史证据）。反例 1/3 的核账与恢复语义仍在 test_reconcile_upload_capacity.py 生效。")



import slide_storage
import upload_guard as guard
import upload_task_store
from test_reconcile_upload_capacity import recon, _uid, _task, _q
import app as app_mod
import task_storage_lock


def cli(pg_uri, root, *extra):
    return recon.main(['--database-url', pg_uri, '--upload-dir', str(root), *map(str, extra)])


def residue(root, tid, payload=b'x'*100):
    p = slide_storage.staging_dir(tid, 'transfer', root=root) / 'data.svs'
    p.parent.mkdir(parents=True)
    p.write_bytes(payload)
    return p


def test_same_size_content_drift_blocks_apply(pg_uri, tmp_path):
    uid = _uid('r13_bytes')
    tid = 'upt_r13_bytes'
    _task(uid, tid, None)
    p = residue(tmp_path, tid)
    plan = tmp_path / 'plan.json'
    assert cli(pg_uri, tmp_path, '--plan-out', plan, '--repair-residuals') == 0
    p.write_bytes(b'y'*100)
    rc = cli(pg_uri, tmp_path, '--apply', '--plan', plan, '--repair-residuals')
    assert rc == 3, 'Changed content was accepted against the frozen plan'


def test_owner_drift_blocks_before_any_mutation(pg_uri, tmp_path):
    a, b = _uid('r13_owner_a'), _uid('r13_owner_b')
    rid = guard.reserve_upload(a, 100)['reservation_id']
    tid = 'upt_r13_owner'
    _task(a, tid, rid)
    plan = tmp_path / 'plan.json'
    assert cli(pg_uri, tmp_path, '--plan-out', plan) == 0
    with psycopg.connect(pg_uri, autocommit=True) as db:
        db.execute('UPDATE upload_tasks SET owner_user_id=%s WHERE upload_id=%s', (b, tid))
    assert cli(pg_uri, tmp_path, '--apply', '--plan', plan) == 3
    with psycopg.connect(pg_uri) as db:
        row = db.execute('SELECT holder_kind,holder_id FROM upload_reservations WHERE reservation_id=%s', (rid,)).fetchone()
        receipts = db.execute('SELECT count(*) FROM upload_capacity_repair_receipts').fetchone()[0]
    assert row == (None, None) and receipts == 0, (row, receipts)


def test_consumed_published_hardlink_not_recharged(pg_uri, tmp_path):
    uid = _uid('r13_consumed')
    tid = 'upt_r13_consumed'
    rid = guard.reserve_upload(uid, 100, holder_kind='upload_task', holder_id=tid, purpose='upload')['reservation_id']
    _task(uid, tid, rid, state='committed')
    guard.consume_reservation(rid, 100, expect_holder=('upload_task', tid))
    p = residue(tmp_path, tid)
    obj = tmp_path / 'objects' / 'published-data.svs'
    obj.parent.mkdir(parents=True)
    os.link(p, obj)
    plan = tmp_path / 'plan.json'
    rc = cli(pg_uri, tmp_path, '--plan-out', plan, '--repair-residuals')
    if rc == 0:
        cli(pg_uri, tmp_path, '--apply', '--plan', plan, '--repair-residuals')
    assert _q(uid) == (100, 0), 'Already consumed bytes were charged a second time'


def test_replay_after_cleanup_is_successful_noop(pg_uri, tmp_path):
    uid = _uid('r13_replay')
    tid = 'upt_r13_replay'
    _task(uid, tid, None)
    residue(tmp_path, tid)
    plan = tmp_path / 'plan.json'
    assert cli(pg_uri, tmp_path, '--plan-out', plan, '--repair-residuals') == 0
    assert cli(pg_uri, tmp_path, '--apply', '--plan', plan, '--repair-residuals') == 0
    slide_storage.remove_staging_tree(tid, root=tmp_path)
    upload_task_store.confirm_cleanup_and_release(tid)
    assert cli(pg_uri, tmp_path, '--apply', '--plan', plan, '--repair-residuals') == 0
    assert _q(uid) == (0, 0)


def test_native_upload_owner_failure_does_not_relock_itself(monkeypatch, tmp_path):
    original = task_storage_lock.task_storage_lock
    def bounded_lock(kind, tid, **kwargs):
        kwargs['timeout'] = 0.05  # bound the real self-wait, don't hang pytest
        kwargs['root'] = tmp_path
        return original(kind, tid, **kwargs)
    monkeypatch.setattr(task_storage_lock, 'task_storage_lock', bounded_lock)
    monkeypatch.setattr(app_mod, '_upload_asset_owner', lambda ident: None)
    with app_mod.app.test_request_context('/api/upload', method='POST'):
        _, status = app_mod._api_upload_native_single(
            None, 'a.svs', 'a.svs', 'svs', {}, None, None)
    assert status == 500
