# -*- coding: utf-8 -*-
"""标注几何位置锁（§6.3 / §6.4，docs/viewer-interaction-lock-contract-20261009）。

契约：``rois.data`` 内 ``geometry_locked: boolean``（缺省 false，无迁移）。
锁定 = 冻结几何 + 防误删（不是权限锁）：备注/元数据照常可改；任何有编辑权
的人可解锁；只读者不能。锁检查在**同一事务、行锁内**进行，且优先于 CAS
（annotation_locked 先于 revision_conflict）。权限验证先于锁字段校验。

覆盖矩阵（见任务书验证清单）：
  1. 锁后改几何 409 annotation_locked（含 current）；锁后改备注 200 + revision 递增；
  2. 同请求锁+几何 400 lock_with_geometry；锁切换缺 expected_revision 400；
     geometry_locked 非布尔 400；
  3. annotation_locked 优先于 revision_conflict（几何 + 过期 CAS → 锁错）；
  4. 只读授权/无写权限查看者不能解锁（403）；另一编辑者（annotation grant
     can_edit）与 owner 可解锁；
  5. 并发（两事务）：行锁持有时第二个事务的几何更新被阻塞并被拒；
  6. 锁状态出现在所有读取接口（/api/annotations、/api/slides/<id>/annotations、
     /api/annotations/changes、/api/share/rois、history、POST/PATCH/restore 响应、
     分享页 /s/<token>/api/rois 投影）与 public_roi_view 旧行缺省；
  7. DELETE 三变体（by-id / admin / token）409；切片删除不受锁影响；
  8. history（op=lock/unlock + actor + geometry_locked）与 change_seq 变化流；
  9. 权限失败先于锁检查（越权带非法锁体 → 403 而非 400/409）；
  10. 创建（POST 工作台 / 分享页）接受可选 geometry_locked；AI 写路径缺省 false。

conftest 默认启动内嵌 PostgreSQL，autouse TRUNCATE 隔离用例。
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pg_store  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import annotation_access  # noqa: E402
import share_server as share_srv  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 分享 ROI 写入的服务端尺寸/MPP 复核需要可信元数据（同 test_annotation_isolation：
    # 100000² @ 60µm/px → side 100px = 6.0mm 预设）
    monkeypatch.setattr(
        share_srv, "_slide_dims_and_mpp",
        lambda safe, path=None: (100000, 100000, 60.0, 60.0))
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _client_as(login_id, password):
    c = _client()
    _login(c, login_id, password)
    return c


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name)  # 建可读仓（id_bundle 行）
    return name


def _setup_world():
    """owner/A/B 三账号；x 归 A 且 public（B 可看）；owner 可删任意切片。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    userB = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    x = _touch("x.svs")
    share_store.set_slide_meta(x, owner_user_id=userA["user_id"], public=True)
    return owner, userA, userB, x


def _anno(client, slide, **extra):
    body = {"slide": slide, "type": "rect", "label": "L",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}
    body.update(extra)
    return client.post("/api/annotation", json=body)


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _visible_items(body):
    out = []
    for grp in body.get("annotations", []):
        out.extend(grp.get("items", []))
    return out


def _lock(client, aid, locked=True, expected=None):
    body = {"geometry_locked": locked}
    if expected is not None:
        body["expected_revision"] = expected
    return client.patch("/api/annotation/id/%s" % aid, json=body)


