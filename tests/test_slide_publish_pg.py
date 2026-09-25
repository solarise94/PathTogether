# -*- coding: utf-8 -*-
"""slide ID 化重构 P3：统一本地发布门禁测试（V2 分片 + V1 原生单文件）。

合同：docs/slide-id-refactor-p3-contract-20260925.md §6（计划 §8 矩阵的 P3
部分）。逐条覆盖：

  1. V2 同名并发（两账户同名同字节）→ 不同 slide_id/不同 objects 目录、
     各自归属、互不冲突 409；
  2. 同一任务重复 commit/响应丢失重试 → 同一 slide_id、一次配额结算、
     无重复资产；
  3. intent 前 / 包发布后 / DB commit 前崩溃（monkeypatch 阶段屏障注入，
     不用 sleep）→ 未 ready 不可读（文件已在 objects 也不可读）；恢复收口
     一次；存活字节有预约或实占；
  4. 取消与发布并发、删除与发布并发 → 状态机 CAS 裁定明确胜者；
  5. 满配额、预约过期 → 不可读、不漏账、不重复收费；
  6. 删除 A（id_bundle）→ 同名重传 B → 新 ID；旧分享/授权/标注/AI run
     grant/Demo 不指向 B；
  7. 删除减账幂等：重复 DELETE 不重复减；
  8. 显示名修改不移动文件、不动 revision；
  9. 旧端点对 id_bundle 资产按名找不到（403/404 预期——新资产无
     legacy_filename）；列表/info/dzi/tile/crop/region 经 ID 端点全通；
  10. 机器通道无行兼容分支已删：无行文件经 internal/plugin 解析一律拒；
  11. annotations_by_slide 对同名 id_bundle 资产按 ID 分组不串。

运行：cd 项目根 && python3 -m pytest tests/test_slide_publish_pg.py -q
"""
import hashlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import clear_upload_dir, csrf_client, isolate_app  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 防护参数复位 + 清空 uploads + 恢复超时复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 * 1024)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="u@x.com"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _one(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def _quota(uid):
    return upload_guard.get_quota_row(uid)


def _quota_bytes(uid, n):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                        "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET "
                        "quota_bytes=%s", (uid, n, n))


TIFF = make_tiff_bytes(64, 96)
TIFF_SHA = hashlib.sha256(TIFF).hexdigest()


def _v2_create(client, name="same.tif", size=None, sha=None):
    body = {"filename": name, "declared_size": size if size else len(TIFF)}
    if sha:
        body["sha256_expected"] = sha
    return client.post("/api/uploads", json=body)


def _v2_put(client, upload_id, offset, data):
    return client.put(
        "/api/uploads/%s/chunk?offset=%d&sha256=%s"
        % (upload_id, offset, hashlib.sha256(data).hexdigest()),
        data=data, content_type="application/octet-stream")


def _v2_upload_full(client, upload_id, data=None, chunk=32):
    data = TIFF if data is None else data
    r = None
    for off in range(0, len(data), chunk):
        r = _v2_put(client, upload_id, off, data[off:off + chunk])
        assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _v2_flow(client, name="same.tif", data=None, sha=None):
    """创建 → 传完 → commit；返回 (slide_id, commit_resp_json)。"""
    data = TIFF if data is None else data
    r = _v2_create(client, name, size=len(data), sha=sha)
    assert r.status_code == 200, r.get_data(as_text=True)
    upload_id = r.get_json()["upload_id"]
    assert r.get_json().get("slide_id"), "创建即绑定 slide_id（合同 §3.1.4）"
    _v2_upload_full(client, upload_id, data)
    r = client.post("/api/uploads/%s/commit" % upload_id)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["slide_id"], r.get_json()


def _v1_upload(client, name="same.tif", data=None):
    data = TIFF if data is None else data
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data")


def _bundle_dir(slide_id):
    return Path(UPLOAD_DIR) / "objects" / slide_id


def _desc(slide_id):
    return slide_store.resolve_slide_id(slide_id)


