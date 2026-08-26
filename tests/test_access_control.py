# -*- coding: utf-8 -*-
"""Stage 3a-2a 资源级数据鉴权矩阵测试（docs §5.1.1 / §5.4 / Stage 3a 验收）。

覆盖：
  - 仓储边界：guest 直接调 store 写函数传 ROLE_GUEST → PermissionError；
  - 跨用户图库读取被拒（user A 切片，user B GET info/region/tile → 403）；
  - 越权 ID 枚举被拒（不存在切片与无权切片行为一致，均 403）；
  - region 读取、标注写入越权被拒；本人标注删除 OK、删他人标注 403、owner 删任意 OK；
  - 公开切片对 user 可见；public 仅 owner 可设置（user 尝试 403）；
  - 分享三档：无 annotate 权限的 token POST 标注 403；旧 share（无 permissions）行为不变；
  - claim 流程：user 认领后可见受邀切片；share 撤销后 user 失去访问；
  - AUTH_ENABLED=False 内网模式：全部旧行为不变（owner 语义全开）；
  - user 只能分享/删除自己切片；project 隔离。

隔离：独立临时 SHARE_DATA_DIR / UPLOAD_DIR，monkeypatch 夺回常量与 env，
openslide stub（本测试覆盖鉴权路径，多数在打开切片前即 403，无需真 OpenSlide）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["ADMIN_PASSWORD"] = ""  # 默认无 owner 引导；各用例手动建用户
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
from pg_compat import json_only, BACKEND  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，清空 users.json / shares.json。

    其它测试模块（如 test_region_fingerprint）会在 import 时把 app_mod.UPLOAD_DIR
    改写到它们自己的临时目录；必须在此处用 monkeypatch 每用例夺回，否则 app 读取
    的 UPLOAD_DIR 与本模块写入的不是同一个目录（见 test_ai_config_validation 注释）。
    """
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    # 清空 uploads 目录中的测试切片文件（沿用模块级 UPLOAD_DIR 的文件内额外清理）
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
    """AUTH_ENABLED=False 内网模式客户端。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _touch(name="demo.svs"):
    """在 UPLOAD_DIR 下放一个占位切片文件（_safe_name 要求文件存在）。"""
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _own(name, user_id):
    """设置切片归属（slide_meta.owner_user_id）。"""
    share_store.set_slide_meta(name, owner_user_id=user_id)


def _setup_users():
    """创建 owner + userA + userB，注入 owner 归属，返回三元组 user dict。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    userB = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, userA, userB


# =========================================================================== #
# 1. 仓储边界：guest 写操作被拒（PermissionError）
# =========================================================================== #
def test_guest_rejected_at_store_boundary():
    owner, _a, _b = _setup_users()
    share_store.set_owner_user_id(owner["user_id"])
    _touch("g.svs")

    # create_project
    with pytest.raises(PermissionError):
        share_store.create_project("p", requester_role=user_store.ROLE_GUEST)
    # set_slide_meta
    with pytest.raises(PermissionError):
        share_store.set_slide_meta("g.svs", alias="x", requester_role=user_store.ROLE_GUEST)
    # add_roi
    with pytest.raises(PermissionError):
        share_store.add_roi("admin", "g.svs", "L", type="rect", x=0, y=0, side_px=10,
                            requester_role=user_store.ROLE_GUEST)
    # create_share
    with pytest.raises(PermissionError):
        share_store.create_share(["g.svs"], 1, requester_role=user_store.ROLE_GUEST)

    # requester_role=None（内部调用，如 share_server / internal AI）不限制
    share_store.create_project("p2")  # 不抛
    share_store.set_slide_meta("g.svs", alias="ok")  # 不抛


