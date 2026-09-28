# -*- coding: utf-8 -*-
"""R12 §4 核账工具合同测试（scripts/reconcile_upload_capacity.py）。

合同要点：默认 dry-run 只读；应用必须走「--plan-out 冻结 → --apply
--plan」；全量预检（计划自洽/DB 前态/文件证据）先过后用、单事务应用 +
action_key 回执幂等；--reattach 显式报错（exit 2）；核账只补责任不恢复
执行；超额如实补记且新准入仍被拒；mismatch/dangling/未知目录/证据漂移
→ no-go（exit 3）。
"""
import json  # noqa: F401
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import importlib.util

import psycopg  # noqa: E402
import pytest  # noqa: E402

import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_upload_dir():
    """共享 UPLOAD_DIR：每用例前清空暂存/锁目录（用例间无残留串扰）。"""
    import shutil
    for sub in (".staging", ".task-locks"):
        base = os.path.join(str(UPLOAD_DIR), sub)
        if os.path.isdir(base):
            shutil.rmtree(base, ignore_errors=True)
    yield

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


def _freeze(tag, upload_dir=None, extra=()):
    """维护窗口两步之一：--plan-out 冻结（返回 (rc, plan_path)）。"""
    out = "/tmp/r12-plan-%s.json" % tag
    rc = recon.main(["--database-url", PG_URI,
                     "--upload-dir", upload_dir or UPLOAD_DIR,
                     "--plan-out", out] + list(extra))
    return rc, out


def _apply(plan_path, upload_dir=None, extra=()):
    return recon.main(["--database-url", PG_URI,
                       "--upload-dir", upload_dir or UPLOAD_DIR,
                       "--apply", "--plan", plan_path] + list(extra))


def test_reattach_flag_reports_error():
    assert recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR,
                       "--reattach"]) == 2


def test_apply_requires_frozen_plan():
    assert recon.main(["--database-url", PG_URI, "--upload-dir", UPLOAD_DIR,
                       "--apply"]) == 2


def test_dry_run_does_not_touch_data():
    uid = _uid("dry")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _task(uid, "upt_rec_dry", rid)
    assert recon.main(["--database-url", PG_URI,
                       "--upload-dir", UPLOAD_DIR]) in (0, 3)
    with psycopg.connect(PG_URI) as db:
        row = db.execute("SELECT holder_kind FROM upload_reservations "
                         "WHERE reservation_id=%s", (rid,)).fetchone()
    assert row[0] is None  # 未绑定（只读）
    assert _q(uid) == (0, 100)


def test_freeze_then_apply_binds_and_stops_idempotently():
    uid = _uid("apply")
    rid_ok = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    _task(uid, "upt_rec_ok", rid_ok)
    rid_bad = upload_guard.reserve_upload(uid, 80)["reservation_id"]
    _task(uid, "upt_rec_bad", rid_bad)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET state='released', "
                   "settled_at=now(), settled_bytes=0 WHERE "
                   "reservation_id=%s", (rid_bad,))
        db.execute("UPDATE upload_user_quotas SET reserved_bytes=100 "
                   "WHERE user_id=%s", (uid,))
    rc, plan = _freeze("apply")
    assert rc == 0, open(plan).read()
    assert _apply(plan) == 0
    with psycopg.connect(PG_URI) as db:
        bound = db.execute("SELECT holder_kind, holder_id FROM "
                           "upload_reservations WHERE reservation_id=%s",
                           (rid_ok,)).fetchone()
        task_bad = db.execute("SELECT state FROM upload_tasks WHERE "
                              "upload_id='upt_rec_bad'").fetchone()[0]
        receipts = db.execute("SELECT COUNT(*) FROM "
                              "upload_capacity_repair_receipts"
                              ).fetchone()[0]
    assert bound == ("upload_task", "upt_rec_ok")
    assert task_bad == "failed"
    # 同计划重跑：回执幂等，全部跳过，不重复收费
    rc2 = _apply(plan)
    assert rc2 == 0
    with psycopg.connect(PG_URI) as db:
        receipts2 = db.execute("SELECT COUNT(*) FROM "
                               "upload_capacity_repair_receipts"
                               ).fetchone()[0]
    assert int(receipts2) == int(receipts)
    assert _q(uid) == (0, 100)