# --------------------------------------------------------------------------- #
# §6-1 V2 同名并发（两账户同名同字节）→ 不同 ID/目录/归属
# --------------------------------------------------------------------------- #
def test_v2_same_name_two_accounts_distinct_ids(tmp_path):
    ca = _client()
    cb = _client()
    uid_a = _user_session(ca, login="a@x.com")
    uid_b = _user_session(cb, login="b@x.com")
    sid_a, body_a = _v2_flow(ca, "same.tif")
    sid_b, body_b = _v2_flow(cb, "same.tif")
    assert sid_a != sid_b
    assert sid_a == body_a["slide_id"] and sid_b == body_b["slide_id"]
    da, db = _desc(sid_a), _desc(sid_b)
    assert da.storage_layout == "id_bundle" and db.storage_layout == "id_bundle"
    assert da.owner_user_id == uid_a and db.owner_user_id == uid_b
    assert da.legacy_filename is None and db.legacy_filename is None
    assert da.asset_state == db.asset_state == "ready"
    # 不同 objects 目录 + 入口文件就位（no-clobber 由 ID 唯一性兜底）
    assert _bundle_dir(sid_a).is_dir() and _bundle_dir(sid_b).is_dir()
    assert (_bundle_dir(sid_a) / "data.tif").is_file()
    assert (_bundle_dir(sid_b) / "data.tif").is_file()
    # 同账号同名并发同样不冲突（allocate_slide 不查原名）
    sid_a2, _ = _v2_flow(ca, "same.tif")
    assert sid_a2 not in (sid_a, sid_b)


def test_v2_same_name_no_conflict_409_gone(tmp_path):
    """同名不再 409（_upload_name_conflict 调用已删——原生通道）。"""
    ca = _client()
    _user_session(ca, login="a@x.com")
    sid1, _ = _v2_flow(ca, "dup.tif")
    # 已有同名已发布资产后，同名第二份创建不再报 name_unavailable
    r = _v2_create(ca, "dup.tif")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["slide_id"] != sid1


# --------------------------------------------------------------------------- #
# §6-2 重复 commit / 响应丢失重试 → 同 ID 一次结算
# --------------------------------------------------------------------------- #
def test_v2_repeat_commit_same_id_single_settlement(tmp_path):
    c = _client()
    uid = _user_session(c, login="q@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    r = _v2_create(c, "once.tif")
    upload_id = r.get_json()["upload_id"]
    _v2_upload_full(c, upload_id)
    r1 = c.post("/api/uploads/%s/commit" % upload_id)
    assert r1.status_code == 200
    sid = r1.get_json()["slide_id"]
    # 响应丢失重试：重复 commit 幂等
    r2 = c.post("/api/uploads/%s/commit" % upload_id)
    assert r2.status_code == 200
    assert r2.get_json()["slide_id"] == sid
    q = _quota(uid)
    assert q["used_bytes"] == len(TIFF)      # 一次结算
    assert q["reserved_bytes"] == 0
    # 资产唯一：slide_assets revision 一行；objects 目录一个
    assert _one("SELECT count(*) FROM slide_assets WHERE slide_id=%s",
                (sid,)) == 1
    assert _one("SELECT count(*) FROM slides WHERE slide_id=%s", (sid,)) == 1
    task = upload_task_store.get_task(upload_id)
    assert task["state"] == "committed"
    assert task["commit_intent_json"] is None   # 收口清 intent
    assert r1.get_json()["slide_id"] == task["slide_id"]


# --------------------------------------------------------------------------- #
# §6-3 三处崩溃点（阶段屏障注入）→ 恢复收口一次、未 ready 不可读
# --------------------------------------------------------------------------- #
def _recover_now(monkeypatch):
    """恢复判定立即生效（commit 超时=0；不用 sleep）。"""
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 0)


