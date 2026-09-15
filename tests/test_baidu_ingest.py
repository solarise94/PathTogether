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


# --------------------------------------------------------------------------- #
# P1 回归：同名检查与复制之间的覆盖窗口（O_EXCL 独占创建 + 竞态兜底）
# --------------------------------------------------------------------------- #
_VICTIM = b"victim-content-from-concurrent-uploader"


def _stage_file(tmp_path, name, content):
    """把字节落成暂存文件（模拟下载产物已就绪）。"""
    p = tmp_path / "staging" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _patch_exists_lie(monkeypatch, target):
    """复现「预检查放行、复制时目标已存在」的竞态窗口。

    真实时序：并发上传在本 ingest 的 exists 预检查之后、复制之前把同名
    文件落盘。测试无法注入毫秒级窗口，等价做法：目标文件预先创建（=
    并发方已完成落盘），仅对它让 ``Path.exists`` 返回 False（= 预检查
    看到窗口期开始前的快照），其余路径保持真实行为。
    """
    real_exists = Path.exists
    target = Path(str(target))

    def fake_exists(self):
        if str(self) == str(target):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def test_copy_new_exclusive_and_no_partial(tmp_path):
    """_copy_new 语义：独占创建、目标已存在抛 FileExistsError 不破坏原内容。"""
    import baidu_ingest
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-bytes")
    dst = tmp_path / "dst.bin"
    baidu_ingest._copy_new(str(src), str(dst))
    assert dst.read_bytes() == b"payload-bytes"
    with pytest.raises(FileExistsError):
        baidu_ingest._copy_new(str(src), str(dst))
    assert dst.read_bytes() == b"payload-bytes"


def test_race_native_must_not_overwrite_existing_file(tmp_path, monkeypatch):
    """native 竞态：预检查放行但目标已存在 → name_unavailable 且不覆盖。"""
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "race.tif", tif)
    victim = Path(UPLOAD_DIR) / "race.tif"
    victim.write_bytes(_VICTIM)
    _patch_exists_lie(monkeypatch, victim)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="race.tif",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "name_unavailable"
    # 既有文件内容原样保留（修复前被 copy2 直接覆盖）
    assert victim.read_bytes() == _VICTIM
    # 暂存原件不受失败影响
    assert staging.read_bytes() == tif


def test_race_convert_must_not_overwrite_existing_file(tmp_path, monkeypatch):
    """convert 竞态：同 native，且失败必须发生在 create_job 之前（无任务残留）。"""
    import baidu_ingest
    kfb_path = build_synthetic_kfb(tmp_path / "gen.kfb")
    staging = _stage_file(tmp_path, "panel.kfb", kfb_path.read_bytes())
    victim = Path(UPLOAD_DIR) / "panel.kfb"
    victim.write_bytes(_VICTIM)
    _patch_exists_lie(monkeypatch, victim)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="panel.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "name_unavailable"
    assert victim.read_bytes() == _VICTIM
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversion_jobs "
                "WHERE canonical_name=%s", ("panel.tif",))
            n = cur.fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_midcopy_failure_leaves_no_partial_no_victim(tmp_path, monkeypatch):
    """复制中途失败：不留半成品、不碰既有他人文件、暂存原件完好。"""
    import shutil
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "broken.tif", tif)
    neighbor = Path(UPLOAD_DIR) / "neighbor.tif"
    neighbor.write_bytes(_VICTIM)

    def _boom(fsrc, fdst, length=0):
        fdst.write(fsrc.read(16))  # 写到一半坏掉
        raise OSError("simulated mid-copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", _boom)
    with pytest.raises(OSError, match="mid-copy"):
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="broken.tif",
            staging_path=str(staging), source_sha256=None, source_size=0)
    # 不留半成品
    assert not (Path(UPLOAD_DIR) / "broken.tif").exists()
    # 不误删他人文件
    assert neighbor.read_bytes() == _VICTIM
    # 暂存原件完好
    assert staging.read_bytes() == tif


def test_ingest_native_success_unaffected(tmp_path):
    """正常 native 入库不受独占创建改造影响（内容逐字节一致）。"""
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "direct.tif", tif)
    out = baidu_ingest.ingest_staging(
        owner_user_id=OWNER, original_name="direct.tif",
        staging_path=str(staging), source_sha256=None, source_size=0)
    assert out["ingest_token"] == "slide:direct.tif"
    assert out["slide_name"] == "direct.tif"
    assert out["conversion_job_id"] is None
    assert out["project_associate_state"] == "not_needed"
    dest = Path(UPLOAD_DIR) / "direct.tif"
    assert dest.is_file()
    assert dest.read_bytes() == tif


