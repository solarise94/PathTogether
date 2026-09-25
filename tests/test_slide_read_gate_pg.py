# -*- coding: utf-8 -*-
"""slide ID 化重构 P1-B2 读通道门禁测试（统一 resolver + 状态门禁 + 首个 ID 原生 API）。

对齐 docs/slide-id-refactor-p1-contract-20260925.md §3/§4/§7 与任务书
docs/slide-id-storage-refactor-agent-plan-20260925.md §4.1 / P1 完成标准：
  - DB asset_state='ready' 是唯一可见性开关：staging/legacy（未回填）/
    deleting/deleted/failed 一律不可经旧端点读（info/dzi/tile/thumbnail/
    crop/region 全通道拒绝），ready 后放行；
  - 无 slides 行的盘上文件：不出现在 /api/slides、不可经旧端点读
    （「外部程序手工放文件不自动注册」）；
  - 分享：create_share 写 share_slides（position=数组序）；share_server
    读按 (token, slide_id) 成员 + ready 门禁放行/拒绝；无行名经分享创建
    懒建行后可读（迁移兼容语义）；
  - GET /api/slides/<slide_id>/info：ID 可读、资产名不可当 ID 用、未知 ID
    404、响应无 storage_relpath；
  - POST /api/slide/<name>/meta 对不存在名 404（隐式建行旁路收口）；
  - admin inventory：DB 驱动（含非 ready 行 + file_exists + state），
    orphan_files 只报告不认领。

运行：.venv/bin/python -m pytest tests/test_slide_read_gate_pg.py -q
（conftest 起内嵌 PG；每用例前 TRUNCATE 业务表）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例存储隔离 + 清空上传目录（模块级 UPLOAD_DIR 的文件内清理）。"""
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 每用例清句柄/瓦片/信息缓存，避免同名文件跨用例代次串扰
    app_mod.slide_cache._slide_cache.clear()
    share_srv._tile_cache.clear()
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id,
                                       "password": password})


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    userb = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera, userb


def _touch_tiff(name="gate.tif"):
    """真实可打开的合成 TIFF（dzi/tile/thumbnail/region 走真实 200 路径）。"""
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_tiff_bytes())
    return name


def _register(name, owner_user_id):
    """等价上传完成的真实状态：文件在盘 + slides 行（legacy writer 建行）。"""
    share_store.set_slide_meta(name, owner_user_id=owner_user_id)
    return share_store.get_slide_id(name)


_READ_URLS = [
    "/api/slide/{name}/info",
    "/api/slide/{name}.dzi",
    "/api/slide/{name}_files/0/0_0.jpeg",
    "/api/slide/{name}/thumbnail",
    "/api/slide/{name}/crop?x=0&y=0&size=8",
    "/api/slide/{name}/region?x=0&y=0&w=8&h=8",
]


def _set_state(slide_id, state):
    """直接 SQL 改 asset_state（模拟 0067 默认 legacy / 任务中间态）。"""
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE slides SET asset_state=%s WHERE slide_id=%s",
                        (state, slide_id))
        conn.commit()
    finally:
        conn.close()


