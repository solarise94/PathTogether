# -*- coding: utf-8 -*-
"""工单 A（P0 数据隔离）测试矩阵：标注可见性按主体与授权范围隔离。

契约（docs/viewer-demo-collaboration-review-plan-20260919 §2）：「能看切片」
≠「能看标注」。个人标注默认私有；跨主体可见只经 annotation_grants（0056）；
shared=true 不再全局公开到同片所有分享/用户。

矩阵：
  - users A/B + owner + 切片 X/Y + 独立分享 S1/S2 + 两访客；
  - A 私有标注在 X：B（可看 X）经 /api/annotations、project 聚合、
    /api/share/rois、changes、comments 直访、S2 的 share_roi_list 均不可见；
  - 显式授权（user B / share S1）：被授权方只见该集合，不见其他私有；
  - S1 vs S2 同片：shared-on-token 不跨链接外溢；
  - 同 label 双作者：分组计数不互相泄漏；author 投影稳定（AI 不是人）；
  - 越权读 403/404 不带正文；增量流不泄漏不可见标注的删除/评论负载；
  - 越权写（PATCH/DELETE）403；unclaimed 隔离；owner 工作台非全量 dump；
  - /internal/ai/spots 按读取主体过滤（无 session 只见 source=ai；绑定
    session 属主时按其业务可见性）。
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
import annotation_access  # noqa: E402
import share_server as share_srv  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 分享 ROI 写入的服务端尺寸/MPP 复核需要可信元数据（同 test_access_control：
    # 100000² @ 60µm/px → side 100px = 6.0mm 预设）
    monkeypatch.setattr(
        share_srv, "_slide_dims_and_mpp",
        lambda safe: (100000, 100000, 60.0, 60.0))
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


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_world():
    """A/B/owner 三账号；X 归 A 且 public（B 可看）；Y 归 B。

    返回 (owner, userA, userB, x, y)。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    userB = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    x = _touch("x.svs")
    y = _touch("y.svs")
    share_store.set_slide_meta(x, owner_user_id=userA["user_id"], public=True)
    share_store.set_slide_meta(y, owner_user_id=userB["user_id"])
    return owner, userA, userB, x, y


def _anno(client, slide, label="L", shared=False, note=""):
    return client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": label,
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0,
        "shared": shared, "note": note or "n",
    })


def _client_as(login_id, password):
    c = _client()
    _login(c, login_id, password)
    return c


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _int_token():
    return {"X-AI-Internal-Token": app_mod.AI_INTERNAL_TOKEN}


def _visible_items(body):
    """/api/annotations?slide= 响应 → 全部 item 列表。"""
    out = []
    for grp in body.get("annotations", []):
        out.extend(grp.get("items", []))
    return out


# =========================================================================== #
# 1. 同片双用户：B 可看 X 但看不到 A 的私有标注（全部读通道）
# =========================================================================== #
def test_user_private_annotation_invisible_to_peer_viewer():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    r = _anno(ca, x, label="A-note", note="A-private-note")
    assert r.status_code == 200, r.get_data(as_text=True)
    aid_a = r.get_json()["annotation_id"]
    idx_a = r.get_json()["index"]

    # B 可看 X（public），但 /api/annotations?slide=X 不含 A 的私有标注
    rb = cb.get("/api/annotations?slide=%s" % x)
    assert rb.status_code == 200
    items = _visible_items(rb.get_json())
    assert items == []  # B 在 X 上还没有任何可见标注

    # 默认聚合 / /api/share/rois 也不含
    by_slide_b = cb.get("/api/annotations").get_json()["by_slide"]
    assert by_slide_b.get(x, []) == []
    rois = cb.get("/api/share/rois").get_json()
    assert all(r["annotation_id"] != aid_a for r in rois)

    # changes：A 的 add 事件不出 B 的流
    ch = cb.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    assert all(c.get("annotation_id") != aid_a for c in ch["changes"])
    assert all("A-private-note" not in str(c) for c in ch["changes"])

    # comments 直访（token+index 可枚举）：404 且不带正文
    rc = cb.get("/api/annotation/admin/%d/comments" % idx_a)
    assert rc.status_code == 404
    assert "A-private-note" not in str(rc.get_json())

    # A 自己仍然可见
    items_a = _visible_items(
        ca.get("/api/annotations?slide=%s" % x).get_json())
    assert [i["annotation_id"] for i in items_a] == [aid_a]


