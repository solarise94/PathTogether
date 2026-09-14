# -*- coding: utf-8 -*-
"""W5 B06/B08：fake 传输 + 真实探测/转换入库。"""
import os
from pathlib import Path

import pytest
import psycopg

import baidu_import_store as store
import pg_store
import conversion_store
import kfb.converter as kfb_converter
import kfb.converter_fl as kfbf_converter
import share_store
import slide_io
from _baidu_helpers import make_ready_enumeration
from _tiff_fixtures import make_tiff_bytes
from kfb.fixture import build_synthetic_kfb
from kfb.fixture_fl import build_synthetic_kfbf
from _pt_helpers import isolate_app, clear_upload_dir
import _bootstrap
UPLOAD_DIR = _bootstrap.UPLOAD_DIR


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setenv("UPLOAD_DIR", str(UPLOAD_DIR))
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-share-secret-key-2026-09-14")
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(kfbf_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    clear_upload_dir(UPLOAD_DIR)
    yield


OWNER = "u-b06"


def _tree(tmp_path):
    tif = make_tiff_bytes()
    kfb_path = build_synthetic_kfb(tmp_path / "panel.kfb")
    kfbf_path = build_synthetic_kfbf(tmp_path / "fl.kfbf")
    kfb_bytes = kfb_path.read_bytes()
    kfbf_bytes = kfbf_path.read_bytes()
    skip = b"not-a-slide"
    return [
        {"path": "/keep/slide.tif", "size": len(tif), "content": tif},
        {"path": "/keep/panel.kfb", "size": len(kfb_bytes), "content": kfb_bytes},
        {"path": "/keep/fl.kfbf", "size": len(kfbf_bytes), "content": kfbf_bytes},
        {"path": "/skip/notes.txt", "size": len(skip), "content": skip},
    ]


def test_b06_native_kfb_kfbf_real_ingest_and_project(tmp_path, monkeypatch):
    entries = _tree(tmp_path)
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    proj = share_store.create_project(
        "百度入库", owner_user_id=OWNER, requester_role="user")
    ids = [
        by_path["keep/slide.tif"]["id"],
        by_path["keep/panel.kfb"]["id"],
        by_path["keep/fl.kfbf"]["id"],
    ]
    batch = store.create_import(
        OWNER, enum_id, ids, target_project_id=proj["pid"],
        idempotency_key="b06-1")
    view = store.run_batch(batch["id"], fake, staging_root=str(tmp_path / "st"))
    assert view["state"] == "succeeded", view
    names = {i["name"]: i for i in view["items"]}
    assert names["slide.tif"]["stage"] == "ready"
    assert names["panel.kfb"]["stage"] == "ready"
    assert names["fl.kfbf"]["stage"] == "ready"
    assert names["slide.tif"]["slide_name"] == "slide.tif"
    assert names["panel.kfb"]["slide_name"] == "panel.tif"
    assert names["fl.kfbf"]["slide_name"] == "fl.ome.tif"
    up = Path(UPLOAD_DIR)
    assert (up / "slide.tif").is_file()
    assert (up / "panel.tif").is_file()
    assert (up / "fl.ome.tif").is_file()
    s = slide_io.open_slide(str(up / "slide.tif"))
    try:
        assert s.level_count >= 1
    finally:
        s.close()
    s2 = slide_io.open_slide(str(up / "fl.ome.tif"))
    try:
        assert getattr(s2, "channel_count", 0) == 2
    finally:
        s2.close()
    got = share_store.get_project(proj["pid"])
    assert set(got["slides"]) == {"slide.tif", "panel.tif", "fl.ome.tif"}
    assert names["slide.tif"]["project_associate_state"] == "succeeded"
    # 未选中 notes.txt 无转存
    skip_fs = fake._files["skip/notes.txt"]["fs_id"]
    selected_fs = {
        fake._files["keep/slide.tif"]["fs_id"],
        fake._files["keep/panel.kfb"]["fs_id"],
        fake._files["keep/fl.kfbf"]["fs_id"],
    }
    transferred = [t[-1] for t in fake.transfers]
    for fs_ids in transferred:
        assert set(fs_ids) <= selected_fs
        assert skip_fs not in fs_ids
    # 配额：三次入库各一次关联，无重复
    store.run_batch(batch["id"], fake, staging_root=str(tmp_path / "st"))
    got2 = share_store.get_project(proj["pid"])
    assert got2["slides"] == got["slides"]


def test_b08_source_changed_and_name_conflict(tmp_path, monkeypatch):
    tif = make_tiff_bytes()
    entries = [{"path": "/x.tif", "size": len(tif), "content": tif}]
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    cid = by_path["x.tif"]["id"]
    batch = store.create_import(OWNER, enum_id, [cid], idempotency_key="b08-1")
    # 转存后漂移大小
    def _wrap_transfer(orig):
        def inner(*a, **k):
            resp = orig(*a, **k)
            copies = fake.copies.get(batch["id"]) or {}
            if "x.tif" in copies:
                copies["x.tif"]["size"] = int(copies["x.tif"]["size"]) + 9
            return resp
        return inner
    fake.transfer_selected = _wrap_transfer(fake.transfer_selected)
    view = store.run_batch(batch["id"], fake, staging_root=str(tmp_path / "st"))
    assert view["items"][0]["stage"] == "failed"
    assert view["items"][0]["error_code"] == "source_changed"
    assert not Path(UPLOAD_DIR).joinpath("x.tif").exists()

    tif2 = make_tiff_bytes(h=40, w=40)
    entries2 = [{"path": "/y.tif", "size": len(tif2), "content": tif2}]
    fake2, enum2, by2 = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries2,
        share_text="https://pan.baidu.com/s/1OtherShareYY")
    Path(UPLOAD_DIR).joinpath("y.tif").write_bytes(tif2)
    share_store.set_slide_meta("y.tif", owner_user_id="other-user",
                               requester_role="user")
    batch2 = store.create_import(
        OWNER, enum2, [by2["y.tif"]["id"]], idempotency_key="b08-2")
    view2 = store.run_batch(batch2["id"], fake2, staging_root=str(tmp_path / "st2"))
    assert view2["items"][0]["stage"] == "failed"
    assert view2["items"][0]["error_code"] == "name_unavailable"
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT owner_user_id FROM slides WHERE legacy_filename=%s",
                ("y.tif",))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row and row[0] == "other-user"


