# -*- coding: utf-8 -*-
"""管理员临时查看按 slide_id 寻址（2026-10-08 docs/admin-viewer-simplified-
20261008.md §3.1；test_admin_slide_visibility_by_id.py 的合同改写）。

冻结：
  - 新资产（id_bundle，无 legacy 名）出现在管理清单（temporary_view=none）；
    temporary-view 开启后真实可见，结束立即不可见；
  - 不可服务资产（未迁移 legacy 布局）开启 → 409 slide_not_servable，不落
    授权行；
  - 授权行存在但资产不可服务 → 清单报 unavailable（不再有
    granted_to_owner/grant_recorded 字段）；
  - 未注册盘上文件不能被临时查看认领（404，不建行）；
  - 旧 visibility 端点对 owner 稳定 410。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from _pt_helpers import publish_test_slide, register_slide_row  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


def _client(user):
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return c


def _setup():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _inventory(c):
    r = c.get("/api/admin/v1/slides/inventory?limit=200")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["items"]


def _visible_ids(c):
    r = c.get("/api/slides")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    items = body if isinstance(body, list) else (body.get("slides") or body.get("items") or [])
    return {it.get("slide_id") for it in items if isinstance(it, dict)}


def _start(c, sid):
    return c.post("/api/admin/v1/slides/%s/temporary-view" % sid)


def _end(c, sid):
    return c.delete("/api/admin/v1/slides/%s/temporary-view" % sid)


def test_new_asset_temporary_view_makes_it_visible():
    owner, usera = _setup()
    sid = publish_test_slide("dup.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    assert sid not in _visible_ids(oc)
    items = [it for it in _inventory(oc) if it.get("slide_id") == sid]
    assert len(items) == 1, "新资产（无 legacy 名）必须出现在管理清单"
    item = items[0]
    assert item["name"] == sid
    assert item["servable"] is True
    assert item["temporary_view"]["status"] == "none"
    r = _start(oc, sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["slide_id"] == sid
    assert body["temporary_view"]["status"] == "active"
    # 1 小时窗口（常量不接受参数）
    assert 3000 < body["temporary_view"]["expires_at"] - body["server_now"] \
        <= 3600
    assert sid in _visible_ids(oc)
    item = [it for it in _inventory(oc) if it.get("slide_id") == sid][0]
    assert item["temporary_view"]["status"] == "active"
    assert item["temporary_view"]["expires_at"] == \
        body["temporary_view"]["expires_at"]
    # 结束：立即不可见；授权行保留（历史）但已到期
    r = _end(oc, sid)
    assert r.status_code == 200
    assert r.get_json()["temporary_view"]["status"] == "ended"
    assert sid not in _visible_ids(oc)
    item = [it for it in _inventory(oc) if it.get("slide_id") == sid][0]
    assert item["temporary_view"]["status"] == "ended"
    # 幂等：再次结束 → none
    r = _end(oc, sid)
    assert r.status_code == 200
    assert r.get_json()["temporary_view"]["status"] == "none"
    rows = _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,), fetch=True)[0][0]
    assert rows == 1  # 行保留（ended 形态），不物理删除


def test_unmigrated_legacy_asset_start_is_refused_not_faked():
    owner, usera = _setup()
    name = "old-flat.svs"
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    sid = register_slide_row(name)
    _sql("UPDATE slides SET asset_state='legacy', storage_layout='legacy', "
         "storage_relpath=NULL, owner_user_id=%s WHERE legacy_filename=%s",
         (usera["user_id"], name))
    oc = _client(owner)
    item = [it for it in _inventory(oc) if it["name"] == name][0]
    assert item["servable"] is False and item["file_exists"] is True
    assert item["temporary_view"]["status"] == "unavailable"
    r = _start(oc, sid)
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"]["code"] == "slide_not_servable"
    assert _sql("SELECT count(*) FROM slide_view_grants g JOIN slides s "
                "ON s.slide_id = g.slide_id WHERE s.legacy_filename=%s",
                (name,), fetch=True)[0][0] == 0


def test_ended_grant_on_unmigrated_asset_reports_unavailable():
    owner, usera = _setup()
    name = "granted-before.svs"
    (Path(UPLOAD_DIR) / name).write_bytes(b"svs-stub")
    sid = register_slide_row(name)
    _sql("UPDATE slides SET owner_user_id=%s WHERE legacy_filename=%s",
         (usera["user_id"], name))
    oc = _client(owner)
    assert _start(oc, sid).status_code == 200
    _sql("UPDATE slides SET asset_state='legacy', storage_layout='legacy', "
         "storage_relpath=NULL WHERE legacy_filename=%s", (name,))
    item = [it for it in _inventory(oc) if it["name"] == name][0]
    # 不可服务优先于授权状态（§3.1：unavailable 同时给出原因字段）
    assert item["servable"] is False
    assert item["temporary_view"]["status"] == "unavailable"
    assert "granted_to_owner" not in item
    assert "grant_recorded" not in item


def test_unregistered_flat_file_is_not_adopted_by_temporary_view():
    owner, _usera = _setup()
    (Path(UPLOAD_DIR) / "stray.svs").write_bytes(b"svs-stub")
    oc = _client(owner)
    r = oc.post("/api/admin/v1/slides/stray.svs/temporary-view")
    assert r.status_code == 404
    assert _sql("SELECT count(*) FROM slides WHERE legacy_filename='stray.svs'",
                fetch=True)[0][0] == 0


def test_old_visibility_endpoint_retired():
    owner, usera = _setup()
    sid = publish_test_slide("retire.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    r = oc.post("/api/admin/v1/slides/%s/visibility" % sid,
                json={"granted": True})
    assert r.status_code == 410
    assert r.get_json()["error"]["code"] == "endpoint_retired"
    assert _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,), fetch=True)[0][0] == 0


def test_repeat_start_after_natural_expiry_opens_new_window():
    """到期后再开启 = 新窗口（granted_at 刷新、过期行被重开）。"""
    owner, usera = _setup()
    sid = publish_test_slide("rewind.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    first = _start(oc, sid).get_json()["temporary_view"]
    # 直接把 expires_at 拨到过去（模拟自然到期）
    _sql("UPDATE slide_view_grants SET expires_at=now() - interval '1 second' "
         "WHERE slide_id=%s", (sid,))
    second = _start(oc, sid).get_json()["temporary_view"]
    assert second["status"] == "active"
    assert second["expires_at"] > first["expires_at"]
    assert second["granted_at"] >= first["granted_at"]
    # 可见性恢复
    assert sid in _visible_ids(oc)