def test_same_label_two_authors_groups_do_not_leak():
    owner, userA, userB, x, y = _setup_world()
    # B 经「认领含 annotate 的分享」获得 X 的标注权（业务正路）
    share = share_store.create_share([x], 24, permissions=["view", "annotate"])
    cb = _client_as("b@x.com", "userBpass123456")
    assert cb.post("/api/share/%s/claim" % share["token"]).status_code == 200

    ca = _client_as("a@x.com", "userApass123456")
    r1 = _anno(ca, x, label="L", note="A-L")
    r2 = _anno(cb, x, label="L", note="B-L")
    assert r1.status_code == 200 and r2.status_code == 200

    for client, expect_note in ((ca, "A-L"), (cb, "B-L")):
        body = client.get("/api/annotations?slide=%s" % x).get_json()
        grps = body["annotations"]
        # 同 label 只出现本作者那条：分组 count 不串
        assert len(grps) == 1 and grps[0]["count"] == 1, grps
        assert grps[0]["items"][0]["note"] == expect_note


# =========================================================================== #
# 2. 项目聚合 / roi_count：不计数他人私有
# =========================================================================== #
def test_project_aggregate_and_roi_count_isolated():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    share = share_store.create_share([x], 24, permissions=["view", "annotate"])
    assert cb.post("/api/share/%s/claim" % share["token"]).status_code == 200
    assert _anno(ca, x, label="A").status_code == 200
    assert _anno(cb, x, label="B").status_code == 200

    r = ca.post("/api/project/create", json={"name": "P", "slides": [x]})
    pid = r.get_json()["pid"]
    # A：项目内只见自己的 1 条
    det = ca.get("/api/project/%s" % pid).get_json()
    items = [it for s in det["slide_annotations"] for g in s["annotations"]
             for it in g["items"]]
    assert len(items) == 1 and items[0]["note"] == "n"
    assert ca.get("/api/projects").get_json()[0]["roi_count"] == 1
    # B（切片可见但项目非本人）无法读项目 → 403（既有语义不变）
    assert cb.get("/api/project/%s" % pid).status_code == 403


# =========================================================================== #
# 3. 显式授权（user grant / share grant）：只见授权集合
# =========================================================================== #
def test_explicit_user_grant_scopes_visibility():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    r1 = _anno(ca, x, label="grant-me", note="granted-note")
    r2 = _anno(ca, x, label="private", note="other-private")
    aid1 = r1.get_json()["annotation_id"]
    idx1 = r1.get_json()["index"]
    idx2 = r2.get_json()["index"]

    # 授权前：B 全不可见
    assert _visible_items(cb.get("/api/annotations?slide=%s" % x).get_json()) == []
    # A 显式授予 B（只读）
    rg = ca.patch("/api/annotation/admin/%d" % idx1,
                  json={"grantee_kind": "user", "grantee_id": userB["user_id"]})
    assert rg.status_code == 200, rg.get_data(as_text=True)
    assert any(g["grantee_id"] == userB["user_id"]
               for g in rg.get_json().get("grants", []))

    items = _visible_items(cb.get("/api/annotations?slide=%s" % x).get_json())
    assert [i["annotation_id"] for i in items] == [aid1]
    assert items[0]["can_edit"] is False and items[0]["can_delete"] is False
    # 授权标注的评论可读；未授权那条仍 404
    assert cb.get("/api/annotation/admin/%d/comments" % idx1).status_code == 200
    assert cb.get("/api/annotation/admin/%d/comments" % idx2).status_code == 404
    # 只读授权不给写
    assert cb.patch("/api/annotation/admin/%d" % idx1,
                    json={"note": "pwn"}).status_code == 403

    # 撤销后回到不可见
    rr = ca.patch("/api/annotation/admin/%d" % idx1,
                  json={"grantee_kind": "user", "grantee_id": userB["user_id"],
                        "revoke_grant": True})
    assert rr.status_code == 200
    assert _visible_items(cb.get("/api/annotations?slide=%s" % x).get_json()) == []


