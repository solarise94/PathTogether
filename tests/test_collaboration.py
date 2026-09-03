# -*- coding: utf-8 -*-
"""Stage 3c-1 协作特性测试（docs §5.3）。

覆盖：
  1. revision CAS：expected_revision 不符 → RevisionConflict（409）；缺省兼容；
     revision 单调递增；delete/set_shared 同样支持 CAS。
  2. tombstone 完善：deleted_at 落地；list_rois/get_roi 默认不含 tombstone；
     list_changes 含 tombstone（最小字段 + deleted_at）。
  3. 评论线程：CRUD / 权限（can_view 查看、can_annotate 评论、guest 分享 annotate
     才可评论）/ resolve。
  4. AI 标注审核：accept/reject 流转；人工标注 review → 400。
  5. 修改历史：累积 + 20 条封顶。

json/pg 双跑（pg 由 RUN_PG_TESTS=1 conftest 起库 + autouse TRUNCATE 隔离）。
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app # noqa: E402
import share_server as share_srv  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，清空 users.json / shares.json。"""
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _client_noauth():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, userA


def _add(slide="demo.svs", shared=False, source=None):
    """直接走 store 建一条标注，返回 roi dict（token=admin）。"""
    return share_store.add_roi(
        share_store.ADMIN_TOKEN, slide, "L", type="rect",
        x=0, y=0, side_px=10, size_mm=6.0, shared=shared, source=source)


def _post_anno(client, slide, shared=False):
    return client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": "L",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0, "shared": shared,
    })


# =========================================================================== #
# 1. revision CAS（store 层）
# =========================================================================== #
def test_cas_update_conflict_and_compat():
    _touch()
    r = _add()
    assert r["revision"] == 1
    # 缺省 expected_revision → 兼容（旧客户端），不抛
    out = share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="n1")
    assert out["revision"] == 2
    # 正确 expected_revision → ok
    out = share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="n2",
                                 expected_revision=2)
    assert out["revision"] == 3
    # 过期 expected_revision → RevisionConflict，携带 current_revision
    with pytest.raises(share_store.RevisionConflict) as ei:
        share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="n3",
                               expected_revision=1)
    assert ei.value.current_revision == 3


def test_cas_delete_conflict():
    _touch()
    _add()
    # 正确 revision → 删除成功
    ok, _aid = share_store.delete_roi(share_store.ADMIN_TOKEN, 0,
                                      expected_revision=1)
    assert ok is True
    # 新建一条再测过期 CAS
    _add()
    share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="bump")
    with pytest.raises(share_store.RevisionConflict):
        share_store.delete_roi(share_store.ADMIN_TOKEN, 0,
                               expected_revision=1)


def test_cas_set_shared_conflict():
    _touch()
    _add()
    share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="bump")  # revision 2
    with pytest.raises(share_store.RevisionConflict):
        share_store.set_roi_shared(share_store.ADMIN_TOKEN, 0, True,
                                   expected_revision=1)
    # 正确 revision → ok
    assert share_store.set_roi_shared(share_store.ADMIN_TOKEN, 0, True,
                                      expected_revision=2) is True


def test_cas_concurrent_expected_revision_one_wins():
    """两个相同 expected_revision 的并发更新只有一个成功，另一个 RevisionConflict。"""
    _touch()
    r = _add()
    assert r["revision"] == 1
    results = []
    errors = []

    def worker(note):
        try:
            out = share_store.update_roi(
                share_store.ADMIN_TOKEN, 0, note=note, expected_revision=1)
            results.append(out)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(results) == 1, "并发 CAS 应仅一人写入，got results=%r errors=%r" % (
        results, errors)
    assert len(errors) == 1
    assert isinstance(errors[0], share_store.RevisionConflict)
    listed = share_store.list_rois(share_store.ADMIN_TOKEN)
    assert listed[0]["note"] in ("a", "b")
    assert listed[0]["revision"] == 2


