# -*- coding: utf-8 -*-
"""旧 index 路由「校验后、写入前」竞态的写入绑定回归（2026-10-17 P1）。

缺陷（7c910139 及更早）：旧 index 路由在 annotation_id 防护（409
annotation_moved）通过之后，写入仍按位置 index **二次定位**
（delete_roi/update_roi/review_roi/set_roi_shared(token, index)）。并发交错
——防护校验完成后、写入执行前，另一请求删除了前面的行——token 下非
tombstone 序整体前移，写入落到**另一条**标注上：用户 A 带 annotation_id=A2
调 DELETE /api/annotation/admin/1 → 200，删掉的是 B 的私有标记，A2 保留。

修复契约（本文件锁定）：旧 index 路由 index 只解析**一次**行；鉴权与
annotation_id 比对绑定该行；写入一律按该行 annotation_id 的 by-id 行锁原语
执行（delete/update/review/set_shared 的 *_by_annotation_id），无论请求是否
携带 annotation_id。写入时该行已被并发删除 → 404，绝不触及其他行。

用 monkeypatch 包住防护函数，在「防护通过之后、写入之前」注入另一行的删除
来稳定复现竞态窗口：
  - app.py 路由   → 包 ``app._guard_index_annotation_id``；
  - share_server.py 路由 → 包 ``share_server._guard_roi_index_annotation_id``。

运行：cd 项目根 && python3 -m pytest tests/test_annotation_index_write_binding.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 分享 ROI 写入的服务端尺寸/MPP 复核需要可信元数据（同
    # test_annotation_stale_index / test_annotation_isolation）
    monkeypatch.setattr(
        share_srv, "_slide_dims_and_mpp",
        lambda safe, path=None: (100000, 100000, 60.0, 60.0))
    yield


# --------------------------------------------------------------------------- #
# 世界构造（同 test_legacy_race.py）：owner + 用户 A/B；切片 X（A 有，public）
# 与 Y（B 有）。工作台标注全部落在共享的 token=admin 下 → index 为跨用户全局序。
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _client_as(login_id, password):
    c = _client()
    assert _login(c, login_id, password).status_code in (200, 302)
    return c


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name, upload_dir=UPLOAD_DIR)  # P6：建可读仓（id_bundle 行）
    return name


def _setup_world():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    user_a = user_store.create_user("a@x.com", "userApass123456", role="user")
    user_b = user_store.create_user("b@x.com", "userBpass123456", role="user")
    x = _touch("x.svs")
    y = _touch("y.svs")
    share_store.set_slide_meta(x, owner_user_id=user_a["user_id"], public=True)
    share_store.set_slide_meta(y, owner_user_id=user_b["user_id"])
    return owner, user_a, user_b, x, y


def _anno(client, slide, label="L"):
    """工作台人工标注（token=admin，source=human）；index 取 live 口径。"""
    r = client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": label,
        "x": 10, "y": 10, "side_px": 100, "size_mm": 6.0,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    return {"annotation_id": body["annotation_id"],
            "index": _live_indexes()[body["annotation_id"]],
            "revision": body.get("revision")}


def _anno_ai(slide, owner_user_id, label="AI"):
    """AI 待审标注（store 直写，source=ai）；index 取 live 口径。"""
    roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, slide, label, type="rect",
        x=20, y=20, side_px=100, size_mm=6.0,
        source="ai", owner_user_id=owner_user_id)
    assert roi.get("source") == "ai"
    return {"annotation_id": roi["annotation_id"],
            "index": _live_indexes()[roi["annotation_id"]],
            "revision": roi.get("revision")}


def _live_indexes():
    """admin token 当前 live 行的 {annotation_id: index}（get_roi 口径）。"""
    return {r["annotation_id"]: r["index"]
            for r in share_store.list_rois(share_store.ADMIN_TOKEN)}


def _inject_shift_after_guard(monkeypatch, deleted_aids):
    """包住 app 的 index 防护：防护通过后、写入前删除 given annotation_id。

    模拟并发请求抢先删除前面的行 → live 序前移、后续按 index 的写入换行。
    """
    original = app_mod._guard_index_annotation_id

    def shift_after_guard(token, index, aid):
        result = original(token, index, aid)
        assert result is None
        for d_aid in deleted_aids:
            assert share_store.delete_roi_by_annotation_id(d_aid)
        return result

    monkeypatch.setattr(app_mod, "_guard_index_annotation_id", shift_after_guard)


def _inject_share_shift_after_guard(monkeypatch, deleted_aids):
    """同上，包 share_server 的 index 防护（分享端旧 index 路由）。"""
    original = share_srv._guard_roi_index_annotation_id

    def shift_after_guard(r, aid):
        result = original(r, aid)
        assert result is None
        for d_aid in deleted_aids:
            assert share_store.delete_roi_by_annotation_id(d_aid)
        return result

    monkeypatch.setattr(share_srv, "_guard_roi_index_annotation_id",
                        shift_after_guard)


# =========================================================================== #
# 工作台 DELETE（admin）：不带 annotation_id 的旧客户端也必须绑定已解析行
# =========================================================================== #
def test_admin_delete_index_without_id_binds_to_resolved_row(monkeypatch):
    """无 annotation_id 的旧客户端：防护通过后前面一行被并发删除，删除仍须
    落在解析时的那行（A2），绝不因 index 前移删掉 B 的私有标注。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [a1["annotation_id"]])

    r = ca.delete("/api/annotation/admin/%d" % a2["index"])
    assert r.status_code == 200, r.get_data(as_text=True)
    live = _live_indexes()
    assert b1["annotation_id"] in live, \
        "B 的私有标注被按前移后的 index 删掉（写入未绑定已解析行）"
    assert a2["annotation_id"] not in live, "删除未落在解析时的目标行 A2 上"


