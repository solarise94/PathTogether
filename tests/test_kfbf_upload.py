# -*- coding: utf-8 -*-
"""KFBF（荧光）上传 → 202 转换任务 → worker 产出 canonical OME-TIFF。

镜像 tests/test_kfb_upload.py 的 KFB 合同（P4-app 断言换新）：源副本归
任务 staging（不对 Viewer 列出、不平铺 UPLOAD_DIR）；产物预分配
slide_id、经统一发布落 objects/<sid>/data.tif；多通道语义保留。
"""
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
import kfb.converter_fl as kfbf_converter  # noqa: E402
import slide_io  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
from kfb.fixture_fl import build_synthetic_kfbf  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    # P4-app：转换链 create_job 即预分配产物资产——本地态注入配置 owner。
    import share_store as _ss
    import user_store as _us
    _ss.set_owner_user_id(
        _us.create_user("p3-local-owner@x.com", "localownerpass12345",
                        role="user")["user_id"])
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(kfbf_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return csrf_client(app_mod.app.test_client())


def test_v1_kfbf_returns_202_and_worker_makes_ome_tif(tmp_path):
    src = build_synthetic_kfbf(tmp_path / "case.kfbf")
    c = _client()
    with open(src, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "case.kfbf")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "conversion_pending"
    assert body["state"] == "queued"
    assert body["source_name"] == "case.kfbf"
    assert body["canonical_name"] == "case.ome.tif"
    job_id = body["conversion_job_id"]
    sid = body["slide_id"]
    assert sid  # create_job 即预分配
    # 源副本归任务 staging（不对 Viewer 列出、不平铺）
    assert (conversion_worker.source_staging_dir(
        job_id, app_mod.UPLOAD_DIR) / "data.kfbf").is_file()
    assert not (app_mod.UPLOAD_DIR / "case.kfbf").exists()
    listed = {s.get("slide_id") for s in c.get("/api/slides").get_json()}
    assert sid not in listed

    claimed = conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert claimed == job_id
    job = conversion_store.get_job(job_id)
    assert job["state"] == "ready"
    assert job["slide_id"] == sid
    desc = slide_store.resolve_slide_id(sid)
    dest = slide_storage.resolve_descriptor_path(desc, root=app_mod.UPLOAD_DIR)
    assert dest.is_file() and dest.name == "data.tif"
    slide = slide_io.open_slide(str(dest))
    try:
        assert slide.level_count >= 1
        assert getattr(slide, "channel_count", 0) == 2
        assert [ch["name"] for ch in slide.ome_channels] == ["DAPI", "520"]
    finally:
        slide.close()
    st = c.get("/api/conversions/" + job_id).get_json()
    assert st["state"] == "ready"
    assert st["slide_id"] == sid
    listed2 = {s.get("slide_id") for s in c.get("/api/slides").get_json()}
    assert sid in listed2


def test_v2_kfbf_commit_returns_202(tmp_path):
    src = build_synthetic_kfbf(tmp_path / "v2.kfbf")
    data = Path(src).read_bytes()
    c = _client()
    r = c.post("/api/uploads", json={
        "filename": "v2.kfbf", "declared_size": len(data)})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["upload_id"]
    sha = hashlib.sha256(data).hexdigest()
    put = c.put("/api/uploads/%s/chunk?offset=0&sha256=%s" % (uid, sha),
                data=data, content_type="application/octet-stream")
    assert put.status_code == 200, put.get_data(as_text=True)
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "conversion_pending"
    assert body["conversion_job_id"]


def test_garbage_kfbf_rejected(tmp_path):
    p = tmp_path / "x.kfbf"
    p.write_bytes(b"not-a-slide")
    c = _client()
    with open(p, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "x.kfbf")},
                   content_type="multipart/form-data")
    assert r.status_code == 400


def test_brightfield_kfb_bytes_named_kfbf_convert_by_magic(tmp_path):
    """明场 KFB 字节改名 .kfbf：worker 按 magic 分派走明场转换器
    （内容嗅探优先于扩展名），任务正常 ready。"""
    from kfb.fixture import build_synthetic_kfb
    src = build_synthetic_kfb(tmp_path / "bf.kfb")
    data = Path(src).read_bytes()
    p = tmp_path / "fake.kfbf"
    p.write_bytes(data)
    c = _client()
    with open(p, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "fake.kfbf")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    jid = body["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert slide_storage.resolve_descriptor_path(
        desc, root=app_mod.UPLOAD_DIR).is_file()
