# -*- coding: utf-8 -*-
"""slide ID 化重构 P4-app：转换链 slide_id 统一发布门禁测试。

合同：docs/slide-id-refactor-p4-contract-20260925.md §3/§8（计划 §8 矩阵的
转换行）。U5 检查点 B 起旧 POST /api/upload 端点已删除：源受理改走服务级
夹具 _kfb_job（预占 → begin_legacy_commit → 建 job → 源副本入任务 staging →
finish_commit 按源字节结算——旧端点的离线等价链）。逐条覆盖：

  1. 同名源/产物不冲突（独立 ID）：同名不同内容的两次 KFB 受理 → 两个
     任务、两个产物资产（canonical 展示快照同名），互不干扰；轮询端点的
     slide_id 从任务绑定读；
  2. failure/retry 不重复 ID/项目关联/配额：任务失败 → 同 id 重试（产物
     绑定复用）→ ready；used_bytes 只结算一次；项目关联按 slide_id 恰一次；
  3. 源删除后产物仍在（独立资产）；
  4. 产物删除按 ID 作废：任务 cancelled + 源副本清理；重传复用同 job 并
     改绑新 slide_id（删除后重传=新资产）；
  5. 统一发布：产物包 objects/<sid>/（data.tif + kfb manifest + associated
     全成员，manifest 指定唯一入口）；intent 结算并入 publish 事务。

运行：cd 项目根 && python3 -m pytest tests/test_conversion_slide_id_pg.py -q
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import conversion_store  # noqa: E402
import conversion_worker  # noqa: E402
import kfb.converter as kfb_converter  # noqa: E402
import share_store  # noqa: E402
import slide_io  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import clear_upload_dir, csrf_client, isolate_app  # noqa: E402
from kfb.fixture import build_synthetic_kfb  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, login="c@x.com", role="user"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _set_quota(user_id, quota_bytes):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                "SET quota_bytes = EXCLUDED.quota_bytes",
                (user_id, quota_bytes))


def _one(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def _kfb_job(uid, src_path, name, *, target_project_id=None):
    """服务级受理一次 KFB/KFBF 源（旧 POST /api/upload 的离线等价链）：
    预占 → begin_legacy_commit → 建 job（预分配产物 slide_id）→ 源副本入
    任务 staging → finish_commit 按源字节结算。返回 job dict。

    stage_source_copy 会**移动**源文件（os.replace）——用例持有的 src_path
    须保持原样，故从私有临时副本入仓。"""
    import tempfile
    src_path = Path(src_path)
    data = src_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    res = upload_guard.reserve_upload(uid, len(data), inflight_limit=10,
                                      hourly_limit=10)
    upload_id, token, _task = upload_task_store.begin_legacy_commit(
        owner_user_id=uid, filename=name, safe_name=name,
        artifacts=[{"name": name, "size": len(data), "sha256": sha,
                    "slide": False}],
        reservation_id=res["reservation_id"])
    job = conversion_store.create_job(
        owner_user_id=uid, upload_id=upload_id, source_name=name,
        source_sha256=sha, source_format=src_path.suffix.lstrip(".").lower(),
        canonical_name="%s.tif" % name.rsplit(".", 1)[0],
        target_project_id=target_project_id)
    fd, tmp_src = tempfile.mkstemp(suffix=src_path.suffix)
    os.close(fd)
    Path(tmp_src).write_bytes(data)
    conversion_worker.stage_source_copy(job["id"], tmp_src, UPLOAD_DIR)
    Path(tmp_src).unlink(missing_ok=True)   # 已被 move 入仓则无残留
    upload_task_store.finish_commit(upload_id, token, sha,
                                    settle_bytes=len(data))
    return job


def _quota_bytes(uid):
    row = upload_guard.get_quota_row(uid)
    return int(row["used_bytes"]) if row else 0


# --------------------------------------------------------------------------- #
# 1. 同名源/产物不冲突（独立 ID）；响应/轮询 slide_id 从任务绑定读
# --------------------------------------------------------------------------- #
def test_same_name_source_and_product_independent_ids(tmp_path):
    c = _client()
    uid = _user_session(c, login="cv-same@x.com")
    _set_quota(uid, 10 ** 8)

    src_a = build_synthetic_kfb(tmp_path / "case.kfb", width=580, height=300)
    src_b = build_synthetic_kfb(tmp_path / "case2.kfb", width=300, height=280)
    ja = _kfb_job(uid, src_a, "case.kfb")
    jb = _kfb_job(uid, src_b, "case.kfb")  # 同名、不同内容
    # 两个任务、两个预分配产物 ID（canonical 展示快照同名——独立资产）
    assert ja["id"] != jb["id"]
    assert ja["slide_id"] and jb["slide_id"] \
        and ja["slide_id"] != jb["slide_id"]
    assert ja["canonical_name"] == jb["canonical_name"] == "case.tif"
    # 源副本各归各任务 staging（同名源不冲突——不平铺）
    assert (conversion_worker.source_staging_dir(
        ja["id"], app_mod.UPLOAD_DIR) / "data.kfb").is_file()
    assert (conversion_worker.source_staging_dir(
        jb["id"], app_mod.UPLOAD_DIR) / "data.kfb").is_file()

    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    for job in (ja, jb):
        row = conversion_store.get_job(job["id"])
        assert row["state"] == "ready"
        assert row["slide_id"] == job["slide_id"]
        desc = slide_store.resolve_slide_id(job["slide_id"])
        assert desc.asset_state == "ready"
        assert desc.storage_layout == "id_bundle"
        assert slide_storage.resolve_descriptor_path(
            desc, root=app_mod.UPLOAD_DIR).is_file()
        # 轮询端点的 slide_id 从任务绑定读（public_view 含 job.slide_id）
        st = c.get("/api/conversions/%s" % row["id"]).get_json()
        assert st["slide_id"] == row["slide_id"]


# --------------------------------------------------------------------------- #
# 2. failure/retry 不重复 ID/项目关联/配额
# --------------------------------------------------------------------------- #
def test_failure_retry_same_id_and_single_settlement(tmp_path, monkeypatch):
    # R1 关闭了重试入口（410）；本用例覆盖 C7 删除前仍在的旧重试代码路径
    monkeypatch.setattr(app_mod, "SERVER_CONVERSION_CREATION", True)
    c = _client()
    uid = _user_session(c, login="cv-retry@x.com")
    _set_quota(uid, 10 ** 8)

    src = build_synthetic_kfb(tmp_path / "rt.kfb")
    pid = _mk_project(uid)
    job = _kfb_job(uid, src, "rt.kfb", target_project_id=pid)
    jid = job["id"]
    sid = job["slide_id"]

    # 第一次运行失败：抽走源副本（worker 判 source missing → failed）
    used_after_upload = _quota_bytes(uid)  # 源字节已结算（受理任务口径）
    src_copy = conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR) / "data.kfb"
    src_bytes = src_copy.read_bytes()
    src_copy.unlink()
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    assert job["state"] == "failed"
    assert slide_store.resolve_slide_id(sid).asset_state == "failed"
    assert _quota_bytes(uid) == used_after_upload  # 产物未结算

    # 放回源副本 → 同 id 重试（产物绑定复用，绝不重新分配）
    src_copy.parent.mkdir(parents=True, exist_ok=True)
    src_copy.write_bytes(src_bytes)
    rr = c.post("/api/conversions/%s/retry" % jid)
    assert rr.status_code == 200, rr.get_data(as_text=True)
    assert rr.get_json()["conversion_job_id"] == jid
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    assert job["slide_id"] == sid
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    assert desc.accounted_bytes > 0
    # 配额只结算一次（canonical_settled_bytes 幂等键并入 publish 事务）：
    # used = 源字节 + 产物字节（恰一次，不含失败尝试的重复）
    used = _quota_bytes(uid)
    assert used == used_after_upload + int(desc.accounted_bytes)
    # 项目关联按 slide_id 恰一次
    proj = share_store.get_project(pid)
    assert (proj.get("slide_ids") or []) == [sid]


def _mk_project(uid):
    proj = share_store.create_project("转换关联", owner_user_id=uid,
                                      requester_role="user")
    return proj["pid"]


# --------------------------------------------------------------------------- #
# 3. 源删除后产物仍在（独立资产）
# --------------------------------------------------------------------------- #
def test_product_survives_source_deletion(tmp_path):
    c = _client()
    uid = _user_session(c, login="cv-srcdel@x.com")
    _set_quota(uid, 10 ** 8)

    src = build_synthetic_kfb(tmp_path / "sd.kfb")
    job = _kfb_job(uid, src, "sd.kfb")
    jid = job["id"]
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    sid = job["slide_id"]
    assert job["state"] == "ready"
    # 抽走源副本（模拟源清理/丢失）：产物照常可读
    shutil_rmtree(conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR))
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    s = slide_io.open_slide(str(slide_storage.resolve_descriptor_path(
        slide_store.resolve_slide_id(sid), root=app_mod.UPLOAD_DIR)))
    try:
        assert s.level_count >= 1
    finally:
        s.close()


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. 产物删除按 ID 作废任务；重传复用同 job 改绑新 ID
# --------------------------------------------------------------------------- #
def test_delete_product_invalidates_job_by_slide_id(tmp_path):
    c = _client()
    uid = _user_session(c, login="cv-del@x.com")
    _set_quota(uid, 10 ** 8)

    src = build_synthetic_kfb(tmp_path / "dl.kfb")
    job = _kfb_job(uid, src, "dl.kfb")
    jid = job["id"]
    sid = job["slide_id"]
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    assert conversion_store.get_job(jid)["state"] == "ready"

    # 按 ID 删除产物 → 任务作废（名占用语义退役）+ 源副本清理 + 减账
    assert c.delete("/api/slides/%s" % sid).status_code == 200
    assert conversion_store.get_job(jid)["state"] == "cancelled"
    assert not conversion_worker.source_staging_dir(
        jid, app_mod.UPLOAD_DIR).exists()
    assert not slide_storage.bundle_dir(sid, root=app_mod.UPLOAD_DIR).exists()
    assert slide_store.resolve_slide_id(sid).asset_state == "deleted"

    # 同内容重传：复用同 job（幂等键），产物改绑**新** slide_id
    job2 = _kfb_job(uid, src, "dl.kfb")
    assert job2["id"] == jid
    assert job2["slide_id"] != sid
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job2 = conversion_store.get_job(jid)
    assert job2["state"] == "ready"
    assert job2["slide_id"] != sid
    assert c.get("/api/slides/%s/info" % job2["slide_id"]).status_code == 200


# --------------------------------------------------------------------------- #
# 5. 统一发布：产物包全成员（data.tif + kfb manifest + associated）
# --------------------------------------------------------------------------- #
def test_product_bundle_contains_all_members(tmp_path):
    c = _client()
    uid = _user_session(c, login="cv-bundle@x.com")
    _set_quota(uid, 10 ** 8)

    from kfb.fixture_fl import build_synthetic_kfbf
    src = build_synthetic_kfbf(tmp_path / "fl.kfbf")
    job = _kfb_job(uid, src, "fl.kfbf")
    jid = job["id"]
    assert conversion_worker.run_once(upload_dir=str(app_mod.UPLOAD_DIR))
    job = conversion_store.get_job(jid)
    assert job["state"] == "ready"
    obj = slide_storage.bundle_dir(job["slide_id"], root=app_mod.UPLOAD_DIR)
    import json
    manifest = json.loads((obj / "manifest.json").read_text())
    # 唯一入口 data.tif；全成员（含 kfb 转换 manifest 与 associated 伴侣）
    assert manifest["entry"] == "data.tif"
    paths = {f["path"] for f in manifest["files"]}
    assert "data.tif" in paths
    assert "data.tif.manifest.json" in paths
    assert any(p.startswith("data.tif.associated/") for p in paths)
    for f in manifest["files"]:
        assert (obj / f["path"]).stat().st_size == f["size"]
    # accounted_bytes = 包内全部成员字节合计（R-12 删除结算口径）
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert desc.accounted_bytes == sum(
        (obj / f["path"]).stat().st_size for f in manifest["files"])
    # 配额口径（现状语义）：源字节（上传任务 finish_commit）+ 产物字节
    # （转换结算，一次）——产物侧恰为 accounted_bytes
    assert _quota_bytes(uid) == int(desc.accounted_bytes) + src.stat().st_size
    # 无平铺产物/源
    assert not (app_mod.UPLOAD_DIR / "fl.ome.tif").exists()
    assert not (app_mod.UPLOAD_DIR / "fl.kfbf").exists()
    # 多通道语义保留
    s = slide_io.open_slide(str(obj / "data.tif"))
    try:
        assert getattr(s, "channel_count", 0) == 2
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- #
# 6. review 门禁回归（F1/F2）：预占失效（请求路径 + 恢复路径）连带收口
#    转换任务——作废 job + 产物资产 failed（ready 则撤包+退款）+ 不死循环
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_reservation_lease_expired_at_commit_still_settles：V2 commit 端点
#   的租约过期结算；原 test_recovery_reservation_expired_cancels_job_no_
#   livelock / test_recovery_withdraws_settled_product_and_refunds：依赖随
#   端点一并删除的 app._upload_legacy_recover_stale 恢复扫描入口。）