# =========================================================================== #
# 1. 锁语义主链路（by-id PATCH）
# =========================================================================== #
def test_lock_blocks_geometry_allows_note_and_delete():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    aid = _anno(ca, x).get_json()["annotation_id"]

    # 锁定：必须带 expected_revision → 成功后 revision 递增
    r = _lock(ca, aid, True, expected=1)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["geometry_locked"] is True
    assert r.get_json()["revision"] == 2

    # 锁后改几何 → 409 annotation_locked（current 携带当前条目）
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"geom": {"x": 10, "y": 10, "w": 100, "h": 100}})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "annotation_locked"
    assert body["current"]["annotation_id"] == aid
    assert body["current"]["geometry_locked"] is True
    assert body["current"]["revision"] == 2

    # 锁后只改备注 → 200，revision 递增
    r = ca.patch("/api/annotation/id/%s" % aid, json={"note": "locked-but-note"})
    assert r.status_code == 200
    assert r.get_json()["revision"] == 3
    assert share_store.get_roi_by_annotation_id(aid)["note"] == "locked-but-note"

    # 锁后删除（by-id）→ 409 annotation_locked
    r = ca.delete("/api/annotation/id/%s" % aid)
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_locked"

    # 解锁（带最新 revision）→ 几何可改、可删
    r = _lock(ca, aid, False, expected=3)
    assert r.status_code == 200
    assert r.get_json()["geometry_locked"] is False
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"geom": {"x": 5, "y": 5, "w": 100, "h": 100}})
    assert r.status_code == 200
    r = ca.delete("/api/annotation/id/%s" % aid)
    assert r.status_code == 200


def test_lock_with_geometry_same_request_400():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    j = _anno(ca, x).get_json()
    aid, idx = j["annotation_id"], j["index"]

    # by-id：锁 + 几何 → 400 lock_with_geometry
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"geometry_locked": True, "expected_revision": 1,
                       "geom": {"x": 0, "y": 0, "w": 50, "h": 50}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "lock_with_geometry"

    # token/index 变体：同语义
    r = ca.patch("/api/annotation/admin/%d" % idx,
                 json={"geometry_locked": True, "expected_revision": 1,
                       "geom": {"x": 0, "y": 0, "w": 50, "h": 50}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "lock_with_geometry"

    # 分享页变体：同语义
    s1 = share_store.create_share([x], 24,
                                  permissions=["view", "annotate"])["token"]
    v1 = _share_client()
    rr = v1.post("/s/%s/api/roi" % s1, json={
        "slide": x, "type": "rect", "label": "V",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0})
    assert rr.status_code == 200, rr.get_data(as_text=True)
    vidx = rr.get_json()["index"]
    r = v1.patch("/s/%s/api/roi/%d" % (s1, vidx),
                 json={"geometry_locked": True, "expected_revision": 1,
                       "geom": {"x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "lock_with_geometry"


def test_lock_toggle_validation_400s():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    aid = _anno(ca, x).get_json()["annotation_id"]

    # 缺 expected_revision → 400 expected_revision_required（仅锁切换；
    # 其它 PATCH 不带 CAS 照旧放行）
    r = _lock(ca, aid, True)
    assert r.status_code == 400
    assert r.get_json()["error"] == "expected_revision_required"
    # 非布尔 → 400
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"geometry_locked": "yes", "expected_revision": 1})
    assert r.status_code == 400
    # 旧客户端普通 PATCH（无锁字段、无 CAS）不受影响（revision → 2）
    r = ca.patch("/api/annotation/id/%s" % aid, json={"note": "legacy-ok"})
    assert r.status_code == 200

    # 锁切换 CAS 过期 → 409 revision_conflict（沿用形状 + current 投影）
    r = _lock(ca, aid, True, expected=2)
    assert r.status_code == 200
    r = _lock(ca, aid, False, expected=1)
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "revision_conflict"
    assert body["current_revision"] == 3
    assert body["current"]["geometry_locked"] is True


def test_annotation_locked_precedes_revision_conflict():
    """已锁 + 改几何 + 过期 CAS → 409 annotation_locked（不是 revision_conflict）。"""
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    aid = _anno(ca, x).get_json()["annotation_id"]
    assert _lock(ca, aid, True, expected=1).status_code == 200  # revision 2
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"geom": {"x": 1, "y": 1, "w": 100, "h": 100},
                       "expected_revision": 1})
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_locked"


# =========================================================================== #
# 2. 权限：谁能锁/解锁
# =========================================================================== #
def test_readonly_grant_cannot_unlock_editor_can():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    j = _anno(ca, x).get_json()
    aid = j["annotation_id"]
    assert _lock(ca, aid, True, expected=1).status_code == 200  # revision 2

    # B 认领含 annotate 的分享 → 切片层可写；但标注无授权 → 403
    share = share_store.create_share([x], 24,
                                     permissions=["view", "annotate"])
    assert cb.post("/api/share/%s/claim" % share["token"]).status_code == 200
    r = _lock(cb, aid, False, expected=2)
    assert r.status_code == 403
    assert "current" not in (r.get_json() or {})  # 不泄露锁状态

    # A 授予 B 只读 → B 可见但仍不能解锁（403）
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"grantee_kind": "user", "grantee_id": userB["user_id"]})
    assert r.status_code == 200
    r = _lock(cb, aid, False, expected=2)
    assert r.status_code == 403

    # A 升级为 can_edit 授权 → B（另一个编辑者）可解锁
    r = ca.patch("/api/annotation/id/%s" % aid,
                 json={"grantee_kind": "user", "grantee_id": userB["user_id"],
                       "can_edit": True})
    assert r.status_code == 200
    r = _lock(cb, aid, False, expected=2)
    assert r.status_code == 200
    assert r.get_json()["geometry_locked"] is False


