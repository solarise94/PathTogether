# -*- coding: utf-8 -*-
"""slide ID 化重构 P4-app：V1 ZIP/MRXS 统一发布门禁测试。

合同：docs/slide-id-refactor-p4-contract-20260925.md §2/§8（计划 §8 矩阵的
ZIP 行）。逐条覆盖：

  1. 多逻辑切片各得各 slide_id（upload_task_items (task_id,item_key) 绑定，
     响应 slide_ids 为真实绑定）；
  2. MRXS 伴侣目录同包原子发布（objects/<sid>/data.mrxs + data/…，manifest
     指定唯一入口；DB ready 前不可读）；
  3. 无法归组的成员整体 400 指名拒绝（孤儿目录——无同 stem 切片）；
  4. 入口打不开的 item 按 item 失败剔除（failures 证据），其余 item 照常
     发布、配额只按已发布字节结算；
  5. 崩溃恢复幂等：受理（intent+绑定）后、发布前崩溃 → 恢复扫描重发，
     同 item 复用原 slide_id，配额一次结算；
  6. 多任务同名 zip 并发：互不影响（独立 ID，无名称冲突面）。

运行：cd 项目根 && python3 -m pytest tests/test_zip_slide_id_pg.py -q
"""
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
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


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 防护复位 + 清空 uploads + 校验放行。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    import share_store as _ss
    import user_store as _us
    _ss.set_owner_user_id(
        _us.create_user("p4z-local-owner@x.com", "p4zlocalpass12345",
                        role="user")["user_id"])
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 * 1024)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=False):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="z@x.com"):
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


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, data in members:
            zf.writestr(fname, data)
    return buf.getvalue()


def _upload_zip(client, members, name="bundle.zip"):
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(_zip_bytes(members)), name)},
        content_type="multipart/form-data")


def _quota(uid):
    return upload_guard.get_quota_row(uid)


def _tasks(**kw):
    return upload_task_store.list_tasks(**kw)


TIFF_A = make_tiff_bytes(32, 32)
TIFF_B = make_tiff_bytes(48, 64)


# --------------------------------------------------------------------------- #
# 1. 多逻辑切片各得各 ID；slide_ids 是 upload_task_items 真实绑定
# --------------------------------------------------------------------------- #
def test_multi_items_each_get_own_slide_id():
    uid = _user_session(_client(auth=True), login="z-multi@x.com")
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    r = _upload_zip(c, [("a.tif", TIFF_A), ("b.tif", TIFF_B)])
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert set(body["extracted"]) == {"a.tif", "b.tif"}
    ids = body["slide_ids"]
    assert set(ids) == {"a.tif", "b.tif"}
    assert len(set(ids.values())) == 2  # 各得各 ID
    assert body["slide_id"] == ids["a.tif"]  # 非 mrxs：排序第一个为主
    # 绑定行 = 真实绑定源（(task_id,item_key) PK；slide_id 全局 UNIQUE）
    t = _tasks()[0]
    rows = {(x["item_key"], x["slide_id"])
            for x in upload_task_store.list_upload_task_items(t["upload_id"])}
    assert rows == {("a.tif", ids["a.tif"]), ("b.tif", ids["b.tif"])}
    for key, sid in ids.items():
        desc = slide_store.resolve_slide_id(sid)
        assert desc.asset_state == "ready"
        assert desc.owner_user_id == uid
        entry = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
        assert entry.is_file()
        assert entry.read_bytes() == (TIFF_A if key == "a.tif" else TIFF_B)
    # 平铺无残留（P4-app：不再写 UPLOAD_DIR 根）
    assert not (Path(UPLOAD_DIR) / "a.tif").exists()
    assert not (Path(UPLOAD_DIR) / "b.tif").exists()
    # 配额一次性结算 = 全部已发布 item 字节合计
    row = _quota(uid)
    assert row["used_bytes"] == len(TIFF_A) + len(TIFF_B)
    assert row["reserved_bytes"] == 0


# --------------------------------------------------------------------------- #
# 2. MRXS 伴侣目录同包原子发布（入口+伴侣同 objects/<sid>/，ready 前不可读）
# --------------------------------------------------------------------------- #
def test_mrxs_companion_same_bundle_atomic():
    uid = _user_session(_client(auth=True), login="z-mrxs@x.com")
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    real_validate = app_mod._validate_slide_file
    app_mod._validate_slide_file = lambda p, **_: None  # stub 入口字节放行
    try:
        r = _upload_zip(c, [
            ("S.mrxs", b"mrxs-main"),
            ("S/", b""),
            ("S/Slidedat.ini", b"ini"),
            ("S/Level_0/data.dat", b"dat"),
        ])
    finally:
        app_mod._validate_slide_file = real_validate
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["name"] == "S.mrxs"  # mrxs 优先为主
    sid = body["slide_ids"]["S.mrxs"]
    obj = slide_storage.bundle_dir(sid, root=UPLOAD_DIR)
    # 入口与伴侣同包：data.mrxs + data/…（伴侣目录 stem 归一为 data——与
    # 入口同 stem；包内相对关系保留）
    assert (obj / "data.mrxs").read_bytes() == b"mrxs-main"
    assert (obj / "data" / "Slidedat.ini").read_bytes() == b"ini"
    assert (obj / "data" / "Level_0" / "data.dat").read_bytes() == b"dat"
    # manifest：唯一入口 + 全成员
    import json
    manifest = json.loads((obj / "manifest.json").read_text())
    assert manifest["entry"] == "data.mrxs"
    assert {f["path"] for f in manifest["files"]} == {
        "data.mrxs", "data/Slidedat.ini", "data/Level_0/data.dat"}
    # ready：ID 通道可读；字节口径 = 入口+伴侣合计
    assert c.get("/api/slides/%s/info" % sid).status_code == 200
    total = len(b"mrxs-main") + len(b"ini") + len(b"dat")
    desc = slide_store.resolve_slide_id(sid)
    assert desc.accounted_bytes == total
    row = _quota(uid)
    assert row["used_bytes"] == total


