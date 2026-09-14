# -*- coding: utf-8 -*-
"""KFBF（荧光）上传 → 202 转换任务 → worker 产出 canonical OME-TIFF。

镜像 tests/test_kfb_upload.py 的 KFB 合同：源文件不对 Viewer 列出，
canonical 为 <stem>.ome.tif，worker 完成后可被 slide_io 打开且保留
多通道语义。
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
import upload_guard  # noqa: E402
from kfb.fixture_fl import build_synthetic_kfbf  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
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
    # 源文件落盘但不对 Viewer 列出
    assert (app_mod.UPLOAD_DIR / "case.kfbf").is_file()
    slides = c.get("/api/slides").get_json()
    names = [s["name"] if isinstance(s, dict) else s for s in slides]
    assert "case.kfbf" not in names
    assert "case.ome.tif" not in names

    claimed = conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert claimed == job_id
    job = conversion_store.get_job(job_id)
    assert job["state"] == "ready"
    dest = app_mod.UPLOAD_DIR / "case.ome.tif"
    assert dest.is_file()
    slide = slide_io.open_slide(str(dest))
    try:
        assert slide.level_count >= 1
        assert getattr(slide, "channel_count", 0) == 2
        assert [ch["name"] for ch in slide.ome_channels] == ["DAPI", "520"]
    finally:
        slide.close()
    st = c.get("/api/conversions/" + job_id).get_json()
    assert st["state"] == "ready"
    slides2 = c.get("/api/slides").get_json()
    names2 = [s["name"] if isinstance(s, dict) else s for s in slides2]
    assert "case.ome.tif" in names2


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
    jid = r.get_json()["conversion_job_id"]
    conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_store.get_job(jid)["state"] == "ready"
    assert (app_mod.UPLOAD_DIR / "fake.ome.tif").is_file()