def test_share_visitor_readonly_cannot_unlock_owner_can():
    owner, userA, userB, x = _setup_world()
    s1 = share_store.create_share([x], 24,
                                  permissions=["view", "annotate"])["token"]
    v1 = _share_client()
    v2 = _share_client()
    rr = v1.post("/s/%s/api/roi" % s1, json={
        "slide": x, "type": "rect", "label": "V",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0})
    assert rr.status_code == 200, rr.get_data(as_text=True)
    idx = rr.get_json()["index"]

    # 创建者（v1）锁定
    r = v1.patch("/s/%s/api/roi/%d" % (s1, idx),
                 json={"geometry_locked": True, "expected_revision": 1})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["geometry_locked"] is True

    # 同链接另一访客（只读视角，非创建者）不能解锁 → 403
    r = v2.patch("/s/%s/api/roi/%d" % (s1, idx),
                 json={"geometry_locked": False, "expected_revision": 2})
    assert r.status_code == 403

    # 创建者改几何 → 409 annotation_locked；解锁后可改
    r = v1.patch("/s/%s/api/roi/%d" % (s1, idx),
                 json={"geom": {"x": 10, "y": 10, "side_px": 100, "size_mm": 6.0}})
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_locked"
    r = v1.patch("/s/%s/api/roi/%d" % (s1, idx),
                 json={"geometry_locked": False, "expected_revision": 2})
    assert r.status_code == 200
    # 分享页删除也被锁挡（先重新锁上）
    v1.patch("/s/%s/api/roi/%d" % (s1, idx),
             json={"geometry_locked": True, "expected_revision": 3})
    r = v1.delete("/s/%s/api/roi/%d" % (s1, idx))
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_locked"


def test_owner_can_unlock_on_owned_slide():
    owner, userA, userB, x = _setup_world()
    # owner 在**自己收录**的切片上才持有写权（升级 B：owner 非全量）
    z = _touch("z.svs")
    share_store.set_slide_meta(z, owner_user_id=owner["user_id"])
    co = _client_as("owner@x.com", "ownerpass123456")
    j = _anno(co, z).get_json()
    aid = j["annotation_id"]
    assert _lock(co, aid, True, expected=1).status_code == 200
    # 锁确实生效：读回 True，几何被拒
    assert share_store.get_roi_by_annotation_id(aid)["geometry_locked"] is True
    r = co.patch("/api/annotation/id/%s" % aid,
                 json={"geom": {"x": 1, "y": 1, "w": 100, "h": 100}})
    assert r.status_code == 409 and r.get_json()["error"] == "annotation_locked"
    assert _lock(co, aid, False, expected=2).status_code == 200
    # 解锁后几何恢复可改
    r = co.patch("/api/annotation/id/%s" % aid,
                 json={"geom": {"x": 1, "y": 1, "w": 100, "h": 100}})
    assert r.status_code == 200