def test_ingest_convert_success_unaffected(tmp_path):
    """正常 convert 入库不受独占创建改造影响。"""
    import baidu_ingest
    kfb_path = build_synthetic_kfb(tmp_path / "gen.kfb")
    staging = _stage_file(tmp_path, "direct.kfb", kfb_path.read_bytes())
    out = baidu_ingest.ingest_staging(
        owner_user_id=OWNER, original_name="direct.kfb",
        staging_path=str(staging), source_sha256=None, source_size=0)
    assert out["slide_name"] == "direct.tif"
    assert out["conversion_job_id"]
    assert out["project_associate_state"] == "not_needed"
    assert (Path(UPLOAD_DIR) / "direct.tif").is_file()


# --------------------------------------------------------------------------- #
# P1/P2 回归：同内容改名 KFB 复用 ready/running 任务；create_job 失败清理
# --------------------------------------------------------------------------- #


def _make_kfb_bytes(tmp_path):
    return build_synthetic_kfb(tmp_path / "_gen.kfb").read_bytes()


def _ingest(tmp_path, name, content, *, owner=OWNER, project_id=None):
    import baidu_ingest
    staging = _stage_file(tmp_path, name, content)
    return baidu_ingest.ingest_staging(
        owner_user_id=owner, original_name=name,
        staging_path=str(staging), source_sha256=None, source_size=0,
        target_project_id=project_id)


def _count_jobs(owner=OWNER):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversion_jobs "
                "WHERE owner_user_id=%s", (owner,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_ready_job_reuse_renamed_kfb_no_reprocess(tmp_path):
    """P1：同内容改名复用 ready 任务——不重跑转换、slide_name 用原
    canonical、删除本次副本、conversion job 恰 1 个、别名已登记。"""
    kfb = _make_kfb_bytes(tmp_path)
    up = Path(UPLOAD_DIR)
    first = _ingest(tmp_path, "first.kfb", kfb)
    assert first["slide_name"] == "first.tif"
    assert (up / "first.kfb").is_file()
    assert (up / "first.tif").is_file()
    proj = share_store.create_project(
        "复用关联", owner_user_id=OWNER, requester_role="user")
    second = _ingest(tmp_path, "second.kfb", kfb, project_id=proj["pid"])
    assert second["ingest_token"] == first["ingest_token"]
    assert second["slide_name"] == "first.tif"  # 原 canonical，不改绑
    assert second["conversion_job_id"] == first["conversion_job_id"]
    assert second["project_associate_state"] == "succeeded"
    assert not (up / "second.kfb").exists()  # 本次复制的源文件已清理
    assert not (up / "second.tif").exists()  # 未生成新产物
    assert _count_jobs() == 1  # conversion job 恰 1 个
    # 别名已登记（源名表含新名）
    assert "second.kfb" in conversion_store.list_source_names(
        first["conversion_job_id"])
    got = share_store.get_project(proj["pid"])
    assert got["slides"] == ["first.tif"]  # 原 canonical 入项目


def test_running_job_reuse_waits_until_ready(tmp_path, monkeypatch):
    """P1：同内容任务他人转换中（running，租约被占）——等待至 ready 后
    按复用收口，不抢租约、不重复转换、副本清理。"""
    import threading
    import time as time_mod
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "second.kfb", kfb)
    digest = baidu_ingest._sha256_file(staging)
    job = conversion_store.create_job(
        owner_user_id=OWNER, upload_id=None, source_name="first.kfb",
        source_sha256=digest, source_format="kfb_kfbio_jpeg",
        canonical_name="first.tif", product_exists=False)
    holder = conversion_store.claim_job(job["id"], "cvw_fake_holder")
    assert holder["state"] == "converting"

    def _finish():
        time_mod.sleep(0.3)
        conversion_store.mark_state(job["id"], "cvw_fake_holder", "ready")

    t = threading.Thread(target=_finish)
    t.start()
    monkeypatch.setattr(baidu_ingest, "RUNNING_POLL_SECONDS", 0.05)
    try:
        out = baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="second.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    finally:
        t.join()
    assert out["slide_name"] == "first.tif"
    assert out["ingest_token"] == "cvj:" + job["id"]
    assert not (Path(UPLOAD_DIR) / "second.kfb").exists()