# --------------------------------------------------------------------------- #
# 3. 无法归组：孤儿目录（无同 stem 切片）→ 400 指名拒绝，无部分状态
# --------------------------------------------------------------------------- #
def test_ungroupable_companion_rejected_named():
    """顶层无关目录（无同 stem 切片）→ 识别层 400 **指名**拒绝；
    同 stem 多切片共享一个伴侣目录（归属不明）→ 分组层 400 指名拒绝。
    均无部分状态。"""
    real_validate = app_mod._validate_slide_file
    app_mod._validate_slide_file = lambda p, **_: None
    try:
        c = _client()
        r = _upload_zip(c, [("a.tif", TIFF_A), ("orphan/x.bin", b"junk")])
        assert r.status_code == 400
        assert "orphan/" in r.get_json()["error"]  # 指名（顶层成员名）
        # 分组层：a.svs 与 a.mrxs 共享 a/ 伴侣目录 → 无法唯一归组
        r2 = _upload_zip(c, [("a.svs", b"svs"), ("a.mrxs", b"mrxs"),
                             ("a/shared.dat", b"d")])
        assert r2.status_code == 400
        assert "a/shared.dat" in r2.get_json()["error"]
    finally:
        app_mod._validate_slide_file = real_validate
    # 无部分状态：无任务、无绑定、无 objects、无暂存残留
    assert _tasks() == []
    assert not (Path(UPLOAD_DIR) / "objects").exists() or \
        not list((Path(UPLOAD_DIR) / "objects").iterdir())
    assert not (Path(UPLOAD_DIR) / ".staging").exists() or \
        not list((Path(UPLOAD_DIR) / ".staging").iterdir())


# --------------------------------------------------------------------------- #
# 4. 入口打不开的 item：按 item 失败剔除（failures 证据），其余照常
# --------------------------------------------------------------------------- #
def test_invalid_entry_item_failed_others_publish():
    uid = _user_session(_client(auth=True), login="z-partial@x.com")
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    # bad.tif 为垃圾字节（真 openslide 打不开 → SlideValidationError 稳定
    # 机器码），good.tif 是合法 tiled TIFF——按 item 失败剔除
    r = _upload_zip(c, [("bad.tif", b"not-a-slide"),
                        ("good.tif", TIFF_A)])
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["extracted"] == ["good.tif"]
    assert len(body["failures"]) == 1
    assert body["failures"][0]["item"] == "bad.tif"
    assert body["failures"][0]["code"]
    sid = body["slide_ids"]["good.tif"]
    assert slide_store.resolve_slide_id(sid).asset_state == "ready"
    # 配额只按已发布 item 结算（bad 的字节不计）
    row = _quota(uid)
    assert row["used_bytes"] == len(TIFF_A)
    assert row["reserved_bytes"] == 0


# --------------------------------------------------------------------------- #
# 5. 崩溃恢复幂等：受理后、发布前崩溃 → 同 item 复用 slide_id、一次结算
# --------------------------------------------------------------------------- #
def test_crash_after_intent_recovery_reuses_slide_ids(monkeypatch):
    uid = _user_session(_client(auth=True), login="z-crash@x.com")
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1

    import slide_publish
    real_publish = slide_publish.publish_batch_item
    called = {"n": 0}

    def _publish_boom(*a, **kw):
        called["n"] += 1
        raise RuntimeError("发布期进程死亡（测试注入）")

    monkeypatch.setattr(slide_publish, "publish_batch_item", _publish_boom)
    r = _upload_zip(c, [("a.tif", TIFF_A), ("b.tif", TIFF_B)])
    monkeypatch.setattr(slide_publish, "publish_batch_item", real_publish)
    # 受理已完成（intent+绑定落库）→ 稳定 commit_in_progress，恢复接管
    assert r.status_code == 503, r.get_data(as_text=True)
    assert r.get_json()["code"] == "commit_in_progress"
    assert called["n"] >= 1
    t = _tasks()[0]
    assert t["state"] == upload_task_store.STATE_COMMITTING
    items_before = sorted(
        upload_task_store.list_upload_task_items(t["upload_id"]),
        key=lambda x: x["item_key"])
    assert len(items_before) == 2
    # 发布未发生：item 全部 staging 且不可读
    for it in items_before:
        assert slide_store.authorize_read(it["slide_id"],
                                          actor_user_id=uid) is False

    # 恢复扫描（commit 超时压 0）：重组装 + 逐 item 发布 + 一次结算
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 0)
    for _ in range(3):  # 重复恢复幂等
        app_mod._upload_legacy_recover_stale({"role": "owner"})
    t2 = _tasks()[0]
    assert t2["state"] == upload_task_store.STATE_COMMITTED
    items_after = sorted(
        upload_task_store.list_upload_task_items(t2["upload_id"]),
        key=lambda x: x["item_key"])
    # 同 item 复用原 slide_id（绝不重新分配，R-13）
    assert [(x["item_key"], x["slide_id"]) for x in items_after] == \
        [(x["item_key"], x["slide_id"]) for x in items_before]
    for it in items_after:
        desc = slide_store.resolve_slide_id(it["slide_id"])
        assert desc.asset_state == "ready"
        assert c.get("/api/slides/%s/info" % it["slide_id"]).status_code == 200
    # 配额一次结算
    row = _quota(uid)
    assert row["used_bytes"] == len(TIFF_A) + len(TIFF_B)
    assert row["reserved_bytes"] == 0