def test_c02_local_upload_native_and_kfb_associate(tmp_path, monkeypatch):
    import app as app_mod
    import upload_guard
    from _pt_helpers import csrf_client
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    pr = c.post("/api/project/create", json={"name": "本地导入", "slides": []})
    assert pr.status_code == 200, pr.get_data(as_text=True)
    proj = pr.get_json()
    tif = make_tiff_bytes()
    r = c.post("/api/upload", data={
        "file": (io_bytes(tif), "local.tif"),
        "target_project_id": proj["pid"],
    })
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    got = share_store.get_project(proj["pid"])
    assert "local.tif" in got["slides"]

    kfb_path = build_synthetic_kfb(tmp_path / "loc.kfb")
    with open(kfb_path, "rb") as fh:
        r2 = c.post("/api/upload", data={
            "file": (fh, "loc.kfb"),
            "target_project_id": proj["pid"],
        })
    assert r2.status_code in (200, 202), r2.get_data(as_text=True)
    job_id = r2.get_json()["conversion_job_id"]
    import conversion_worker
    if r2.status_code == 202:
        conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(job_id)
    assert job["state"] == "ready"
    assert job.get("project_associate_state") == "succeeded"
    got = share_store.get_project(proj["pid"])
    assert "loc.tif" in got["slides"]
    kfb2 = build_synthetic_kfb(tmp_path / "gone.kfb", width=400, height=280)
    with open(kfb2, "rb") as fh:
        r3 = c.post("/api/upload", data={
            "file": (fh, "gone.kfb"),
            "target_project_id": proj["pid"],
        })
    assert r3.status_code in (200, 202), r3.get_data(as_text=True)
    job3 = r3.get_json()["conversion_job_id"]
    share_store.delete_project(proj["pid"])
    if r3.status_code == 202:
        conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(job3)
    assert job["state"] == "ready"
    assert job.get("project_associate_state") == "failed"
    assert (app_mod.UPLOAD_DIR / "gone.tif").is_file()


def io_bytes(data):
    import io
    return io.BytesIO(data)
