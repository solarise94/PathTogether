# -*- coding: utf-8 -*-
"""标注导出（QuPath 风格 GeoJSON）：纯模块 + HTTP 端点测试。

覆盖：
  - 四种几何 → GeoJSON（rect v1/v2 闭合环、polygon/freehand 补闭合点、
    arrow → LineString）；畸形/tombstone → skipped；
  - Feature id/objectType/name/classification/color/isLocked；
  - metadata 全字符串、AI 行 ai_* 与 ai 块、AI 橙色 vs 标签哈希色；
  - 原生 provenance 的敏感字段（provider host / 用户/会话/运行 id /
    idempotency / snapshot / 渲染指纹 / revision / visitor / token /
    effect_key / client_action_id）绝不进入序列化输出；
  - label_rgb 与 static/app.js labelColor 的 node 实测值逐位一致；
  - 文件名清洗（中文保留、路径分隔符/控制字符剔除、空名回退）；
  - 列表顺序与 pathtogether 统计；
  - 路由：owner 导出（人工矩形 + AI 描绘 polygon）、他人不可见、
    越权 403 与列表端点同口径、未知 ID 404、响应头、format 校验。
"""
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import annotation_export as export  # noqa: E402
import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    csrf_client,
    isolate_app,
    make_snapshot_attestation,
    register_slide_row,
)

SLIDE_W, SLIDE_H = 4000, 3000
_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例独立存储 + 可信切片元数据（mock 读路径，不碰真 OpenSlide）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", "test-internal-token")
    # AI 描绘通道：传输鉴权/描绘授权/审计/资产 revision 均由既有测试覆盖，
    # 本文件聚焦导出语义。
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(
        app_mod, "_active_drawing_grant_for_session",
        lambda *a, **k: {"grant_id": "g-export", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                        lambda gate, safe: "rev0")
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    # 元数据缓存按 (name, mtime) 命中；桩文件跨用例 mtime 可能碰撞
    with app_mod.slide_cache._info_cache_lock:
        app_mod.slide_cache._info_cache.clear()
    pair = {"osr": mock.Mock(dimensions=(SLIDE_W, SLIDE_H))}
    meta = {"width": SLIDE_W, "height": SLIDE_H,
            "mpp_x": 0.25, "mpp_y": 0.25,
            "objective": None, "mpp_source": "metadata"}
    monkeypatch.setattr(app_mod, "_get_slide", lambda *a, **k: _FAKE_ENTRY)
    monkeypatch.setattr(app_mod.slide_cache, "borrow_pair",
                        lambda _e: _borrow_pair_ctx(pair))
    monkeypatch.setattr(app_mod, "_read_metadata", lambda *a, **k: dict(meta))
    yield


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _touch(name="demo.svs"):
    path = Path(UPLOAD_DIR) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return register_slide_row(name)


def _setup_owner():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    share_store.set_owner_user_id(owner["user_id"])
    return owner


def _setup_peer():
    return user_store.create_user("peer@x.com", "peerpass1234567",
                                  role="user")


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id="owner@x.com", password="ownerpass123456"):
    r = client.post("/login", data={"username": login_id,
                                    "password": password})
    assert r.status_code in (200, 302), r.get_data(as_text=True)


def _post_human_rect(client, sid, x=10, y=20, w=320, h=170, label="人工矩形"):
    return client.post("/api/annotation", json={
        "slide_id": sid, "type": "rect", "label": label,
        "x": x, "y": y, "w": w, "h": h, "note": "human note",
    })


def _post_ai_polygon(owner, sid, points=None, label="AI 病灶"):
    points = points or [[100, 100], [900, 120], [500, 800]]
    snap = "snap-export-1"
    session_id = "sess-export-ai"
    attestation = make_snapshot_attestation(
        "test-internal-token", sid=session_id, snap=snap,
        bbox=None, rev=None, fp=None)
    client = app_mod.app.test_client()
    return client.post("/internal/ai/annotate", json={
        "slide_id": sid, "label": label, "type": "polygon",
        "points": points, "note": "ai note", "effect_key": "ek-export-1",
        "session_id": session_id,
        "plugin_id": "histopilot", "plugin_version": "0.9.0",
        "run_id": "run-export", "model": "gpt-4o",
        "base_url": "https://api.example.com/v1",
        "created_by_user_id": owner["user_id"],
        "skill": "delineate", "skill_intent": "explicit",
        "contour_refine": {"refined": False},
        "snapshot_id": snap, "snapshot_attestation": attestation,
    })


