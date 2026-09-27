# -*- coding: utf-8 -*-
"""R11 复核反例（2026-09-27，/tmp/slide-id-review-r11/test_r11.py 原样入仓）。

审查记录：/tmp/slide-id-review-r11/REVIEW.md（repo 内副本见
docs/upload-capacity-lifecycle-inventory-20260927.md 附录）。

断言不改：修复以让本文件两个用例转绿为目标——

1. P1：COS 活跃任务在预约被同用户新准入回收后，不得长期停留在
   「uploading + released 预约」的 skipped 状态（要么恢复有效容量绑定，
   要么明确转入终止/清理，不能无容量保障地保持活跃）。
2. P2：R10 并发验收在「续租先赢」的确定性调度下必须通过（原实现把
   第二轮续租结果当首轮断言，拒绝 renewed）。

生命周期修复方案：docs/task-capacity-lifecycle-repair-agent-plan-20260927.md
（绑定模型落地后：场景 1 期望「同一 rid 重发执行租约、容量从未丢失」，
场景 2 期望首轮即 renewed——断言语义保持「活跃任务必须有有效预约或
明确终态」，与方案 §7 验收矩阵一致）。
"""
import psycopg
import ingestion_store as store
import upload_guard as guard
import user_store
import threading
import test_slide_id_review_r10 as acceptance

def test_active_cos_job_recovers_after_other_admission_reclaims_lease(pg_uri):
    uid=user_store.create_user('r11-recover@example.com','pass1234pass1234',role='user')['user_id']
    job,_=store.create_waiting_job(uid,'user','r11.tif','r11.tif','tif',100)
    rid=guard.reserve_upload(uid,100)['reservation_id']
    with psycopg.connect(pg_uri,autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='uploading',local_reservation_id=%s,capacity_admitted_at=now(),pool_reserved_bytes=100 WHERE job_id=%s",(rid,job['job_id']))
        db.execute("UPDATE upload_reservations SET expires_at=now()-interval '1 second' WHERE reservation_id=%s",(rid,))
    # One valid serialization of the R10 race: new admission wins first.
    guard.reserve_upload(uid,50)
    for _ in range(3):
        store.renew_active_local_reservations()
    after=store.get_job(job['job_id'])
    assert after['state'] not in store.ACTIVE_UPLOAD_STATES or guard.reservation_is_active(guard.get_reservation(after['local_reservation_id'])), after

def test_r10_accepts_renewal_first_schedule(monkeypatch):
    original=acceptance._run_concurrently
    def renewal_wins(first,second):
        done=threading.Event()
        def a():
            try: return first()
            finally: done.set()
        def b():
            assert done.wait(5)
            return second()
        return original(a,b)
    monkeypatch.setattr(acceptance,'_run_concurrently',renewal_wins)
    acceptance.test_cos_renewal_vs_same_user_admission()