def test_repair_residuals_stops_and_records_without_resurrecting():
    """R12-2 新合同：只补责任、任务保持停止、随后清理恰一次释放。"""
    uid = _uid("repair")
    _task(uid, "upt_rec_rep", None)  # 预约缺失 + 残留
    data = slide_storage.staging_dir("upt_rec_rep", "transfer",
                                     root=UPLOAD_DIR) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 120)
    rc, plan = _freeze("repair", extra=["--repair-residuals"])
    assert rc == 0, open(plan).read()
    assert _apply(plan, extra=["--repair-residuals"]) == 0
    with psycopg.connect(PG_URI) as db:
        state, rid = db.execute(
            "SELECT state, reservation_id FROM upload_tasks WHERE "
            "upload_id='upt_rec_rep'").fetchone()
        origin, hk = db.execute(
            "SELECT origin, holder_kind FROM upload_reservations WHERE "
            "reservation_id=%s", (rid,)).fetchone()
        pending = db.execute(
            "SELECT reservation_id FROM upload_cleanup_pending WHERE "
            "upload_id='upt_rec_rep'").fetchone()
    assert state == "failed"          # 不恢复执行（R12 §3.4）
    assert hk == "upload_task"
    assert origin == "reconcile"
    assert pending == (rid,)          # pending 指向新责任（不指旧 released）
    assert _q(uid) == (0, 120)
    # 随后清理恰一次释放（清理确认收口）
    import upload_task_store
    slide_storage.remove_staging_tree("upt_rec_rep", root=UPLOAD_DIR)
    upload_task_store.confirm_cleanup_and_release("upt_rec_rep")
    assert _q(uid) == (0, 0)
    assert upload_guard.get_reservation(rid)["state"] == "released"


def test_terminal_pending_legacy_stock_bound_not_reclaimed():
    """R12-3：终态 pending + 过期未绑定预约——补绑定后 TTL 不再回收。"""
    uid = _uid("term")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    tid = "upt_rec_term"
    _task(uid, tid, rid, state="failed")
    data = slide_storage.staging_dir(tid, "transfer",
                                     root=UPLOAD_DIR) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 100)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("INSERT INTO upload_cleanup_pending(upload_id, "
                   "reservation_id) VALUES(%s,%s)", (tid, rid))
        db.execute("UPDATE upload_reservations SET expires_at="
                   "now()-interval '1 second' WHERE reservation_id=%s",
                   (rid,))
    rc, plan = _freeze("term")
    assert rc == 0, open(plan).read()
    assert _apply(plan) == 0
    upload_guard.reserve_upload(uid, 50)  # 新准入触发惰性回收
    assert upload_guard.get_reservation(rid)["state"] == "reserved"
    assert _q(uid) == (0, 150)
    # 清理确认后才降为 50
    slide_storage.remove_staging_tree(tid, root=UPLOAD_DIR)
    import upload_task_store
    upload_task_store.confirm_cleanup_and_release(tid)
    assert _q(uid) == (0, 50)


def test_reconcile_existing_bytes_can_exceed_quota():
    """R12-4：额度 50、残留 100——维护补记如实成功；新上传仍被拒。"""
    uid = _uid("over")
    tid = "upt_rec_over"
    _task(uid, tid, None)
    upload_guard.get_quota_row(uid)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_user_quotas SET quota_bytes=50 "
                   "WHERE user_id=%s", (uid,))
    data = slide_storage.staging_dir(tid, "transfer",
                                     root=UPLOAD_DIR) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 100)
    rc, plan = _freeze("over", extra=["--repair-residuals"])
    assert rc == 0, open(plan).read()
    assert _apply(plan, extra=["--repair-residuals"]) == 0
    assert _q(uid) == (0, 100)  # 超额如实补记，额度不变
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(uid, 10)  # 新上传仍被拒


def test_file_evidence_drift_blocks_whole_apply():
    uid = _uid("drift")
    tid = "upt_rec_drift"
    _task(uid, tid, None)
    data = slide_storage.staging_dir(tid, "transfer",
                                     root=UPLOAD_DIR) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 50)
    rc, plan = _freeze("drift", extra=["--repair-residuals"])
    assert rc == 0
    data.write_bytes(b"x" * 60)  # 冻结后文件漂移
    assert _apply(plan, extra=["--repair-residuals"]) == 3
    # 整体未应用（无 stop/repair 落库）
    with psycopg.connect(PG_URI) as db:
        state = db.execute("SELECT state FROM upload_tasks WHERE "
                           "upload_id=%s", (tid,)).fetchone()[0]
        n = db.execute("SELECT COUNT(*) FROM "
                       "upload_capacity_repair_receipts").fetchone()[0]
    assert state == "active"
    assert int(n) == 0


def test_cleanup_then_rerun_old_plan_does_not_recreate_duty():
    """清理完成后重跑旧计划：回执在 → 跳过，不重新制造责任。"""
    uid = _uid("rerun")
    rid = upload_guard.reserve_upload(uid, 100)["reservation_id"]
    tid = "upt_rec_rerun"
    _task(uid, tid, rid)
    rc, plan = _freeze("rerun")
    assert rc == 0
    assert _apply(plan) == 0
    # 模拟任务后续终态 + 清理确认（责任释放）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_tasks SET state='cancelled' WHERE "
                   "upload_id=%s", (tid,))
    import upload_task_store
    upload_task_store.confirm_cleanup_and_release(tid)
    assert _q(uid) == (0, 0)
    rc2 = _apply(plan)  # 旧计划重跑
    assert rc2 == 0
    assert _q(uid) == (0, 0)  # 未重新绑定/补记