def test_explicit_share_grant_and_cross_share_isolation():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    r = _anno(ca, x, label="curated", note="curated-note")
    aid = r.get_json()["annotation_id"]
    idx = r.get_json()["index"]

    s1 = share_store.create_share([x], 24)["token"]
    s2 = share_store.create_share([x], 24)["token"]
    c1 = _share_client()
    c2 = _share_client()

    # 授权前：两个链接的访客都看不到
    l1 = c1.get("/s/%s/api/rois" % s1).get_json()
    l2 = c2.get("/s/%s/api/rois" % s2).get_json()
    assert all(i.get("annotation_id") != aid for i in l1 + l2)

    # A 把该标注显式授予 S1
    rg = ca.patch("/api/annotation/admin/%d" % idx,
                  json={"grantee_kind": "share_token", "grantee_id": s1})
    assert rg.status_code == 200, rg.get_data(as_text=True)

    l1 = c1.get("/s/%s/api/rois" % s1).get_json()
    l2 = c2.get("/s/%s/api/rois" % s2).get_json()
    hits1 = [i for i in l1 if i.get("annotation_id") == aid]
    assert len(hits1) == 1 and hits1[0]["source"] == "admin"
    assert all(i.get("annotation_id") != aid for i in l2)  # S2 不外溢


# =========================================================================== #
# 4. 分享访客隔离：同链接不同访客 / shared-on-own-token 不跨链接
# =========================================================================== #
def test_visitor_records_isolated_by_identity():
    owner, userA, userB, x, y = _setup_world()
    s1 = share_store.create_share([x], 24, permissions=["view", "annotate"])["token"]
    s2 = share_store.create_share([x], 24, permissions=["view", "annotate"])["token"]
    v1 = _share_client()
    v2 = _share_client()
    body = {"slide": x, "type": "rect", "label": "V",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}
    r1 = v1.post("/s/%s/api/roi" % s1, json=body)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    r2 = v2.post("/s/%s/api/roi" % s1, json=dict(body, label="V2"))
    assert r2.status_code == 200

    # 同链接另一访客：只见自己的（source=me），对方私有不可见
    listed_v2 = v2.get("/s/%s/api/rois" % s1).get_json()
    mine_v2 = [i for i in listed_v2 if i["source"] == "me"]
    assert len(mine_v2) == 1
    assert all(i.get("label") != "V" for i in listed_v2)
    # v1 同理
    listed_v1 = v1.get("/s/%s/api/rois" % s1).get_json()
    assert [i for i in listed_v1 if i["source"] == "me"][0]["label"] == "V"
    assert all(i.get("label") != "V2" for i in listed_v1)
    # 越权编辑他人私有：403
    other_idx = next(i["index"] for i in listed_v2 if i["label"] == "V2")
    # v1 拿不到 v2 的 index（不可见），但直接枚举 index 也必须 403/404
    r = v1.patch("/s/%s/api/roi/%d" % (s1, other_idx), json={"note": "pwn"})
    assert r.status_code in (403, 404)

    # 兄弟链接 S2：完全看不到 S1 上的记录
    listed_s2 = v2.get("/s/%s/api/rois" % s2).get_json()
    assert listed_s2 == []


def test_shared_on_own_token_visible_readonly_not_across_links():
    owner, userA, userB, x, y = _setup_world()
    s1 = share_store.create_share([x], 24, permissions=["view", "annotate"])["token"]
    s2 = share_store.create_share([x], 24, permissions=["view", "annotate"])["token"]
    v1 = _share_client()
    v2 = _share_client()
    body = {"slide": x, "type": "rect", "label": "PUB",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}
    rr = v1.post("/s/%s/api/roi" % s1, json=body)
    assert rr.status_code == 200
    idx = rr.get_json()["index"]

    # shared=true（own-token 语义）：S1 的其他访客只读可见；S2 不可见
    assert share_store.set_roi_shared(s1, idx, True) is True
    listed_v2_s1 = v2.get("/s/%s/api/rois" % s1).get_json()
    hits = [i for i in listed_v2_s1 if i.get("label") == "PUB"]
    assert len(hits) == 1 and hits[0]["source"] == "shared"
    # v2（非创建者）不可编辑
    r = v2.patch("/s/%s/api/roi/%d" % (s1, idx), json={"note": "pwn"})
    assert r.status_code == 403
    assert all(i.get("label") != "PUB"
               for i in v2.get("/s/%s/api/rois" % s2).get_json())
    # 关掉后回到私有
    assert share_store.set_roi_shared(s1, idx, False) is True
    assert all(i.get("label") != "PUB"
               for i in v2.get("/s/%s/api/rois" % s1).get_json())


