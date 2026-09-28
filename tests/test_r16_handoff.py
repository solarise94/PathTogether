# -*- coding: utf-8 -*-
"""R16 修复的补充验收（审查反例之外的两种胜者、交接落点崩溃、清理失败、
扫描失败）。

不变量：ZIP/KFB 在 validating 内产生的副作用（item 资产、held 转换子
任务）都在父任务行锁内登记，父任务终态事务同步作废；源文件在 intent
之后才交给子任务；ZIP 写入第一个字节前已补占到峰值，暂存清理确认后
才结算。
"""
import os

import pytest

import test_cos_ingestion_kinds as h
from test_cos_ingestion_kinds import _env, _fake_probe  # noqa: F401  autouse
import conversion_store
import conversion_worker
import cos_ingest_worker as ciw
import ingestion_store as ist
import slide_io
import slide_storage
import slide_store
import upload_guard
from test_upload_drain import _drain_tool, _mk_task


# --------------------------------------------------------------------------- #
# KFB 交接
# --------------------------------------------------------------------------- #
def test_held_child_is_not_claimable_before_settle(monkeypatch, _fake_probe):
    fake, st = h.FakeCos(), {}
    job = h._drive_kfb(fake, st, owner="r16_hold")
    jid = job["job_id"]

    def settle_rejected(*a, **kw):
        raise ist.IngestionStateError("注入：结算事务被拒")

    monkeypatch.setattr(ist, "worker_settle_source", settle_rejected)
    assert ciw.process_validating(cos=fake, state=st) is None
    child = conversion_store.get_job_by_upload_id(jid)
    assert child["state"] == "held"
    assert conversion_store.claim_one("cvw_test") is None


def test_crash_after_source_move_recovers_once(monkeypatch, tmp_path,
                                               _fake_probe):
    fake, st = h.FakeCos(), {}
    job = h._drive_kfb(fake, st, owner="r16_move", size=240)
    jid = job["job_id"]
    real = ist.worker_settle_source

    def settle_rejected(*a, **kw):
        raise ist.IngestionStateError("注入：搬源后、结算前中断")

    monkeypatch.setattr(ist, "worker_settle_source", settle_rejected)
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(jid)
    assert out["state"] == ist.VALIDATING and out["commit_intent_json"]
    child = conversion_store.get_job_by_upload_id(jid)
    src_dir = conversion_worker.source_staging_dir(child["id"], str(tmp_path))
    assert any(src_dir.iterdir())  # 源已交给子任务

    monkeypatch.setattr(ist, "worker_settle_source", real)
    assert ciw.process_validating(cos=fake, state=st) == jid
    out = ist.get_job(jid)
    assert out["state"] == ist.READY
    assert out["conversion_job_id"] == child["id"]
    assert conversion_store.get_job(child["id"])["state"] == "queued"
    assert h._quota("r16_move") == (0, 240)  # 恰一次源字节结算


def test_cancel_after_intent_is_refused_and_child_runs(monkeypatch,
                                                      _fake_probe):
    fake, st = h.FakeCos(), {}
    job = h._drive_kfb(fake, st, owner="r16_commit")
    jid = job["job_id"]
    real = ist.worker_persist_commit_intent
    outcome = {}

    def persist_then_cancel(*a, **kw):
        out = real(*a, **kw)
        with pytest.raises(ist.CommitInProgress):
            ist._terminate_cancel_tx(jid)
        outcome["refused"] = True
        return out

    monkeypatch.setattr(ist, "worker_persist_commit_intent",
                        persist_then_cancel)
    assert ciw.process_validating(cos=fake, state=st) == jid
    assert outcome.get("refused")
    child = conversion_store.get_job_by_upload_id(jid)
    assert child["state"] == "queued"


