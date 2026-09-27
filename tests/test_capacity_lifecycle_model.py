# -*- coding: utf-8 -*-
"""0072 上传容量生命周期模型测试（plan §4/§6-B：模型约束与状态转换）。

「任务持有容量，租约控制执行」：
  - 绑定（holder 三元组）后预约不参加 TTL 回收；expires_at 退化为执行
    租约（过期可重发）；
  - 释放/转实占需持有者上下文（清理确认后释放 / 发布结算的任务语境）；
  - 唯一性：同一 (holder_kind, holder_id, purpose) 至多一份未结算责任；
  - 在途/每小时计数的新口径（绑定不看租约；pending 豁免执行槽；核账
    origin 不计每小时）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import pg_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


def _mkuser(tag):
    return user_store.create_user(
        "life-%s@example.com" % tag, "pass1234pass1234", role="user")["user_id"]


def _expire(rid, seconds=1):
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute(
            "UPDATE upload_reservations SET expires_at="
            "now() - make_interval(secs => %s) WHERE reservation_id=%s",
            (int(seconds), rid))


def _uid_row(rid):
    with psycopg.connect(PG_URI) as db:
        return db.execute(
            "SELECT state, holder_kind, holder_id, purpose, origin "
            "FROM upload_reservations WHERE reservation_id=%s",
            (rid,)).fetchone()


def _bind(rid, task_id, kind="upload_task", purpose="upload"):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                return upload_guard.bind_reservation_locked(
                    cur, rid, kind, task_id, purpose)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 绑定原语（plan §4.1）
# --------------------------------------------------------------------------- #
def test_reserve_with_holder_binds_atomically():
    uid = _mkuser("bind1")
    out = upload_guard.reserve_upload(
        uid, 100, holder_kind="ingestion_job", holder_id="inj_x1",
        purpose="ingest_local")
    row = _uid_row(out["reservation_id"])
    assert row == ("reserved", "ingestion_job", "inj_x1", "ingest_local",
                   "admission")


def test_bind_then_rebind_same_holder_idempotent():
    uid = _mkuser("bind2")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    out = _bind(rid, "upt_a")
    assert out["holder_id"] == "upt_a"
    out2 = _bind(rid, "upt_a")  # 幂等（重试/恢复复用）
    assert out2["holder_id"] == "upt_a"
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 100


def test_bind_other_holder_refused():
    uid = _mkuser("bind3")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _bind(rid, "upt_a")
    with pytest.raises(upload_guard.ReservationHolderMismatch):
        _bind(rid, "upt_b")
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 100


def test_bind_released_refused():
    uid = _mkuser("bind4")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    upload_guard.release_reservation(rid)  # 未绑定 → 直接释放
    with pytest.raises(upload_guard.ReservationInvalid):
        _bind(rid, "upt_a")


def test_bad_holder_combos_rejected():
    uid = _mkuser("bind5")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    with pytest.raises(ValueError):  # 未知种类
        _bind(rid, "t1", kind="workflow", purpose="upload")
    with pytest.raises(ValueError):  # 种类×用途不匹配
        _bind(rid, "t2", kind="upload_task", purpose="ingest_local")
    row = _uid_row(rid)
    assert row[1] is None  # 未被部分绑定污染


def test_one_open_duty_per_holder_unique():
    uid = _mkuser("uniq")
    upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_u1",
        purpose="upload")
    with pytest.raises(psycopg.errors.UniqueViolation):
        upload_guard.reserve_upload(
            uid, 100, holder_kind="upload_task", holder_id="upt_u1",
            purpose="upload")
    # 不同用途可并存（通道显式区分用途的合法多份预算）
    out2 = upload_guard.reserve_upload(
        uid, 100, holder_kind="ingestion_job", holder_id="inj_u1",
        purpose="ingest_local")
    assert out2["state"] == "reserved"


# --------------------------------------------------------------------------- #
# TTL 回收只针对未绑定（plan §2 入口 1 / §D 拆除）
# --------------------------------------------------------------------------- #
def test_reclaim_skips_bound_expired_reclaims_unbound_expired():
    uid = _mkuser("reclaim")
    bound = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_r1",
        purpose="upload")["reservation_id"]
    unbound = upload_guard.reserve_upload(uid, 200)["reservation_id"]
    _expire(bound)
    _expire(unbound)
    upload_guard.reserve_upload(uid, 50)  # 触发惰性回收
    assert _uid_row(bound)[0] == "reserved"     # 绑定责任不被回收
    assert _uid_row(unbound)[0] == "released"   # 准备期未绑定照旧回收
    q = upload_guard.get_quota_row(uid)
    assert q["reserved_bytes"] == 150  # 100（绑定）+ 50（新）


# --------------------------------------------------------------------------- #
# 执行租约：过期重发（绑定）vs 不复活（未绑定）
# --------------------------------------------------------------------------- #
def test_renew_releases_bound_expired_and_keeps_unbound_semantics():
    uid = _mkuser("renew")
    bound = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_n1",
        purpose="upload")["reservation_id"]
    _expire(bound)
    out = upload_guard.renew_reservation(bound)
    assert out["state"] == "reserved"
    assert upload_guard.reservation_is_active(out)  # 租约重发
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 100

    unbound = upload_guard.reserve_upload(uid, 50)["reservation_id"]
    _expire(unbound)
    out2 = upload_guard.renew_reservation(unbound)
    assert out2["state"] == "reserved"
    assert not upload_guard.reservation_is_active(out2)  # 不复活


def test_capacity_vs_permit_split():
    uid = _mkuser("split")
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_s1",
        purpose="upload")["reservation_id"]
    _expire(rid)
    out = upload_guard.get_reservation(rid)
    assert upload_guard.reservation_holds_capacity(out)   # 容量仍在
    assert not upload_guard.reservation_is_active(out)    # 执行许可失效
    assert upload_guard.reservation_holder_matches(out, "upload_task", "upt_s1")
    assert not upload_guard.reservation_holder_matches(out, "upload_task", "x")


# --------------------------------------------------------------------------- #
# 释放 / 转实占的持有者语境（plan §4.2）
# --------------------------------------------------------------------------- #
def test_release_bound_requires_holder_context():
    uid = _mkuser("rel1")
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_c1",
        purpose="upload")["reservation_id"]
    with pytest.raises(upload_guard.ReservationHolderMismatch):
        upload_guard.release_reservation(rid)
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 100
    with pytest.raises(upload_guard.ReservationHolderMismatch):
        upload_guard.release_reservation(
            rid, expect_holder=("upload_task", "upt_other"))
    # 正确持有者（清理确认收口语义）→ 释放
    out = upload_guard.release_reservation(
        rid, expect_holder=("upload_task", "upt_c1"))
    assert out["state"] == "released"
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 0


def test_release_unbound_without_context_still_works():
    uid = _mkuser("rel2")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    assert upload_guard.release_reservation(rid)["state"] == "released"


def test_consume_bound_ignores_lease_expiry_and_verifies_holder():
    uid = _mkuser("con1")
    rid = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_k1",
        purpose="upload")["reservation_id"]
    _expire(rid)
    with pytest.raises(upload_guard.ReservationHolderMismatch):
        upload_guard.consume_reservation(rid, 100)  # 无语境
    with pytest.raises(upload_guard.ReservationHolderMismatch):
        upload_guard.consume_reservation(
            rid, 100, expect_holder=("upload_task", "upt_other"))
    out = upload_guard.consume_reservation(
        rid, 90, expect_holder=("upload_task", "upt_k1"))  # 租约过期仍可结算
    assert out["state"] == "consumed"
    q = upload_guard.get_quota_row(uid)
    assert q["used_bytes"] == 90 and q["reserved_bytes"] == 0
    # 幂等：重复 consume 不双扣
    out2 = upload_guard.consume_reservation(
        rid, 90, expect_holder=("upload_task", "upt_k1"))
    assert out2["state"] == "consumed"
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 90


def test_consume_unbound_expired_still_refused():
    uid = _mkuser("con2")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _expire(rid)
    with pytest.raises(upload_guard.ReservationInvalid):
        upload_guard.consume_reservation(rid, 100)


# --------------------------------------------------------------------------- #
# 在途 / 每小时计数口径（plan §4.2）
# --------------------------------------------------------------------------- #
def test_inflight_counts_bound_regardless_of_lease_and_exempts_pending():
    uid = _mkuser("inflight")
    # 绑定且租约过期：仍占执行槽（容量也在账上）
    upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_i1",
        purpose="upload")
    rid2 = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_i2",
        purpose="upload")
    _expire(rid2["reservation_id"])
    # 第三份（pending 引用）不占槽但占容量
    rid3 = upload_guard.reserve_upload(
        uid, 100, holder_kind="upload_task", holder_id="upt_i3",
        purpose="upload")["reservation_id"]
    import upload_task_store
    upload_task_store.record_cleanup_pending("upt_i3", rid3, error="IO")
    # 上限 3：占槽的 2 份（i1、i2）+ 新预约 → 恰好第 3 份可准入
    out = upload_guard.reserve_upload(uid, 10, inflight_limit=3)
    assert out["state"] == "reserved"
    # 第 4 份被在途上限拒绝（占槽 3 份：i1、i2、新——i3 pending 豁免）
    with pytest.raises(upload_guard.InflightLimitExceeded):
        upload_guard.reserve_upload(uid, 10, inflight_limit=3)


def test_hourly_count_ignores_reconcile_origin():
    uid = _mkuser("hourly")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute(
            "INSERT INTO upload_reservations "
            "(reservation_id, user_id, reserved_bytes, state, reserved_at, "
            " expires_at, origin) VALUES "
            "('upr_rec1', %s, 100, 'released', "
            " now(), now() + interval '1 hour', 'reconcile')", (uid,))
    # reconcile 行不计每小时 → 上限 1 下仍可正常准入一次
    out = upload_guard.reserve_upload(uid, 50, hourly_limit=1)
    assert out["state"] == "reserved"
    with pytest.raises(upload_guard.RateLimitExceeded):
        upload_guard.reserve_upload(uid, 50, hourly_limit=1)


# --------------------------------------------------------------------------- #
# used_bytes 财务 SQL 单实现（plan §8：结算只有一份实现）
# --------------------------------------------------------------------------- #
def test_add_used_bytes_locked_primitive():
    uid = _mkuser("used")
    upload_guard.get_quota_row(uid)  # 惰性建行（配额行由 reserve/get 建）
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                assert upload_guard.add_used_bytes_locked(cur, uid, 42)
                assert not upload_guard.add_used_bytes_locked(cur, uid, 0)
                assert not upload_guard.add_used_bytes_locked(cur, "", 42)
    finally:
        conn.close()
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 42