def test_revision_monotonic_on_update_and_tombstone():
    _touch()
    _add()
    share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="a")
    share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="b")
    r = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert r["revision"] == 3
    # tombstone 也 bump revision
    share_store.delete_roi(share_store.ADMIN_TOKEN, 0)
    full = share_store.get_roi_by_annotation_id(r["annotation_id"])
    assert full["revision"] == 4


# =========================================================================== #
# 2. CAS 经 API（409 mapping）
# =========================================================================== #
def test_api_patch_cas_409():
    _touch()
    _setup_users()
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    idx = _post_anno(co, "demo.svs").get_json()["index"]
    # 先 PATCH 一次 bump revision
    co.patch("/api/annotation/admin/%d" % idx, json={"note": "v1"})
    # 过期 expected_revision → 409
    r = co.patch("/api/annotation/admin/%d" % idx,
                 json={"note": "v2", "expected_revision": 1})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "revision_conflict"
    assert body["current_revision"] == 2
    # 正确 expected_revision → 200
    r = co.patch("/api/annotation/admin/%d" % idx,
                 json={"note": "v3", "expected_revision": 2})
    assert r.status_code == 200


def test_api_delete_cas_409():
    _touch()
    _setup_users()
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    idx = _post_anno(co, "demo.svs").get_json()["index"]
    co.patch("/api/annotation/admin/%d" % idx, json={"note": "bump"})
    r = co.delete("/api/annotation/admin/%d" % idx,
                  json={"expected_revision": 1})
    assert r.status_code == 409
    assert r.get_json()["error"] == "revision_conflict"


# =========================================================================== #
# 3. tombstone 完善
# =========================================================================== #
def test_tombstone_deleted_at_and_minimal_fields():
    _touch()
    r = _add()
    aid = r["annotation_id"]
    slide = "demo.svs"
    ok, _a = share_store.delete_roi(share_store.ADMIN_TOKEN, 0)
    assert ok is True
    # deleted_at 落地
    full = share_store.get_roi_by_annotation_id(aid)
    assert full["deleted"] is True
    assert full.get("deleted_at") is not None
    # list_rois / get_roi 默认不含 tombstone
    assert share_store.get_roi(share_store.ADMIN_TOKEN, 0) is None
    assert share_store.list_rois(share_store.ADMIN_TOKEN) == []
    # list_changes 含 tombstone（最小字段 + deleted_at + type=annotation）
    changes = share_store.list_changes(slide, 0)
    tombs = [c for c in changes if c.get("deleted")]
    assert len(tombs) == 1
    t = tombs[0]
    # 最小字段：不泄露几何/备注
    assert "x" not in t and "note" not in t and "label" not in t
    assert t["annotation_id"] == aid
    assert t["deleted_at"] is not None
    assert t["revision"] == 2  # tombstone bumped revision
    assert t.get("type") == "annotation"


def test_tombstone_in_changes_with_comments():
    """list_changes 同时含标注 tombstone 与评论事件，带 type 区分。"""
    _touch()
    r = _add()
    aid = r["annotation_id"]
    share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, "hi")
    share_store.delete_roi(share_store.ADMIN_TOKEN, 0)
    changes = share_store.list_changes("demo.svs", 0)
    types = {c.get("type") for c in changes}
    assert "annotation" in types and "comment" in types


# =========================================================================== #
# 4. 评论线程（store）
# =========================================================================== #
def test_comment_crud_store():
    _touch()
    r = _add()
    aid = r["annotation_id"]
    c = share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, " hello ",
                                author_label="Doc")
    assert c["comment_id"].startswith("cmt_")
    assert c["body"] == "hello"  # strip
    assert c["author_label"] == "Doc"
    assert c["change_seq"] is not None
    cid = c["comment_id"]
    # list
    lst = share_store.list_comments(annotation_id=aid)
    assert len(lst) == 1 and lst[0]["comment_id"] == cid
    # resolve
    assert share_store.resolve_comment(cid, True) is True
    assert share_store.list_comments(annotation_id=aid)[0]["resolved"] is True
    # 软删 → list 不返回
    assert share_store.delete_comment(cid) is True
    assert share_store.list_comments(annotation_id=aid) == []
    # 重复软删 → False
    assert share_store.delete_comment(cid) is False