def _flatten(payload):
    out = []
    for group in payload.get("annotations") or []:
        out.extend(group.get("items") or [])
    return out


def _feature_props(feature):
    return feature.get("properties") or {}


# =========================================================================== #
# 1. 纯模块：几何
# =========================================================================== #
def test_rect_v2_closed_ring():
    item = {"annotation_id": "a1", "type": "rect", "x": 5, "y": 6,
            "w": 320, "h": 170}
    feature = export.build_feature(item)
    assert feature["geometry"] == {
        "type": "Polygon",
        "coordinates": [[[5, 6], [325, 6], [325, 176], [5, 176], [5, 6]]],
    }


def test_rect_v1_side_px_compat():
    item = {"annotation_id": "a1", "type": "rect", "x": 1, "y": 2,
            "side_px": 100}
    feature = export.build_feature(item)
    assert feature["geometry"]["coordinates"] == [[
        [1, 2], [101, 2], [101, 102], [1, 102], [1, 2]]]


def test_rect_v1_w_h_wins_over_side_px():
    # 读兼容补出的 w/h 是权威（非正方形不得被 side_px 冒充）
    item = {"annotation_id": "a1", "type": "rect", "x": 0, "y": 0,
            "w": 30, "h": 20, "side_px": 20}
    feature = export.build_feature(item)
    assert feature["geometry"]["coordinates"][0][2] == [30, 20]


def test_polygon_and_freehand_close_ring():
    points = [[10, 10], [60, 10], [35, 50]]
    for typ in ("polygon", "freehand"):
        feature = export.build_feature(
            {"annotation_id": "a-" + typ, "type": typ, "points": points})
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["geometry"]["coordinates"] == [
            [[10, 10], [60, 10], [35, 50], [10, 10]]]

    # 已带闭合尾点：原样保留，不重复追加
    closed = [[10, 10], [60, 10], [35, 50], [10, 10]]
    feature = export.build_feature(
        {"annotation_id": "a-closed", "type": "polygon", "points": closed})
    assert feature["geometry"]["coordinates"] == [closed]


def test_arrow_linestring():
    feature = export.build_feature({
        "annotation_id": "a2", "type": "arrow",
        "x1": 1, "y1": 2, "x2": 30, "y2": 40,
    })
    assert feature["geometry"] == {
        "type": "LineString", "coordinates": [[1, 2], [30, 40]]}


def test_malformed_and_tombstone_skipped():
    cases = [
        {"annotation_id": "bad-rect", "type": "rect", "x": 0, "y": 0,
         "w": 0, "h": 10},
        {"annotation_id": "bad-poly", "type": "polygon",
         "points": [[0, 0], [1, 1]]},
        {"annotation_id": "bad-arrow", "type": "arrow", "x1": 0, "y1": 0},
        {"annotation_id": "unknown", "type": "bezier"},
        {"annotation_id": "tomb", "type": "rect", "x": 0, "y": 0,
         "w": 10, "h": 10, "deleted": True},
        {"annotation_id": "nan", "type": "rect", "x": 0, "y": 0,
         "w": float("nan"), "h": 10},
    ]
    for item in cases:
        assert export.build_feature(item) is None, item


def test_collection_order_and_counts():
    items = [
        {"annotation_id": "b", "type": "rect", "x": 0, "y": 0,
         "w": 10, "h": 10},
        {"annotation_id": "skip", "type": "polygon", "points": []},
        {"annotation_id": "a", "type": "arrow", "x1": 0, "y1": 0,
         "x2": 1, "y2": 1},
    ]
    raw_by_id = {"a": {"annotation_id": "a", "source": "ai",
                       "created_by_session_id": "s",
                       "provenance": {"model": "m"}}}
    collection = export.build_feature_collection(
        items, raw_by_id,
        {"slide_id": "sld_1", "display_name": "Demo", "width": 10,
         "height": 20, "mpp_x": 0.5, "mpp_y": 0.5,
         "mpp_source": "metadata"},
        "2026-10-28T00:00:00Z")
    assert collection["type"] == "FeatureCollection"
    assert [f["id"] for f in collection["features"]] == ["b", "a"]
    assert collection["pathtogether"] == {
        "schema": export.SCHEMA,
        "exported_at": "2026-10-28T00:00:00Z",
        "slide": {
            "slide_id": "sld_1", "name": "Demo",
            "width": 10, "height": 20,
            "mpp_x": 0.5, "mpp_y": 0.5, "mpp_source": "metadata",
        },
        "coordinate_system": export.COORDINATE_SYSTEM,
        "feature_count": 2,
        "ai_feature_count": 1,
        "skipped": 1,
    }