def test_admin_token_shared_true_no_longer_publishes_to_shares():
    """0056 breaking change：admin 标注 shared=true 不再对同片分享链接公开。"""
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    r = _anno(ca, x, label="LEGACY-SHARED", shared=True)
    assert r.status_code == 200
    idx = r.get_json()["index"]
    s1 = share_store.create_share([x], 24)["token"]
    listed = _share_client().get("/s/%s/api/rois" % s1).get_json()
    assert all(i.get("label") != "LEGACY-SHARED" for i in listed)
    # 显式授予该 token 后才可见
    ca.patch("/api/annotation/admin/%d" % idx,
             json={"grantee_kind": "share_token", "grantee_id": s1})
    listed = _share_client().get("/s/%s/api/rois" % s1).get_json()
    assert any(i.get("label") == "LEGACY-SHARED" for i in listed)


# =========================================================================== #
# 5. 增量流：不可见标注的删除/评论负载不泄漏
# =========================================================================== #
def test_changes_stream_no_leak_of_delete_and_comment_payload():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    # B 认领含 annotate 的分享，获得 X 的标注权（业务正路）
    share = share_store.create_share([x], 24, permissions=["view", "annotate"])
    assert cb.post("/api/share/%s/claim" % share["token"]).status_code == 200
    r1 = _anno(ca, x, label="A1", note="A-secret")
    idx1 = r1.get_json()["index"]
    aid1 = r1.get_json()["annotation_id"]
    r2 = _anno(cb, x, label="B1", note="B-own")
    assert r1.status_code == 200 and r2.status_code == 200

    # A 在自己标注下评论，随后删除该标注
    assert ca.post("/api/annotation/admin/%d/comments" % idx1,
                   json={"body": "A-secret-comment"}).status_code == 200
    assert ca.delete("/api/annotation/admin/%d" % idx1).status_code == 200

    ch = cb.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    blob = str(ch["changes"])
    assert aid1 not in blob
    assert "A-secret" not in blob
    assert "A-secret-comment" not in blob
    # B 自己的事件（add）仍在流里
    assert any(c.get("note") == "B-own" for c in ch["changes"])
    # 游标语义保留：水位照常推进
    assert ch["cursor"] >= 1 and ch["reset_required"] is False


# =========================================================================== #
# 6. 越权写：B 不能 PATCH/DELETE A 的私有标注
# =========================================================================== #
def test_peer_cannot_mutate_private_annotation():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    r = _anno(ca, x, label="A-private")
    idx = r.get_json()["index"]
    assert cb.patch("/api/annotation/admin/%d" % idx,
                    json={"note": "pwn"}).status_code == 403
    assert cb.delete("/api/annotation/admin/%d" % idx).status_code == 403
    # A 自己可以
    assert ca.patch("/api/annotation/admin/%d" % idx,
                    json={"note": "ok"}).status_code == 200


# =========================================================================== #
# 7. owner 工作台不是全量 dump；管理清点走 store 级报表
# =========================================================================== #
def test_owner_workbench_not_a_dump_of_private_notes():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    co = _client_as("owner@x.com", "ownerpass123456")
    r = _anno(ca, x, label="A-private", note="should-not-appear")
    aid = r.get_json()["annotation_id"]

    # owner 显式收录 X（切片可见）后：切片可见，但标注仍不可见
    assert co.post("/api/admin/v1/slides/%s/visibility" % x,
                   json={"granted": True}).status_code == 200
    assert co.get("/api/slide/%s/info" % x).status_code != 403
    assert all(i["annotation_id"] != aid
               for i in co.get("/api/share/rois").get_json())
    body = co.get("/api/annotations?slide=%s" % x).get_json()
    assert _visible_items(body) == []
    ch = co.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    assert aid not in str(ch["changes"])
    # owner 写特权保留（既有语义）：仍可删（mutate 与 read 分离）
    idx = r.get_json()["index"]
    assert co.delete("/api/annotation/admin/%d" % idx).status_code == 200


