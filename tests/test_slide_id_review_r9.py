# -*- coding: utf-8 -*-
"""R9 复核（2026-09-27）复现用例——原样入仓为回归（断言不动）。

审查记录：/tmp/slide-id-review-r9/REVIEW.md。两个反例同一根因：
登记在取得配额行锁前读了预约状态、取锁后不重读，重激活 UPDATE 无
expected-state CAS——①锁前读到 reserved、锁内已被回收→漏账；②两个
登记方都锁前读到 released→重复补账。修复＝锁内 CAS（released→reserved
RETURNING）+ 仅按实际转换行补记；release/consume 锁序统一为
quota 行 → reservation 行。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg
import upload_task_store
import upload_guard
import user_store

def before_quota_lock(monkeypatch, callback):
    """Run another committed transaction after the snapshot read, before lock.
    No thread timing or sleeps; all SQL and transactions use real PostgreSQL.
    """
    original = upload_task_store._pg_connect
    fired = False
    class Cursor:
        def __init__(self, cur): self.cur = cur
        def __enter__(self): self.cur.__enter__(); return self
        def __exit__(self, *args): return self.cur.__exit__(*args)
        def __getattr__(self, name): return getattr(self.cur, name)
        def execute(self, sql, params=None):
            nonlocal fired
            if not fired and sql.startswith('SELECT reserved_bytes FROM upload_user_quotas'):
                fired = True
                callback()
            return self.cur.execute(sql, params)
    class Connection:
        def __init__(self): self.conn = original()
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs): return Cursor(self.conn.cursor(*args, **kwargs))
    monkeypatch.setattr(upload_task_store, '_pg_connect', Connection)

def test_reclaim_after_snapshot_before_lock_is_accounted(monkeypatch, pg_uri):
    uid = user_store.create_user('r9-reclaim@example.com','pass1234pass1234',role='user')['user_id']
    rid = upload_guard.reserve_upload(uid,100)['reservation_id']
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("UPDATE upload_reservations SET expires_at=now()-interval '1 second' WHERE reservation_id=%s", (rid,))
    before_quota_lock(monkeypatch, lambda: upload_guard.reserve_upload(uid,50))
    upload_task_store.record_cleanup_pending('upt-r9-reclaim',rid,error='IO')
    assert upload_task_store.get_cleanup_pending('upt-r9-reclaim') is not None
    assert upload_guard.get_quota_row(uid)['reserved_bytes'] == 150

def test_two_pending_registrations_do_not_double_charge(monkeypatch):
    uid = user_store.create_user('r9-double@example.com','pass1234pass1234',role='user')['user_id']
    rid = upload_guard.reserve_upload(uid,100)['reservation_id']
    upload_guard.release_reservation(rid)
    before_quota_lock(monkeypatch, lambda: upload_task_store.record_cleanup_pending('upt-r9-double',rid,error='IO worker B'))
    upload_task_store.record_cleanup_pending('upt-r9-double',rid,error='IO worker A')
    assert upload_task_store.get_cleanup_pending('upt-r9-double') is not None
    assert upload_guard.get_quota_row(uid)['reserved_bytes'] == 100