# --------------------------------------------------------------------------- #
# 6. 多任务同名 zip：互不影响（独立 ID；无名称冲突面）
# --------------------------------------------------------------------------- #
def test_same_name_zip_concurrent_tasks_independent():
    ua = _user_session(_client(auth=True), login="z-twin-a@x.com")
    _set_quota(ua, 10 ** 7)
    ca = _client(auth=True)
    with ca.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = ua
        sess["role"] = "user"
        sess["auth_version"] = 1
    ub = _user_session(_client(auth=True), login="z-twin-b@x.com")
    _set_quota(ub, 10 ** 7)
    cb = _client(auth=True)
    with cb.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = ub
        sess["role"] = "user"
        sess["auth_version"] = 1

    ra = _upload_zip(ca, [("twin.tif", TIFF_A)])
    rb = _upload_zip(cb, [("twin.tif", TIFF_B)])  # 同名不同内容
    assert ra.status_code == 200 and rb.status_code == 200
    sid_a = ra.get_json()["slide_ids"]["twin.tif"]
    sid_b = rb.get_json()["slide_ids"]["twin.tif"]
    assert sid_a != sid_b
    # 各自归属、互不可见
    assert slide_store.resolve_slide_id(sid_a).owner_user_id == ua
    assert slide_store.resolve_slide_id(sid_b).owner_user_id == ub
    assert cb.get("/api/slides/%s/info" % sid_a).status_code == 403
    assert ca.get("/api/slides/%s/info" % sid_b).status_code == 403
    # 同名同内容也一样（独立资产；同内容不代表同身份）
    rc = _upload_zip(ca, [("twin.tif", TIFF_A)])
    assert rc.status_code == 200
    sid_c = rc.get_json()["slide_ids"]["twin.tif"]
    assert sid_c not in (sid_a, sid_b)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- #
# 7. review 门禁回归（F0）：发布中段预占失效 → 已发布 item 整体撤回
#    （ready→failed 可迁移 + 撤包），不留「ready 行 + 无包」破态
# --------------------------------------------------------------------------- #
def test_reservation_expired_mid_publish_withdraws_published(monkeypatch):
    c0 = _client(auth=True)
    uid = _user_session(c0, login="z-exp@x.com")
    _set_quota(uid, 10 ** 7)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = 1
    import slide_publish
    real_publish = slide_publish.publish_batch_item
    state = {"n": 0}

    def _expire_after_first(*a, **kw):
        out = real_publish(*a, **kw)
        state["n"] += 1
        if state["n"] == 1:
            # 首个 item 发布成功后预占立即过期 → 第二个 item 发布时
            # renew 拒绝（ReservationInvalid）→ 整体撤回
            with psycopg.connect(PG_URI, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE upload_reservations SET expires_at = "
                                "now() - interval '1 second' "
                                "WHERE state='reserved'")
        return out

    monkeypatch.setattr(slide_publish, "publish_batch_item",
                        _expire_after_first)
    r = _upload_zip(c, [("a.tif", TIFF_A), ("b.tif", TIFF_B)])
    assert r.status_code == 409
    assert r.get_json()["code"] == "reservation_expired"
    # 两资产均 failed（含已 ready 后被撤回的 a.tif）——无 ready 残留
    descs = []
    for t in _tasks():
        for it in upload_task_store.list_upload_task_items(t["upload_id"]):
            descs.append(slide_store.resolve_slide_id(it["slide_id"]))
    assert len(descs) == 2
    for d in descs:
        assert d.asset_state == "failed"
        assert not slide_storage.bundle_dir(d.slide_id,
                                            root=UPLOAD_DIR).exists()
    # 任务 failed、预占释放、used_bytes 恒 0（consume 从未发生）
    assert _tasks()[0]["state"] == upload_task_store.STATE_FAILED
    row = _quota(uid)
    assert row["reserved_bytes"] == 0
    assert row["used_bytes"] == 0