# =========================================================================== #
# 2. 纯模块：properties 白名单 / 颜色 / 元数据
# =========================================================================== #
def test_human_feature_properties():
    label = "Tumor: high grade"
    raw = {
        "annotation_id": "a1", "slide_id": "sld_1", "type": "rect",
        "x": 5, "y": 6, "w": 10, "h": 20, "label": label,
        "note": "human note", "source": "human", "review_status": "none",
        "ts": 1700000000.0, "updated_at": 1700000100.0,
    }
    item = {
        "annotation_id": "a1", "slide_id": "sld_1", "type": "rect",
        "x": 5, "y": 6, "w": 10, "h": 20, "source": "human",
        "review_status": "none", "geometry_locked": True,
        "author_kind": "user", "author_label_safe": "管理员",
    }
    feature = export.build_feature(item, raw)
    props = _feature_props(feature)
    assert feature["id"] == "a1"
    assert props["objectType"] == "annotation"
    assert props["name"] == label
    assert props["classification"] == {
        "name": "Tumor： high grade", "color": export.label_rgb(label)}
    assert props["color"] == export.label_rgb(label)
    assert props["isLocked"] is True
    assert props["annotation_id"] == "a1"
    assert props["shape"] == "rect"
    assert props["source"] == "human"
    assert props["ai_generated"] is False
    assert props["review_status"] == "none"
    assert props["label"] == label
    assert props["note"] == "human note"
    assert props["created_at"] == "2023-11-14T22:13:20Z"
    assert props["updated_at"] == "2023-11-14T22:15:00Z"
    assert props["author_kind"] == "user"
    assert props["author_label"] == "管理员"
    assert "ai" not in props
    assert all(isinstance(v, str) for v in props["metadata"].values())
    assert props["metadata"]["created_at"] == "2023-11-14T22:13:20Z"
    assert props["metadata"]["shape"] == "rect"
    assert "ai_skill" not in props["metadata"]


def test_empty_label_omits_classification_but_keeps_color():
    feature = export.build_feature({
        "annotation_id": "a1", "type": "rect", "x": 0, "y": 0,
        "w": 10, "h": 10,
    }, {"label": "", "source": "human"})
    props = _feature_props(feature)
    assert props["name"] == ""
    assert "classification" not in props
    assert props["color"] == export.label_rgb("")
    assert "note" not in props["metadata"]


def test_ai_feature_colour_and_ai_block():
    raw = {
        "annotation_id": "a-ai", "slide_id": "sld_1", "type": "polygon",
        "points": [[0, 0], [10, 0], [5, 10]], "label": "AI 病灶",
        "source": "ai", "review_status": "pending",
        "created_by_session_id": "sess",
        "provenance": {
            "skill": "delineate", "model": "gpt-4o",
            "contour_refine": {"refined": True},
        },
    }
    item = {"annotation_id": "a-ai", "slide_id": "sld_1",
            "type": "polygon", "points": [[0, 0], [10, 0], [5, 10]],
            "source": "ai", "review_status": "pending",
            "author_kind": "ai", "author_label_safe": "AI"}
    props = _feature_props(export.build_feature(item, raw))
    assert props["ai_generated"] is True
    assert props["color"] == [214, 108, 10]
    assert props["classification"]["color"] == export.label_rgb("AI 病灶")
    assert props["ai"] == {
        "skill": "delineate", "model": "gpt-4o", "contour_refined": True}
    assert props["metadata"]["ai_skill"] == "delineate"
    assert props["metadata"]["ai_model"] == "gpt-4o"
    assert props["metadata"]["ai_contour_refined"] == "true"

    # raw 不可用时按 source 判定（不把 AI 冒充人工）
    props2 = _feature_props(export.build_feature(item, None))
    assert props2["ai_generated"] is True
    assert props2["color"] == [214, 108, 10]