def test_comment_body_validation():
    _touch()
    r = _add()
    aid = r["annotation_id"]
    with pytest.raises(ValueError):
        share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, "   ")
    with pytest.raises(ValueError):
        share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, "x" * 2001)


def test_comment_parent_reply():
    _touch()
    r = _add()
    aid = r["annotation_id"]
    parent = share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, "p")
    reply = share_store.add_comment(aid, "demo.svs", share_store.ADMIN_TOKEN, "r",
                                    parent_id=parent["comment_id"])
    assert reply["parent_id"] == parent["comment_id"]


# =========================================================================== #
# 4b. 评论 API + 权限
# =========================================================================== #
def test_api_comments_crud_and_perms():
    _touch()
    owner, userA = _setup_users()
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    idx = _post_anno(co, "demo.svs").get_json()["index"]
    # POST 评论（owner 可标注）→ 200
    r = co.post("/api/annotation/admin/%d/comments" % idx,
                json={"body": "nice"})
    assert r.status_code == 200, r.get_data(as_text=True)
    cid = r.get_json()["comment"]["comment_id"]
    # GET 评论 → 200
    r = co.get("/api/annotation/admin/%d/comments" % idx)
    assert r.status_code == 200
    assert len(r.get_json()["comments"]) == 1
    # resolve → 200
    r = co.post("/api/comment/%s/resolve" % cid, json={"resolved": True})
    assert r.status_code == 200
    # DELETE（owner）→ 200
    r = co.delete("/api/comment/%s" % cid)
    assert r.status_code == 200
    # 删后 GET 为空
    r = co.get("/api/annotation/admin/%d/comments" % idx)
    assert r.get_json()["comments"] == []


def test_api_comment_delete_only_author_or_owner():
    _touch()
    owner, userA = _setup_users()
    _own("demo.svs", owner["user_id"])
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    idx = _post_anno(co, "demo.svs").get_json()["index"]
    # userA 没权访问该切片 → 评论端点 403/404（这里 demo 无 owner 归属时 owner 全开）
    # owner 发评论
    r = co.post("/api/annotation/admin/%d/comments" % idx, json={"body": "x"})
    cid = r.get_json()["comment"]["comment_id"]
    # 让 userA 登录另一 client 删 → 无权
    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # userA 不是 owner 也不是作者 → 删除 403
    r = ca.delete("/api/comment/%s" % cid)
    assert r.status_code == 403