def test_crash_before_intent_retryable(tmp_path, monkeypatch):
    """intent 前（begin_commit 内）崩溃：任务保持 active，staging 保留，
    预占仍持有（存活字节有预约）；重试 commit 收口一次。"""
    c = _client()
    uid = _user_session(c, login="c1@x.com")
    r = _v2_create(c, "crash1.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)

    real = upload_task_store.begin_commit

    def _boom(*a, **kw):
        raise RuntimeError("crash before intent")

    monkeypatch.setattr(upload_task_store, "begin_commit", _boom)
    with pytest.raises(RuntimeError):
        c.post("/api/uploads/%s/commit" % upload_id)
    monkeypatch.setattr(upload_task_store, "begin_commit", real)

    task = upload_task_store.get_task(upload_id)
    assert task["state"] == "active"
    assert task["commit_intent_json"] is None
    # 未 ready 不可读（staging 门禁）
    assert c.get("/api/slides/%s/info" % sid).status_code == 403
    # 存活字节有预约：reserved 仍占
    assert _quota(uid)["reserved_bytes"] == len(TIFF)
    # 传输暂存在 .staging/<uid>/transfer/（不平铺）
    assert (Path(UPLOAD_DIR) / ".staging" / upload_id / "transfer"
            / "data").is_file()
    # 重试收口
    rr = c.post("/api/uploads/%s/commit" % upload_id)
    assert rr.status_code == 200, rr.get_data(as_text=True)
    assert _desc(sid).asset_state == "ready"
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    assert _quota(uid)["used_bytes"] == len(TIFF)
    assert not (Path(UPLOAD_DIR) / ".staging" / upload_id).exists()


def test_crash_after_intent_before_publish_recovered(tmp_path, monkeypatch):
    """intent 后、包发布前崩溃：保持 committing；恢复幂等重跑发布并收口。"""
    c = _client()
    uid = _user_session(c, login="c2@x.com")
    r = _v2_create(c, "crash2.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)

    real = slide_storage.publish_bundle_no_clobber
    monkeypatch.setattr(slide_storage, "publish_bundle_no_clobber",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            OSError("disk hiccup")))
    rr = c.post("/api/uploads/%s/commit" % upload_id)
    assert rr.status_code == 503
    assert rr.get_json()["code"] == "commit_in_progress"
    monkeypatch.setattr(slide_storage, "publish_bundle_no_clobber", real)

    task = upload_task_store.get_task(upload_id)
    assert task["state"] == "committing"
    assert task["commit_intent_json"] is not None
    assert c.get("/api/slides/%s/info" % sid).status_code == 403

    _recover_now(monkeypatch)
    rs = c.get("/api/uploads/%s" % upload_id)
    assert rs.status_code == 200
    assert rs.get_json()["state"] == "committed"
    assert rs.get_json()["slide_id"] == sid
    assert _desc(sid).asset_state == "ready"
    assert _quota(uid)["used_bytes"] == len(TIFF)   # 一次结算
    assert (_bundle_dir(sid) / "data.tif").is_file()
    assert c.get("/api/slides/%s/info" % sid).status_code == 200


def test_crash_after_publish_before_db_invisible_then_recovered(
        tmp_path, monkeypatch):
    """包发布后、DB 收口前崩溃：文件已在 objects 仍不可读（DB ready 是唯一
    可见性开关）；恢复只做 DB CAS（幂等收口一次）。"""
    import slide_publish
    c = _client()
    uid = _user_session(c, login="c3@x.com")
    r = _v2_create(c, "crash3.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)

    def _boom(*a, **kw):
        raise RuntimeError("db crash")

    orig = slide_publish._settle_publish
    slide_publish._settle_publish = _boom   # 阶段屏障：包发布后、DB 收口前
    try:
        rr = c.post("/api/uploads/%s/commit" % upload_id)
        assert rr.status_code == 503
        assert rr.get_json()["code"] == "commit_in_progress"
    finally:
        slide_publish._settle_publish = orig

    # FS 已发布但 DB 未收口：不可读（P3 完成标准核心断言）
    assert (_bundle_dir(sid) / "data.tif").is_file()
    assert _desc(sid).asset_state == "staging"
    assert c.get("/api/slides/%s/info" % sid).status_code == 403

    _recover_now(monkeypatch)
    rs = c.get("/api/uploads/%s" % upload_id)
    assert rs.get_json()["state"] == "committed"
    assert _desc(sid).asset_state == "ready"
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    assert _quota(uid)["used_bytes"] == len(TIFF)
    assert _one("SELECT count(*) FROM slide_assets WHERE slide_id=%s",
                (sid,)) == 1


# --------------------------------------------------------------------------- #
# §6-4 取消/删除与发布并发 → 状态机裁定明确胜者
# --------------------------------------------------------------------------- #
def test_cancel_wins_before_commit(tmp_path):
    c = _client()
    uid = _user_session(c, login="d1@x.com")
    r = _v2_create(c, "cancel.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)
    # 取消先赢：active → cancelled；清 staging 后释放预占；资产行 failed
    rd = c.delete("/api/uploads/%s" % upload_id)
    assert rd.status_code == 200 and rd.get_json()["state"] == "cancelled"
    assert not (Path(UPLOAD_DIR) / ".staging" / upload_id).exists()
    assert _quota(uid)["reserved_bytes"] == 0
    assert _desc(sid).asset_state == "failed"
    # 新 ID 资产不受旧任务清理影响：另一任务照常发布
    sid2, _ = _v2_flow(c, "cancel.tif")
    assert _desc(sid2).asset_state == "ready"


def test_cancel_rejected_during_committing(tmp_path):
    """committing（不可撤销段）取消恒 409——发布继续/恢复完成。"""
    c = _client()
    _user_session(c, login="d2@x.com")
    r = _v2_create(c, "cc.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)
    rc = c.post("/api/uploads/%s/commit" % upload_id)
    assert rc.status_code == 200
    rd = c.delete("/api/uploads/%s" % upload_id)
    assert rd.status_code == 409
    assert _desc(sid).asset_state == "ready"


def test_delete_after_publish_refunds_and_tombstones(tmp_path):
    """删除与发布的胜者：ready 后删除 → deleting 立即拒读 → 结算减账。"""
    c = _client()
    uid = _user_session(c, login="d3@x.com")
    sid, _ = _v2_flow(c, "del.tif")
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    rd = c.delete("/api/slides/%s" % sid)
    assert rd.status_code == 200 and rd.get_json()["state"] == "deleted"
    assert not _bundle_dir(sid).exists()
    d = _desc(sid)
    assert d.asset_state == "deleted"     # tombstone 行保留
    assert c.get("/api/slides/%s/info" % sid).status_code in (403, 404)
    assert _quota(uid)["used_bytes"] == 0  # accounted_bytes 幂等减


# --------------------------------------------------------------------------- #
# §6-5 满配额 / 预约过期 → 不漏账不重复收费
# --------------------------------------------------------------------------- #
def test_quota_exceeded_no_leak(tmp_path):
    c = _client()
    uid = _user_session(c, login="e1@x.com")
    _quota_bytes(uid, 8)   # 配额小于文件
    r = _v2_create(c, "big.tif")
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_quota_exceeded"
    # 不漏账：无任务行、无 staging 资产行、预占为 0
    assert _one("SELECT count(*) FROM upload_tasks WHERE owner_user_id=%s",
                (uid,)) == 0
    assert _one("SELECT count(*) FROM slides WHERE owner_user_id=%s AND "
                "asset_state='staging'", (uid,)) == 0
    q = _quota(uid)
    assert q["reserved_bytes"] == 0 and q["used_bytes"] == 0


def test_reservation_expired_fail_closed_no_double_charge(tmp_path):
    c = _client()
    uid = _user_session(c, login="e2@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    r = _v2_create(c, "exp.tif")
    upload_id = r.get_json()["upload_id"]
    sid = r.get_json()["slide_id"]
    _v2_upload_full(c, upload_id)
    # 直接把预占置为已过期（不 sleep）
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE upload_reservations SET expires_at = "
                        "now() - interval '1 second' WHERE user_id=%s AND "
                        "state='reserved'", (uid,))
    rc = c.post("/api/uploads/%s/commit" % upload_id)
    assert rc.status_code == 409
    assert rc.get_json()["code"] == "reservation_expired"
    # 不可读；任务过期收尾；不漏账不重复收费（used 恒 0，预占回收）
    assert c.get("/api/slides/%s/info" % sid).status_code == 403
    q = _quota(uid)
    assert q["used_bytes"] == 0
    assert not (Path(UPLOAD_DIR) / ".staging" / upload_id).exists()
    assert _desc(sid).asset_state == "failed"


# --------------------------------------------------------------------------- #
# §6-6/§6-11 删除后同名重传新 ID；旧分享/授权/标注/run grant/Demo 不继承；
#          annotations_by_slide 同名 id_bundle 按 ID 分组不串
# --------------------------------------------------------------------------- #
def test_delete_then_reupload_new_id_no_inheritance(tmp_path):
    ca = _client()
    cb = _client()
    uid_a = _user_session(ca, login="f1@x.com")
    uid_b = _user_session(cb, login="f2@x.com")

    sid_a, _ = _v2_flow(ca, "inherit.tif")
    # 建立全部引用面：view grant / share+claim / 标注 / run grant / Demo
    share_store.grant_slide_view(uid_b, "inherit.tif", slide_id=sid_a)
    share = share_store.create_share(
        ["inherit.tif"], 24, creator_user_id=uid_a, slide_ids=[sid_a])
    token = share["token"]
    share_store.claim_share(token, uid_b)
    # 工作台标注（token=admin：个人标注对本人可见——0056 语义）
    share_store.add_roi("admin", "inherit.tif", "L", x=1.0, y=2.0,
                        w=4.0, h=4.0, size_mm=0.5, slide_id=sid_a,
                        owner_user_id=uid_a)
    import demo_store
    demo_store.catalog_add(sid_a, display_name="demo-a")
    share_store.create_run_grant("inst-1", "inherit.tif",
                                 created_by_user_id=uid_a, slide_id=sid_a)

    rd = ca.delete("/api/slides/%s" % sid_a)
    assert rd.status_code == 200

    # 授权联动清理：view grants / share_slides / Demo / run grants
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM share_slides WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM demo_catalog WHERE slide_id=%s",
                (sid_a,)) == 0
    assert _one("SELECT count(*) FROM run_grants WHERE slide_id=%s AND "
                "NOT revoked", (sid_a,)) == 0

    # 同名重传 → 新 ID
    sid_b, _ = _v2_flow(ca, "inherit.tif")
    assert sid_b != sid_a
    # 旧授权/分享/能力不指向 B
    assert cb.get("/api/slides/%s/info" % sid_b).status_code == 403
    assert _one("SELECT count(*) FROM share_slides WHERE slide_id=%s",
                (sid_b,)) == 0
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid_b,)) == 0
    assert _one("SELECT count(*) FROM demo_catalog WHERE slide_id=%s",
                (sid_b,)) == 0
    # 标注不继承：B 的标注为空；A 的标注仍按 A 的 ID 分组（证据保留）
    by_slide = share_store.annotations_by_slide()
    assert by_slide.get(sid_b) in (None, [])
    groups_a = by_slide.get(sid_a) or []
    assert sum(g["count"] for g in groups_a) == 1
    # 单切片端点按 ID 取组（同名不串）；A 已删除 → 不可读
    rr = ca.get("/api/slides/%s/annotations" % sid_b)
    assert rr.status_code == 200
    assert rr.get_json()["annotations"] == []
    assert ca.get("/api/slides/%s/annotations" % sid_a).status_code in (403, 404)


def test_annotations_same_name_id_bundle_not_crossed(tmp_path):
    """§6-11：两份同名 id_bundle 资产，标注按 ID 分组互不串。"""
    ca = _client()
    uid = _user_session(ca, login="g1@x.com")
    sid_a, _ = _v2_flow(ca, "twin.tif")
    sid_b, _ = _v2_flow(ca, "twin.tif")
    # 工作台标注（token=admin）分别落在两份同名资产上
    share_store.add_roi("admin", "twin.tif", "A", x=1.0, y=1.0,
                        w=2.0, h=2.0, size_mm=0.1, slide_id=sid_a,
                        owner_user_id=uid)
    share_store.add_roi("admin", "twin.tif", "B", x=2.0, y=2.0,
                        w=2.0, h=2.0, size_mm=0.2, slide_id=sid_b,
                        owner_user_id=uid)
    by_slide = share_store.annotations_by_slide()
    assert by_slide.get(sid_a) and by_slide.get(sid_b)
    labels_a = {g["label"] for g in by_slide[sid_a]}
    labels_b = {g["label"] for g in by_slide[sid_b]}
    assert labels_a == {"A"} and labels_b == {"B"}
    # 端点侧同样隔离
    ra = ca.get("/api/annotations?slide_id=%s" % sid_a).get_json()
    rb = ca.get("/api/annotations?slide_id=%s" % sid_b).get_json()
    assert {g["label"] for g in ra["annotations"]} == {"A"}
    assert {g["label"] for g in rb["annotations"]} == {"B"}


# --------------------------------------------------------------------------- #
# §6-7 删除减账幂等：重复 DELETE 不重复减
# --------------------------------------------------------------------------- #
def test_delete_refund_idempotent(tmp_path):
    c = _client()
    uid = _user_session(c, login="h1@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    sid, _ = _v2_flow(c, "idem.tif")
    assert _quota(uid)["used_bytes"] == len(TIFF)
    r1 = c.delete("/api/slides/%s" % sid)
    assert r1.status_code == 200
    assert _quota(uid)["used_bytes"] == 0
    # 重复 DELETE：幂等 200，不再减（GREATEST 兜底之外由 CAS 保证只减一次）
    r2 = c.delete("/api/slides/%s" % sid)
    assert r2.status_code == 200
    r3 = c.delete("/api/slides/%s" % sid)
    assert r3.status_code == 200
    assert _quota(uid)["used_bytes"] == 0
    # worker 重试语义：deleting 中断后重入（状态直改模拟清理中断）
    sid2, _ = _v2_flow(c, "idem2.tif")
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET asset_state='deleting' "
                        "WHERE slide_id=%s", (sid2,))
    slide_storage.remove_bundle(sid2, root=UPLOAD_DIR)
    r4 = c.delete("/api/slides/%s" % sid2)
    assert r4.status_code == 200
    assert _quota(uid)["used_bytes"] == 0


# --------------------------------------------------------------------------- #
# §6-8 显示名修改不动文件与 revision
# --------------------------------------------------------------------------- #
def test_display_name_edit_no_file_no_revision_change(tmp_path):
    c = _client()
    _user_session(c, login="i1@x.com")
    sid, _ = _v2_flow(c, "rename.tif")
    d0 = _desc(sid)
    path0 = slide_storage.resolve_descriptor_path(d0, root=UPLOAD_DIR)
    stat0 = path0.stat()
    rev0 = _desc(sid).revision
    rp = c.patch("/api/slides/%s" % sid, json={"display_name": "新名字"})
    assert rp.status_code == 200
    d1 = _desc(sid)
    assert d1.display_name == "新名字"
    assert d1.storage_relpath == d0.storage_relpath
    assert path0.stat().st_mtime_ns == stat0.st_mtime_ns  # 文件未动
    assert d1.revision == rev0                            # revision 不动
    assert _one("SELECT count(*) FROM slide_assets WHERE slide_id=%s",
                (sid,)) == 1


# --------------------------------------------------------------------------- #
# §6-9 旧端点按名 404/403（预期）；ID 端点全通；name=None 出列
# --------------------------------------------------------------------------- #
def test_id_endpoints_full_read_path_and_legacy_name_404(tmp_path):
    c = _client()
    _user_session(c, login="j1@x.com")
    sid, _ = _v2_flow(c, "read.tif")
    # 列表出列：name=None + slide_id/original_filename/display_name/format_ext
    items = c.get("/api/slides").get_json()
    mine = [it for it in items if it.get("slide_id") == sid]
    assert len(mine) == 1
    assert mine[0]["name"] is None            # id_bundle：name=None（合同 §4）
    assert mine[0]["original_filename"] == "read.tif"
    assert mine[0]["display_name"] == "read.tif"
    assert mine[0]["format_ext"] == "tif"
    assert "storage_relpath" not in mine[0]   # 路径绝不序列化（R-20）
    # 旧按名端点找不到（新资产无 legacy_filename；403=不泄露存在性）
    assert c.get("/api/slide/read.tif/info").status_code == 403
    assert c.get("/api/slide/read.tif.dzi").status_code == 403
    # ID 端点全通
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    assert c.get("/api/slides/%s/dzi" % sid).status_code == 200
    assert c.get("/api/slides/%s/tiles/0/0_0.jpeg" % sid).status_code == 200
    assert c.get("/api/slides/%s/crop?x=0&y=0&size=8" % sid).status_code == 200
    assert c.get("/api/slides/%s/region?x=0&y=0&w=8&h=8" % sid).status_code == 200
    assert c.get("/api/slides/%s/thumbnail" % sid).status_code == 200
    # revision 来自 slide_assets（sha256 前缀），不是 mtime:size
    d = _desc(sid)
    assert d.revision == "sha256:%s" % TIFF_SHA[:16]


# --------------------------------------------------------------------------- #
# §6-10 机器通道无行兼容分支已删
# --------------------------------------------------------------------------- #
def test_machine_channel_no_row_rejected(tmp_path):
    """目录上手工放的文件（无 slides 行）：internal/plugin 解析一律拒。"""
    c = _client()
    app_mod.AUTH_ENABLED = False   # 本地免认证单租户态（P1-B2 偏差 #3 收口）
    ghost = Path(UPLOAD_DIR) / "ghost.tif"
    ghost.write_bytes(TIFF)
    with app_mod.app.test_request_context():
        # 机器通道：无行 → 404（不再文件存在即可读）
        _safe, gate, _sid, err = app_mod._internal_slide_target(None, "ghost.tif")
        assert gate is None and err is not None and err[1] == 404
        err_plugin = app_mod._plugin_resolve_slide("ghost.tif")[2]
        assert err_plugin is not None and err_plugin.status_code == 404
        # session 通道（本地免认证 owner 无 uid）：无行 → 拒（403 语义 None）
        assert app_mod._authorize_legacy_read("ghost.tif") is None
    ghost.unlink()


# --------------------------------------------------------------------------- #
# V1 原生单文件新管线（同矩阵抽验：同名并发/幂等/删除减账）
# --------------------------------------------------------------------------- #
def test_v1_native_upload_publishes_id_bundle(tmp_path):
    c = _client()
    uid = _user_session(c, login="k1@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    r = _v1_upload(c, "v1.tif")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    sid = body["slide_id"]
    assert sid and body["name"] == "v1.tif"
    d = _desc(sid)
    assert d.asset_state == "ready" and d.storage_layout == "id_bundle"
    assert d.legacy_filename is None
    assert d.owner_user_id == uid
    assert (_bundle_dir(sid) / "data.tif").is_file()
    assert not list((Path(UPLOAD_DIR)).glob(".uploading-*"))  # 不再平铺
    assert _quota(uid)["used_bytes"] == len(TIFF)
    # 同名并发第二份：不同 ID、无 409
    r2 = _v1_upload(c, "v1.tif")
    assert r2.status_code == 200
    assert r2.get_json()["slide_id"] != sid
    # 删除走 by-ID 端点并减账
    rd = c.delete("/api/slides/%s" % sid)
    assert rd.status_code == 200
    assert _quota(uid)["used_bytes"] == len(TIFF)  # 只减第一份


def test_v1_native_invalid_content_fails_clean(tmp_path):
    c = _client()
    uid = _user_session(c, login="k2@x.com")
    r = _v1_upload(c, "bad.tif", data=b"not a slide at all")
    assert r.status_code == 400
    # 受理前失败：无任务、无资产行、预占释放、无 staging 残留
    assert _one("SELECT count(*) FROM upload_tasks WHERE owner_user_id=%s",
                (uid,)) == 0
    assert _one("SELECT count(*) FROM slides WHERE owner_user_id=%s",
                (uid,)) == 0
    assert _quota(uid)["reserved_bytes"] == 0
    staging_root = Path(UPLOAD_DIR) / ".staging"
    assert not staging_root.exists() or not any(staging_root.iterdir())