def test_historical_ai_labelled_human_rows_export_as_human():
    # 2026-10-19 前工作台存量：人工标注落库 source=ai，无 AI 会话/溯源
    item = {"annotation_id": "h-1", "slide_id": "sld_1", "type": "freehand",
            "points": [[0, 0], [10, 0], [5, 10]], "source": "ai",
            "review_status": "accepted", "author_kind": "user"}
    raw = dict(item, label="肿瘤", created_by_session_id="", ts=1756857600)
    props = _feature_props(export.build_feature(item, raw))
    assert props["source"] == "human"
    assert props["metadata"]["source"] == "human"
    assert props["ai_generated"] is False
    assert "ai" not in props
    assert not any(k.startswith("ai_") for k in props["metadata"])
    assert props["color"] == export.label_rgb("肿瘤")
    assert props["review_status"] == "accepted"

    # 同一行带 AI 会话（无 provenance）即为真 AI 产出
    props_ai = _feature_props(export.build_feature(
        item, dict(raw, created_by_session_id="sess_abc")))
    assert props_ai["source"] == "ai"
    assert props_ai["ai_generated"] is True
    assert props_ai["color"] == [214, 108, 10]
    assert "ai" in props_ai

    # 混合导出：AI 计数只算真 AI 行
    collection = export.build_feature_collection(
        [item, dict(item, annotation_id="h-2")],
        {"h-1": raw,
         "h-2": dict(raw, annotation_id="h-2",
                     created_by_session_id="sess_abc")},
        {"slide_id": "sld_1"}, "2026-10-29T00:00:00Z")
    assert collection["pathtogether"]["ai_feature_count"] == 1


def test_ai_provenance_never_exported():
    forbidden = {
        "provider": "FORBIDDEN_PROVIDER_HOST",
        "created_by_user_id": "FORBIDDEN_CREATED_BY",
        "run_id": "FORBIDDEN_RUN",
        "session_id": "FORBIDDEN_SESSION",
        "idempotency_key": "FORBIDDEN_IDEM",
        "snapshot_id": "FORBIDDEN_SNAP",
        "render_context_fingerprint": "FORBIDDEN_FP",
        "slide_revision": "FORBIDDEN_SLIDE_REV",
        "slide_asset_revision": "FORBIDDEN_ASSET_REV",
        "effect_key": "FORBIDDEN_EFFECT",
        "client_action_id": "FORBIDDEN_CLIENT_ACTION",
        "token": "FORBIDDEN_TOKEN",
        "visitor": "FORBIDDEN_VISITOR",
        "owner_user_id": "FORBIDDEN_OWNER",
    }
    raw = {
        "annotation_id": "a-ai", "slide_id": "sld_1", "type": "polygon",
        "points": [[0, 0], [10, 0], [5, 10]], "label": "AI",
        "source": "ai", "review_status": "pending",
        "created_by_session_id": "FORBIDDEN_SESSION",
        "revision": 7, "change_seq": 42,
        "history": [{"marker": "FORBIDDEN_HISTORY"}],
        **{k: v for k, v in forbidden.items()
           if k not in ("created_by_user_id",)},
        "provenance": {
            "plugin_id": "histopilot", "plugin_version": "0.9.0",
            "run_id": forbidden["run_id"],
            "session_id": forbidden["session_id"],
            "model": "gpt-4o",
            "provider": forbidden["provider"],
            "created_by_user_id": forbidden["created_by_user_id"],
            "slide_asset_revision": forbidden["slide_asset_revision"],
            "idempotency_key": forbidden["idempotency_key"],
            "snapshot_id": forbidden["snapshot_id"],
            "snapshot_bbox": {"x": 91001, "y": 91002, "w": 91003, "h": 91004},
            "render_context_fingerprint": forbidden[
                "render_context_fingerprint"],
            "slide_revision": forbidden["slide_revision"],
            "skill": "delineate", "skill_intent": "explicit",
            "contour_refine": {"refined": True,
                               "original_points": [[1, 2], [3, 4], [5, 6]]},
        },
    }
    item = {"annotation_id": "a-ai", "slide_id": "sld_1",
            "type": "polygon", "points": [[0, 0], [10, 0], [5, 10]],
            "source": "ai", "review_status": "pending",
            "author_kind": "ai", "author_label_safe": "AI",
            "geometry_locked": False}
    feature = export.build_feature(item, raw)
    text = json.dumps(feature, ensure_ascii=False)
    for key, value in forbidden.items():
        if key == "created_by_user_id":
            continue  # 已随 source dict 合并为顶层键
        assert value not in text, key
    assert forbidden["created_by_user_id"] not in text
    for marker in ("91001", "91002", "91003", "91004",
                   "FORBIDDEN_HISTORY"):
        assert marker not in text
    props = _feature_props(feature)
    assert props["metadata"]["ai_model"] == "gpt-4o"
    assert props["ai"]["model"] == "gpt-4o"


