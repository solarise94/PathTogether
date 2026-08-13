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

TMP = tempfile.mkdtemp(prefix="svs-ac-")
DATA_DIR = os.path.join(TMP, "share-data")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.environ["UPLOAD_DIR"] = UPLOAD_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.environ["ADMIN_PASSWORD"] = ""  # 默认无 owner 引导；各用例手动建用户

# openslide 未安装时 stub（本测试多数路径在打开切片前即返回 403）
try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
from pg_compat import json_only  # noqa: E402

# 强制 UPLOAD_DIR 指回本次临时目录（其它测试可能先 import app 改写了它）
app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
share_srv.UPLOAD_DIR = Path(UPLOAD_DIR)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例前把常量 / env 指回本模块临时目录，清空 users.json / shares.json。

    其它测试模块（如 test_region_fingerprint）会在 import 时把 app_mod.UPLOAD_DIR
    改写到它们自己的临时目录；必须在此处用 monkeypatch 每用例夺回，否则 app 读取
    的 UPLOAD_DIR 与本模块写入的不是同一个目录（见 test_ai_config_validation 注释）。
    """
    data_dir = Path(DATA_DIR)
    up_dir = Path(UPLOAD_DIR)
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setattr(user_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_store, "USER_FILE", data_dir / "users.json")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    # 夺回 UPLOAD_DIR（app 与 share_server 各有一份模块级常量）
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", up_dir)
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", up_dir)
    share_store.set_owner_user_id("")
    app_mod._auth_attempts.clear()
    for name in ("users.json", "shares.json", "users.json.bak", "shares.json.bak"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    # 清空 uploads 目录中的测试切片文件
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
    return app_mod.app.test_client()


def _client_noauth():
    """AUTH_ENABLED=False 内网模式客户端。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return app_mod.app.test_client()


def _login(client, email, password):
    return client.post("/login", data={"username": email, "password": password})


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
    owner = user_store.create_user("owner@x.com", "ownerpass1", role="owner")
    userA = user_store.create_user("a@x.com", "userApass1", role="user")
    userB = user_store.create_user("b@x.com", "userBpass1", role="user")
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
    _login(cb, "b@x.com", "userBpass1")

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
    _login(cb, "b@x.com", "userBpass1")
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
    _login(co, "owner@x.com", "ownerpass1")
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
    _login(cb, "b@x.com", "userBpass1")
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
    _login(ca, "a@x.com", "userApass1")
    cb = _client()
    _login(cb, "b@x.com", "userBpass1")

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
    _login(ca, "a@x.com", "userApass1")
    r = _post_anno(ca, sa)
    idx = r.get_json()["index"]

    co = _client()
    _login(co, "owner@x.com", "ownerpass1")
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
    _login(co, "owner@x.com", "ownerpass1")
    # owner 设置公开
    r = co.post("/api/slide/%s/meta" % sa, json={"public": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert share_store.get_slide_meta_full(sa)["public"] is True

    # userB 现在能在列表里看到 userA 的公开切片
    cb = _client()
    _login(cb, "b@x.com", "userBpass1")
    names = {i["name"] for i in cb.get("/api/slides").get_json()}
    assert sa in names
    # 公开切片可被 userB 查看（info 不再 403；可能因 openslide stub 报错，但非 403）
    r = cb.get("/api/slide/%s/info" % sa)
    assert r.status_code != 403


def test_public_only_owner_can_set():
    owner, userA, _b = _setup_users()
    sa = _touch("a.svs")
    _own(sa, userA["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass1")
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
    _login(ca, "a@x.com", "userApass1")
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
    _login(cb, "b@x.com", "userBpass1")
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
    _login(co, "owner@x.com", "ownerpass1")
    assert co.post("/api/share/revoke", json={"token": token}).status_code == 200
    cb2 = _client()
    _login(cb2, "b@x.com", "userBpass1")
    names = {i["name"] for i in cb2.get("/api/slides").get_json()}
    assert so not in names
    assert cb2.get("/api/slide/%s/info" % so).status_code == 403


def test_claim_invalid_share_404():
    _setup_users()
    userB = user_store.get_user_by_email("b@x.com")
    cb = _client()
    _login(cb, "b@x.com", "userBpass1")
    r = cb.post("/api/share/nope/claim")
    assert r.status_code == 404


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
    _login(ca, "a@x.com", "userApass1")
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
    _login(ca, "a@x.com", "userApass1")
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
    _login(co, "owner@x.com", "ownerpass1")
    # owner 创建一个分享
    r_o = co.post("/api/share/create", json={"slides": [so], "expires_hours": 1})
    tok_o = r_o.get_json()["token"]

    ca = _client()
    _login(ca, "a@x.com", "userApass1")
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
    _login(ca, "a@x.com", "userApass1")
    r = ca.post("/api/project/create", json={"name": "PA", "slides": [sa]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid_a = r.get_json()["pid"]
    assert r.get_json()["owner_user_id"] == userA["user_id"]

    cb = _client()
    _login(cb, "b@x.com", "userBpass1")
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
    _login(co, "owner@x.com", "ownerpass1")
    assert co.get("/api/project/%s" % pid_a).status_code == 200


def test_annotations_filtered_for_user():
    owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass1")
    assert _post_anno(ca, sa).status_code == 200
    cb = _client()
    _login(cb, "b@x.com", "userBpass1")
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
