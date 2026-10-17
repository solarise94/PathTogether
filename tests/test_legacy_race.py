# -*- coding: utf-8 -*-
"""过期 index 寻址缺陷回归（dogfood BUG-1 2026-10-17）。

缺陷：工作台对单条标注的删除/编辑/审核/评论/历史按「token 下的序号 index」
寻址。admin token 是**全体工作台用户共享的同一列表**（index = 该 token 下非
tombstone 行的插入序位置）：他人增删标注后，客户端缓存的 index 会指向另一条
标注——非 owner 403「只能修改自己创建的标注」（dogfood：连删 3 个自己的 AI
标记，第 3 个 DELETE /api/annotation/admin/205 → 403）；owner 账号则静默删/
审错任何人的标注。

修复契约（本文件锁定）：
  1. 客户端寻址键 = annotation_id（/api/annotation/id/<aid> 及其
     /review、/comments、/history 子路由；后两者为本次补齐）；
  2. 旧 index 路由保留兼容，但请求可带 annotation_id（query 或 body）做
     一致性校验：与该 index 当前指向行不一致 → 409 annotation_moved，
     绝不删/审错；
  3. by-id 路由鉴权与 index 版同口径（切片层 × can_read/write_annotation，
     owner 任意、创建者 ∪ 授权可写；统一 404 不泄露）。

运行：cd 项目根 && python3 -m pytest tests/test_annotation_stale_index.py -q
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
    # 分享 ROI 写入的服务端尺寸/MPP 复核需要可信元数据（100000² @ 60µm/px
    # → side 100px = 6.0mm 预设；同 test_annotation_isolation）
    monkeypatch.setattr(
        share_srv, "_slide_dims_and_mpp",
        lambda safe, path=None: (100000, 100000, 60.0, 60.0))
    yield


# --------------------------------------------------------------------------- #
# 世界构造：owner + 用户 A/B；切片 X（A 有，public，B 可看）与 Y（B 有）。
# 工作台标注全部落在同一个 token=ADMIN_TOKEN 下 → index 为**跨用户全局序**。
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
    """工作台人工标注（token=admin，source=human）。index 取 live 口径。"""
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
    """AI 待审标注（store 直写；与 /internal/ai 写入同一入口语义）。

    注意 index 取 live 口径（get_roi/delete 的位置）：add_roi 返回的 index
    是「含 tombstone 的插入总数」，与 live 位置从有删除起就漂移——这正是
    缺陷面的一部分（新建后客户端缓存的 index 也可能是错的）。
    """
    roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, slide, label, type="rect",
        x=20, y=20, side_px=100, size_mm=6.0,
        source="ai", owner_user_id=owner_user_id)
    assert roi.get("source") == "ai"
    return {"annotation_id": roi["annotation_id"],
            "index": _live_indexes()[roi["annotation_id"]],
            "revision": roi.get("revision")}


def _live_indexes():
    """admin token 当前 live 行的 {annotation_id: index}（get_roi 口径）。

    list_rois 输出按 ts 倒序但每项带 pre-filter index（token 内 live 位置），
    直接读该项 index，不重新枚举。"""
    return {r["annotation_id"]: r["index"]
            for r in share_store.list_rois(share_store.ADMIN_TOKEN)}


# =========================================================================== #
# 1. dogfood 情形重现：连删 3 个自己的标注，第 3 个命中他人行 → 403
# =========================================================================== #
def test_dogfood_sequence_third_delete_hits_peer_row_403():
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    a3 = _anno(ca, x, "A3")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    b2 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B2")
    assert (a1["index"], a2["index"], a3["index"]) == (0, 1, 2)
    assert (b1["index"], b2["index"]) == (3, 4)

    # 客户端按缓存 index 连删自己的 3 个标注（旧客户端行为，不带 annotation_id）
    r1 = ca.delete("/api/annotation/admin/%d" % a1["index"])
    assert r1.status_code == 200
    # a1 删除后整体前移：a2(0) a3(1) b1(2) b2(3)。缓存 index=1 现在指向 a3
    # → 200 但删的是**另一条自己的**（静默删错，dogfood 第 2 次删除）
    r2 = ca.delete("/api/annotation/admin/%d" % a2["index"])
    assert r2.status_code == 200
    live = _live_indexes()
    assert a3["annotation_id"] not in live, \
        "按缓存 index 的第 2 次删除实际删掉了 a3（静默删错另一条）"
    assert a2["annotation_id"] in live, "a2 反而还在"
    # 缓存 index=2 现在指向 b2（B 的标注）→ 403「只能修改自己创建的标注」
    #（dogfood 第 3 次删除；owner 账号在此处会直接删掉 B2）
    r3 = ca.delete("/api/annotation/admin/%d" % a3["index"])
    assert r3.status_code == 403, r3.get_data(as_text=True)
    assert "只能修改自己创建的标注" in r3.get_json()["error"]
    live = _live_indexes()
    assert b2["annotation_id"] in live and a2["annotation_id"] in live


def test_owner_stale_index_delete_silently_removes_peer_row():
    """同形态换成 owner 会话：第 2 次按缓存 index 删除**成功且删的是 B1**
    （owner 任意）——这正是「owner 账号能删掉任何人的」缺陷形态。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    co = _client_as("owner@x.com", "ownerpass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert (a1["index"], a2["index"], b1["index"]) == (0, 1, 2)

    # owner 按客户端缓存的 index 连删（owner 可删任意来源标注）
    assert co.delete("/api/annotation/admin/%d" % a1["index"]).status_code == 200
    # a1 删除后前移：a2(0) b1(1)。缓存 a2.index=1 现在指向 b1 → 200 静默删错
    r = co.delete("/api/annotation/admin/%d" % a2["index"])
    assert r.status_code == 200
    live = _live_indexes()
    assert b1["annotation_id"] not in live, "owner 按过期 index 删掉了 B1"
    assert a2["annotation_id"] in live


# =========================================================================== #
# 2. 修复：旧 index 路由的 annotation_id 一致性防护（409 annotation_moved）
# =========================================================================== #
def test_delete_index_with_mismatched_annotation_id_409_moved():
    """带 annotation_id 的旧路由请求：index 当前指向行与 id 不一致 → 409，
    绝不删错（含 owner）。body 与 query 两种携带方式都防护。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    co = _client_as("owner@x.com", "ownerpass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    assert co.delete("/api/annotation/admin/%d" % a1["index"],
                     json={"annotation_id": a1["annotation_id"]}).status_code == 200

    # ---- body 携带：缓存 index=1（原 a2）现在指向 b1 → 409 annotation_moved
    r = co.delete("/api/annotation/admin/%d" % a2["index"],
                  json={"annotation_id": a2["annotation_id"]})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"] == "annotation_moved"
    live = _live_indexes()
    assert b1["annotation_id"] in live and a2["annotation_id"] in live

    # ---- query 携带：同样防护（index0 现在是 a2，带 b1 的 id → 409）
    r = co.delete("/api/annotation/admin/0?annotation_id=%s"
                  % b1["annotation_id"])
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_moved"
    assert _live_indexes().get(a2["annotation_id"]) == 0  # a2 未被动过

    # ---- id 与 index 一致 → 照常删除（兼容路径可用）
    r = co.delete("/api/annotation/admin/0?annotation_id=%s"
                  % a2["annotation_id"])
    assert r.status_code == 200
    assert a2["annotation_id"] not in _live_indexes()

    # ---- PATCH 同口径：不匹配 → 409（index0 现在是 b1，带 a2 的 id）
    r = co.patch("/api/annotation/admin/0",
                 json={"note": "pwn", "annotation_id": a2["annotation_id"]})
    assert r.status_code == 409
    assert r.get_json()["error"] == "annotation_moved"
    # b1 未被改到（note 维持创建时的空串）
    assert share_store.get_roi_by_annotation_id(
        b1["annotation_id"])["note"] == ""


def test_delete_by_id_after_list_shift_targets_right_row():
    """修复后的客户端路径：按 annotation_id 删除不受列表变动影响。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    # 中间插入他人标注 + 删除前面一条之后，仍按 id 删自己的 a1
    assert ca.delete("/api/annotation/id/%s" % a1["annotation_id"]).status_code == 200
    live = _live_indexes()
    assert a2["annotation_id"] in live and b1["annotation_id"] in live
    # 再删 a2（id 寻址）→ 只删 a2
    assert ca.delete("/api/annotation/id/%s" % a2["annotation_id"]).status_code == 200
    live = _live_indexes()
    assert a2["annotation_id"] not in live
    assert b1["annotation_id"] in live


# =========================================================================== #
# 3. 修复：review / comments / history 的 by-id 路由（鉴权与 index 版同口径）
# =========================================================================== #
def test_review_by_annotation_id_route():
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    ai1 = _anno_ai(x, user_a["user_id"], "AI-1")

    # 本人 accept → 200
    r = ca.post("/api/annotation/id/%s/review" % ai1["annotation_id"],
                json={"action": "accept"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["review_status"] == "accepted"
    assert share_store.get_roi_by_annotation_id(
        ai1["annotation_id"])["review_status"] == "accepted"

    # reject → rejected
    r = ca.post("/api/annotation/id/%s/review" % ai1["annotation_id"],
                json={"action": "reject"})
    assert r.status_code == 200 and r.get_json()["review_status"] == "rejected"

    # 非法 action → 400；人工标注 → 400（与 index 版同口径；工作台
    # POST /api/annotation 缺省 source=ai（admin token 非 shared），故
    # 人工行经 store 显式 source="human" 造出）
    r = ca.post("/api/annotation/id/%s/review" % ai1["annotation_id"],
                json={"action": "nope"})
    assert r.status_code == 400
    human_roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, x, "H1", type="rect",
        x=10, y=10, side_px=100, size_mm=6.0,
        source="human", owner_user_id=user_a["user_id"])
    r = ca.post("/api/annotation/id/%s/review" % human_roi["annotation_id"],
                json={"action": "accept"})
    assert r.status_code == 400

    # B 不能标注 X → 403（切片层判定同 index 版 can_annotate_slide）
    r = cb.post("/api/annotation/id/%s/review" % ai1["annotation_id"],
                json={"action": "accept"})
    assert r.status_code == 403

    # 不存在 / 已删 → 404
    assert ca.post("/api/annotation/id/roi_missing/review",
                   json={"action": "accept"}).status_code == 404
    assert ca.delete("/api/annotation/id/%s" % ai1["annotation_id"]).status_code == 200
    assert ca.post("/api/annotation/id/%s/review" % ai1["annotation_id"],
                   json={"action": "accept"}).status_code == 404


def test_review_index_route_with_annotation_id_guard_409():
    """旧 index review 路由带 annotation_id：不一致 → 409，绝不审错。"""
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    ai1 = _anno_ai(x, user_a["user_id"], "AI-1")
    ai2 = _anno_ai(x, user_a["user_id"], "AI-2")
    assert (ai1["index"], ai2["index"]) == (0, 1)
    # 前一条被删（如另一端撤销）→ 缓存 ai2.index=1 现指向 ai3（自己另一条）
    assert ca.delete("/api/annotation/id/%s" % ai1["annotation_id"]).status_code == 200
    ai3 = _anno_ai(x, user_a["user_id"], "AI-3")
    assert ai3["index"] == 1

    r = ca.post("/api/annotation/admin/%d/review" % ai2["index"],
                json={"action": "accept",
                      "annotation_id": ai2["annotation_id"]})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"] == "annotation_moved"
    # ai2 未被误审、ai3 未被误审
    assert share_store.get_roi_by_annotation_id(
        ai2["annotation_id"])["review_status"] == "pending"
    assert share_store.get_roi_by_annotation_id(
        ai3["annotation_id"])["review_status"] == "pending"
    # 无 annotation_id 的旧请求维持旧行为（兼容）：审到 ai3（自己另一条）
    r = ca.post("/api/annotation/admin/%d/review" % ai2["index"],
                json={"action": "accept"})
    assert r.status_code == 200
    assert share_store.get_roi_by_annotation_id(
        ai3["annotation_id"])["review_status"] == "accepted"


def test_comments_and_history_by_annotation_id():
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    a1 = _anno(ca, x, "A1")

    # POST 评论（by id）
    r = ca.post("/api/annotation/id/%s/comments" % a1["annotation_id"],
                json={"body": "nice"})
    assert r.status_code == 200, r.get_data(as_text=True)
    cid = r.get_json()["comment"]["comment_id"]
    # GET 评论（by id）
    r = ca.get("/api/annotation/id/%s/comments" % a1["annotation_id"])
    assert r.status_code == 200
    assert [c["comment_id"] for c in r.get_json()["comments"]] == [cid]
    # history（by id）：先改一条备注产生历史快照，再读（快照记变更前值）
    r = ca.patch("/api/annotation/id/%s" % a1["annotation_id"],
                 json={"note": "v2"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = ca.get("/api/annotation/id/%s/history" % a1["annotation_id"])
    assert r.status_code == 200
    hist = r.get_json()["history"]
    assert isinstance(hist, list) and hist, hist
    assert hist[-1].get("note") == ""  # 变更前快照

    # 越权口径同 index 版：B 可看 X（public）但标注归 A 私有——
    #   GET（require_annotate=False）→ 切片可读过、标注不可见 → 404 不泄露；
    #   POST（require_annotate=True）→ 切片 annotate 档先拒 → 403
    #  （与 index 版 _resolve_anno 同序）。
    assert cb.get("/api/annotation/id/%s/comments"
                  % a1["annotation_id"]).status_code == 404
    assert cb.get("/api/annotation/id/%s/history"
                  % a1["annotation_id"]).status_code == 404
    assert cb.post("/api/annotation/id/%s/comments" % a1["annotation_id"],
                   json={"body": "hi"}).status_code == 403

    # 不存在 → 404
    assert ca.get("/api/annotation/id/roi_missing/comments").status_code == 404
    assert ca.get("/api/annotation/id/roi_missing/history").status_code == 404


# =========================================================================== #
# 4. 分享页：/s/<token>/api/roi/id/<aid> + 旧 index 路由防护
# =========================================================================== #
def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _share_body():
    return {"slide": "x.svs", "type": "rect", "label": "V",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}


def _visitor_anno(v, tok, label):
    """访客建一条标记；POST 响应只带 index（且是含 tombstone 的计数，会漂移）
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


def test_share_delete_and_patch_by_annotation_id():
    owner, user_a, user_b, x, y = _setup_world()
    tok = share_store.create_share([x], 24,
                                   permissions=["view", "annotate"])["token"]
    v = _share_client()
    m1 = _visitor_anno(v, tok, "V1")
    m2 = _visitor_anno(v, tok, "V2")
    assert (m1["index"], m2["index"]) == (0, 1)

    # 列表变动（删除 m1）后按 id 删 m2 → 删的是对的
    assert v.delete("/s/%s/api/roi/%d" % (tok, m1["index"])).status_code == 200
    r = v.delete("/s/%s/api/roi/id/%s" % (tok, m2["annotation_id"]))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_roi_by_annotation_id(
        m2["annotation_id"])["deleted"] is True

    # PATCH by id：另一访客再建一条，仍按 id 编辑自己的
    v2 = _share_client()
    w1 = _visitor_anno(v2, tok, "W1")
    r = v2.patch("/s/%s/api/roi/id/%s" % (tok, w1["annotation_id"]),
                 json={"note": "edited"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_roi_by_annotation_id(
        w1["annotation_id"])["note"] == "edited"

    # 他人（其他访客）的行：by-id 编辑/删除 → 403（同 index 版归属判定）
    r = v.patch("/s/%s/api/roi/id/%s" % (tok, w1["annotation_id"]),
                json={"note": "pwn"})
    assert r.status_code == 403
    r = v.delete("/s/%s/api/roi/id/%s" % (tok, w1["annotation_id"]))
    assert r.status_code == 403

    # 越权 token：行不在该 token 下 → 404（不泄露）
    other_tok = share_store.create_share([x], 24,
                                         permissions=["view", "annotate"])["token"]
    r = _share_client().delete("/s/%s/api/roi/id/%s"
                               % (other_tok, w1["annotation_id"]))
    assert r.status_code == 404


def test_share_index_delete_with_annotation_id_guard_409():
    """分享端旧 index 删除带 annotation_id：不一致 → 409，绝不删错。"""
    owner, user_a, user_b, x, y = _setup_world()
    tok = share_store.create_share([x], 24,
                                   permissions=["view", "annotate"])["token"]
    v = _share_client()
    m1 = _visitor_anno(v, tok, "V1")
    m2 = _visitor_anno(v, tok, "V2")
    assert (m1["index"], m2["index"]) == (0, 1)
    assert v.delete("/s/%s/api/roi/%d" % (tok, m1["index"])).status_code == 200
    # 缓存 m2.index=1 现指向 m3（同访客另一条）→ 带 id 请求必须 409 不删错
    m3 = _visitor_anno(v, tok, "V3")
    assert m3["index"] == 1
    r = v.delete("/s/%s/api/roi/%d" % (tok, m2["index"]),
                 json={"annotation_id": m2["annotation_id"]})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"] == "annotation_moved"
    assert share_store.get_roi_by_annotation_id(m3["annotation_id"])["deleted"] is False
    # id 一致 → 放行（m2 已被挤到 index 0）
    r = v.delete("/s/%s/api/roi/0?annotation_id=%s" % (tok, m2["annotation_id"]))
    assert r.status_code == 200
    assert share_store.get_roi_by_annotation_id(m2["annotation_id"])["deleted"] is True


def test_review_index_guard_must_keep_checked_target(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(cb, y, "B1")
    original = app_mod._guard_index_annotation_id
    def shift_after_guard(token, index, aid):
        result = original(token, index, aid)
        assert result is None
        # Concurrent request deletes A1 after auth/ID validation, before write.
        assert share_store.delete_roi_by_annotation_id(a1["annotation_id"])
        return result
    monkeypatch.setattr(app_mod, "_guard_index_annotation_id", shift_after_guard)
    response = ca.delete("/api/annotation/admin/1", json={
        "annotation_id": a2["annotation_id"]})
    peer = share_store.get_roi_by_annotation_id(b1["annotation_id"])
    target = share_store.get_roi_by_annotation_id(a2["annotation_id"])
    assert not peer.get("deleted"), (response.status_code, response.get_json(),
        "B's private annotation deleted by A; intended A2 remains", target)