def test_permission_failure_precedes_lock_validation():
    """越权请求即使带非法锁体（锁+几何/缺 CAS）也按权限 403，不泄露锁语义。"""
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    j = _anno(ca, x).get_json()
    aid = j["annotation_id"]
    assert _lock(ca, aid, True, expected=1).status_code == 200

    # B 无切片 annotate、无标注授权：先权限失败（403），不是 400 lock_with_geometry
    r = cb.patch("/api/annotation/id/%s" % aid,
                 json={"geometry_locked": False, "expected_revision": 1,
                       "geom": {"x": 0, "y": 0, "w": 50, "h": 50}})
    assert r.status_code == 403
    # 缺 CAS 的越权解锁同样 403（而非 400 expected_revision_required）
    r = cb.patch("/api/annotation/id/%s" % aid, json={"geometry_locked": False})
    assert r.status_code == 403


# =========================================================================== #
# 3. 并发：行锁内检查，两事务不能绕过锁
# =========================================================================== #
def test_row_locked_transaction_blocks_concurrent_geometry_write():
    _touch("demo.svs")
    r = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "L",
                            type="rect", x=0, y=0, side_px=100, size_mm=6.0,
                            geometry_locked=True)
    aid = r["annotation_id"]
    acquired = threading.Event()
    release = threading.Event()

    def hold_row_lock():
        import psycopg.rows
        conn = pg_store.connect()
        conn.row_factory = psycopg.rows.dict_row
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM rois WHERE annotation_id=%s FOR UPDATE",
                        (aid,))
                    acquired.set()
                    release.wait(10)
        finally:
            conn.close()

    holder = threading.Thread(target=hold_row_lock)
    holder.start()
    assert acquired.wait(5), "行锁未取得"
    outcome = {}

    def try_geom_write():
        try:
            share_store.update_roi_by_annotation_id(
                aid, geom={"x": 5, "y": 5, "w": 60, "h": 60})
            outcome["ok"] = True
        except Exception as e:  # noqa: BLE001
            outcome["err"] = e

    worker = threading.Thread(target=try_geom_write)
    worker.start()
    worker.join(0.8)
    assert worker.is_alive(), "几何写应被行锁阻塞（同事务串行）"
    release.set()
    holder.join(10)
    worker.join(10)
    assert isinstance(outcome.get("err"), share_store.AnnotationLocked), outcome
    # 行锁持有期间/释放后：几何未被改写
    cur_roi = share_store.get_roi_by_annotation_id(aid)
    assert cur_roi["x"] == 0 and cur_roi["side_px"] == 100
    assert cur_roi["geometry_locked"] is True


def test_concurrent_locked_writes_all_rejected():
    _touch("demo.svs")
    r = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "L",
                            type="rect", x=0, y=0, side_px=100, size_mm=6.0,
                            geometry_locked=True)
    aid = r["annotation_id"]
    errors = []

    def worker():
        try:
            share_store.update_roi_by_annotation_id(
                aid, geom={"x": 1, "y": 1, "w": 60, "h": 60})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(errors) == 4
    assert all(isinstance(e, share_store.AnnotationLocked) for e in errors)
    assert share_store.get_roi_by_annotation_id(aid)["x"] == 0