def test_label_rgb_matches_viewer_hash():
    # 期望值由 node -e 运行 static/app.js 的 labelColor 哈希 + HSL 换算得出
    assert export.label_rgb("Tumor") == [195, 182, 34]
    assert export.label_rgb("管理员") == [195, 34, 133]
    assert export.label_rgb("🫠") == [34, 195, 125]
    assert export.label_rgb("AI 描绘") == [34, 53, 195]
    assert export.label_rgb("") == [195, 34, 34]
    assert export.label_rgb("Necrosis") == [109, 195, 34]


def test_export_filename_sanitises_and_keeps_chinese():
    assert export.export_filename(
        {"original_filename": "病理切片 01.svs"}) == \
        "病理切片 01_annotations.geojson"
    assert export.export_filename(
        {"original_filename": "dir/sub\\name.svs"}) == \
        "name_annotations.geojson"
    assert export.export_filename(
        {"original_filename": "bad\x01name.svs"}) == \
        "badname_annotations.geojson"
    assert export.export_filename(
        {"original_filename": "", "display_name": "别名.svs"}) == \
        "别名_annotations.geojson"
    assert export.export_filename({"slide_id": "sld_abc"}) == \
        "slide_sld_abc_annotations.geojson"
    assert export.export_filename({}) == "slide_annotations.geojson"


def test_shapely_valid_polygons_if_available():
    shapely_geom = pytest.importorskip("shapely.geometry")
    items = [
        {"annotation_id": "r", "type": "rect", "x": 10, "y": 20,
         "w": 320, "h": 170},
        {"annotation_id": "p", "type": "polygon",
         "points": [[100, 100], [900, 120], [500, 800]]},
    ]
    for item in items:
        feature = export.build_feature(item)
        coords = feature["geometry"]["coordinates"][0]
        assert shapely_geom.Polygon(coords).is_valid