def test_running_job_reuse_timeout_conversion_busy(tmp_path, monkeypatch):
    """P1：等待超时 → IngestError("conversion_busy")（不在
    NON_RETRYABLE_ERROR_CODES 中，可重试）且本次副本已清理、他人任务不受扰。"""
    import baidu_ingest
    from baidu_import_store import NON_RETRYABLE_ERROR_CODES
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "second.kfb", kfb)
    digest = baidu_ingest._sha256_file(staging)
    job = conversion_store.create_job(
        owner_user_id=OWNER, upload_id=None, source_name="first.kfb",
        source_sha256=digest, source_format="kfb_kfbio_jpeg",
        canonical_name="first.tif", product_exists=False)
    holder = conversion_store.claim_job(job["id"], "cvw_fake_holder")
    assert holder["state"] == "converting"
    monkeypatch.setattr(baidu_ingest, "RUNNING_POLL_SECONDS", 0.02)
    monkeypatch.setattr(baidu_ingest, "RUNNING_TIMEOUT_SECONDS", 0.15)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="second.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "conversion_busy"
    assert "conversion_busy" not in NON_RETRYABLE_ERROR_CODES
    assert not (Path(UPLOAD_DIR) / "second.kfb").exists()
    # 他人任务未被本次入库扰动（仍在转换中、租约仍属原持有者）
    after = conversion_store.get_job(job["id"])
    assert after["state"] == "converting"
    assert after["lease_owner"] == "cvw_fake_holder"


def test_create_job_name_conflict_cleans_copied_source(tmp_path, monkeypatch):
    """P2：create_job 抛 NameConflict → IngestError("name_unavailable")
    且本次复制的源文件不遗留。"""
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "panel.kfb", kfb)

    def _conflict(**kwargs):
        raise conversion_store.NameConflict("canonical 名已被占用")

    monkeypatch.setattr(conversion_store, "create_job", _conflict)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="panel.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "name_unavailable"
    assert not (Path(UPLOAD_DIR) / "panel.kfb").exists()


def test_create_job_generic_error_becomes_conversion_failed(
        tmp_path, monkeypatch):
    """P2：create_job 抛未知异常 → IngestError("conversion_failed")
    （条目级失败优于批次级崩溃），保留 cause，副本不遗留。"""
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "panel.kfb", kfb)

    def _boom(**kwargs):
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(conversion_store, "create_job", _boom)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="panel.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "conversion_failed"
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert not (Path(UPLOAD_DIR) / "panel.kfb").exists()


def test_batch_same_content_renamed_kfb_succeeds(tmp_path, monkeypatch):
    """P1 批次级：同内容改名 KFB 走 run_batch → 条目 ready、批次
    succeeded、无孤立文件、conversion job 仍恰 1 个。"""
    kfb = _make_kfb_bytes(tmp_path)
    entries1 = [{"path": "/keep/alpha.kfb", "size": len(kfb),
                 "content": kfb}]
    fake1, enum1, by1 = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries1)
    b1 = store.create_import(
        OWNER, enum1, [by1["keep/alpha.kfb"]["id"]],
        idempotency_key="reuse-b1")
    v1 = store.run_batch(b1["id"], fake1,
                         staging_root=str(tmp_path / "st1"))
    assert v1["state"] == "succeeded", v1
    entries2 = [{"path": "/keep/beta.kfb", "size": len(kfb),
                 "content": kfb}]
    fake2, enum2, by2 = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries2,
        share_text="https://pan.baidu.com/s/1SecondShareZZ")
    b2 = store.create_import(
        OWNER, enum2, [by2["keep/beta.kfb"]["id"]],
        idempotency_key="reuse-b2")
    v2 = store.run_batch(b2["id"], fake2,
                         staging_root=str(tmp_path / "st2"))
    assert v2["state"] == "succeeded", v2
    item = v2["items"][0]
    assert item["stage"] == "ready", item
    assert item["slide_name"] == "alpha.tif"  # 原 canonical
    up = Path(UPLOAD_DIR)
    assert (up / "alpha.tif").is_file()
    assert not (up / "beta.kfb").exists()  # 副本清理，无孤立文件
    assert not (up / "beta.tif").exists()
    assert _count_jobs() == 1
