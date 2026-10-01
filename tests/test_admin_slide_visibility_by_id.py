# -*- coding: utf-8 -*-
"""管理端「加入我的工作区」按 slide_id 寻址（2026-10-02 生产回归）。

生产现象：管理员在后台对切片点「加入」返回成功，但工作区里仍看不到。
  - 新资产（id_bundle，legacy_filename 为空）不在管理清单里，也无法按名授权；
  - 未迁移的旧布局资产授权「成功」但可见集只认 ready + id_bundle——授权永不生效。
本文件冻结：清单按 slide_id 列出新资产；授权按 slide_id 生效并真实可见；
不可服务的资产授权返回 409 且不落授权行；按 ID 收回。
"""
import os
import sys
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


def test_new_asset_listed_by_id_and_grant_makes_it_visible():
    owner, usera = _setup()
    sid = publish_test_slide("dup.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    oc = _client(owner)
    assert sid not in _visible_ids(oc)
    items = [it for it in _inventory(oc) if it.get("slide_id") == sid]
    assert len(items) == 1, "新资产（无 legacy 名）必须出现在管理清单"
    item = items[0]
    assert item["name"] == sid
    assert item["servable"] is True and item["granted_to_owner"] is False
    r = oc.post("/api/admin/v1/slides/%s/visibility" % item["name"],
                json={"granted": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["slide_id"] == sid
    assert sid in _visible_ids(oc)
    item = [it for it in _inventory(oc) if it.get("slide_id") == sid][0]
    assert item["granted_to_owner"] is True and item["granted_at"]
    r = oc.post("/api/admin/v1/slides/%s/visibility" % sid, json={"granted": False})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["existed"] is True
    assert sid not in _visible_ids(oc)
    assert _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,), fetch=True)[0][0] == 0


def test_unmigrated_legacy_asset_grant_is_refused_not_faked():
    owner, usera = _setup()
    name = "old-flat.svs"
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    register_slide_row(name)
    _sql("UPDATE slides SET asset_state='legacy', storage_layout='legacy', "
         "storage_relpath=NULL, owner_user_id=%s WHERE legacy_filename=%s",
         (usera["user_id"], name))
    oc = _client(owner)
    item = [it for it in _inventory(oc) if it["name"] == name][0]
    assert item["servable"] is False and item["file_exists"] is True
    r = oc.post("/api/admin/v1/slides/%s/visibility" % name, json={"granted": True})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"]["code"] == "slide_not_servable"
    assert _sql("SELECT count(*) FROM slide_view_grants g JOIN slides s "
                "ON s.slide_id = g.slide_id WHERE s.legacy_filename=%s",
                (name,), fetch=True)[0][0] == 0


def test_existing_grant_on_unmigrated_asset_is_not_reported_as_included():
    owner, usera = _setup()
    name = "granted-before.svs"
    (Path(UPLOAD_DIR) / name).write_bytes(b"svs-stub")
    register_slide_row(name)
    oc = _client(owner)
    assert oc.post("/api/admin/v1/slides/%s/visibility" % name,
                   json={"granted": True}).status_code == 200
    _sql("UPDATE slides SET asset_state='legacy', storage_layout='legacy', "
         "storage_relpath=NULL WHERE legacy_filename=%s", (name,))
    item = [it for it in _inventory(oc) if it["name"] == name][0]
    assert item["granted_to_owner"] is False
    assert item["grant_recorded"] is True and item["servable"] is False


def test_unregistered_flat_file_is_not_adopted_by_grant():
    owner, _usera = _setup()
    (Path(UPLOAD_DIR) / "stray.svs").write_bytes(b"svs-stub")
    oc = _client(owner)
    r = oc.post("/api/admin/v1/slides/stray.svs/visibility", json={"granted": True})
    assert r.status_code == 404, r.get_data(as_text=True)
    assert _sql("SELECT count(*) FROM slides WHERE legacy_filename='stray.svs'",
                fetch=True)[0][0] == 0
