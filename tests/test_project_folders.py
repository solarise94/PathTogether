# -*- coding: utf-8 -*-
"""文件夹（项目层级）验收（docs/admin-viewer-simplified-20261008.md §5.3 / §8-4）。

覆盖：
  - 建子文件夹（/api/project/create + parent_project_id）；
  - 移动（PATCH parent_project_id；null=移到根）；
  - 环检测：A→B 后 B→A 被拒；移到自己的子孙被拒；自引用被拒；
  - 跨 owner 被拒（403）；
  - 父不存在 400；父归档 409；层级 >5 拒（建与移动两条路径）；
  - 删除父文件夹 → 子文件夹回根（FK SET NULL）、切片不删、归属不丢；
  - /api/projects 返回 parent_project_id；
  - 幂等创建键含父级（同键换父级 409）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app, publish_test_slide  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _client(user):
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return c


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _setup():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userpass12345678")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _create(c, name, parent=None, key=None, slides=None):
    headers = {"Idempotency-Key": key} if key else None
    r = c.post("/api/project/create", json={
        "name": name, "parent_project_id": parent,
        "slide_ids": slides or []}, headers=headers)
    return r


def _parent_of(pid):
    rows = _sql("SELECT parent_project_id FROM projects WHERE project_id=%s",
                (pid,), fetch=True)
    return rows[0]["parent_project_id"] if rows else None


def test_create_child_folder_and_projects_list_exposes_parent():
    owner, usera = _setup()
    oc = _client(owner)
    root = _create(oc, "教学切片").get_json()
    assert root["parent_project_id"] is None
    child = _create(oc, "复核", parent=root["pid"]).get_json()
    assert child["pid"] and child["parent_project_id"] == root["pid"]

    # /api/projects 返回 parent_project_id（owner 视角按 owner 归属过滤）
    items = {p["pid"]: p for p in oc.get("/api/projects").get_json()}
    assert items[child["pid"]]["parent_project_id"] == root["pid"]
    assert items[root["pid"]]["parent_project_id"] is None

    # 深链合法：5 层（root=1 … level5）
    p = root["pid"]
    for i in range(4):
        r = _create(oc, "L%d" % (i + 2), parent=p)
        assert r.status_code == 200, r.get_data(as_text=True)
        p = r.get_json()["pid"]
    # 第 6 层被拒（409 depth）
    r = _create(oc, "L6", parent=p)
    assert r.status_code == 409
    assert r.get_json()["code"] == "parent_depth_exceeded"

    # 形状校验：空串 400
    r = _create(oc, "bad", parent="  ")
    assert r.status_code == 400


def test_move_folder_and_root_move():
    owner, usera = _setup()
    oc = _client(owner)
    a = _create(oc, "A").get_json()["pid"]
    b = _create(oc, "B").get_json()["pid"]
    # A → B 下
    r = oc.patch("/api/project/%s" % a, json={"parent_project_id": b})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _parent_of(a) == b
    # 移回根（null）
    r = oc.patch("/api/project/%s" % a, json={"parent_project_id": None})
    assert r.status_code == 200
    assert _parent_of(a) is None
    # 不带键：不动
    r = oc.patch("/api/project/%s" % a, json={"name": "A2"})
    assert r.status_code == 200
    assert _parent_of(a) is None


def test_cycles_rejected():
    owner, usera = _setup()
    oc = _client(owner)
    a = _create(oc, "A").get_json()["pid"]
    b = _create(oc, "B", parent=a).get_json()["pid"]
    # A→B（把父移动到自己子孙下）：409 cycle
    r = oc.patch("/api/project/%s" % a, json={"parent_project_id": b})
    assert r.status_code == 409
    assert r.get_json()["code"] == "parent_cycle"
    # 自引用：409
    r = oc.patch("/api/project/%s" % a, json={"parent_project_id": a})
    assert r.status_code == 409
    # 更深的环：A→B→C 后把 A 移到 C（自己的孙子的孙子？——A 是 C 的祖父）
    # A 移到 C 下将形成 A→C→B→A 环 → 409
    c2 = _create(oc, "C", parent=b).get_json()["pid"]
    r = oc.patch("/api/project/%s" % a, json={"parent_project_id": c2})
    assert r.status_code == 409
    assert r.get_json()["code"] == "parent_cycle"
    assert _parent_of(c2) == b and _parent_of(b) == a  # 原树未被改动


def test_cross_owner_and_missing_and_archived_rejected():
    owner, usera = _setup()
    oc = _client(owner)
    ac = _client(usera)
    other_root = _create(ac, "别人的根").get_json()["pid"]
    mine = _create(oc, "我的").get_json()["pid"]

    # 跨 owner 建：403
    r = _create(oc, "偷", parent=other_root)
    assert r.status_code == 403
    assert r.get_json()["code"] == "parent_not_owner"
    # 跨 owner 移：403
    r = oc.patch("/api/project/%s" % mine, json={"parent_project_id": other_root})
    assert r.status_code == 403
    assert r.get_json()["code"] == "parent_not_owner"

    # 父不存在：400
    r = _create(oc, "ghost-child", parent="prj_nope")
    assert r.status_code == 400
    assert r.get_json()["code"] == "parent_not_found"
    r = oc.patch("/api/project/%s" % mine,
                 json={"parent_project_id": "prj_nope"})
    assert r.status_code == 400

    # 父已归档：409
    archived = _create(oc, "归档父").get_json()["pid"]
    assert oc.post("/api/project/%s/archive" % archived).status_code == 200
    r = _create(oc, "arch-child", parent=archived)
    assert r.status_code == 409
    assert r.get_json()["code"] == "parent_archived"
    r = oc.patch("/api/project/%s" % mine, json={"parent_project_id": archived})
    assert r.status_code == 409


def test_move_depth_limit_counts_subtree():
    """移动整个子树：层级按最深 descendant 计算（不只自身）。"""
    owner, usera = _setup()
    oc = _client(owner)
    # 链1：D1(1)→D2(2)→D3(3)→D4(4)（D4 子树高 1，可再挂到 level-1 父下：
    # 新 D4 层级=2，D1..D4 仍 ≤5）
    d1 = _create(oc, "D1").get_json()["pid"]
    d2 = _create(oc, "D2", parent=d1).get_json()["pid"]
    d3 = _create(oc, "D3", parent=d2).get_json()["pid"]
    d4 = _create(oc, "D4", parent=d3).get_json()["pid"]
    # 新目标链：E1(1)→E2(2)→E3(3)→E4(4)
    e1 = _create(oc, "E1").get_json()["pid"]
    e2 = _create(oc, "E2", parent=e1).get_json()["pid"]
    e3 = _create(oc, "E3", parent=e2).get_json()["pid"]
    e4 = _create(oc, "E4", parent=e3).get_json()["pid"]
    # 把 D1 子树（高 4）移到 E4 下：新 D1 层级=5，D4 层级=8 > 5 → 409
    r = oc.patch("/api/project/%s" % d1, json={"parent_project_id": e4})
    assert r.status_code == 409
    assert r.get_json()["code"] == "parent_depth_exceeded"
    # 把 D4（叶子）移到 E4 下：新层级 5 → 允许
    r = oc.patch("/api/project/%s" % d4, json={"parent_project_id": e4})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _parent_of(d4) == e4


def test_delete_parent_children_go_root_and_slides_remain():
    owner, usera = _setup()
    oc = _client(owner)
    sid = publish_test_slide("folder-slide.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    parent = _create(oc, "父").get_json()["pid"]
    child = _create(oc, "子", parent=parent, slides=[sid]).get_json()["pid"]
    # 子文件夹确实持有切片
    rows = _sql("SELECT slide_id FROM project_slides WHERE project_id=%s",
                (child,), fetch=True)
    assert {r["slide_id"] for r in rows} == {sid}

    # 删除父 → 子回根；切片引用保留（project_slides 随项目删除语义不变：
    # 子文件夹的成员行不动）；切片本身不删
    r = oc.delete("/api/project/%s" % parent)
    assert r.status_code == 200
    assert _parent_of(child) is None
    rows = _sql("SELECT slide_id FROM project_slides WHERE project_id=%s",
                (child,), fetch=True)
    assert {r["slide_id"] for r in rows} == {sid}
    assert _sql("SELECT asset_state FROM slides WHERE slide_id=%s",
                (sid,), fetch=True)[0]["asset_state"] == "ready"


def test_idempotency_key_binds_parent():
    owner, usera = _setup()
    oc = _client(owner)
    root = _create(oc, "幂等根").get_json()["pid"]
    r1 = _create(oc, "P", parent=root, key="idem-1")
    assert r1.status_code == 200
    pid = r1.get_json()["pid"]
    # 同键同父级：重放返回原项目
    r2 = _create(oc, "P", parent=root, key="idem-1")
    assert r2.status_code == 200
    assert r2.get_json()["pid"] == pid
    # 同键换父级：409（负载不同）
    r3 = _create(oc, "P", key="idem-1")
    assert r3.status_code == 409
    assert r3.get_json()["code"] == "idempotency_key_conflict"


def test_user_role_scoping_unchanged():
    """普通 user 只能操作自己的文件夹（既有语义不受 parent 影响）。"""
    owner, usera = _setup()
    ac = _client(usera)
    root = _create(ac, "user根").get_json()["pid"]
    child = _create(ac, "user子", parent=root).get_json()["pid"]
    assert _parent_of(child) == root
    # user B 的项目不在 user A 的列表
    userb = user_store.create_user("b@x.com", "userpass12345678")
    bc = _client(userb)
    pids = {p["pid"] for p in bc.get("/api/projects").get_json()}
    assert root not in pids and child not in pids
