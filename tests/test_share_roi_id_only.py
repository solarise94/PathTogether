# -*- coding: utf-8 -*-
"""分享页标注保存：ID-only（无 legacy 名）资产回归（2026-10-08 缺陷修复）。

缺陷：``share_store_pg.add_roi`` 的 slide ∈ share 成员判定用 shares.slides
JSONB 名快照（``if slide not in (share.get("slides") or [])``）；ID-only
发布资产（id_bundle、legacy_filename=NULL）经分享页保存标注时，路由层已按
slide_id 校验通过，store 却因传入的 legacy 名为 None 被名快照判定误拒
400「slide not in share」。

修复口径（R-04 同源）：slide_id 已知时成员判定走 share_slides(token,
slide_id) ID 关系（与读通道 _require_slide* 同一授权来源）；仅无 slide_id
的历史名行回退名快照。

覆盖：
  - ID-only 发布切片建分享（slide_ids）→ 分享页 POST /s/<token>/api/roi
    （body 只带 slide_id）保存成功，rois 列表回读带同一 slide_id；
  - 不在分享内的 slide_id 经同一路由仍被拒（403）；
  - store 层直连：非成员 slide_id → ValueError("slide not in share")，
    成员 slide_id（名传 None）成功——名快照误拒路径的正面回归。
"""
import os
import sys
import importlib
from pathlib import Path
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
from _pt_helpers import csrf_client, isolate_app, publish_test_slide  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _, up_dir = isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    app_mod.slide_cache._slide_cache.clear()
    app_mod._DEFAULT_FP_CACHE.clear()
    share_srv._tile_cache.clear()
    share_srv._DEFAULT_FP_CACHE.clear()
    yield


def _client(user):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user["user_id"]
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return c


def _share_client():
    share_srv.app.config["TESTING"] = True
    # Exercise the WSGI target actually shipped by the platform container.
    # Calling share_srv.app directly hid a production outage: docker_entry.sh
    # served only app:app, so every /s/* URL missed the share application.
    from werkzeug.test import Client
    from werkzeug.wrappers import Response
    entry = (Path(__file__).resolve().parents[1] / "docker_entry.sh").read_text()
    module, name = re.search(r"^exec gunicorn ([\w.]+):([\w]+)", entry, re.M).groups()
    return Client(getattr(importlib.import_module(module), name), Response)


ARROW = {"type": "arrow", "x1": 1, "y1": 1, "x2": 9, "y2": 9}


def test_id_only_share_roi_add_and_non_member_rejected():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    # ID-only 发布资产（id_bundle、legacy_filename=NULL）
    sid = publish_test_slide("annot-id-only.tif", make_tiff_bytes(),
                             owner_user_id=usera["user_id"])
    other = publish_test_slide("annot-other.tif", make_tiff_bytes(),
                               owner_user_id=usera["user_id"])
    # 上传者本人建分享（user 只能分享自己的切片；slide_ids 权威通道）
    c = _client(usera)
    r = c.post("/api/share/create",
               json={"slide_ids": [sid], "expires_hours": 24})
    assert r.status_code == 200, r.get_data(as_text=True)
    token = r.get_json()["token"]

    sc = _share_client()
    assert sc.get("/s/healthz").get_json() == {"status": "ok"}
    assert sc.get("/s/%s" % token).status_code == 200
    # Serving public shares must not expose the uploader's authenticated API.
    assert sc.get("/api/slides").status_code == 401
    # 分享页保存标注：只带 slide_id（ID-only 无 legacy 名可带）→ 200
    r2 = sc.post("/s/%s/api/roi" % token,
                 json=dict({"slide_id": sid, "label": "L1"}, **ARROW))
    assert r2.status_code == 200, r2.get_data(as_text=True)
    # 回读：rois 列表带同一 slide_id
    rois = sc.get("/s/%s/api/rois" % token).get_json()
    assert any(x.get("slide_id") == sid and x.get("type") == "arrow"
               for x in rois), rois

    # 不在分享内的 slide_id：同一拒绝路径（路由层 403，不泄露差异）
    r3 = sc.post("/s/%s/api/roi" % token,
                 json=dict({"slide_id": other, "label": "L2"}, **ARROW))
    assert r3.status_code == 403, r3.get_data(as_text=True)

    # store 层直连：非成员 slide_id → ValueError；成员（名传 None）成功
    with pytest.raises(ValueError, match="slide not in share"):
        share_store.add_roi(token, None, "L3", slide_id=other, **ARROW)
    roi = share_store.add_roi(token, None, "L4", slide_id=sid, **ARROW)
    assert roi.get("slide_id") == sid
