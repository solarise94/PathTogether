# -*- coding: utf-8 -*-
"""KFB 上传 → 202 转换任务 → worker 产出 canonical TIFF。"""
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
import upload_guard  # noqa: E402
from kfb.fixture import build_synthetic_kfb  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def test_v1_kfb_returns_202_and_worker_makes_tif(tmp_path):
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
    # 源文件已落盘但不在可打开列表（扩展名不在 SUPPORTED_EXTS）
    assert (app_mod.UPLOAD_DIR / "case.kfb").is_file()
    slides = c.get("/api/slides").get_json()
    names = [s["name"] if isinstance(s, dict) else s for s in slides]
    assert "case.kfb" not in names
    assert "case.tif" not in names

    claimed = conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert claimed == job_id
    job = conversion_store.get_job(job_id)
    assert job["state"] == "ready"
    dest = app_mod.UPLOAD_DIR / "case.tif"
    assert dest.is_file()
    slide = slide_io.open_slide(str(dest))
    try:
        assert slide.level_count >= 1
    finally:
        slide.close()
    st = c.get("/api/conversions/" + job_id).get_json()
    assert st["state"] == "ready"
    slides2 = c.get("/api/slides").get_json()
    names2 = [s["name"] if isinstance(s, dict) else s for s in slides2]
    assert "case.tif" in names2


def test_kfbf_still_rejected(tmp_path):
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
    src = build_synthetic_kfb(tmp_path / "ow.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "ow.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    src_name = body["source_name"]
    canon = body["canonical_name"]
    assert (app_mod.UPLOAD_DIR / src_name).is_file()
    victim = app_mod.UPLOAD_DIR / canon
    victim.write_bytes(b"KEEP-ME-NOT-A-TIFF")
    jid = body["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert victim.read_bytes() == b"KEEP-ME-NOT-A-TIFF"
    job = conversion_store.get_job(jid)
    assert job["state"] == "failed"
    assert job["error_code"] in ("name_unavailable", "conversion_validation_failed")


def test_delete_then_reupload_requeues(tmp_path):
    src = build_synthetic_kfb(tmp_path / "dl.kfb")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "dl.kfb")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    jid = r.get_json()["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_store.get_job(jid)["state"] == "ready"
    assert (app_mod.UPLOAD_DIR / "dl.tif").is_file()
    d = c.delete("/api/slide/dl.tif")
    assert d.status_code in (200, 204) or d.status_code == 200
    assert not (app_mod.UPLOAD_DIR / "dl.tif").exists()
    with open(src, "rb") as f:
        r2 = c.post("/api/upload", data={"file": (f, "dl.kfb")},
                    content_type="multipart/form-data")
    assert r2.status_code == 202, r2.get_data(as_text=True)
    body = r2.get_json()
    assert body["state"] == "queued"
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert (app_mod.UPLOAD_DIR / "dl.tif").is_file()
    st = c.get("/api/conversions/" + body["conversion_job_id"]).get_json()
    assert st["state"] == "ready"


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
    junk = tmp_path / "own.kfb"
    junk.write_bytes(b"not-the-same-bytes-at-all-xxxx")
    with open(junk, "rb") as f:
        r2 = cb.post("/api/upload", data={"file": (f, "own.kfb")},
                     content_type="multipart/form-data")
    assert r2.status_code == 409, r2.get_data(as_text=True)
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


def test_worker_link_fail_does_not_delete_foreign_tif(tmp_path):
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