# =========================================================================== #
# 4. 锁状态出现在全部读取接口与分享投影
# =========================================================================== #
def test_geometry_locked_in_all_read_channels():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    # 一条带锁创建（草稿保存时带锁）、一条常规
    j1 = _anno(ca, x, geometry_locked=True).get_json()
    j2 = _anno(ca, x).get_json()
    assert j1["geometry_locked"] is True
    assert j2["geometry_locked"] is False
    aid = j1["annotation_id"]
    slide_id = share_store.get_slide_id(x)

    # /api/annotations?slide=
    items = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    by_aid = {i["annotation_id"]: i for i in items}
    assert by_aid[aid]["geometry_locked"] is True
    assert by_aid[j2["annotation_id"]]["geometry_locked"] is False

    # /api/slides/<slide_id>/annotations
    r = ca.get("/api/slides/%s/annotations" % slide_id)
    assert r.status_code == 200
    items = _visible_items(r.get_json())
    assert {i["annotation_id"]: i for i in items}[aid]["geometry_locked"] is True

    # /api/share/rois
    rois = ca.get("/api/share/rois").get_json()
    by_aid = {i["annotation_id"]: i for i in rois}
    assert by_aid[aid]["geometry_locked"] is True

    # /api/annotations/changes：锁切换/add 事件携带锁状态
    ch = ca.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    events = {c.get("annotation_id"): c for c in ch["changes"]}
    assert events[aid]["geometry_locked"] is True

    # history 端点：新建未修改 → 空（add 不记快照）；修改后快照带锁状态
    r = ca.get("/api/annotation/admin/%d/history" % j1["index"])
    assert r.status_code == 200
    assert r.get_json()["history"] == []

    # PATCH 响应带锁状态（revision → 2）
    r = ca.patch("/api/annotation/id/%s" % aid, json={"note": "n"})
    assert r.get_json()["geometry_locked"] is True

    # restore 响应带锁状态（解锁→删除→恢复）
    rev_now = share_store.get_roi_by_annotation_id(aid)["revision"]
    assert _lock(ca, aid, False, expected=rev_now).status_code == 200
    rev_now = share_store.get_roi_by_annotation_id(aid)["revision"]
    ca.delete("/api/annotation/id/%s" % aid,
              json={"expected_revision": rev_now})
    tomb = share_store.get_roi_by_annotation_id(aid)
    r = ca.post("/api/annotation/id/%s/restore" % aid,
                json={"expected_revision": tomb["revision"]})
    assert r.status_code == 200
    assert r.get_json()["geometry_locked"] is False

    # 分享投影（授权给分享链接后，分享页只读可见锁状态）
    s1 = share_store.create_share([x], 24, permissions=["view"])["token"]
    ca.patch("/api/annotation/id/%s" % aid,
             json={"grantee_kind": "share_token", "grantee_id": s1})
    listed = _share_client().get("/s/%s/api/rois" % s1).get_json()
    hit = next(i for i in listed if i["annotation_id"] == aid)
    assert hit["geometry_locked"] is False
    assert hit["source"] in ("admin", "shared")

    # 旧行（无 geometry_locked 键）经 public_roi_view → 显式 false
    legacy = annotation_access.public_roi_view({"annotation_id": "old"})
    assert legacy["geometry_locked"] is False


def test_lock_toggle_visible_in_changes_and_history():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    j = _anno(ca, x).get_json()
    aid = j["annotation_id"]
    cursor0 = ca.get("/api/annotations/changes?slide=%s&after=0" % x) \
        .get_json()["cursor"]
    assert _lock(ca, aid, True, expected=1).status_code == 200

    # 变化流是**状态式**事件（roi 当前快照 + 最新 change_seq，同 update 语义）：
    # 锁定后消费方可读到 geometry_locked=true 的新状态
    ch = ca.get("/api/annotations/changes?slide=%s&after=%d" % (x, cursor0)) \
        .get_json()
    events = [c for c in ch["changes"] if c.get("annotation_id") == aid]
    assert len(events) == 1
    assert events[0]["geometry_locked"] is True
    assert events[0]["revision"] == 2
    cursor1 = ch["cursor"]

    assert _lock(ca, aid, False, expected=2).status_code == 200
    ch = ca.get("/api/annotations/changes?slide=%s&after=%d" % (x, cursor1)) \
        .get_json()
    events = [c for c in ch["changes"] if c.get("annotation_id") == aid]
    assert len(events) == 1
    assert events[0]["geometry_locked"] is False
    assert events[0]["revision"] == 3

    # history：op=lock/unlock + 操作者（user:<uid>）+ 锁状态快照
    hist = ca.get("/api/annotation/admin/%d/history" % j["index"]) \
        .get_json()["history"]
    ops = [h.get("op") for h in hist]
    assert ops == ["lock", "unlock"]
    assert hist[0]["actor"] == "user:%s" % userA["user_id"]
    assert hist[0]["geometry_locked"] is False  # 快照是修改前状态
    assert hist[1]["geometry_locked"] is True