# =========================================================================== #
# 8. unclaimed 隔离 + 审计报表
# =========================================================================== #
def test_unclaimed_annotations_isolated_from_normal_lists():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    # 无 owner 且无 visitor（owner 注入清空后直接落库）→ unclaimed
    share_store.set_owner_user_id("")
    orphan = share_store.add_roi("admin", x, "ORPHAN", type="rect",
                                 x=0, y=0, side_px=10, size_mm=6.0)
    share_store.set_owner_user_id(owner["user_id"])
    aid = orphan["annotation_id"]

    assert _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json()) == []
    ch = ca.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    assert aid not in str(ch["changes"])
    # owner（认证）也不可见；管理清点（store 级 / 报表）可见
    co = _client_as("owner@x.com", "ownerpass123456")
    assert _visible_items(co.get("/api/annotations?slide=%s" % x).get_json()) == []
    store_all = share_store.annotations_by_slide()
    assert any(i["annotation_id"] == aid
               for g in store_all.get(x, []) for i in g["items"])
    report = share_store.annotation_visibility_report()
    assert report["unclaimed"]["count"] >= 1


# =========================================================================== #
# 9. AI / internal spots：按读取主体过滤
# =========================================================================== #
def test_internal_ai_spots_fail_closed_without_session():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    assert _anno(ca, x, label="human-private",
                 note="HUMAN-SECRET-NOTE").status_code == 200
    ai = share_store.add_roi("admin", x, "AI", type="rect",
                             x=1, y=1, side_px=10, source="ai",
                             created_by_session_id="sess-ai",
                             provenance={"plugin_id": "histopilot",
                                         "session_id": "sess-ai"})
    client = app_mod.app.test_client()
    # 无 internal token → 401
    assert client.get("/internal/ai/spots?slide=%s" % x).status_code == 401
    # 无 session_id：空集合。不得凭 source=ai 跨用户放行。
    r = client.get("/internal/ai/spots?slide=%s&after_seq=0" % x,
                   headers=_int_token())
    assert r.status_code == 200
    blob = str(r.get_json()["changes"])
    assert ai["annotation_id"] not in blob
    assert "HUMAN-SECRET-NOTE" not in blob
    assert "human-private" not in blob


def test_internal_ai_spots_session_owner_scoped():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    # B 认领含 annotate 的分享，获得 X 的标注权（业务正路）
    share = share_store.create_share([x], 24, permissions=["view", "annotate"])
    assert cb.post("/api/share/%s/claim" % share["token"]).status_code == 200
    ra = _anno(ca, x, label="A-own", note="A-note")
    rb = _anno(cb, x, label="B-own", note="B-note")
    assert ra.status_code == 200 and rb.status_code == 200
    # 绑定 A 的 run grant + session
    grant = share_store.create_run_grant("inst_test", x, session_id="sess-a",
                                         created_by_user_id=userA["user_id"])
    assert grant["grant_id"]
    client = app_mod.app.test_client()
    r = client.get("/internal/ai/spots?slide=%s&after_seq=0&session_id=sess-a" % x,
                   headers=_int_token())
    assert r.status_code == 200
    blob = str(r.get_json()["changes"])
    assert ra.get_json()["annotation_id"] in blob  # 属主自己的可见
    assert rb.get_json()["annotation_id"] not in blob  # B 的私有不进 A 的会话