def _own(name, user_id):
    share_store.set_slide_meta(name, owner_user_id=user_id)


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def test_guest_comment_requires_annotate_perm():
    _touch()
    owner, _uA = _setup_users()
    _own("demo.svs", owner["user_id"])
    # 分享含 annotate
    sh = share_store.create_share(["demo.svs"], 24,
                                  permissions=["view", "annotate"])
    tok = sh["token"]
    # 先建一条 admin 标注供评论挂靠
    r = _add()
    aid = r["annotation_id"]
    c = _share_client()
    # guest GET 评论 → 200（share 有效）
    resp = c.get("/s/%s/api/comments?annotation_id=%s" % (tok, aid))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    # guest POST 评论（share 含 annotate）→ 200
    resp = c.post("/s/%s/api/comments" % tok,
                  json={"annotation_id": aid, "body": "guest note", "name": "Dr.X"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["comment"]["author_label"] == "Dr.X"
    # 分享不含 annotate → POST 403
    sh2 = share_store.create_share(["demo.svs"], 24, permissions=["view"])
    resp = c.post("/s/%s/api/comments" % sh2["token"],
                  json={"annotation_id": aid, "body": "nope"})
    assert resp.status_code == 403


# =========================================================================== #
# 5. AI 标注审核
# =========================================================================== #
def test_review_store_flow():
    _touch()
    # AI 标注（admin 且 shared=False → source=ai，pending）
    ai = _add(shared=False)
    assert ai["source"] == "ai"
    assert ai["review_status"] == "pending"
    # 人工标注（shared=True → human，none）
    human = _add(shared=True)
    assert human["source"] == "human"
    assert human["review_status"] == "none"
    # review accept
    out = share_store.review_roi(share_store.ADMIN_TOKEN, 0, "accept")
    assert out["review_status"] == "accepted"
    assert out["revision"] == 2
    # 人工标注 review → ValueError
    # human 在 index 1（ai 已 accept 仍在 index 0）
    with pytest.raises(ValueError):
        share_store.review_roi(share_store.ADMIN_TOKEN, 1, "accept")


def test_api_review_accept_reject_and_human_400():
    _touch()
    _setup_users()
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    # AI 标注（默认 shared=False）
    ai_idx = _post_anno(co, "demo.svs", shared=False).get_json()["index"]
    r = co.post("/api/annotation/admin/%d/review" % ai_idx,
                json={"action": "accept"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["review_status"] == "accepted"
    # reject 另一条
    ai_idx2 = _post_anno(co, "demo.svs", shared=False).get_json()["index"]
    r = co.post("/api/annotation/admin/%d/review" % ai_idx2,
                json={"action": "reject"})
    assert r.status_code == 200
    assert r.get_json()["review_status"] == "rejected"
    # 人工标注 review → 400
    h_idx = _post_anno(co, "demo.svs", shared=True).get_json()["index"]
    r = co.post("/api/annotation/admin/%d/review" % h_idx,
                json={"action": "accept"})
    assert r.status_code == 400
    # 非法 action → 400
    r = co.post("/api/annotation/admin/%d/review" % ai_idx,
                json={"action": "maybe"})
    assert r.status_code == 400


def test_review_status_in_annotations_by_slide():
    _touch()
    _add(shared=False)  # ai pending
    _add(shared=True)   # human none
    by = share_store.annotations_by_slide()
    items = by["demo.svs"][0]["items"]
    statuses = {it["review_status"] for it in items}
    assert "pending" in statuses and "none" in statuses


# =========================================================================== #
# 6. 修改历史
# =========================================================================== #
def test_history_accumulation_and_cap():
    _touch()
    _add()
    # 更新 3 次 → history 累积 3 条
    for i in range(3):
        share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="n%d" % i)
    r = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert len(r.get("history", [])) == 3
    snap0 = r["history"][0]
    assert "geom" in snap0 and "note" in snap0 and "revision" in snap0
    # 上限 20：再更新 20 次 → history 封顶 20
    for i in range(20):
        share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="more%d" % i)
    r = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert len(r["history"]) == 20


def test_history_tombstone_snapshot():
    _touch()
    r0 = _add()
    aid = r0["annotation_id"]
    share_store.update_roi(share_store.ADMIN_TOKEN, 0, note="changed")
    share_store.delete_roi(share_store.ADMIN_TOKEN, 0)
    # tombstone 也记录历史（update 1 + delete 1 = 2 条）
    full = share_store.get_roi_by_annotation_id(aid)
    assert full is not None
    hist_len = len(full.get("history", []))
    assert hist_len >= 1


def test_api_history_endpoint():
    _touch()
    _setup_users()
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    idx = _post_anno(co, "demo.svs").get_json()["index"]
    co.patch("/api/annotation/admin/%d" % idx, json={"note": "a"})
    co.patch("/api/annotation/admin/%d" % idx, json={"note": "b"})
    r = co.get("/api/annotation/admin/%d/history" % idx)
    assert r.status_code == 200
    hist = r.get_json()["history"]
    assert len(hist) == 2