def test_mismatch_owner_blocks_and_apply_does_not_touch():
    uid_a = _uid("owna")
    uid_b = _uid("ownb")
    rid = upload_guard.reserve_upload(uid_b, 100)["reservation_id"]
    _task(uid_a, "upt_rec_mm", rid)
    assert recon.main(["--database-url", PG_URI,
                       "--upload-dir", UPLOAD_DIR]) == 3
    rc, plan = _freeze("mm")
    assert rc == 3  # 冻结阶段就 no-go（阻断）
    with psycopg.connect(PG_URI) as db:
        state = db.execute("SELECT state FROM upload_tasks WHERE "
                           "upload_id='upt_rec_mm'").fetchone()[0]
    assert state == "active"  # 阻断人工核对，未擅动


def test_dangling_reservation_and_unknown_dir_are_no_go():
    uid = _uid("dang")
    upload_guard.reserve_upload(uid, 70)  # 无任务引用 → dangling
    assert recon.main(["--database-url", PG_URI,
                       "--upload-dir", UPLOAD_DIR]) == 3
    sroot = os.path.join(str(UPLOAD_DIR), ".staging")
    os.makedirs(os.path.join(sroot, "upt_unknown"), exist_ok=True)
    with open(os.path.join(sroot, "upt_unknown", "x"), "wb") as fh:
        fh.write(b"z")
    assert recon.main(["--database-url", PG_URI,
                       "--upload-dir", UPLOAD_DIR]) == 3


def test_hardlink_counted_once(tmp_path):
    a = tmp_path / ".staging" / "a" / "transfer"
    a.mkdir(parents=True)
    f1 = a / "data.svs"
    f1.write_bytes(b"y" * 30)
    b = tmp_path / ".staging" / "b" / "transfer"
    b.mkdir(parents=True)
    os.link(f1, b / "data.svs")  # 同 inode 两任务
    n1, t1 = recon.scan_task_tree(str(tmp_path), "a")
    n2, t2 = recon.scan_task_tree(str(tmp_path), "b")
    assert (n1, t1) == (1, 30) and (n2, t2) == (1, 30)
    os.link(f1, a / "data2.svs")  # 单任务内硬链接也只计一次（物理 inode）
    n3, t3 = recon.scan_task_tree(str(tmp_path), "a")
    assert (n3, t3) == (1, 30)


def test_manifest_catches_rename_same_size(tmp_path):
    """R13-3：同大小改名（内容不变）——逐成员路径清单可检出，总数/总字节
    比对不可检出。"""
    uid = _uid("rename")
    tid = "upt_rec_rename"
    _task(uid, tid, None)
    data = slide_storage.staging_dir(tid, "transfer", root=tmp_path) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 40)
    rc, plan = _freeze("rename", upload_dir=str(tmp_path),
                       extra=["--repair-residuals"])
    assert rc == 0
    os.replace(data, data.with_name("renamed.svs"))  # 同内容同大小改名
    assert _apply(plan, upload_dir=str(tmp_path),
                  extra=["--repair-residuals"]) == 3
    with psycopg.connect(PG_URI) as db:
        n = db.execute("SELECT COUNT(*) FROM "
                       "upload_capacity_repair_receipts").fetchone()[0]
    assert int(n) == 0


def test_unreadable_directory_blocks_apply(tmp_path):
    """R13-3：目录枚举失败（chmod 000）= 证据不完整 → no-go，不是 0。"""
    uid = _uid("unread")
    tid = "upt_rec_unread"
    _task(uid, tid, None)
    data = slide_storage.staging_dir(tid, "transfer", root=tmp_path) / "data.svs"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"x" * 40)
    rc, plan = _freeze("unread", upload_dir=str(tmp_path),
                       extra=["--repair-residuals"])
    assert rc == 0
    os.chmod(data.parent, 0o000)
    try:
        assert _apply(plan, upload_dir=str(tmp_path),
                      extra=["--repair-residuals"]) == 3
    finally:
        os.chmod(data.parent, 0o755)
    with psycopg.connect(PG_URI) as db:
        n = db.execute("SELECT COUNT(*) FROM "
                       "upload_capacity_repair_receipts").fetchone()[0]
    assert int(n) == 0


def test_directory_symlink_in_tree_blocks_apply(tmp_path):
    """R13-3：树内目录符号链接（越过即漏扫描）→ 显式拒绝。"""
    uid = _uid("dsym")
    tid = "upt_rec_dsym"
    _task(uid, tid, None)
    tdir = slide_storage.staging_dir(tid, "transfer", root=tmp_path)
    (tdir / "extra").mkdir(parents=True, exist_ok=True)
    (tdir / "extra" / "part.bin").write_bytes(b"z" * 10)
    rc, plan = _freeze("dsym", upload_dir=str(tmp_path),
                       extra=["--repair-residuals"])
    assert rc == 0
    (tdir / "extra" / "part.bin").unlink()
    (tdir / "extra").rmdir()
    os.symlink(str(tmp_path), str(tdir / "extra"))
    assert _apply(plan, upload_dir=str(tmp_path),
                  extra=["--repair-residuals"]) == 3
    with psycopg.connect(PG_URI) as db:
        n = db.execute("SELECT COUNT(*) FROM "
                       "upload_capacity_repair_receipts").fetchone()[0]
    assert int(n) == 0
