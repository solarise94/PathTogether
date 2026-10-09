# -*- coding: utf-8 -*-
"""按 annotation_id 的读写端点对 id_bundle 资产（无 legacy 名）按 ID 鉴权。

P2 起新上传都是 id_bundle：rois.slide 只是显示名快照，不是名通道键。
_resolve_anno_by_id 曾只走 can_annotate_slide(roi.slide)，导致工作台对
这类切片的 PATCH/DELETE/restore 一律 403（新 UI 松手保存、锁切换与撤销
都走 by-id 路由）。

运行：cd 项目根 && python3 -m pytest tests/test_annotation_by_id_id_bundle.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    clear_upload_dir,
    isolate_app,
    make_client,
    publish_test_slide,
)
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

TIFF = make_tiff_bytes(64, 96)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _login(client, login):
    u = user_store.create_user(login, "pass1234pass1234", role="user")
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = "user"
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _setup_owned_annotation():
    c = make_client(auth=True)
    uid = _login(c, "byid-owner@x.com")
    sid = publish_test_slide("byid.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    desc = slide_store.resolve_slide_id(sid)
    assert desc is not None and not desc.legacy_filename  # id_bundle 前提
    r = c.post("/api/annotation", json={
        "slide_id": sid, "type": "rect", "label": "A",
        "x": 4.0, "y": 4.0, "w": 10.0, "h": 8.0})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    return c, sid, body["annotation_id"], body["revision"]


def test_patch_geometry_by_id_on_id_bundle_slide():
    c, _sid, aid, rev = _setup_owned_annotation()
    r = c.patch("/api/annotation/id/%s" % aid, json={
        "geom": {"x": 6.0, "y": 6.0, "w": 10.0, "h": 8.0},
        "expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["revision"] == rev + 1


def test_lock_toggle_and_delete_restore_by_id_on_id_bundle_slide():
    c, _sid, aid, rev = _setup_owned_annotation()
    r = c.patch("/api/annotation/id/%s" % aid, json={
        "geometry_locked": True, "expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["geometry_locked"] is True
    rev = r.get_json()["revision"]
    r = c.delete("/api/annotation/id/%s" % aid, json={"expected_revision": rev})
    assert r.status_code == 409 and r.get_json()["error"] == "annotation_locked"
    r = c.patch("/api/annotation/id/%s" % aid, json={
        "geometry_locked": False, "expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    rev = r.get_json()["revision"]
    r = c.delete("/api/annotation/id/%s" % aid, json={"expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    r = c.post("/api/annotation/id/%s/restore" % aid, json={})
    assert r.status_code == 200, r.get_json()


def test_other_user_still_denied_on_id_bundle_slide():
    _c, _sid, aid, rev = _setup_owned_annotation()
    other = make_client(auth=True)
    _login(other, "byid-other@x.com")
    r = other.patch("/api/annotation/id/%s" % aid, json={
        "geom": {"x": 7.0, "y": 7.0, "w": 10.0, "h": 8.0},
        "expected_revision": rev})
    assert r.status_code in (403, 404)
    r = other.delete("/api/annotation/id/%s" % aid,
                     json={"expected_revision": rev})
    assert r.status_code in (403, 404)
