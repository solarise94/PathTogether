# -*- coding: utf-8 -*-
"""0072 §6-E 存量核账工具测试（scripts/reconcile_upload_capacity.py）。

覆盖：只读默认不动数据；apply 绑定一致项；异常项终止进清理编排；
--reattach 按审计字节补建 origin='reconcile' 预约并原子绑定；幂等重跑；
超额只报告不改额度；mismatch 阻断（非零退出）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import importlib.util

import psycopg  # noqa: E402
import pytest  # noqa: E402

import upload_guard  # noqa: E402
import user_store  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
_SPEC = importlib.util.spec_from_file_location(
    "reconcile_upload_capacity",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "reconcile_upload_capacity.py"))
recon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recon)


def _uid(tag):
    return user_store.create_user(
        "rec-%s@example.com" % tag, "pass1234pass1234", role="user"
    )["user_id"]


def _task(uid, task_id, rid, state="active"):
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute(
            "INSERT INTO upload_tasks (upload_id, owner_user_id, filename,"
            " safe_name, declared_size, chunk_size, confirmed_offset, state,"
            " expires_at, created_at, updated_at, reservation_id)"
            " VALUES (%s,%s,'a.svs','a.svs',100,0,100,%s,"
            " now()+interval '1 hour', now(), now(), %s)",
            (task_id, uid, state, rid))


def _q(uid):
    with psycopg.connect(PG_URI) as db:
        r = db.execute("SELECT used_bytes, reserved_bytes FROM "
                       "upload_user_quotas WHERE user_id=%s",
                       (uid,)).fetchone()
    return int(r[0]), int(r[1])


def test_dry_run_does_not_touch_data(capsys):
    uid = _uid("dry")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _task(uid, "upt_rec_dry", rid)
    rc = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR])
    assert rc in (0, 3)
    with psycopg.connect(PG_URI) as db:
        row = db.execute("SELECT holder_kind FROM upload_reservations "
                         "WHERE reservation_id=%s", (rid,)).fetchone()
    assert row[0] is None  # 未绑定（只读）
    assert _q(uid) == (0, 100)


def test_apply_binds_consistent_and_terminates_anomalies():
    uid = _uid("apply")
    rid_ok = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _task(uid, "upt_rec_ok", rid_ok)
    rid_bad = upload_guard.reserve_upload(uid, 80)["reservation_id"]
    _task(uid, "upt_rec_bad", rid_bad)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET state='released', "
                   "settled_at=now(), settled_bytes=0 WHERE reservation_id=%s",
                   (rid_bad,))
        db.execute("UPDATE upload_user_quotas SET reserved_bytes=100 "
                   "WHERE user_id=%s", (uid,))
    rc = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR,
                     "--apply"])
    assert rc == 0
    with psycopg.connect(PG_URI) as db:
        bound = db.execute("SELECT holder_kind, holder_id FROM "
                           "upload_reservations WHERE reservation_id=%s",
                           (rid_ok,)).fetchone()
        task_bad = db.execute("SELECT state FROM upload_tasks WHERE "
                              "upload_id='upt_rec_bad'").fetchone()[0]
        pending = db.execute("SELECT reservation_id FROM "
                             "upload_cleanup_pending WHERE "
                             "upload_id='upt_rec_bad'").fetchone()
    assert bound == ("upload_task", "upt_rec_ok")
    assert task_bad == "failed"
    assert pending == (rid_bad,)
    # 幂等：重跑零动作（ok 项跳过；failed 任务不在活跃集）
    rc2 = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR,
                      "--apply"])
    assert rc2 == 0
    assert _q(uid) == (0, 100)


def test_reattach_creates_reconcile_reservation(tmp_path):
    uid = _uid("reat")
    _task(uid, "upt_rec_reat", None)  # 预约缺失
    # 暂存树实际字节（审计输入）
    staging = tmp_path / "staging" / "upt_rec_reat" / "transfer"
    staging.mkdir(parents=True)
    (staging / "data").write_bytes(b"z" * 321)
    import slide_storage
    real_dir = slide_storage.staging_task_dir
    slide_storage.staging_task_dir = (
        lambda task_id, root=None: tmp_path / "staging" / task_id)
    try:
        rc = recon.main(["--database-url", PG_URI, "--upload-dir",
                         str(tmp_path), "--apply", "--reattach"])
    finally:
        slide_storage.staging_task_dir = real_dir
    assert rc == 0
    with psycopg.connect(PG_URI) as db:
        rid, origin, hk, hid = db.execute(
            "SELECT reservation_id, origin, holder_kind, holder_id FROM "
            "upload_reservations WHERE user_id=%s", (uid,)).fetchone()
        state = db.execute("SELECT state FROM upload_tasks WHERE "
                           "upload_id='upt_rec_reat'").fetchone()[0]
    assert state == "active"
    assert (hk, hid) == ("upload_task", "upt_rec_reat")
    assert origin == "reconcile"
    assert _q(uid) == (0, 321)
    # 核账预约不计每小时准入数（origin 过滤）
    out = upload_guard.reserve_upload(uid, 10, hourly_limit=1)
    assert out["state"] == "reserved"


def test_over_quota_reported_not_capped(capsys):
    uid = _uid("over")
    upload_guard.reserve_upload(uid, 100)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_user_quotas SET quota_bytes=50 "
                   "WHERE user_id=%s", (uid,))
    rc = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR])
    assert rc == 0  # 超额不是 mismatch
    assert '"over_quota"' in capsys.readouterr().out  # 如实报告
    with psycopg.connect(PG_URI) as db:
        quota = db.execute("SELECT quota_bytes FROM upload_user_quotas "
                           "WHERE user_id=%s", (uid,)).fetchone()[0]
    assert int(quota) == 50  # 不调高额度掩盖差额


def test_mismatch_owner_blocks_with_nonzero_rc():
    uid_a = _uid("owna")
    uid_b = _uid("ownb")
    rid = upload_guard.reserve_upload(uid_b, 100)["reservation_id"]
    _task(uid_a, "upt_rec_mm", rid)  # A 的任务指向 B 的预约
    rc = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR])
    assert rc == 3
    # apply 也不自动修正（不转移归属）
    rc2 = recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR,
                      "--apply"])
    assert rc2 == 3
    with psycopg.connect(PG_URI) as db:
        state = db.execute("SELECT state FROM upload_tasks WHERE "
                           "upload_id='upt_rec_mm'").fetchone()[0]
    assert state == "active"  # 阻断人工核对，未擅动