# =========================================================================== #
# 10. 作者投影与能力位（people 口径）
# =========================================================================== #
def test_author_projection_kinds():
    roi_ai = {"source": "ai", "annotation_id": "aid-ai",
              "created_by_session_id": "s1",
              "provenance": {"plugin_id": "histopilot"}}
    assert annotation_access.author_projection(roi_ai)["author_kind"] == "ai"
    # 工作台未公开标注（写路径推断 source=ai 但无会话/溯源）：按真实作者计
    roi_inferred = {"source": "ai", "token": "admin", "owner_user_id": "usr_x",
                    "annotation_id": "aid-inf"}
    assert annotation_access.author_projection(
        roi_inferred, label_of_user=lambda uid: "a@x.com")["author_kind"] == "user"
    roi_vis = {"source": "human", "visitor": "h1.abcdef0123456789",
               "annotation_id": "aid-v"}
    proj = annotation_access.author_projection(roi_vis)
    assert proj["author_kind"] == "visitor"
    assert proj["author_key"].startswith("visitor:")
    assert "h1.abcdef0123456789" not in proj["author_key"]  # 不回传完整哈希
    roi_user = {"source": "human", "owner_user_id": "usr_x", "token": "admin",
                "annotation_id": "aid-u"}
    proj = annotation_access.author_projection(
        roi_user, label_of_user=lambda uid: "a@x.com")
    assert proj == {"author_key": "user:usr_x", "author_kind": "user",
                    "author_label_safe": "a@x.com"}
    # 未知作者不造人
    assert annotation_access.author_projection(
        {"annotation_id": "aid-?"})["author_kind"] == "unknown"
    # 列表响应带作者与能力位
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    assert _anno(ca, x).status_code == 200
    items = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    assert items[0]["author_kind"] == "user"
    assert items[0]["can_edit"] is True and items[0]["can_delete"] is True


