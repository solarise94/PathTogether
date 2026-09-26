# -*- coding: utf-8 -*-
"""P6 第二段：运行时 legacy 物理读取退役的合同测试。

合同：docs/slide-id-refactor-p6-contract-20260925.md §3/§4（运行时退役段）。
覆盖四组不变量：

  1. legacy 布局资产在运行时**不可读**（403/404 按存在性不泄露口径）：
     名通道（/api/slide/<name>/*）、ID 通道（/api/slides/<sid>/*）、分享
     通道（/s/<token>/…）、机器通道（internal/plugin）全部拒绝；列表不出列
     （authorize_read/visible_ready_slide_ids 的 layout 门禁）。
  2. **冻结别名**固定 ID 查找仍工作：resolve_legacy_alias 照常解析（书签
     兼容）；**已迁移行**（id_bundle + 保留 legacy_filename）按别名解析到
     行、按 ID 读全通、分享 token 按 ID 成员照常、成员清单路径不再指旧位。
  3. 升级窗口分支拆除后的恢复语义：committing 旧形态 → fail-closed 保持
     committing（新链路恢复回归见 test_upload_v2 / test_slide_publish_pg /
     test_kfb_upload 的既有用例）。
  4. ``.uploading-*.lock`` sidecar 收进任务 staging 目录：锁在
     ``.staging/<upload_id>/chunk.lock`` 生效，UPLOAD_DIR 根**无任何**
     ``.uploading-*`` 残留（含锁与 .part）。

运行：cd 项目根 && python3 -m pytest tests/test_p6_legacy_runtime_retirement.py -q
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
import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import clear_upload_dir, csrf_client, isolate_app  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
TIFF = make_tiff_bytes(64, 96)
TIFF_SHA = hashlib.sha256(TIFF).hexdigest()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    # 本地免认证态需配置 owner（不允许空 owner 自动认领）
    share_store.set_owner_user_id(
        user_store.create_user("p6-local-owner@x.com", "p6localpass12345",
                               role="user")["user_id"])
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _exec(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _seed_legacy_row(slide_id, legacy_name, owner, *, public=False):
    """播种 legacy 布局行（ready + 平铺文件）——退役后的「待迁移」形态。"""
    (Path(UPLOAD_DIR) / legacy_name).write_bytes(TIFF)
    _exec(
        "INSERT INTO slides (slide_id, legacy_filename, owner_user_id, "
        "asset_state, storage_layout, public, accounted_bytes, "
        "original_filename, format_ext) "
        "VALUES (%s,%s,%s,'ready','legacy',%s,%s,%s,'tif')",
        (slide_id, legacy_name, owner, public, len(TIFF), legacy_name))


def _v2_publish_migrated(client, name, alias):
    """V2 上传发布 id_bundle 资产后，模拟 P6 迁移的冻结别名（行保留
    legacy_filename、布局 id_bundle）——「已迁移行」形态。"""
    r = client.post("/api/uploads", json={"filename": name,
                                          "declared_size": len(TIFF)})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["upload_id"]
    data = TIFF
    for off in range(0, len(data), 4096):
        rr = client.put(
            "/api/uploads/%s/chunk?offset=%d&sha256=%s"
            % (uid, off, hashlib.sha256(data[off:off + 4096]).hexdigest()),
            data=data[off:off + 4096],
            content_type="application/octet-stream")
        assert rr.status_code == 200, rr.get_data(as_text=True)
    r = client.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    sid = r.get_json()["slide_id"]
    _exec("UPDATE slides SET legacy_filename=%s WHERE slide_id=%s", (alias, sid))
    return sid


# =========================================================================== #
# 1. legacy 布局资产运行时不可读（不泄露存在性）
# =========================================================================== #
def test_legacy_layout_unreadable_all_channels():
    c = _client()
    owner = user_store.create_user("p6own@x.com", "pass1234pass1234",
                                   role="user")["user_id"]
    _seed_legacy_row("sld_p6_legacy01", "old-flat.svs", owner, public=True)

    # 名通道：legacy 名与不存在的名同 403（不泄露存在性）
    assert c.get("/api/slide/old-flat.svs/info").status_code == 403
    assert c.get("/api/slide/no-such.svs/info").status_code == 403
    assert c.get("/api/slide/old-flat.svs.dzi").status_code == 403
    # 列表不出列（DB 层 layout 门禁 + resolver fail-closed）
    items = c.get("/api/slides").get_json()
    assert all(it.get("slide_id") != "sld_p6_legacy01" for it in items)
    assert all(it.get("name") != "old-flat.svs" for it in items)
    # ID 通道：行存在（ready+public）但 legacy 布局 → 403 同无权口径
    assert c.get("/api/slides/sld_p6_legacy01/info").status_code == 403
    assert c.get("/api/slides/sld_p6_legacy01/tiles/0/0_0.jpeg").status_code == 403
    # 机器通道：legacy 名（有行）与无行同 404
    with app_mod.app.test_request_context():
        _safe, gate, _sid, err = app_mod._internal_slide_target(
            None, "old-flat.svs")
        assert gate is None and err is not None and err[1] == 404
        err_plugin = app_mod._plugin_resolve_slide("old-flat.svs")[2]
        assert err_plugin is not None and err_plugin.status_code == 404
        assert app_mod._authorize_legacy_read("old-flat.svs") is None


def test_legacy_layout_share_member_denied_and_not_leaked():
    """分享成员含 legacy 布局行：token 页/按 ID 读 403；成员清单 exists=False
    （不泄露存在性——与文件缺失同形）。"""
    c = _client()
    owner = user_store.create_user("p6sown@x.com", "pass1234pass1234",
                                   role="user")["user_id"]
    _seed_legacy_row("sld_p6_legacy02", "share-old.svs", owner)
    share = share_store.create_share(
        ["share-old.svs"], 24, creator_user_id=owner,
        slide_ids=["sld_p6_legacy02"])
    token = share["token"]
    sc = _share_client()
    # 成员清单：legacy 行不可读（exists=False，路径不解析——不 stat 平铺文件）
    items = sc.get("/s/%s/api/slides" % token).get_json()
    assert len(items) == 1
    assert items[0]["slide_id"] == "sld_p6_legacy02"
    assert items[0]["exists"] is False
    # 按 ID 读 / 按名读：403（与无此成员同口径）
    assert sc.get("/s/%s/api/slides/sld_p6_legacy02/info" % token).status_code \
        == 403
    assert sc.get("/s/%s/api/slide/share-old.svs/info" % token).status_code == 403
    assert sc.get("/s/%s/api/slides/sld_p6_legacy02/tiles/0/0_0.jpeg"
                  % token).status_code == 403


# =========================================================================== #
# 2. 冻结别名固定 ID 查找仍工作（已迁移行照常）
# =========================================================================== #
def test_frozen_alias_and_migrated_row_still_readable_by_id():
    c = _client()
    owner = user_store.create_user("p6mig@x.com", "pass1234pass1234",
                                   role="user")["user_id"]
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = owner
        sess["role"] = "user"
        sess["auth_version"] = 1
    sid = _v2_publish_migrated(c, "migrated.tif", "migrated-alias.svs")
    # 迁移源冻结保留在根（不删源），但读路径只认 objects/<sid>/
    assert (Path(UPLOAD_DIR) / "migrated.tif").exists() is False
    # 冻结别名解析照常（固定 ID 查找——书签兼容）
    desc = slide_store.resolve_legacy_alias("migrated-alias.svs")
    assert desc is not None and desc.slide_id == sid
    assert desc.storage_layout == slide_store.StorageLayout.ID_BUNDLE
    # 按 ID 读全通（含按别名端点：别名只是解析键，物理读走 resolver）
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    assert c.get("/api/slide/migrated-alias.svs/info").status_code == 200
    assert c.get("/api/slides/%s/tiles/0/0_0.jpeg" % sid).status_code == 200
    # 列表出列（name=冻结别名）
    items = c.get("/api/slides").get_json()
    mine = [it for it in items if it.get("slide_id") == sid]
    assert len(mine) == 1 and mine[0]["name"] == "migrated-alias.svs"


def test_migrated_row_share_token_by_id_member_works():
    """分享 token 按 ID 成员照常（P6-1 演练「授权/引用不串」的运行时面）。"""
    c = _client()
    owner = user_store.create_user("p6sh@x.com", "pass1234pass1234",
                                   role="user")["user_id"]
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = owner
        sess["role"] = "user"
        sess["auth_version"] = 1
    sid = _v2_publish_migrated(c, "sharedmig.tif", "shared-alias.svs")
    share = share_store.create_share(
        ["shared-alias.svs"], 24, creator_user_id=owner, slide_ids=[sid])
    token = share["token"]
    sc = _share_client()
    items = sc.get("/s/%s/api/slides" % token).get_json()
    assert len(items) == 1 and items[0]["slide_id"] == sid
    assert items[0]["exists"] is True  # 路径按 slide_id 解析（不再 stat 旧位）
    assert sc.get("/s/%s/api/slides/%s/info" % (token, sid)).status_code == 200
    assert sc.get("/s/%s/api/slide/shared-alias.svs/info"
                  % token).status_code == 200
    assert sc.get("/s/%s/api/slides/%s/tiles/0/0_0.jpeg"
                  % (token, sid)).status_code == 200
    assert sc.get("/s/%s/api/slides/%s/thumbnail" % (token, sid)).status_code \
        == 200


# =========================================================================== #
# 3. 升级窗口分支拆除：committing 旧形态 fail-closed（请求路径确定失败的
#    对应用例在 test_upload_v2.py；此处补恢复扫描入口的端到端形态）
# =========================================================================== #
def test_old_form_committing_survives_recovery_scan(monkeypatch):
    c = _client()
    r = c.post("/api/uploads", json={"filename": "oldrec.svs",
                                     "declared_size": 64})
    uid = r.get_json()["upload_id"]
    # 降级为升级窗口旧形态（无 slide_id / 无 intent / 无 v1_artifacts）
    _exec("UPDATE upload_tasks SET slide_id=NULL WHERE upload_id=%s", (uid,))
    data = b"o" * 64
    rr = c.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
               % (uid, hashlib.sha256(data).hexdigest()),
               data=data, content_type="application/octet-stream")
    assert rr.status_code == 200
    upload_task_store.begin_commit(uid)
    _exec("UPDATE upload_tasks SET commit_started_at=now() - interval '1 hour' "
          "WHERE upload_id=%s", (uid,))
    out = app_mod._upload_v2_maintain(upload_task_store.get_task(uid))
    assert out["state"] == "committing"  # fail-closed：不猜、不入账、不释放


# =========================================================================== #
# 4. .uploading-*.lock 收进任务 staging 目录（旧位零残留）
# =========================================================================== #
def test_chunk_lock_lives_in_staging_no_flat_residue():
    c = _client()
    r = c.post("/api/uploads", json={"filename": "lock.tif",
                                     "declared_size": len(TIFF)})
    uid = r.get_json()["upload_id"]
    rr = c.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
               % (uid, hashlib.sha256(TIFF).hexdigest()),
               data=TIFF, content_type="application/octet-stream")
    assert rr.status_code == 200
    # 锁在新位生效（任务 staging 内）；平铺旧位零产生（.part/.lock 都不在根）
    lock = slide_storage.staging_task_dir(uid, root=Path(UPLOAD_DIR)) \
        / "chunk.lock"
    assert lock.is_file()
    flat = [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")]
    assert flat == []
    part = app_mod._upload_v2_part_path(upload_task_store.get_task(uid))
    assert part.is_file() and part.parent.parent.parent.name == ".staging"
    # 收口（commit）后整树清理（含锁）；根目录仍无任何 .uploading-*
    rc = c.post("/api/uploads/%s/commit" % uid)
    assert rc.status_code == 200, rc.get_data(as_text=True)
    assert not slide_storage.staging_task_dir(
        uid, root=Path(UPLOAD_DIR)).exists()
    assert [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")] == []


def test_chunk_lock_cleanup_on_cancel():
    c = _client()
    r = c.post("/api/uploads", json={"filename": "lock2.tif",
                                     "declared_size": len(TIFF)})
    uid = r.get_json()["upload_id"]
    c.put("/api/uploads/%s/chunk?offset=0&sha256=%s"
          % (uid, hashlib.sha256(TIFF[:32]).hexdigest()),
          data=TIFF[:32], content_type="application/octet-stream")
    assert c.delete("/api/uploads/%s" % uid).status_code == 200
    assert not slide_storage.staging_task_dir(
        uid, root=Path(UPLOAD_DIR)).exists()
    assert [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")] == []


# =========================================================================== #
# 兼容壳删除（canonical_is_live / NameConflict）——模块面断言
# =========================================================================== #
def test_name_conflict_shells_removed():
    import inspect

    import baidu_ingest  # noqa: F401
    import conversion_store
    assert not hasattr(conversion_store, "canonical_is_live")
    assert not hasattr(conversion_store, "NameConflict")
    # baidu 磁盘级 O_EXCL 检查仍在（名占用仍可从磁盘层拒绝）
    src = inspect.getsource(baidu_ingest._ingest_convert)
    assert "source_dest.exists()" in src and "canon_path.exists()" in src


# =========================================================================== #
# _slide_revision：id_bundle 走 slide_assets；legacy 布局无 revision 消费者
# =========================================================================== #
def test_slide_revision_id_bundle_from_assets_legacy_empty():
    c = _client()
    owner = user_store.create_user("p6rev@x.com", "pass1234pass1234",
                                   role="user")["user_id"]
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = owner
        sess["role"] = "user"
        sess["auth_version"] = 1
    sid = _v2_publish_migrated(c, "rev.tif", None)
    desc = slide_store.resolve_slide_id(sid)
    assert app_mod._slide_revision(desc) == "sha256:%s" % TIFF_SHA[:16]
    # legacy 布局 descriptor：不可读 → revision 空串（无消费者）
    _seed_legacy_row("sld_p6_revlegacy", "rev-old.svs", owner)
    legacy_desc = slide_store.resolve_slide_id("sld_p6_revlegacy")
    assert app_mod._slide_revision(legacy_desc) == ""
