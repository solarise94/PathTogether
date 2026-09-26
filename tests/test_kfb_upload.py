# -*- coding: utf-8 -*-
"""KFB 上传 → 202 转换任务 → worker 产出 canonical TIFF。"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import conversion_store  # noqa: E402
import conversion_worker  # noqa: E402
import kfb.converter as kfb_converter  # noqa: E402
import slide_io  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
from kfb.fixture import build_synthetic_kfb  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    # P4-app：转换链 create_job 即预分配产物资产（staging/id_bundle 行）——
    # 本地免认证态需配置 owner（不允许空 owner 自动认领）。
    import share_store as _ss
    import user_store as _us
    _ss.set_owner_user_id(
        _us.create_user("p3-local-owner@x.com", "localownerpass12345",
                        role="user")["user_id"])
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def _v2_commit_file(c, filename, data):
    r = c.post("/api/uploads", json={
        "filename": filename, "declared_size": len(data)})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["upload_id"]
    sha = hashlib.sha256(data).hexdigest()
    put = c.put("/api/uploads/%s/chunk?offset=0&sha256=%s" % (uid, sha),
                data=data, content_type="application/octet-stream")
    assert put.status_code == 200, put.get_data(as_text=True)
    r = c.post("/api/uploads/%s/commit" % uid)
    return uid, r


def test_v1_kfb_returns_202_and_worker_makes_tif(tmp_path):
    """P4-app 断言换新：产物预分配 slide_id（202 响应即带）；源副本在任务
    staging（不再平铺 UPLOAD_DIR）；worker 经统一发布出 objects/<sid>/ 产物。"""
    src = build_synthetic_kfb(tmp_path / "case.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "case.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "conversion_pending"
    assert body["state"] == "queued"
    assert body["source_name"] == "case.kfb"
    assert body["canonical_name"] == "case.tif"
    job_id = body["conversion_job_id"]
    sid = body["slide_id"]
    assert sid  # create_job 即预分配（ready 前后都在）
    # 源副本归任务 staging（P4-app §3：源不落资产、不平铺）
    job = conversion_store.get_job(job_id)
    assert (conversion_worker.source_staging_dir(
        job_id, app_mod.UPLOAD_DIR) / "data.kfb").is_file()
    assert not (app_mod.UPLOAD_DIR / "case.kfb").exists()
    assert not (app_mod.UPLOAD_DIR / "case.tif").exists()
    listed = {s.get("slide_id") for s in c.get("/api/slides").get_json()}
    assert sid not in listed  # 产物未 ready 不可见

    claimed = conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert claimed == job_id
    job = conversion_store.get_job(job_id)
    assert job["state"] == "ready"
    assert job["slide_id"] == sid
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    assert desc.storage_layout == "id_bundle"
    dest = slide_storage.resolve_descriptor_path(desc, root=app_mod.UPLOAD_DIR)
    assert dest.is_file() and dest.name == "data.tif"
    slide = slide_io.open_slide(str(dest))
    try:
        assert slide.level_count >= 1
    finally:
        slide.close()
    st = c.get("/api/conversions/" + job_id).get_json()
    assert st["state"] == "ready"
    assert st["slide_id"] == sid  # 轮询打开目标按 ID（任务绑定读）
    listed2 = {s.get("slide_id") for s in c.get("/api/slides").get_json()}
    assert sid in listed2


def test_kfbf_garbage_bytes_rejected(tmp_path):
    """.kfbf 已是 convert-required（可上传），但垃圾字节过不了探测。"""
    p = tmp_path / "x.kfbf"
    p.write_bytes(b"not-a-slide")
    c = _client()
    with open(p, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "x.kfbf")},
                   content_type="multipart/form-data")
    assert r.status_code == 400


def test_supported_exts_still_excludes_kfb():
    assert "kfb" not in app_mod.SUPPORTED_EXTS


def test_v2_kfb_commit_returns_202_and_replay_keeps_job(tmp_path):
    src = build_synthetic_kfb(tmp_path / "v2.kfb")
    data = Path(src).read_bytes()
    c = _client()
    r = c.post("/api/uploads", json={
        "filename": "v2.kfb", "declared_size": len(data)})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["upload_id"]
    sha = __import__("hashlib").sha256(data).hexdigest()
    put = c.put("/api/uploads/%s/chunk?offset=0&sha256=%s" % (uid, sha),
                data=data, content_type="application/octet-stream")
    assert put.status_code == 200, put.get_data(as_text=True)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "conversion_pending"
    assert body["conversion_job_id"]
    jid = body["conversion_job_id"]
    r2 = c.post("/api/uploads/%s/commit" % uid)
    assert r2.status_code == 202
    assert r2.get_json()["conversion_job_id"] == jid


def test_worker_does_not_overwrite_existing_tif(tmp_path):
    """P4-app 断言换新（同名产物=独立资产）：目录里已有的同名平铺 TIFF 不再
    构成 name_unavailable——产物进自己的 objects/<sid>/，他人文件不动。"""
    src = build_synthetic_kfb(tmp_path / "ow.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "ow.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    victim = app_mod.UPLOAD_DIR / body["canonical_name"]
    victim.write_bytes(b"KEEP-ME-NOT-A-TIFF")
    jid = body["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert victim.read_bytes() == b"KEEP-ME-NOT-A-TIFF"
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert slide_storage.resolve_descriptor_path(
        desc, root=app_mod.UPLOAD_DIR).is_file()


def test_delete_then_reupload_requeues(tmp_path):
    """删除产物（按 ID）→ 任务作废 + 源清理；同内容重传复用同 job（requeue）
    并改绑**新** slide_id（删除后重传=新资产，P4-app §3.5）。"""
    src = build_synthetic_kfb(tmp_path / "dl.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "dl.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    body = r.get_json()
    jid = body["conversion_job_id"]
    sid_old = body["slide_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_store.get_job(jid)["state"] == "ready"
    assert c.delete("/api/slides/%s" % sid_old).status_code == 200
    # 删除联动：任务按 slide_id 作废；源副本目录清理
    assert conversion_store.get_job(jid)["state"] == "cancelled"
    assert not conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR).exists()
    with open(src, "rb") as f:
        r2 = c.post("/api/upload", data={"file": (f, "dl.kfb")},
                    content_type="multipart/form-data")
    assert r2.status_code == 202, r2.get_data(as_text=True)
    body2 = r2.get_json()
    assert body2["conversion_job_id"] == jid  # 同 id requeue（幂等键）
    assert body2["state"] == "queued"
    assert body2["slide_id"] != sid_old  # 产物已删 → 改绑新 ID
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job2 = conversion_store.get_job(jid)
    assert job2["state"] == "ready"
    assert job2["slide_id"] == body2["slide_id"]
    desc = slide_store.resolve_slide_id(job2["slide_id"])
    assert desc.asset_state == "ready"
    assert slide_storage.resolve_descriptor_path(
        desc, root=app_mod.UPLOAD_DIR).is_file()


def test_v1_same_name_recover_requires_owner_and_digest(tmp_path, monkeypatch):
    """他人同名垃圾上传不得认领已落盘源文件。"""
    import user_store
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    src = build_synthetic_kfb(tmp_path / "own.kfb")
    ca = _client()
    ua = user_store.create_user("a@x.com", "pass1234pass1234", role="user")
    with ca.session_transaction() as s:
        s["auth_user"] = ua.get("login_id") or "a@x.com"
        s["user_id"] = ua["user_id"]
        s["role"] = "user"
        s["auth_version"] = ua.get("auth_version", 1)
    with open(src, "rb") as f:
        r = ca.post("/api/upload", data={"file": (f, "own.kfb")},
                    content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    jid_a = r.get_json()["conversion_job_id"]

    cb = _client()
    ub = user_store.create_user("b@x.com", "pass1234pass1234", role="user")
    with cb.session_transaction() as s:
        s["auth_user"] = ub.get("login_id") or "b@x.com"
        s["user_id"] = ub["user_id"]
        s["role"] = "user"
        s["auth_version"] = ub.get("auth_version", 1)
    junk = build_synthetic_kfb(tmp_path / "own.kfb", width=300, height=280)
    with open(junk, "rb") as f:
        r2 = cb.post("/api/upload", data={"file": (f, "own.kfb")},
                     content_type="multipart/form-data")
    # P4-app 断言换新：同名不同内容不再 409（name_unavailable 族拆除）——
    # B 得到自己的独立任务/独立产物 ID；A 的任务归属不变。
    assert r2.status_code == 202, r2.get_data(as_text=True)
    body_b = r2.get_json()
    assert body_b["conversion_job_id"] != jid_a
    assert body_b["slide_id"] != r.get_json()["slide_id"]
    job = conversion_store.get_job(jid_a)
    assert job["owner_user_id"] == ua["user_id"]
    listed = cb.get("/api/conversions/" + jid_a)
    assert listed.status_code == 404


def test_same_bytes_rename_does_not_rewrite_inflight_paths(tmp_path):
    src_a = build_synthetic_kfb(tmp_path / "a.kfb")
    c = _client()
    with open(src_a, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "a.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["conversion_job_id"]
    with open(src_a, "rb") as f:
        r2 = c.post("/api/upload", data={"file": (f, "b.kfb")},
                    content_type="multipart/form-data")
    assert r2.status_code in (200, 202), r2.get_data(as_text=True)
    job2 = conversion_store.get_job(jid)
    assert job2["source_name"] == "a.kfb"
    assert job2["canonical_name"] == "a.tif"
    assert "b.kfb" in conversion_store.list_source_names(jid)
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_store.get_job(jid)["state"] == "ready"
    c.delete("/api/slide/a.tif")
    assert not (app_mod.UPLOAD_DIR / "a.kfb").exists()
    assert not (app_mod.UPLOAD_DIR / "b.kfb").exists()


def test_worker_link_fail_does_not_delete_foreign_tif(tmp_path):
    """P4-app 断言换新：不存在共享平铺 dest/hardlink 面——失败清自己的
    staging 即可，他人文件（任何名字）结构性不可触碰。"""
    src = build_synthetic_kfb(tmp_path / "fx.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "fx.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    victim = app_mod.UPLOAD_DIR / "fx.tif"
    victim.write_bytes(b"FOREIGN-TIFF-BYTES")
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert victim.read_bytes() == b"FOREIGN-TIFF-BYTES"
    job = conversion_store.get_job(r.get_json()["conversion_job_id"])
    assert job["state"] == "ready"


def test_v2_old_commit_cannot_claim_replaced_source(tmp_path, monkeypatch):
    """删除后同名新源落盘，原 upload 重放 commit 不得认领新文件。"""
    import user_store
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    src_a = build_synthetic_kfb(tmp_path / "a.kfb", width=580, height=300)
    src_b = build_synthetic_kfb(tmp_path / "b.kfb", width=300, height=280)
    data_a = Path(src_a).read_bytes()
    data_b = Path(src_b).read_bytes()
    assert hashlib.sha256(data_a).digest() != hashlib.sha256(data_b).digest()

    ca = _client()
    ua = user_store.create_user("owna@x.com", "pass1234pass1234", role="user")
    with ca.session_transaction() as s:
        s["auth_user"] = ua.get("login_id") or "owna@x.com"
        s["user_id"] = ua["user_id"]
        s["role"] = "user"
        s["auth_version"] = ua.get("auth_version", 1)
    uid_a, r = _v2_commit_file(ca, "claim.kfb", data_a)
    assert r.status_code == 202, r.get_data(as_text=True)
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job_a = conversion_store.get_job(r.get_json()["conversion_job_id"])
    assert job_a["state"] == "ready"
    assert ca.delete("/api/slides/%s" % job_a["slide_id"]).status_code == 200

    cb = _client()
    ub = user_store.create_user("ownb@x.com", "pass1234pass1234", role="user")
    with cb.session_transaction() as s:
        s["auth_user"] = ub.get("login_id") or "ownb@x.com"
        s["user_id"] = ub["user_id"]
        s["role"] = "user"
        s["auth_version"] = ub.get("auth_version", 1)
    _uid_b, rb = _v2_commit_file(cb, "claim.kfb", data_b)
    assert rb.status_code == 202, rb.get_data(as_text=True)
    sha_b = hashlib.sha256(data_b).hexdigest()
    job_b = conversion_store.get_job(rb.get_json()["conversion_job_id"])
    assert job_b["owner_user_id"] == ub["user_id"]
    assert job_b["source_sha256"] == sha_b

    # P4-app 断言换新：A 的 commit 重放不再有「按名认领他人源」面（源副本
    # 各归各任务 staging）——幂等重放返回自己的既有任务；B 的任务/源不受扰。
    replay = ca.post("/api/uploads/%s/commit" % uid_a)
    assert replay.status_code in (200, 202), replay.get_data(as_text=True)
    replay_body = replay.get_json()
    assert replay_body["conversion_job_id"] != job_b["id"]
    assert conversion_store.get_job(job_b["id"])["source_sha256"] == sha_b
    assert (conversion_worker.source_staging_dir(
        job_b["id"], app_mod.UPLOAD_DIR) / "data.kfb").is_file()


def test_ready_same_bytes_rename_keeps_original_canonical(tmp_path):
    src = build_synthetic_kfb(tmp_path / "a.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "a.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    sid = job["slide_id"]
    with open(src, "rb") as f:
        r2 = c.post("/api/upload", data={"file": (f, "b.kfb")},
                    content_type="multipart/form-data")
    assert r2.status_code in (200, 202), r2.get_data(as_text=True)
    job = conversion_store.get_job(jid)
    assert job["source_name"] == "a.kfb"
    assert job["canonical_name"] == "a.tif"
    assert job["state"] == "ready"
    assert job["slide_id"] == sid  # 复用既有任务及其产物 ID
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    aliases = conversion_store.list_source_names(jid)
    assert "b.kfb" in aliases
    # 删除产物：任务作废 + 源副本（a.kfb 本体 + b.kfb 别名副本）一并清理
    c.delete("/api/slides/%s" % sid)
    assert conversion_store.get_job(jid)["state"] == "cancelled"
    assert not conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR).exists()


def test_worker_resumes_complete_work_same_attempt(tmp_path, monkeypatch):
    """断点续跑（场景保留、断言换新）：同代次 work 已完整（崩溃在转换完成与
    intent 之间）→ 重跑**不重转换**（直接复用 staging work）并收口 ready。"""
    src = build_synthetic_kfb(tmp_path / "resume.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "resume.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["conversion_job_id"]
    claimed = conversion_store.claim_job(jid, "cvw_resume_test")
    assert claimed is not None and claimed["attempt"] == 1
    source = str(conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR) / "data.kfb")
    gen_dir = slide_storage.staging_dir(
        jid, str(claimed["attempt"]), root=app_mod.UPLOAD_DIR)
    gen_dir.mkdir(parents=True, exist_ok=True)
    work = str(gen_dir / conversion_worker.PRODUCT_ENTRY)
    kfb_converter.convert_kfb(source, work)
    assert os.path.isfile(work) and os.path.isfile(work + ".manifest.json")

    def _no_reconvert(*_a, **_k):
        raise AssertionError("应复用已完整的同代次 work，不得重转换")

    monkeypatch.setattr(conversion_worker, "_convert_for_source", _no_reconvert)
    ok = conversion_worker.process_job(claimed, str(app_mod.UPLOAD_DIR),
                                       "cvw_resume_test")
    assert ok
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    desc = slide_store.resolve_slide_id(job["slide_id"])
    dest = slide_storage.resolve_descriptor_path(desc, root=app_mod.UPLOAD_DIR)
    assert dest.is_file()
    listed = {s.get("slide_id") for s in c.get("/api/slides").get_json()}
    assert job["slide_id"] in listed