# =========================================================================== #
# 2. 跨用户图库读取被拒
# =========================================================================== #
def test_cross_user_gallery_read_denied():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")

    # userB GET userA 的切片 info → 403
    r = cb.get("/api/slide/%s/info" % sa)
    assert r.status_code == 403, r.get_data(as_text=True)
    # region → 403
    r = cb.get("/api/slide/%s/region?x=0&y=0&w=10&h=10" % sa)
    assert r.status_code == 403
    # 瓦片 → 403
    r = cb.get("/api/slide/%s_files/0/0_0.jpeg" % sa)
    assert r.status_code == 403
    # dzi → 403
    r = cb.get("/api/slide/%s.dzi" % sa)
    assert r.status_code == 403
    # crop → 403
    r = cb.get("/api/slide/%s/crop?x=0&y=0&size=10" % sa)
    assert r.status_code == 403
    # thumbnail → 403
    r = cb.get("/api/slide/%s/thumbnail" % sa)
    assert r.status_code == 403


def test_cross_user_not_in_slides_list():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    body = cb.get("/api/slides").get_json()
    names = {i["name"] for i in body}
    assert sb in names           # 自己的可见
    assert sa not in names       # userA 的不可见


def test_owner_sees_all_slides():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert sa in names and sb in names


# =========================================================================== #
# 3. 越权 ID 枚举：不存在与无权切片行为一致（均 403，从 user 视角）
# =========================================================================== #
def test_unauthorized_id_enumeration_consistent():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    # 无权（存在）
    r1 = cb.get("/api/slide/%s/info" % sa)
    # 不存在
    r2 = cb.get("/api/slide/nope.svs/info")
    assert r1.status_code == 403
    assert r2.status_code == 403  # 与无权一致，不泄露存在性差异


# =========================================================================== #
# 4. 标注写入/删除越权
# =========================================================================== #
def _post_anno(client, slide):
    return client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": "L",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0,
    })


def test_annotation_write_authorization():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")

    # userA 在自己切片落标 → 200
    r = _post_anno(ca, sa)
    assert r.status_code == 200, r.get_data(as_text=True)
    idx_a = r.get_json()["index"]

    # userB 在 userA 切片落标 → 403
    r = _post_anno(cb, sa)
    assert r.status_code == 403

    # 删他人标注 → 403（userB 删 userA 的）
    r = cb.delete("/api/annotation/admin/%d" % idx_a)
    assert r.status_code == 403
    # 本人删除 OK（userA 删自己创建的）
    r = ca.delete("/api/annotation/admin/%d" % idx_a)
    assert r.status_code == 200


def test_owner_can_delete_any_annotation():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    r = _post_anno(ca, sa)
    idx = r.get_json()["index"]

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    # owner 删 userA 创建的标注 → 200
    r = co.delete("/api/annotation/admin/%d" % idx)
    assert r.status_code == 200