# =========================================================================== #
# 5. DELETE 变体与生命周期
# =========================================================================== #
def test_delete_variants_blocked_when_locked():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    j = _anno(ca, x).get_json()
    assert _lock(ca, j["annotation_id"], True, expected=1).status_code == 200
    # 1) by-id
    r = ca.delete("/api/annotation/id/%s" % j["annotation_id"])
    assert r.status_code == 409 and r.get_json()["error"] == "annotation_locked"
    # 2) /api/annotation/admin/<index>
    r = ca.delete("/api/annotation/admin/%d" % j["index"])
    assert r.status_code == 409 and r.get_json()["error"] == "annotation_locked"
    # 3) /api/annotation/<token>/<index>（真实分享 token 上的条目）
    s1 = share_store.create_share([x], 24,
                                  permissions=["view", "annotate"])["token"]
    roi = share_store.add_roi(s1, x, "S", type="rect",
                              x=0, y=0, side_px=100, size_mm=6.0,
                              geometry_locked=True)
    co = _client_as("owner@x.com", "ownerpass123456")
    r = co.delete("/api/annotation/%s/%d" % (s1, roi["index"]))
    assert r.status_code == 409 and r.get_json()["error"] == "annotation_locked"


def test_slide_delete_not_blocked_by_annotation_lock():
    owner, userA, userB, x = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    j = _anno(ca, x, geometry_locked=True).get_json()
    assert j["geometry_locked"] is True
    co = _client_as("owner@x.com", "ownerpass123456")
    r = co.delete("/api/slide/%s" % x)
    assert r.status_code == 200, r.get_data(as_text=True)
    # 标注生命周期随切片终结，不受锁阻挡（列表不再返回）
    items = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    assert all(i["annotation_id"] != j["annotation_id"] for i in items)


# =========================================================================== #
# 6. 创建与 AI 缺省
# =========================================================================== #
def test_share_page_create_with_lock_and_defaults():
    owner, userA, userB, x = _setup_world()
    s1 = share_store.create_share([x], 24,
                                  permissions=["view", "annotate"])["token"]
    v1 = _share_client()
    r = v1.post("/s/%s/api/roi" % s1, json={
        "slide": x, "type": "rect", "label": "V",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0,
        "geometry_locked": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    idx = r.get_json()["index"]
    listed = v1.get("/s/%s/api/rois" % s1).get_json()
    hit = next(i for i in listed if i["index"] == idx)
    assert hit["geometry_locked"] is True
    # 非布尔 → 400
    r = v1.post("/s/%s/api/roi" % s1, json={
        "slide": x, "type": "rect", "label": "V2",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0,
        "geometry_locked": "yes"})
    assert r.status_code == 400


def test_ai_write_path_creates_unlocked():
    _touch("demo.svs")
    roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, "demo.svs", "AI", type="rect",
        x=0, y=0, side_px=100, size_mm=6.0,
        source="ai", created_by_session_id="sess-1",
        provenance={"plugin_id": "histopilot", "run_id": "r1"})
    assert roi["geometry_locked"] is False
    # 工作台 POST 不带锁字段 → false
    assert share_store.get_roi_by_annotation_id(
        roi["annotation_id"])["geometry_locked"] is False