# =========================================================================== #
# 3. HTTP 路由
# =========================================================================== #
def test_owner_export_headers_human_and_ai_features():
    owner = _setup_owner()
    sid = _touch("病理切片 01.svs")
    share_store.set_slide_meta("病理切片 01.svs",
                               owner_user_id=owner["user_id"])
    client = _client()
    _login(client)

    human = _post_human_rect(client, sid)
    assert human.status_code == 200, human.get_data(as_text=True)
    ai = _post_ai_polygon(owner, sid)
    assert ai.status_code == 200, ai.get_data(as_text=True)

    r = client.get("/api/slides/%s/annotations/export" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"].startswith("application/geo+json")
    assert r.headers.get("Cache-Control") == "no-store"
    assert r.headers.get("X-Annotation-Count") == "2"
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert "filename*=UTF-8''" in cd
    assert "%E7%97%85%E7%90%86%E5%88%87%E7%89%87" in cd  # 病理切片

    body = json.loads(r.get_data(as_text=True))
    assert body["type"] == "FeatureCollection"
    assert body["pathtogether"]["feature_count"] == 2
    assert body["pathtogether"]["ai_feature_count"] == 1
    assert body["pathtogether"]["skipped"] == 0
    assert body["pathtogether"]["slide"]["slide_id"] == sid
    assert body["pathtogether"]["slide"]["width"] == SLIDE_W
    assert body["pathtogether"]["slide"]["height"] == SLIDE_H
    assert body["pathtogether"]["slide"]["mpp_x"] == 0.25

    # 顺序与列表端点一致
    listing = client.get("/api/annotations?slide_id=%s" % sid)
    assert listing.status_code == 200
    listed_ids = [it.get("annotation_id")
                  for it in _flatten(listing.get_json())]
    assert [f["id"] for f in body["features"]] == listed_ids

    rect_feature, ai_feature = body["features"]
    assert rect_feature["geometry"]["type"] == "Polygon"
    assert rect_feature["geometry"]["coordinates"] == [[
        [10, 20], [330, 20], [330, 190], [10, 190], [10, 20]]]
    rect_props = _feature_props(rect_feature)
    assert rect_props["objectType"] == "annotation"
    assert rect_props["name"] == "人工矩形"
    assert rect_props["source"] == "human"
    assert rect_props["ai_generated"] is False
    assert rect_props["color"] == export.label_rgb("人工矩形")
    assert all(isinstance(v, str) for v in rect_props["metadata"].values())

    assert ai_feature["geometry"]["type"] == "Polygon"
    assert ai_feature["geometry"]["coordinates"] == [[
        [100, 100], [900, 120], [500, 800], [100, 100]]]
    ai_props = _feature_props(ai_feature)
    assert ai_props["source"] == "ai"
    assert ai_props["ai_generated"] is True
    assert ai_props["color"] == [214, 108, 10]
    assert ai_props["review_status"] == "pending"
    assert ai_props["ai"]["skill"] == "delineate"
    assert ai_props["ai"]["model"] == "gpt-4o"
    assert ai_props["metadata"]["annotation_id"] == ai_feature["id"]
    assert ai_props["metadata"]["slide_id"] == sid

    # provenance 白名单：provider host / 运行/会话/幂等键绝不外泄
    text = r.get_data(as_text=True)
    for marker in ("api.example.com", "run-export", "sess-export-ai",
                   "ek-export-1", owner["user_id"]):
        assert marker not in text, marker


def test_peer_export_matches_own_visible_list_and_no_private_leak():
    owner = _setup_owner()
    sid = _touch("public.svs")
    share_store.set_slide_meta("public.svs", owner_user_id=owner["user_id"],
                               public=True)
    owner_client = _client()
    _login(owner_client)
    assert _post_human_rect(owner_client, sid).status_code == 200

    peer = _setup_peer()
    peer_client = _client()
    _login(peer_client, "peer@x.com", "peerpass1234567")
    r = peer_client.get("/api/slides/%s/annotations/export" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("X-Annotation-Count") == "0"
    body = json.loads(r.get_data(as_text=True))
    assert body["features"] == []
    assert body["pathtogether"]["feature_count"] == 0

    listing = peer_client.get("/api/annotations?slide_id=%s" % sid)
    assert listing.status_code == 200
    assert len(_flatten(listing.get_json())) == \
        body["pathtogether"]["feature_count"]
    assert peer["user_id"] != owner["user_id"]


def test_invisible_slide_denied_same_as_list():
    owner = _setup_owner()
    private_sid = _touch("private.svs")
    share_store.set_slide_meta("private.svs",
                               owner_user_id=owner["user_id"])
    _setup_peer()
    peer_client = _client()
    _login(peer_client, "peer@x.com", "peerpass1234567")

    listed = peer_client.get("/api/annotations?slide_id=%s" % private_sid)
    exported = peer_client.get(
        "/api/slides/%s/annotations/export" % private_sid)
    assert listed.status_code == 403
    assert exported.status_code == 403
    assert exported.get_json() == listed.get_json()


def test_unknown_slide_404():
    _setup_owner()
    client = _client()
    _login(client)
    r = client.get("/api/slides/sld_does_not_exist/annotations/export")
    assert r.status_code == 404
    assert r.get_json().get("code") == "slide_not_found"


def test_unsupported_format_400():
    owner = _setup_owner()
    sid = _touch("demo.svs")
    share_store.set_slide_meta("demo.svs", owner_user_id=owner["user_id"])
    client = _client()
    _login(client)
    r = client.get(
        "/api/slides/%s/annotations/export?format=xml" % sid)
    assert r.status_code == 400
    body = r.get_json()
    assert body.get("code") == "unsupported_format"

    ok = client.get(
        "/api/slides/%s/annotations/export?format=geojson" % sid)
    assert ok.status_code == 200