# =========================================================================== #
# 11. 幂等创建（client_action_id）
# =========================================================================== #
def test_client_action_id_idempotent_create():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    r1 = ca.post("/api/annotation", json={
        "slide": x, "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0, "client_action_id": "act-1"})
    r2 = ca.post("/api/annotation", json={
        "slide": x, "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0, "client_action_id": "act-1"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["annotation_id"] == r2.get_json()["annotation_id"]
    items = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    assert len(items) == 1


# =========================================================================== #
# 12. 本地免认证单租户态：local_owner 全量（既有不变量）
# =========================================================================== #
def test_noauth_local_owner_sees_all():
    _setup_world()
    share_store.set_owner_user_id("")
    share_store.add_roi("admin", "x.svs", "ORPHAN", type="rect",
                        x=0, y=0, side_px=10, size_mm=6.0)
    app_mod.AUTH_ENABLED = False
    c = csrf_client(app_mod.app.test_client())
    body = c.get("/api/annotations?slide=x.svs").get_json()
    items = _visible_items(body)
    assert len(items) == 1  # unclaimed 对 local_owner 可见（单租户例外）


# =========================================================================== #
# 13. review R1–R6：未绑定 AI、token 投影、授权增量、恢复 tombstone
# =========================================================================== #
def test_unbound_ai_subject_reads_nothing():
    roi_b = {
        "annotation_id": "aid-b", "token": "admin", "slide": "x.svs",
        "source": "ai", "created_by_session_id": "session-B",
        "provenance": {"plugin_id": "histopilot"},
        "owner_user_id": "usr_b", "shared": False,
        "x": 1, "y": 1, "w": 10, "h": 10, "type": "rect",
    }
    unbound = annotation_access.ai_subject()
    assert annotation_access.can_read_annotation(unbound, roi_b) is False
    a_user = annotation_access.user_subject("usr_a", "user")
    assert annotation_access.can_read_annotation(a_user, roi_b) is False


def test_grant_does_not_leak_source_share_token():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    share = share_store.create_share([x], 24, permissions=["view", "annotate"],
                                     creator_user_id=userA["user_id"])
    roi = share_store.add_roi(share["token"], x, "from-s1", type="rect",
                              x=0, y=0, side_px=10, size_mm=6.0,
                              owner_user_id=userA["user_id"])
    aid = roi["annotation_id"]
    share_store.grant_annotation_to_user(aid, userB["user_id"])
    items = _visible_items(cb.get("/api/annotations?slide=%s" % x).get_json())
    assert any(it.get("annotation_id") == aid for it in items)
    for it in items:
        if it.get("annotation_id") == aid:
            assert it.get("token") != share["token"]
            assert "token" not in it or it.get("token") == "admin"


def test_grant_revoke_emits_access_change_for_grantee():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    r = _anno(ca, x, label="share-me")
    assert r.status_code == 200
    aid = r.get_json()["annotation_id"]
    before = share_store.current_change_seq(x)
    share_store.grant_annotation_to_user(aid, userB["user_id"])
    after_grant = share_store.current_change_seq(x)
    assert after_grant > before
    changes = cb.get(
        "/api/annotations/changes?slide=%s&after=%s" % (x, before)).get_json()
    access = [c for c in changes.get("changes") or [] if c.get("type") == "access"]
    assert access and access[0]["op"] == "grant"
    assert access[0]["annotation_id"] == aid
    assert "token" not in access[0]
    share_store.revoke_grant(aid, "user", userB["user_id"])
    after_rev = share_store.current_change_seq(x)
    assert after_rev > after_grant
    changes2 = cb.get(
        "/api/annotations/changes?slide=%s&after=%s" % (x, after_grant)).get_json()
    revokes = [c for c in changes2.get("changes") or []
               if c.get("type") == "access" and c.get("op") == "revoke"]
    assert revokes and revokes[0].get("reset_required") is True


def test_restore_after_delete_same_client_action_id():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    created = ca.post("/api/annotation", json={
        "slide": x, "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0, "client_action_id": "act-restore"})
    assert created.status_code == 200
    aid = created.get_json()["annotation_id"]
    rev = created.get_json()["revision"]
    deleted = ca.delete("/api/annotation/id/%s" % aid,
                        json={"expected_revision": rev})
    assert deleted.status_code == 200
    tomb_rev = deleted.get_json().get("revision")
    assert tomb_rev and tomb_rev > rev
    restored = ca.post("/api/annotation/id/%s/restore" % aid,
                       json={"expected_revision": tomb_rev})
    assert restored.status_code == 200
    items = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    assert any(it.get("annotation_id") == aid and not it.get("deleted")
               for it in items)
    # 再 INSERT 同键不得再造一条
    again = ca.post("/api/annotation", json={
        "slide": x, "type": "rect", "label": "L", "x": 0, "y": 0,
        "side_px": 100, "size_mm": 6.0, "client_action_id": "act-restore"})
    assert again.status_code == 200
    assert again.get_json()["annotation_id"] == aid
    items2 = _visible_items(ca.get("/api/annotations?slide=%s" % x).get_json())
    assert len([it for it in items2 if it.get("annotation_id") == aid]) == 1


def test_comments_do_not_leak_source_share_token():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    cb = _client_as("b@x.com", "userBpass123456")
    share = share_store.create_share([x], 24, permissions=["view", "annotate"],
                                     creator_user_id=userA["user_id"])
    roi = share_store.add_roi(share["token"], x, "from-s1", type="rect",
                              x=0, y=0, side_px=10, size_mm=6.0,
                              owner_user_id=userA["user_id"])
    aid = roi["annotation_id"]
    share_store.add_comment(aid, x, share["token"], "secret-comment",
                            author_user_id=userA["user_id"])
    share_store.grant_annotation_to_user(aid, userB["user_id"])
    ch = cb.get("/api/annotations/changes?slide=%s&after=0" % x).get_json()
    for c in ch.get("changes") or []:
        if c.get("type") == "comment":
            assert c.get("token") != share["token"]
            assert "secret-comment" in str(c.get("body"))
    listed = share_store.list_comments(annotation_id=aid,
                                       subject=annotation_access.user_subject(
                                           userB["user_id"], "user"))
    assert listed
    assert all(c.get("token") != share["token"] for c in listed)


def test_principal_bind_does_not_overwrite_owner():
    share_store.upsert_ai_session_principal("sess-victim", "user-A", "x.svs")
    share_store.upsert_ai_session_principal("sess-victim", "user-B", "x.svs")
    got = share_store.get_ai_session_principal("sess-victim")
    assert got["user_id"] == "user-A"


def test_delete_by_id_missing_is_404_not_index_fallback():
    owner, userA, userB, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    r = ca.delete("/api/annotation/id/does-not-exist",
                  json={"expected_revision": 1})
    assert r.status_code == 404
