# -*- coding: utf-8 -*-
"""R10（2026-09-27）锁序统一验收：三场景真实并发（线程起始屏障 + 真实 PG）。

审查要求（/tmp/slide-id-review-r10 同源指令）：
  - 全部预约操作收敛同一锁协议（quota 行 → reservation 行）；
  - release 公开入口删除重复实现、topup 锁内重验、COS 常驻续租整事务
    不再「先锁预约、过期后才取配额」；
  - 验收三场景：释放×过期回收、补占×转实占、COS 过期续租×同用户新准入；
  - 验证无死锁、合法状态转换、账本一致（quota.reserved_bytes ==
    SUM(state='reserved')）、重复调用不重复记账；
  - 屏障只做同时起跑（起始屏障）——锁序统一后「持配额锁等预约行/反序」
    的交错已被协议禁止，不再用中途注入强制非法交错。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import ingestion_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
_JOIN_TIMEOUT = 30.0


def _run_concurrently(*fns):
    """起始屏障同时起跑；返回各线程异常列表（None=成功）。join 超时视为
    死锁（fail）。"""
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def _wrap(i, fn):
        barrier.wait(timeout=_JOIN_TIMEOUT)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 原样回报给主线程断言
            results[i] = exc

    threads = [threading.Thread(target=_wrap, args=(i, fn))
               for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)
        assert not t.is_alive(), "并发场景死锁（线程未在限期内完成）"
    return results


def _ledger_ok(uid):
    """账本一致不变量：quota.reserved_bytes == SUM(在约 reserved 行)。"""
    with psycopg.connect(PG_URI) as db:
        q = db.execute("SELECT reserved_bytes FROM upload_user_quotas "
                       "WHERE user_id=%s", (uid,)).fetchone()[0]
        s = db.execute("SELECT COALESCE(SUM(reserved_bytes),0) "
                       "FROM upload_reservations "
                       "WHERE user_id=%s AND state='reserved'",
                       (uid,)).fetchone()[0]
    return int(q) == int(s), int(q), int(s)


def _expire(rid):
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now()-interval '1 second' WHERE reservation_id=%s",
                   (rid,))


# --------------------------------------------------------------------------- #
# 场景 1：释放 × 过期回收（同一预约）
# --------------------------------------------------------------------------- #
def test_release_vs_reclaim_same_reservation():
    uid = user_store.create_user(
        "r10-rel@example.com", "pass1234pass1234", role="user")["user_id"]
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _expire(rid)
    results = _run_concurrently(
        lambda: upload_guard.release_reservation(rid),
        lambda: upload_guard.reserve_upload(uid, 50),
    )
    assert results == [None, None], results  # 无死锁/无异常
    with psycopg.connect(PG_URI) as db:
        state = db.execute("SELECT state FROM upload_reservations "
                           "WHERE reservation_id=%s", (rid,)).fetchone()[0]
        q = db.execute("SELECT reserved_bytes FROM upload_user_quotas "
                       "WHERE user_id=%s", (uid,)).fetchone()[0]
    assert state == "released"          # 合法转换：恰一次 reserved→released
    assert q == 50                      # 账本一致：旧 100 归还 + 新 50
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
    # 重复调用不重复记账
    out = upload_guard.release_reservation(rid)
    assert out["state"] == "released"
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 50


# --------------------------------------------------------------------------- #
# 场景 2：补占 × 转实占（同一预约）
# --------------------------------------------------------------------------- #
def test_topup_vs_consume_same_reservation():
    uid = user_store.create_user(
        "r10-top@example.com", "pass1234pass1234", role="user")["user_id"]
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    results = _run_concurrently(
        lambda: upload_guard.topup_reservation(rid, 30),
        lambda: upload_guard.consume_reservation(rid, 100),
    )
    # 合法终态唯一：预约恰一次 reserved→consumed；失败方只能是补占撞上
    # consumed 的 ReservationInvalid（整笔回滚，无半更新）。
    by_type = [type(r).__name__ for r in results]
    assert set(by_type) <= {"NoneType", "ReservationInvalid"}, results
    assert "NoneType" in by_type  # 至少转实占成功
    with psycopg.connect(PG_URI) as db:
        state, rb = db.execute(
            "SELECT state, reserved_bytes FROM upload_reservations "
            "WHERE reservation_id=%s", (rid,)).fetchone()
        used, reserved = db.execute(
            "SELECT used_bytes, reserved_bytes FROM upload_user_quotas "
            "WHERE user_id=%s", (uid,)).fetchone()
    assert state == "consumed"
    assert int(used) == 100 and int(reserved) == 0
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
    # 补占失败方（若有）没有留下半更新：预约行字节只有 100 或 130 之一
    # 且结算额恒为 used+reserved 的组成部分（上面断言已覆盖）。
    assert int(rb) in (100, 130)
    # 重复 consume 不重复记账
    out = upload_guard.consume_reservation(rid, 100)
    assert out["state"] == "consumed"
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 100


# --------------------------------------------------------------------------- #
# 场景 3：COS 过期续租（整事务）× 同用户新准入
# --------------------------------------------------------------------------- #
def test_cos_renewal_vs_same_user_admission():
    uid = user_store.create_user(
        "r10-cos@example.com", "pass1234pass1234", role="user")["user_id"]
    job, _created = ingestion_store.create_waiting_job(
        uid, "user", "r10.tif", "r10.tif", "tif", 100)
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state='uploading', "
                   "local_reservation_id=%s, capacity_admitted_at=now(), "
                   "pool_reserved_bytes=100 WHERE job_id=%s",
                   (rid, job["job_id"]))
    _expire(rid)

    results = _run_concurrently(
        ingestion_store.renew_active_local_reservations,
        lambda: upload_guard.reserve_upload(uid, 50),
    )
    assert results == [None, None], results  # 无死锁/无异常

    outcomes = ingestion_store.renew_active_local_reservations()
    after = ingestion_store.get_job(job["job_id"])
    with psycopg.connect(PG_URI) as db:
        old_state = db.execute("SELECT state FROM upload_reservations "
                               "WHERE reservation_id=%s",
                               (rid,)).fetchone()[0]
    assert old_state == "released"  # 旧预约两条路径下都终态 released
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
    # 合法结果二选一：续租先赢 → re-admit（新 rid、账本 100+50）；
    # 准入先赢 → 旧预约已被回收（reservation_state_conflict 保守跳过，
    # 账本 50）。第二轮幂等（recovered→renewed 不改账本）。
    first = outcomes.get(job["job_id"])
    assert first in ("recovered", "skipped"), outcomes
    q = upload_guard.get_quota_row(uid)
    if first == "recovered":
        assert int(q["reserved_bytes"]) == 150
        assert after["local_reservation_id"] != rid
        assert outcomes[job["job_id"]] == "renewed"  # 第二轮：续新约
    else:
        assert int(q["reserved_bytes"]) == 50
    # 重复调用不重复记账（第三轮不变）
    ingestion_store.renew_active_local_reservations()
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == \
        int(q["reserved_bytes"])
    ok, qv, sv = _ledger_ok(uid)
    assert ok, (qv, sv)