def _fetch_state(slide_id):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asset_state FROM slides WHERE slide_id=%s",
                        (slide_id,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# =========================================================================== #
# 1. 状态门禁：非 ready 全通道拒绝；ready 放行
# =========================================================================== #
@pytest.mark.parametrize("state", [
    slide_store.SlideState.STAGING,
    slide_store.SlideState.LEGACY,       # 0067 默认（未回填）不可读
    slide_store.SlideState.DELETING,
    slide_store.SlideState.DELETED,
    slide_store.SlideState.FAILED,
])
def test_read_endpoints_reject_non_ready_states(state):
    """staging/legacy/deleting/deleted/failed → 旧读端点全部 403。"""
    owner, usera, _b = _setup_users()
    name = _touch_tiff("gate.tif")
    slide_id = _register(name, usera["user_id"])
    assert slide_id
    _set_state(slide_id, state)   # 直接 SQL 置非 ready（任务书 D.1）

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    for tpl in _READ_URLS:
        url = tpl.format(name=name)
        r = ca.get(url)
        assert r.status_code == 403, (state, url, r.status_code,
                                      r.get_data(as_text=True)[:120])
    # ready → 全通道放行（真实 TIFF：dzi/tile/thumbnail/crop/region 均 200）
    _set_state(slide_id, slide_store.SlideState.READY)
    for tpl in _READ_URLS:
        url = tpl.format(name=name)
        r = ca.get(url)
        assert r.status_code == 200, (url, r.status_code,
                                      r.get_data(as_text=True)[:120])


def test_legacy_state_not_readable_by_owner_either():
    """非 ready 对认证 owner 同样拒绝（管理台 inventory 才是「看全部」出口）。"""
    owner, usera, _b = _setup_users()
    name = _touch_tiff("gate2.tif")
    slide_id = _register(name, usera["user_id"])
    _set_state(slide_id, slide_store.SlideState.LEGACY)
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    r = co.get("/api/slide/%s/info" % name)
    assert r.status_code == 403
    # owner 在 admin inventory 里可见该行（含非 ready 状态）
    inv = co.get("/api/admin/v1/slides/inventory").get_json()
    row = next(i for i in inv["items"] if i["name"] == name)
    assert row["asset_state"] == "legacy"


# =========================================================================== #
# 2. 无 slides 行的盘上文件：不列出、不可读（不自动注册）
# =========================================================================== #
def test_unregistered_disk_file_not_listed_nor_readable():
    owner, _a, _b = _setup_users()
    stray = _touch_tiff("stray.tif")   # 盘上文件，无 slides 行
    registered = _touch_tiff("reg.tif")
    _register(registered, owner["user_id"])

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert registered in names
    assert stray not in names
    # 认证部署下旧端点对无行名一律拒（403——不泄露存在性差异）
    r = co.get("/api/slide/%s/info" % stray)
    assert r.status_code == 403
    r = co.get("/api/slide/%s.dzi" % stray)
    assert r.status_code == 403
    # 读取没有隐式建行（不自动注册）
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM slides WHERE legacy_filename=%s",
                (stray,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# =========================================================================== #
# 3. 分享：create_share 写 share_slides；share_server 按新关系门禁
# =========================================================================== #
def test_create_share_populates_share_slides_and_gates():
    owner, usera, _b = _setup_users()
    inside = _touch_tiff("inside.tif")
    inside_id = _register(inside, usera["user_id"])
    outside = _touch_tiff("outside.tif")
    _register(outside, owner["user_id"])

    share = share_store.create_share([inside], 24, permissions=["view"])
    token = share["token"]

    # share_slides 有行：token × slide_id（position=数组序）
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slide_id, position FROM share_slides"
                " WHERE token=%s ORDER BY position", (token,))
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [(inside_id, 0)], rows

    sc = _share_client()
    # 成员 + ready → 读放行（403/404 语义：成员判定 403、文件缺失 404）
    r = sc.get("/s/%s/api/slide/%s.dzi" % (token, inside))
    assert r.status_code == 200, r.get_data(as_text=True)[:120]
    r = sc.get("/s/%s/api/slide/%s/info" % (token, inside))
    assert r.status_code == 200
    # 非成员（另一已注册切片）→ 403
    r = sc.get("/s/%s/api/slide/%s.dzi" % (token, outside))
    assert r.status_code == 403
    # 成员行进入 deleting → 拒（状态门禁对分享进程同样生效）
    _set_state(inside_id, slide_store.SlideState.DELETING)
    r = sc.get("/s/%s/api/slide/%s.dzi" % (token, inside))
    assert r.status_code == 403
    _set_state(inside_id, slide_store.SlideState.READY)
    r = sc.get("/s/%s/api/slide/%s.dzi" % (token, inside))
    assert r.status_code == 200


def test_share_unregistered_name_lazy_row_readable():
    """P2 收口（合同 §4 / P1-B2 偏差 #4）：分享创建仅接受已存在资产。

    原「先建分享后放文件/从未注册的名 → 懒建行」语义已按合同收紧为
    ValueError（400，指明哪一个）；已注册资产建分享 → share_slides 映射
    ready、分享端可读（原断言保留——夹具顺序调整为先注册行再建分享）。
    """
    owner, _a, _b = _setup_users()
    name = _touch_tiff("late.tif")
    # 未注册的名：不再懒建行，整体拒绝
    with pytest.raises(ValueError, match="late.tif"):
        share_store.create_share([name], 24)
    # 注册后建分享（夹具顺序调整；断言不变）
    _register(name, owner["user_id"])
    share = share_store.create_share([name], 24)
    token = share["token"]

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.asset_state FROM share_slides ss"
                " JOIN slides s ON s.slide_id=ss.slide_id WHERE ss.token=%s",
                (token,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "ready"
    sc = _share_client()
    r = sc.get("/s/%s/api/slide/%s.dzi" % (token, name))
    assert r.status_code == 200


def test_share_listing_from_share_slides_relation():
    """/s/<token>/api/slides 成员清单来自 share_slides ID 关系。"""
    owner, _a, _b = _setup_users()
    a = _touch_tiff("share_a.tif")
    _register(a, owner["user_id"])
    b = _touch_tiff("share_b.tif")
    _register(b, owner["user_id"])
    share = share_store.create_share([a], 24)
    token = share["token"]

    sc = _share_client()
    items = sc.get("/s/%s/api/slides" % token).get_json()
    assert [i["name"] for i in items] == [a]


# =========================================================================== #
# 4. 首个 ID 原生端点 /api/slides/<slide_id>/info
# =========================================================================== #
def test_id_native_info_endpoint():
    owner, usera, userb = _setup_users()
    name = _touch_tiff("byid.tif")
    slide_id = _register(name, usera["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    r = ca.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    info = r.get_json()
    assert info["slide_id"] == slide_id
    assert info["name"] == name
    assert info["original_filename"] == name
    assert info["display_name"] == name
    assert info["format_ext"] == "tif"
    # R-20：路径字段绝不序列化进响应
    assert "storage_relpath" not in info
    assert "storage_relpath" not in str(sorted(info.keys()))
    assert info.get("width") and info.get("height")

    # 资产名不可当 ID 用（未知 ID 形态 → 404）
    r = ca.get("/api/slides/%s/info" % name)
    assert r.status_code == 404
    # 未知随机 ID → 404（不凭 sld_ 前缀当成功）
    r = ca.get("/api/slides/sld_doesnotexist00/info")
    assert r.status_code == 404
    # 无权主体（userB）→ 403
    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    r = cb.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 403
    # 非 ready → 403（ID 通道同受状态门禁）
    _set_state(slide_id, slide_store.SlideState.DELETING)
    r = ca.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 403


# =========================================================================== #
# 5. api_slide_meta 隐式建行旁路收口（无行 404）
# =========================================================================== #
def test_api_slide_meta_404_for_unregistered_name():
    owner, _a, _b = _setup_users()
    ghost = _touch_tiff("ghost.tif")   # 文件在盘、无 slides 行
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    r = co.post("/api/slide/%s/meta" % ghost, json={"alias": "x"})
    assert r.status_code == 404
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM slides WHERE legacy_filename=%s",
                (ghost,))
            assert cur.fetchone()[0] == 0   # 未建行（旁路已收口）
    finally:
        conn.close()


# =========================================================================== #
# 6. admin inventory：DB 驱动 + orphan_files 只报告不认领
# =========================================================================== #
def test_admin_inventory_db_driven_with_orphan_report():
    owner, usera, _b = _setup_users()
    ready_name = _touch_tiff("inv_ready.tif")
    ready_id = _register(ready_name, usera["user_id"])
    busy_name = _touch_tiff("inv_busy.tif")
    busy_id = _register(busy_name, usera["user_id"])
    _set_state(busy_id, slide_store.SlideState.DELETING)
    orphan_name = _touch_tiff("inv_orphan.tif")   # 盘上无行

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    body = co.get("/api/admin/v1/slides/inventory").get_json()
    by_name = {i["name"]: i for i in body["items"]}
    # DB 行（含非 ready）全量呈现 + 状态/file_exists/slide_id
    assert by_name[ready_name]["slide_id"] == ready_id
    assert by_name[ready_name]["asset_state"] == "ready"
    assert by_name[ready_name]["file_exists"] is True
    assert by_name[busy_name]["asset_state"] == "deleting"
    # orphan：只在报告面出现（name+size），不做任何隐式认领
    orphans = {o["name"]: o for o in body["orphan_files"]}
    assert orphan_name in orphans
    assert orphans[orphan_name]["size_bytes"] == \
        (Path(UPLOAD_DIR) / orphan_name).stat().st_size
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM slides WHERE legacy_filename=%s",
                (orphan_name,))
            assert cur.fetchone()[0] == 0   # 未认领/未建行
    finally:
        conn.close()
    # 非 ready 行不出现在普通 /api/slides（owner 未收录 + 状态门禁）
    names = {i["name"] for i in co.get("/api/slides").get_json()}
    assert busy_name not in names


# =========================================================================== #
# 7. 删除端点直写 deleted：门禁立即生效（P5 两阶段前的过渡）
# =========================================================================== #
def test_slide_delete_marks_deleted_and_gates():
    owner, usera, _b = _setup_users()
    name = _touch_tiff("del.tif")
    slide_id = _register(name, usera["user_id"])

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    assert ca.get("/api/slide/%s/info" % name).status_code == 200
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    assert co.delete("/api/slide/%s" % name).status_code == 200

    assert _fetch_state(slide_id) == "deleted"
    # 旧端点立即拒绝；列表不再呈现
    r = ca.get("/api/slide/%s/info" % name)
    assert r.status_code == 403
    names = {i["name"] for i in ca.get("/api/slides").get_json()}
    assert name not in names
    # tombstone 保留 legacy_filename（旧别名不重绑）
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT legacy_filename, deleted_at FROM slides"
                " WHERE slide_id=%s", (slide_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row[0] == name and row[1] is not None