def test_reused_existing_child_is_not_voided(monkeypatch, _fake_probe):
    """同 owner+sha 已有转换任务（历史上传/百度导入创建）时本次上传复用
    它；本次上传在 intent 前被取消不得作废该任务（只作废本任务创建的
    held）。"""
    import hashlib
    h._mk_user("r16_share")
    existing = conversion_store.create_job(
        owner_user_id="r16_share", upload_id="upt_earlier",
        source_name="src.kfb",
        source_sha256=hashlib.sha256(h._payload(300)).hexdigest(),
        source_format="kfb_kfbio_jpeg", canonical_name="src.tif")
    assert existing["state"] == "queued"
    fake, st = h.FakeCos(), {}
    job = h._drive_kfb(fake, st, owner="r16_share", size=300)
    jid = job["job_id"]
    real = ist.worker_persist_commit_intent

    def cancel_then_persist(*a, **kw):
        ist._terminate_cancel_tx(jid)
        return real(*a, **kw)

    monkeypatch.setattr(ist, "worker_persist_commit_intent",
                        cancel_then_persist)
    assert ciw.process_validating(cos=fake, state=st) is None
    assert ist.get_job(jid)["state"] == ist.CANCELLED
    assert conversion_store.get_job(existing["id"])["state"] == "queued"


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #
def test_zip_cancel_after_binding_fails_bound_assets(monkeypatch):
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    payload, _ = h._zip_bytes({"a.tif": h._payload(200),
                               "b.tif": h._payload(300)})
    fake, st = h.FakeCos(), {}
    job = h._drive_to_validating(fake, st, payload, owner="r16_zc")
    jid = job["job_id"]
    real = ist.worker_persist_commit_intent

    def cancel_then_persist(*a, **kw):
        ist._terminate_cancel_tx(jid)
        return real(*a, **kw)

    monkeypatch.setattr(ist, "worker_persist_commit_intent",
                        cancel_then_persist)
    assert ciw.process_validating(cos=fake, state=st) is None
    ist._local_cleanup_finish(jid)
    assert ist.get_job(jid)["state"] == ist.CANCELLED
    items = ist.list_ingestion_job_items(jid)
    assert len(items) == 2
    assert all(h._slide_state(r["slide_id"])[0]
               == slide_store.SlideState.FAILED for r in items)


def test_zip_quota_rejected_before_any_byte_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    payload, _ = h._zip_bytes({"a.tif": h._payload(3000) + b"\x00" * 1000})
    h._mk_user("r16_zq", quota=len(payload))
    fake, st = h.FakeCos(), {}
    job = h._drive_to_validating(fake, st, payload, owner="r16_zq",
                                 role="user")
    jid = job["job_id"]
    seen = []
    real = upload_guard.topup_reservation

    def observe(rid, extra, *a, **kw):
        extract = slide_storage.staging_dir(jid, "extract", root=tmp_path)
        seen.append([p for p in extract.rglob("*") if p.is_file()])
        return real(rid, extra, *a, **kw)

    monkeypatch.setattr(upload_guard, "topup_reservation", observe)
    assert ciw.process_validating(cos=fake, state=st) is None
    assert ist.get_job(jid)["fail_code"] == "zip_quota_exceeded"
    assert seen == [[]]
    assert h._quota("r16_zq") == (0, 0)


def test_zip_cleanup_failure_defers_settlement(monkeypatch):
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    payload, _ = h._zip_bytes({"a.tif": h._payload(400)})
    fake, st = h.FakeCos(), {}
    job = h._drive_to_validating(fake, st, payload, owner="r16_zcf",
                                 role="user")
    jid = job["job_id"]
    real = slide_storage.remove_staging_tree

    def fail_once(task_id, *a, **kw):
        if task_id == jid:
            raise OSError("注入：暂存清理失败")
        return real(task_id, *a, **kw)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", fail_once)
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(jid)
    assert out["state"] == ist.VALIDATING
    rid = out["local_reservation_id"]
    assert upload_guard.get_reservation(rid)["state"] == "reserved"

    monkeypatch.setattr(slide_storage, "remove_staging_tree", real)
    assert ciw.process_validating(cos=fake, state=st) == jid
    out = ist.get_job(jid)
    assert out["state"] == ist.READY
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert h._quota("r16_zcf") == (0, 400)


# --------------------------------------------------------------------------- #
# 排空核验
# --------------------------------------------------------------------------- #
def test_drain_empty_terminal_dir_passes(tmp_path):
    task = _mk_task("r16_empty.tif")
    import upload_task_store
    upload_task_store.cancel_task(task["upload_id"])
    slide_storage.staging_dir(task["upload_id"], "transfer",
                              root=tmp_path).mkdir(parents=True)
    assert _drain_tool().main(["report", "--upload-dir", str(tmp_path)]) == 0


def test_drain_scan_failure_is_no_go(tmp_path):
    task = _mk_task("r16_link.tif")
    import upload_task_store
    upload_task_store.cancel_task(task["upload_id"])
    d = slide_storage.staging_dir(task["upload_id"], "transfer", root=tmp_path)
    d.mkdir(parents=True)
    (tmp_path / "outside").write_bytes(b"x")
    os.symlink(tmp_path / "outside", d / "data.tif")
    assert _drain_tool().main(["report", "--upload-dir", str(tmp_path)]) == 3


def test_drain_conversion_source_is_foreign_not_blocking(tmp_path):
    h._mk_user("r16_foreign")
    job = conversion_store.create_job(
        owner_user_id="r16_foreign", upload_id="inj_x", source_name="s.kfb",
        source_sha256="ab" * 32, source_format="kfb_kfbio_jpeg",
        canonical_name="s.tif")
    src = conversion_worker.source_staging_dir(job["id"], str(tmp_path))
    src.mkdir(parents=True)
    (src / "data.kfb").write_bytes(b"k" * 10)
    assert _drain_tool().main(["report", "--upload-dir", str(tmp_path)]) == 0