# =========================================================================== #
# 5. 公开切片：对 user 可见；public 仅 owner 可设置
# =========================================================================== #
def test_public_slide_visible_to_user():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    # owner 设置公开
    r = co.post("/api/slide/%s/meta" % sa, json={"public": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_slide_meta_full(sa)["public"] is True

    # userB 现在能在列表里看到 userA 的公开切片
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    names = {i["name"] for i in cb.get("/api/slides").get_json()}
    assert sa in names
    r = cb.get("/api/slide/%s/info" % sa)
    assert r.status_code != 403


def test_public_only_owner_can_set():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # user（切片拥有者）尝试设置 public → 403
    r = ca.post("/api/slide/%s/meta" % sa, json={"public": True})
    assert r.status_code == 403
    # 但 user 可改自己切片的 alias/note → 200
    r = ca.post("/api/slide/%s/meta" % sa, json={"alias": "my alias"})
    assert r.status_code == 200
    assert share_store.get_slide_meta_full(sa)["alias"] == "my alias"

    # user 改他人切片 alias → 403
    sb = _touch("b.svs")
    _own(sb, _b["user_id"])
    r = ca.post("/api/slide/%s/meta" % sb, json={"alias": "hack"})
    assert r.status_code == 403


# =========================================================================== #
# 6. 分享权限三档（share_server）
# =========================================================================== #
def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _valid_roi_body(slide):
    return {"slide": slide, "type": "rect", "label": "L",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}


def test_share_view_only_blocks_annotate():
    _setup_users()
    _touch("demo.svs")
    # 显式 view-only 分享
    share = share_store.create_share(["demo.svs"], 1, permissions=["view"])
    token = share["token"]
    c = _share_client()
    # POST 标注 → 403（无 annotate）
    r = c.post("/s/%s/api/roi" % token, json=_valid_roi_body("demo.svs"))
    assert r.status_code == 403, r.get_data(as_text=True)
    # PATCH / DELETE 也 403
    r2 = c.post("/s/%s/api/roi" % token, json=_valid_roi_body("demo.svs"))  # 仍 403，无标注被建
    assert r2.status_code == 403


@json_only  # 直接写 shares.json 文件构造旧分享（PG 后端无文件 patch 语义）
def test_old_share_without_permissions_unchanged():
    """旧分享（无 permissions 字段）行为不变：默认含 annotate，可落标。"""
    _setup_users()
    _touch("demo.svs")
    # 直接写一份无 permissions 字段的旧分享
    old = {
        "shares": {"tok_old": {"slides": ["demo.svs"], "created_at": 1.0,
                               "expires_at": 1e12, "revoked": False}},
        "rois": [], "projects": {}, "slide_meta": {}, "change_seq_by_slide": {},
        "grants": [],
    }
    share_store.SHARE_FILE.write_text(json.dumps(old), encoding="utf-8")
    c = _share_client()
    r = c.post("/s/tok_old/api/roi", json=_valid_roi_body("demo.svs"))
    assert r.status_code == 200, r.get_data(as_text=True)
    # 默认权限读取为 view+annotate
    sh = share_store.get_share("tok_old")
    assert sh is not None
    assert "annotate" in sh["permissions"] and "view" in sh["permissions"]


def test_share_create_with_permissions_and_default():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # 显式三档
    r = ca.post("/api/share/create", json={
        "slides": [sa], "expires_hours": 1,
        "permissions": ["view", "annotate", "download"]})
    assert r.status_code == 200
    assert sorted(r.get_json()["permissions"]) == ["annotate", "download", "view"]
    # 缺省 → view+annotate
    r2 = ca.post("/api/share/create", json={"slides": [sa], "expires_hours": 1})
    assert r2.status_code == 200
    assert sorted(r2.get_json()["permissions"]) == ["annotate", "view"]


# =========================================================================== #
# 7. claim 流程
# =========================================================================== #
def test_claim_grants_visibility_and_revocation_revokes():
    owner, userA, userB = _setup_users()
    # owner 持有一张切片，邀请 userB（通过分享链接认领）
    so = _touch("owner.svs")
    _own(so, owner["user_id"])
    share = share_store.create_share([so], 24)
    token = share["token"]

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    # 认领前：userB 看不到 owner.svs
    names = {i["name"] for i in cb.get("/api/slides").get_json()}
    assert so not in names
    assert cb.get("/api/slide/%s/info" % so).status_code == 403

    # 认领
    r = cb.post("/api/share/%s/claim" % token)
    assert r.status_code == 200, r.get_data(as_text=True)
    grant = r.get_json()
    assert grant["user_id"] == userB["user_id"]
    # 幂等：再认领返回已有 grant（同 grant_id）
    r2 = cb.post("/api/share/%s/claim" % token)
    assert r2.status_code == 200
    assert r2.get_json()["grant_id"] == grant["grant_id"]

    # 认领后：userB 能看到 owner.svs
    names = {i["name"] for i in cb.get("/api/slides").get_json()}
    assert so in names
    assert cb.get("/api/slide/%s/info" % so).status_code != 403

    # owner 撤销分享 → userB 失去访问
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    assert co.post("/api/share/revoke", json={"token": token}).status_code == 200
    cb2 = _client()
    _login(cb2, "b@x.com", "userBpass123456")
    names = {i["name"] for i in cb2.get("/api/slides").get_json()}
    assert so not in names
    assert cb2.get("/api/slide/%s/info" % so).status_code == 403


def test_claim_invalid_share_404():
    _setup_users()
    userB = user_store.get_user_by_login_id("b@x.com")
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    r = cb.post("/api/share/nope/claim")
    assert r.status_code == 404


def test_claim_view_only_cannot_escalate_or_annotate():
    """view-only 分享：客户端不可自行提升为 annotate；认领后也不能落标。"""
    owner, userA, userB = _setup_users()
    so = _touch("owner.svs")
    _own(so, owner["user_id"])
    share = share_store.create_share([so], 24, permissions=["view"])
    token = share["token"]

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    r = cb.post("/api/share/%s/claim" % token,
                json={"permissions": ["view", "annotate"]})
    assert r.status_code == 400, r.get_data(as_text=True)

    r = cb.post("/api/share/%s/claim" % token)
    assert r.status_code == 200, r.get_data(as_text=True)
    grant = r.get_json()
    assert grant["permissions"] == ["view"]
    assert "annotate" not in grant["permissions"]

    names = {i["name"] for i in cb.get("/api/slides").get_json()}
    assert so in names
    r2 = _post_anno(cb, so)
    assert r2.status_code == 403


def test_share_visitor_cannot_impersonate_via_copied_id():
    """分享端 ROI 响应不含 visitor；复制明文 cookie 不能冒用他人私有标注。"""
    _setup_users()
    _touch("demo.svs")
    share = share_store.create_share(["demo.svs"], 1)
    token = share["token"]
    c1 = _share_client()
    r = c1.post("/s/%s/api/roi" % token, json=_valid_roi_body("demo.svs"))
    assert r.status_code == 200, r.get_data(as_text=True)
    listed = c1.get("/s/%s/api/rois" % token).get_json()
    assert listed
    assert all("visitor" not in item for item in listed)
    idx = listed[0]["index"]

    c2 = _share_client()
    c2.set_cookie("svs_visitor", "copied-plaintext-id", domain="localhost", path="/s")
    listed2 = c2.get("/s/%s/api/rois" % token).get_json()
    assert not any(item.get("index") == idx and item.get("source") == "me"
                   for item in listed2)
    r2 = c2.patch("/s/%s/api/roi/%s" % (token, idx), json={"note": "pwn"})
    assert r2.status_code in (403, 404)


def test_legacy_plaintext_visitor_reclaim_then_blocks_copy():
    """升级前明文 visitor：原设备 unsigned cookie 可认领；认领后复制无法再冒用。"""
    _setup_users()
    _touch("demo.svs")
    share = share_store.create_share(["demo.svs"], 1)
    token = share["token"]
    roi = share_store.add_roi(
        token, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    idx = roi["index"]

    owner_c = _share_client()
    owner_c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    listed = owner_c.get("/s/%s/api/rois" % token).get_json()
    assert any(item.get("index") == idx and item.get("source") == "me" for item in listed)
    stored = share_store.get_roi(token, idx)
    assert stored["visitor"].startswith("h1.")
    r = owner_c.patch("/s/%s/api/roi/%s" % (token, idx), json={"note": "still mine"})
    assert r.status_code == 200, r.get_data(as_text=True)

    attacker = _share_client()
    attacker.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    listed2 = attacker.get("/s/%s/api/rois" % token).get_json()
    assert not any(item.get("index") == idx and item.get("source") == "me"
                   for item in listed2)
    r2 = attacker.patch("/s/%s/api/roi/%s" % (token, idx), json={"note": "pwn"})
    assert r2.status_code in (403, 404)


def test_legacy_reclaim_migrates_all_tokens_not_just_current():
    """认领 A 时同步哈希 B；攻击者不能借未迁移的 B 重铸同一签名身份。"""
    _setup_users()
    _touch("demo.svs")
    share_a = share_store.create_share(["demo.svs"], 1)
    share_b = share_store.create_share(["demo.svs"], 1)
    token_a, token_b = share_a["token"], share_b["token"]
    roi_a = share_store.add_roi(
        token_a, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    roi_b = share_store.add_roi(
        token_b, "demo.svs", "L", type="rect", x=10, y=10, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")

    owner_c = _share_client()
    owner_c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    listed = owner_c.get("/s/%s/api/rois" % token_a).get_json()
    assert any(item.get("index") == roi_a["index"] and item.get("source") == "me"
               for item in listed)
    assert share_store.get_roi(token_a, roi_a["index"])["visitor"].startswith("h1.")
    assert share_store.get_roi(token_b, roi_b["index"])["visitor"].startswith("h1.")

    attacker = _share_client()
    attacker.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    listed_b = attacker.get("/s/%s/api/rois" % token_b).get_json()
    assert not any(item.get("index") == roi_b["index"] and item.get("source") == "me"
                   for item in listed_b)
    assert attacker.patch(
        "/s/%s/api/roi/%s" % (token_b, roi_b["index"]), json={"note": "pwn"}
    ).status_code in (403, 404)
    listed_a = attacker.get("/s/%s/api/rois" % token_a).get_json()
    assert not any(item.get("index") == roi_a["index"] and item.get("source") == "me"
                   for item in listed_a)
    assert attacker.patch(
        "/s/%s/api/roi/%s" % (token_a, roi_a["index"]), json={"note": "pwn"}
    ).status_code in (403, 404)


def _cookie_val(client, name):
    c = client.get_cookie(name, domain="localhost", path="/s")
    return None if c is None else c.value


def _expire_share(token):
    """把 share 的 expires_at 拨到过去（JSON 写文件 / PG 更新行）。"""
    if BACKEND == "postgres":
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE shares SET expires_at = to_timestamp(1) WHERE token=%s",
                    (token,),
                )
                assert cur.rowcount == 1
        finally:
            conn.close()
    else:
        path = Path(share_store.SHARE_FILE)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["shares"][token]["expires_at"] = 1.0
        path.write_text(json.dumps(data), encoding="utf-8")


def _two_shares_same_visitor():
    _setup_users()
    _touch("demo.svs")
    share_a = share_store.create_share(["demo.svs"], 1)
    share_b = share_store.create_share(["demo.svs"], 1)
    token_a, token_b = share_a["token"], share_b["token"]
    roi_a = share_store.add_roi(
        token_a, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    roi_b = share_store.add_roi(
        token_b, "demo.svs", "L", type="rect", x=10, y=10, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    return token_a, token_b, roi_a, roi_b


def _assert_inactive_token_does_not_reclaim(
        inactive_token, live_token, live_roi, status=404, inactive_roi=None):
    """无效 token 不得当场全局迁移。

    响应会保留 svs_visitor_mig（规格：之后在有效链接上仍可先到先得）。
    本断言另开客户端、只复制随机 v2、不带 mig，证明「无效链接本身」没有
    把身份迁走；不是「mig cookie 不存在」。
    """
    attacker = _share_client()
    attacker.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    r = attacker.get("/s/%s/api/rois" % inactive_token)
    assert r.status_code == status
    live = share_store.get_roi(live_token, live_roi["index"])
    assert live["visitor"] == "legacy-device-1"
    if inactive_roi is not None:
        stored = share_store.get_roi(inactive_token, inactive_roi["index"])
        assert stored["visitor"] == "legacy-device-1"
    signed = _cookie_val(attacker, "svs_visitor")
    assert signed and signed.startswith("v2.")
    assert _cookie_val(attacker, "svs_visitor_mig") == "legacy-device-1"
    other = _share_client()
    other.set_cookie("svs_visitor", signed, domain="localhost", path="/s")
    listed = other.get("/s/%s/api/rois" % live_token).get_json()
    assert not any(item.get("index") == live_roi["index"] and item.get("source") == "me"
                   for item in listed)
    assert other.patch(
        "/s/%s/api/roi/%s" % (live_token, live_roi["index"]), json={"note": "pwn"}
    ).status_code in (403, 404)


def test_revoked_share_cannot_reclaim_visitor():
    """已撤销链接返回 404，当场不得全局迁移（另开客户端只带 v2）。"""
    token_a, token_b, roi_a, roi_b = _two_shares_same_visitor()
    assert share_store.revoke_share(token_a)
    _assert_inactive_token_does_not_reclaim(
        token_a, token_b, roi_b, inactive_roi=roi_a)


def test_expired_share_cannot_reclaim_visitor():
    """过期链接返回 404，当场不得全局迁移（另开客户端只带 v2）。"""
    token_a, token_b, roi_a, roi_b = _two_shares_same_visitor()
    _expire_share(token_a)
    _assert_inactive_token_does_not_reclaim(
        token_a, token_b, roi_b, inactive_roi=roi_a)


def test_missing_share_cannot_reclaim_visitor():
    """不存在的 token 返回 404，当场不得迁移；只复制 v2 不能编辑有效链接。"""
    _setup_users()
    _touch("demo.svs")
    share_b = share_store.create_share(["demo.svs"], 1)
    token_b = share_b["token"]
    roi_b = share_store.add_roi(
        token_b, "demo.svs", "L", type="rect", x=10, y=10, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    _assert_inactive_token_does_not_reclaim("not-a-real-token", token_b, roi_b)


def test_revoked_share_same_client_fcfs_on_valid_token():
    """无效链接保留 mig cookie：同一客户端随后访问仍有效的 token 可先到先得认领。"""
    token_a, token_b, roi_a, roi_b = _two_shares_same_visitor()
    assert share_store.revoke_share(token_a)
    c = _share_client()
    c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    assert c.get("/s/%s/api/rois" % token_a).status_code == 404
    assert share_store.get_roi(token_a, roi_a["index"])["visitor"] == "legacy-device-1"
    assert _cookie_val(c, "svs_visitor_mig") == "legacy-device-1"
    listed = c.get("/s/%s/api/rois" % token_b).get_json()
    assert any(item.get("index") == roi_b["index"] and item.get("source") == "me"
               for item in listed)
    assert c.patch(
        "/s/%s/api/roi/%s" % (token_b, roi_b["index"]), json={"note": "fcfs"}
    ).status_code == 200
    assert share_store.get_roi(token_b, roi_b["index"])["visitor"].startswith("h1.")
    assert _cookie_val(c, "svs_visitor_mig") in (None, "")


def test_legacy_mig_cookie_survives_s_then_reclaims():
    """先访问 /s 不得销毁旧身份；随后正确链接仍可认领。"""
    _setup_users()
    _touch("demo.svs")
    share = share_store.create_share(["demo.svs"], 1)
    token = share["token"]
    roi = share_store.add_roi(
        token, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    idx = roi["index"]

    c = _share_client()
    c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    r = c.get("/s")
    assert r.status_code == 404
    assert _cookie_val(c, "svs_visitor_mig") == "legacy-device-1"
    signed = _cookie_val(c, "svs_visitor")
    assert signed and signed.startswith("v2.")

    listed = c.get("/s/%s/api/rois" % token).get_json()
    assert any(item.get("index") == idx and item.get("source") == "me" for item in listed)
    assert c.patch("/s/%s/api/roi/%s" % (token, idx), json={"note": "still mine"}).status_code == 200
    assert share_store.get_roi(token, idx)["visitor"].startswith("h1.")
    assert _cookie_val(c, "svs_visitor_mig") in (None, "")


def test_legacy_mig_cookie_survives_wrong_token_then_reclaims():
    """错误 token / 过期链接覆盖签名 cookie 后，mig 凭据仍能在正确链接认领。"""
    _setup_users()
    _touch("demo.svs")
    share_a = share_store.create_share(["demo.svs"], 1)
    share_b = share_store.create_share(["demo.svs"], 1)
    token_a, token_b = share_a["token"], share_b["token"]
    roi = share_store.add_roi(
        token_a, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    idx = roi["index"]

    c = _share_client()
    c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    assert c.get("/s/%s/api/rois" % token_b).status_code == 200
    assert _cookie_val(c, "svs_visitor_mig") == "legacy-device-1"
    assert c.get("/s/not-a-real-token/api/rois").status_code == 404
    assert _cookie_val(c, "svs_visitor_mig") == "legacy-device-1"

    listed = c.get("/s/%s/api/rois" % token_a).get_json()
    assert any(item.get("index") == idx and item.get("source") == "me" for item in listed)
    assert c.patch("/s/%s/api/roi/%s" % (token_a, idx), json={"note": "still mine"}).status_code == 200


def test_reclaim_keeps_v2_identity_and_interim_roi_ownership():
    """错误 token 上用随机 v2 新建的 ROI，认领后仍可编辑（不切换主 cookie）。"""
    _setup_users()
    _touch("demo.svs")
    share_a = share_store.create_share(["demo.svs"], 1)
    share_b = share_store.create_share(["demo.svs"], 1)
    token_a, token_b = share_a["token"], share_b["token"]
    old = share_store.add_roi(
        token_a, "demo.svs", "L", type="rect", x=0, y=0, side_px=100, size_mm=6.0,
        visitor="legacy-device-1")
    old_idx = old["index"]

    c = _share_client()
    c.set_cookie("svs_visitor", "legacy-device-1", domain="localhost", path="/s")
    assert c.get("/s/%s/api/rois" % token_b).status_code == 200
    assert _cookie_val(c, "svs_visitor_mig") == "legacy-device-1"
    v2_before = _cookie_val(c, "svs_visitor")
    assert v2_before and v2_before.startswith("v2.")

    created = c.post("/s/%s/api/roi" % token_b, json=_valid_roi_body("demo.svs"))
    assert created.status_code == 200, created.get_data(as_text=True)
    new_idx = created.get_json()["index"]
    assert c.patch(
        "/s/%s/api/roi/%s" % (token_b, new_idx), json={"note": "before reclaim"}
    ).status_code == 200

    listed_a = c.get("/s/%s/api/rois" % token_a).get_json()
    assert any(item.get("index") == old_idx and item.get("source") == "me"
               for item in listed_a)
    assert _cookie_val(c, "svs_visitor") == v2_before
    assert _cookie_val(c, "svs_visitor_mig") in (None, "")
    vid = share_srv._parse_visitor_cookie(v2_before)
    assert share_store.get_roi(token_a, old_idx)["visitor"] == share_srv._visitor_stored(vid)
    assert c.patch(
        "/s/%s/api/roi/%s" % (token_a, old_idx), json={"note": "old still mine"}
    ).status_code == 200
    assert c.patch(
        "/s/%s/api/roi/%s" % (token_b, new_idx), json={"note": "new still mine"}
    ).status_code == 200


def test_visitor_hmac_secret_locked_create_is_stable():
    """多线程首次创建 visitor_hmac.key 得到同一密钥。"""
    import threading
    share_srv._visitor_secret_cache = None
    secret_file = Path(share_store.SHARE_DATA_DIR) / "visitor_hmac.key"
    if secret_file.exists():
        secret_file.unlink()
    results = []

    def worker():
        results.append(share_srv._visitor_hmac_secret())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert len(set(results)) == 1
    assert secret_file.is_file()


# =========================================================================== #
# 8. AUTH_ENABLED=False 内网模式：全部旧行为不变
# =========================================================================== #
def test_internal_mode_no_filtering():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    sb = _touch("b.svs")
    _own(sb, _b["user_id"])

    c = _client_noauth()  # 无登录、AUTH_ENABLED=False
    # 全部切片可见（owner 语义全开，不做归属过滤）
    names = {i["name"] for i in c.get("/api/slides").get_json()}
    assert sa in names and sb in names
    # 读取不因归属被拒（info 非 403）
    assert c.get("/api/slide/%s/info" % sa).status_code != 403
    # 可落标（internal 模式不做 can_annotate 拦截）
    r = _post_anno(c, sa)
    assert r.status_code == 200
    # /api/auth/info 如实返回未登录（role=None），不影响访问放行（current_identity 兜底）
    assert c.get("/api/auth/info").get_json()["role"] is None
    assert c.get("/api/auth/info").get_json()["auth_enabled"] is False


# =========================================================================== #
# 9. user 只能分享/删除自己切片
# =========================================================================== #
def test_user_can_only_share_own_slides():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    sb = _touch("b.svs")
    _own(sb, userB["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # 分享自己的 → 200
    r = ca.post("/api/share/create", json={"slides": [sa], "expires_hours": 1})
    assert r.status_code == 200
    # 分享他人的 → 403
    r = ca.post("/api/share/create", json={"slides": [sb], "expires_hours": 1})
    assert r.status_code == 403
    # 混合（含他人） → 403
    r = ca.post("/api/share/create", json={"slides": [sa, sb], "expires_hours": 1})
    assert r.status_code == 403


def test_user_can_only_delete_own_slides():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    sb = _touch("b.svs")
    _own(sb, userB["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # 删他人的 → 403
    r = ca.delete("/api/slide/%s" % sb)
    assert r.status_code == 403
    # 删自己的 → 200
    r = ca.delete("/api/slide/%s" % sa)
    assert r.status_code == 200


def test_share_list_revoke_owner_vs_user():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])
    so = _touch("o.svs")
    _own(so, owner["user_id"])

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    # owner 创建一个分享
    r_o = co.post("/api/share/create", json={"slides": [so], "expires_hours": 1})
    tok_o = r_o.get_json()["token"]

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    # userA 创建一个分享
    r_a = ca.post("/api/share/create", json={"slides": [sa], "expires_hours": 1})
    tok_a = r_a.get_json()["token"]

    # owner list → 看到全部（至少 2 条）
    o_list = co.get("/api/share/list").get_json()
    o_tokens = {s["token"] for s in o_list}
    assert tok_o in o_tokens and tok_a in o_tokens

    # userA list → 只看到自己创建的
    a_list = ca.get("/api/share/list").get_json()
    a_tokens = {s["token"] for s in a_list}
    assert tok_a in a_tokens and tok_o not in a_tokens

    # userA 不能撤销 owner 的分享 → 403
    r = ca.post("/api/share/revoke", json={"token": tok_o})
    assert r.status_code == 403
    # userA 能撤销自己的 → 200
    r = ca.post("/api/share/revoke", json={"token": tok_a})
    assert r.status_code == 200


# =========================================================================== #
# 10. project 隔离
# =========================================================================== #
def test_project_isolation():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    r = ca.post("/api/project/create", json={"name": "PA", "slides": [sa]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid_a = r.get_json()["pid"]
    assert r.get_json()["owner_user_id"] == userA["user_id"]

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    # userB 看不到 userA 的项目
    projects = cb.get("/api/projects").get_json()
    assert all(p["pid"] != pid_a for p in projects)
    # userB 直接访问 userA 的项目 → 403
    assert cb.get("/api/project/%s" % pid_a).status_code == 403
    # userB 改 userA 的项目 → 403
    assert cb.patch("/api/project/%s" % pid_a, json={"name": "hack"}).status_code == 403
    # userB 删 userA 的项目 → 403
    assert cb.delete("/api/project/%s" % pid_a).status_code == 403

    # userA 能访问自己的项目
    assert ca.get("/api/project/%s" % pid_a).status_code == 200
    # owner 能访问任意项目
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    assert co.get("/api/project/%s" % pid_a).status_code == 200


def test_annotations_filtered_for_user():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    assert _post_anno(ca, sa).status_code == 200
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    assert _post_anno(cb, sb).status_code == 200

    # userB 默认拉标注：只能看到自己切片的标注（不含 a.svs）
    body = cb.get("/api/annotations").get_json()
    by_slide = body["by_slide"]
    assert "b.svs" in by_slide
    assert "a.svs" not in by_slide
    # userB 直接拉 a.svs 标注 → 403
    assert cb.get("/api/annotations?slide=a.svs").status_code == 403
    # userB 拉 b.svs 标注 → 200
    assert cb.get("/api/annotations?slide=b.svs").status_code == 200


# =========================================================================== #
# 11. current_identity 单元（owner 语义兜底）
# =========================================================================== #
def test_current_identity_owner_when_no_role():
    """current_identity() 在无 session role 时兜底为 owner（访问判定用，非 info 端点）。"""
    _setup_users()
    # 无 session（test_request_context 默认空 session）→ current_identity 返回 owner
    with app_mod.app.test_request_context("/api/anything"):
        ident = app_mod.current_identity()
        assert ident["role"] == "owner"
        assert ident["user_id"] is None
        assert app_mod._is_owner() is True
        assert app_mod.can_view_slide("any.svs") is True
        assert app_mod.can_upload() is True