def test_admin_delete_index_target_gone_404_peer_untouched(monkeypatch):
    """防护通过后**目标行自身**被并发删除 → 写入按 annotation_id 落空
    → 404；绝不按 index 二次定位触及其他行（旧实现此处删掉 B1）。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [a2["annotation_id"]])

    r = ca.delete("/api/annotation/admin/%d" % a2["index"],
                  json={"annotation_id": a2["annotation_id"]})
    assert r.status_code == 404, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "标注不存在"
    assert share_store.get_roi_by_annotation_id(
        b1["annotation_id"])["deleted"] is False, \
        "目标行已被并发删除时按 index 二次定位删掉了 B 的私有标注"
    assert share_store.get_roi_by_annotation_id(
        a2["annotation_id"])["deleted"] is True  # hook 删的，路由未再动它


# =========================================================================== #
# 工作台 PATCH：note 与 shared 两段写入都绑定已解析行
# =========================================================================== #
def test_patch_index_note_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [a1["annotation_id"]])

    r = ca.patch("/api/annotation/admin/%d" % a2["index"],
                 json={"note": "v2", "annotation_id": a2["annotation_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["note"] == "v2"
    assert share_store.get_roi_by_annotation_id(
        a2["annotation_id"])["note"] == "v2", "备注未落在解析时的目标行 A2 上"
    assert share_store.get_roi_by_annotation_id(
        b1["annotation_id"])["note"] == "", \
        "备注写到了前移后 index 指向的 B 的私有标注上"


def test_patch_index_shared_flag_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [a1["annotation_id"]])

    r = ca.patch("/api/annotation/admin/%d" % a2["index"],
                 json={"shared": True, "annotation_id": a2["annotation_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["shared"] is True
    assert share_store.get_roi_by_annotation_id(
        a2["annotation_id"])["shared"] is True, "shared 未落在目标行 A2 上"
    assert share_store.get_roi_by_annotation_id(
        b1["annotation_id"])["shared"] is False, \
        "shared 标志写到了前移后 index 指向的 B 的私有标注上"


# =========================================================================== #
# 工作台 review / comments POST
# =========================================================================== #
def test_review_index_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    ai1 = _anno_ai(x, user_a["user_id"], "AI-1")
    ai2 = _anno_ai(x, user_a["user_id"], "AI-2")
    ai3 = _anno_ai(x, user_a["user_id"], "AI-3")
    assert (ai1["index"], ai2["index"], ai3["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [ai1["annotation_id"]])

    r = ca.post("/api/annotation/admin/%d/review" % ai2["index"],
                json={"action": "accept", "annotation_id": ai2["annotation_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["review_status"] == "accepted"
    assert share_store.get_roi_by_annotation_id(
        ai2["annotation_id"])["review_status"] == "accepted", \
        "审核未落在解析时的目标行 AI-2 上"
    assert share_store.get_roi_by_annotation_id(
        ai3["annotation_id"])["review_status"] == "pending", \
        "审核按前移后的 index 落到了 AI-3 上"


def test_comments_post_index_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)
    _inject_shift_after_guard(monkeypatch, [a1["annotation_id"]])

    r = ca.post("/api/annotation/admin/%d/comments" % a2["index"],
                json={"body": "hey", "annotation_id": a2["annotation_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    cid = r.get_json()["comment"]["comment_id"]
    # 评论挂靠解析时的目标行 A2；B1 名下无评论
    cmts = share_store.list_comments(annotation_id=a2["annotation_id"])
    assert [c["comment_id"] for c in cmts] == [cid]
    assert share_store.list_comments(annotation_id=b1["annotation_id"]) == []


# =========================================================================== #
# 分享端 DELETE / PATCH（旧 index 路由）
# =========================================================================== #
def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _share_body():
    return {"slide": "x.svs", "type": "rect", "label": "V",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}


def _visitor_anno(v, tok, label):
    """访客建一条标记；POST 响应只带 index（含 tombstone 计数，会漂移）
    → 按唯一 label 从列表回读 annotation_id 与 live index。"""
    r = v.post("/s/%s/api/roi" % tok,
               json=dict(_share_body(), label=label))
    assert r.status_code == 200, r.get_data(as_text=True)
    listed = [i for i in v.get("/s/%s/api/rois" % tok).get_json()
              if i.get("source") == "me" and i.get("label") == label]
    assert len(listed) == 1
    it = listed[0]
    return {"annotation_id": it["annotation_id"], "index": it["index"],
            "revision": it.get("revision")}


def test_share_delete_index_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    tok = share_store.create_share([x], 24,
                                   permissions=["view", "annotate"])["token"]
    v = _share_client()
    m1 = _visitor_anno(v, tok, "V1")
    m2 = _visitor_anno(v, tok, "V2")
    m3 = _visitor_anno(v, tok, "V3")
    assert (m1["index"], m2["index"], m3["index"]) == (0, 1, 2)
    _inject_share_shift_after_guard(monkeypatch, [m1["annotation_id"]])

    # 不带 annotation_id 的旧客户端：防护通过后 V1 被并发删除，删除仍须
    # 落在解析时的 V2 上，绝不因 index 前移删掉 V3
    r = v.delete("/s/%s/api/roi/%d" % (tok, m2["index"]))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_roi_by_annotation_id(
        m3["annotation_id"])["deleted"] is False, \
        "V3 被按前移后的 index 删掉（分享端写入未绑定已解析行）"
    assert share_store.get_roi_by_annotation_id(
        m2["annotation_id"])["deleted"] is True, "删除未落在解析时的 V2 上"


def test_share_patch_index_binds_to_checked_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    tok = share_store.create_share([x], 24,
                                   permissions=["view", "annotate"])["token"]
    v = _share_client()
    m1 = _visitor_anno(v, tok, "V1")
    m2 = _visitor_anno(v, tok, "V2")
    m3 = _visitor_anno(v, tok, "V3")
    assert (m1["index"], m2["index"], m3["index"]) == (0, 1, 2)
    _inject_share_shift_after_guard(monkeypatch, [m1["annotation_id"]])

    r = v.patch("/s/%s/api/roi/%d" % (tok, m2["index"]),
                json={"note": "v2"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["note"] == "v2"
    assert share_store.get_roi_by_annotation_id(
        m2["annotation_id"])["note"] == "v2", "备注未落在解析时的 V2 上"
    assert share_store.get_roi_by_annotation_id(
        m3["annotation_id"])["note"] == "", \
        "备注写到了前移后 index 指向的 V3 上"
