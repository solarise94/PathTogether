# -*- coding: utf-8 -*-
"""升级 C（批次 4a/4b）：通用矩形工具端到端测试（升级方案 §6/§10）。

覆盖：
  1. 几何权威口径（§6.2/§6.3）：v2 成对 w/h 权威；旧 side_px 读时归一
     w=h=side_px（存储与 revision/annotation_id/change_seq 不动）；只给一个
     w/h、非法值、v2 与 side_px 冲突 → 拒绝；真正方形可带一致 side_px；
     非正方形不得伪造 side_px。
  2. 真实切片边界（§6.3-4）：出界 400 不静默裁剪；路径入口校验（主站
     POST /api/annotation 与 PATCH geom；internal/plugin v1 annotate w/h）。
  3. crop（§6.3-5）：主站与分享端 w/h 输出尺寸精确等于 w/h；出界拒绝；
     size 与 w/h 混用 400；旧 size 兼容分支不变；crop_guard 按真实 w*h 计。
  4. 分享（§6.4）：preset_only（含旧分享缺字段）新增/编辑两条路径都限制
     预设正方形（真实几何 + 可信 MPP，不信客户端自报 size_mm）；custom
     分享允许任意宽高；分享 crop 不受 roi_sizes 约束；readonly/撤销不放宽。
  5. PG 往返：v1/v2 ROI、history、change feed 几何字段贯穿。
"""
import io
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

from PIL import Image  # noqa: E402

import crop_guard  # noqa: E402
import share_shared  # noqa: E402
import share_store  # noqa: E402
import share_server as share_srv  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例隔离 + 统一注入可信切片元数据（1000×800 @ mpp 分轴）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 真实切片边界（测试切片是字节 stub；注入可信 dims 供路径入口校验）
    monkeypatch.setattr(app_mod, "_annotation_slide_bounds",
                        lambda safe: (1000.0, 800.0))
    # 分享端真实元数据（分轴 MPP：mpp_x=0.2, mpp_y=0.1 µm/px）
    monkeypatch.setattr(share_srv, "_slide_dims_and_mpp",
                        lambda safe: (1000, 800, 0.2, 0.1))
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    return name


def _setup_owner():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    share_store.set_owner_user_id(owner["user_id"])
    share_store.set_slide_meta("demo.svs", owner_user_id=owner["user_id"])
    return owner


def _client(login_as_owner=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    c = csrf_client(app_mod.app.test_client())
    if login_as_owner:
        r = c.post("/login", data={"username": "owner@x.com",
                                   "password": "ownerpass123456"})
        assert r.status_code in (200, 302), r.get_data(as_text=True)
    return c


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


# =========================================================================== #
# 1. 几何归一化（share_shared 纯函数）
# =========================================================================== #
def test_geom_v2_pair_is_authoritative():
    g = share_shared._validate_geom("rect", {"x": 5, "y": 6, "w": 3200, "h": 1700})
    assert g["x"] == 5 and g["y"] == 6 and g["w"] == 3200 and g["h"] == 1700
    assert g["geometry_version"] == 2
    assert "side_px" not in g  # 非正方形不得伪造 side_px
    assert g["size_mm"] == 0.0


def test_geom_v2_square_keeps_side_px_compat():
    g = share_shared._validate_geom(
        "rect", {"x": 0, "y": 0, "w": 100, "h": 100, "side_px": 100})
    assert g["w"] == 100 and g["h"] == 100 and g["side_px"] == 100
    assert g["geometry_version"] == 2


def test_geom_v2_side_conflict_rejected():
    with pytest.raises(ValueError):
        share_shared._validate_geom(
            "rect", {"x": 0, "y": 0, "w": 3200, "h": 1700, "side_px": 3200})
    with pytest.raises(ValueError):
        share_shared._validate_geom(
            "rect", {"x": 0, "y": 0, "w": 100, "h": 100, "side_px": 50})


def test_geom_unpaired_w_or_h_rejected():
    with pytest.raises(ValueError):
        share_shared._validate_geom("rect", {"x": 0, "y": 0, "w": 100})
    with pytest.raises(ValueError):
        share_shared._validate_geom("rect", {"x": 0, "y": 0, "h": 100})


def test_geom_invalid_values_rejected():
    for bad in (0, -5, 40001, float("nan"), float("inf"), "abc", None, True):
        with pytest.raises(ValueError):
            share_shared._validate_geom("rect", {"x": 0, "y": 0, "w": bad, "h": 10})


def test_geom_v1_side_px_compat_and_pixel_budget():
    g = share_shared._validate_geom("rect", {"x": 1, "y": 2, "side_px": 300})
    assert g == {"type": "rect", "x": 1, "y": 2, "side_px": 300, "size_mm": 0.0}
    # w*h 像素预算（与 crop 硬闸同源）：40001×40001 被单边上限挡；
    # 40000×2000 超 4096² 预算被拒
    with pytest.raises(ValueError):
        share_shared._validate_geom("rect", {"x": 0, "y": 0, "w": 40000, "h": 2000})
    # 恰在预算内放行
    g2 = share_shared._validate_geom("rect", {"x": 0, "y": 0, "w": 4096, "h": 4096})
    assert g2["w"] == 4096 and g2["h"] == 4096


# =========================================================================== #
# 2. v1/v2 PG 往返 + 读兼容
# =========================================================================== #
def test_v2_roi_roundtrip_and_history_and_change_feed():
    _touch()
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "R",
                              type="rect", x=10, y=20, w=320, h=170)
    assert roi["w"] == 320 and roi["h"] == 170
    assert roi["geometry_version"] == 2
    cur = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert (cur["x"], cur["y"], cur["w"], cur["h"]) == (10, 20, 320, 170)
    # 编辑（v2 w/h）→ revision/历史/change feed 全链贯穿 w/h
    upd = share_store.update_roi(share_store.ADMIN_TOKEN, 0,
                                 geom={"x": 15, "y": 25, "w": 300, "h": 150})
    assert upd["revision"] == 2
    assert (upd["w"], upd["h"]) == (300, 150)
    hist = share_store.get_roi(share_store.ADMIN_TOKEN, 0)["history"]
    assert hist and hist[0]["geom"]["w"] == 320 and hist[0]["geom"]["h"] == 170
    changes = share_store.list_changes("demo.svs", 0)
    # change feed 承接几何字段（v2 w/h；feed 反映该行最新状态）
    assert len(changes) >= 1
    assert all(c.get("w") == 300 and c.get("h") == 150 for c in changes
               if c.get("annotation_id") == roi["annotation_id"])
    # 列表/项目索引返回全部承接 w/h
    assert share_store.list_rois(share_store.ADMIN_TOKEN)[0]["w"] == 300
    by_slide = share_store.annotations_by_slide()["demo.svs"][0]["items"]
    assert by_slide[0]["w"] == 300 and by_slide[0]["h"] == 150


def test_v1_side_px_read_normalized_storage_untouched():
    _touch()
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "R",
                              type="rect", x=1, y=2, side_px=100, size_mm=6.0)
    aid = roi["annotation_id"]
    rev0 = roi["revision"]
    cs0 = roi["change_seq"]
    # 读时归一 w=h=side_px
    out = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert out["w"] == 100 and out["h"] == 100
    assert out["annotation_id"] == aid and out["revision"] == rev0
    assert out["change_seq"] == cs0
    # 存储不被批量改写：data 原样保留 side_px、无 w/h
    full = share_store.get_roi_by_annotation_id(aid)
    assert full["w"] == 100 and full["h"] == 100  # 该读路径也归一
    # 直接查库验证存储形态
    import psycopg
    import pg_store
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rois WHERE annotation_id=%s", (aid,))
            raw = cur.fetchone()[0]
    finally:
        conn.close()
    assert "w" not in raw and "h" not in raw
    assert raw["side_px"] == 100


def test_store_rejects_contradictory_geometry():
    _touch()
    share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "R",
                        type="rect", x=0, y=0, w=100, h=100)
    # 旧 side_px 编辑 v2 非正方形 → 拒绝（旧客户端不得把矩形改回方形）
    share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "R",
                        type="rect", x=0, y=0, w=200, h=50)
    with pytest.raises(ValueError):
        share_store.update_roi(share_store.ADMIN_TOKEN, 1,
                               geom={"x": 0, "y": 0, "side_px": 120})
    # v2 编辑成对 w/h 正常
    out = share_store.update_roi(share_store.ADMIN_TOKEN, 1,
                                 geom={"x": 5, "y": 5, "w": 180, "h": 40})
    assert (out["w"], out["h"]) == (180, 40)


# =========================================================================== #
# 3. 主站 API：POST /api/annotation w/h + 出界拒绝 + PATCH
# =========================================================================== #
def test_api_annotation_v2_and_bounds():
    _touch()
    _setup_owner()
    c = _client()  # owner 已建号后登录
    r = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 100, "y": 100, "w": 320, "h": 170,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    idx = r.get_json()["index"]
    item = share_store.annotations_by_slide()["demo.svs"][0]["items"][idx]
    assert item["w"] == 320 and item["h"] == 170
    assert item["geometry_version"] == 2

    # 出界（x+w>1000）→ 400，不静默裁剪
    r2 = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 900, "y": 100, "w": 320, "h": 170,
    })
    assert r2.status_code == 400
    assert "不自动裁剪" in r2.get_json()["error"]

    # 只给 w → 400
    r3 = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 0, "y": 0, "w": 100,
    })
    assert r3.status_code == 400

    # v2 与 side_px 冲突 → 400
    r4 = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 0, "y": 0, "w": 320, "h": 170, "side_px": 320,
    })
    assert r4.status_code == 400

    # 旧 side_px 兼容仍可用
    r5 = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 0, "y": 0, "side_px": 100,
    })
    assert r5.status_code == 200


def test_api_patch_v2_geom_with_cas():
    _touch()
    _setup_owner()
    c = _client()
    r = c.post("/api/annotation", json={
        "slide": "demo.svs", "type": "rect", "label": "L",
        "x": 0, "y": 0, "w": 320, "h": 170,
    })
    idx = r.get_json()["index"]
    # 成对 w/h 编辑 + CAS 成功
    r2 = c.patch("/api/annotation/admin/%d" % idx,
                 json={"geom": {"x": 1, "y": 2, "w": 300, "h": 150},
                       "expected_revision": 1})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    # 过期 CAS → 409
    r3 = c.patch("/api/annotation/admin/%d" % idx,
                 json={"geom": {"x": 1, "y": 2, "w": 300, "h": 150},
                       "expected_revision": 1})
    assert r3.status_code == 409
    assert r3.get_json()["current_revision"] == 2
    # 出界编辑 → 400
    r4 = c.patch("/api/annotation/admin/%d" % idx,
                 json={"geom": {"x": 900, "y": 700, "w": 300, "h": 150}})
    assert r4.status_code == 400


# =========================================================================== #
# 4. AI 写入通道（internal + plugin v1）接受 w/h
# =========================================================================== #
def test_internal_annotate_accepts_wh(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_legacy_slide_revision", lambda safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI", "x": 10, "y": 10,
        "width_px": 200, "height_px": 80, "note": "n", "effect_key": "ek-1",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["w"] == 200 and body["h"] == 80
    # 幂等重放同 effect_key → 同一标注
    r2 = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI", "x": 10, "y": 10,
        "width_px": 200, "height_px": 80, "effect_key": "ek-1",
    })
    assert r2.get_json()["annotation_id"] == body["annotation_id"]


# =========================================================================== #
# 5. 分享：rect_policy（preset_only / custom）两条写入路径
# =========================================================================== #
def test_share_preset_paths_with_trusted_mpp(monkeypatch):
    """preset_only：新增与编辑都按真实几何+可信 MPP 复核（6mm ⇔ 100px @60µm/px）。"""
    _touch()
    _setup_owner()
    # mpp 60µm/px：6mm = 100px
    monkeypatch.setattr(share_srv, "_slide_dims_and_mpp",
                        lambda safe: (1000, 800, 60.0, 60.0))
    tok = share_store.create_share(["demo.svs"], 1)["token"]
    sc = _share_client()
    good = {"slide": "demo.svs", "type": "rect", "label": "V",
            "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0}
    r = sc.post("/s/%s/api/roi" % tok, json=good)
    assert r.status_code == 200, r.get_data(as_text=True)
    # 谎报 size_mm=6 实画 300px → 403（真实几何复核）
    cheat = dict(good, side_px=300)
    r2 = sc.post("/s/%s/api/roi" % tok, json=cheat)
    assert r2.status_code == 403
    # v2 非正方形 → 403（preset_only 不允许自定义矩形）
    r3 = sc.post("/s/%s/api/roi" % tok,
                 json={"slide": "demo.svs", "type": "rect", "label": "V",
                       "x": 0, "y": 0, "w": 200, "h": 50})
    assert r3.status_code == 403
    # 编辑路径：把预设改成 6.5mm 以外的尺寸 → 403；改回预设内 → 200
    idx = share_store.list_rois(tok)[0]["index"]
    r4 = sc.patch("/s/%s/api/roi/%s" % (tok, idx),
                  json={"geom": {"x": 0, "y": 0, "w": 300, "h": 300}})
    assert r4.status_code == 403
    r5 = sc.patch("/s/%s/api/roi/%s" % (tok, idx),
                  json={"geom": {"x": 5, "y": 5, "w": 100, "h": 100}})
    assert r5.status_code == 200, r5.get_data(as_text=True)


def test_share_custom_allows_rect_and_old_share_defaults_preset(monkeypatch):
    _touch()
    _setup_owner()
    monkeypatch.setattr(share_srv, "_slide_dims_and_mpp",
                        lambda safe: (1000, 800, 60.0, 60.0))
    custom = share_store.create_share(["demo.svs"], 1, rect_policy="custom")
    assert custom["rect_policy"] == "custom"
    tok = custom["token"]
    sc = _share_client()
    r = sc.post("/s/%s/api/roi" % tok,
                json={"slide": "demo.svs", "type": "rect", "label": "V",
                      "x": 10, "y": 10, "w": 320, "h": 170})
    assert r.status_code == 200, r.get_data(as_text=True)
    listed = sc.get("/s/%s/api/rois" % tok).get_json()
    assert any(i.get("w") == 320 and i.get("h") == 170 for i in listed)
    # custom 编辑也允许任意宽高
    idx = listed[0]["index"]
    r2 = sc.patch("/s/%s/api/roi/%s" % (tok, idx),
                  json={"geom": {"x": 0, "y": 0, "w": 400, "h": 90}})
    assert r2.status_code == 200, r2.get_data(as_text=True)

    # 旧分享缺字段（直接 SQL 模拟 NULL rect_policy）→ preset_only
    import psycopg
    import pg_store
    legacy_tok = share_store.create_share(["demo.svs"], 1)["token"]
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE shares SET rect_policy=NULL WHERE token=%s",
                        (legacy_tok,))
        conn.commit()
    finally:
        conn.close()
    sc2 = _share_client()
    r3 = sc2.post("/s/%s/api/roi" % legacy_tok,
                  json={"slide": "demo.svs", "type": "rect", "label": "V",
                        "x": 0, "y": 0, "w": 320, "h": 170})
    assert r3.status_code == 403
    assert sc2.get("/s/%s/api/config" % legacy_tok).get_json()["rect_policy"] == "preset_only"


def test_share_revoked_and_viewonly_unchanged(monkeypatch):
    _touch()
    _setup_owner()
    monkeypatch.setattr(share_srv, "_slide_dims_and_mpp",
                        lambda safe: (1000, 800, 60.0, 60.0))
    share = share_store.create_share(["demo.svs"], 1, rect_policy="custom",
                                     permissions=["view"])
    tok = share["token"]
    sc = _share_client()
    # view-only 不因 custom 获得写权限
    r = sc.post("/s/%s/api/roi" % tok,
                json={"slide": "demo.svs", "type": "rect", "label": "V",
                      "x": 0, "y": 0, "w": 10, "h": 20})
    assert r.status_code == 403
    # 撤销后同样拒绝
    share_store.revoke_share(tok)
    r2 = sc.post("/s/%s/api/roi" % tok,
                 json={"slide": "demo.svs", "type": "rect", "label": "V",
                       "x": 0, "y": 0, "w": 10, "h": 20})
    assert r2.status_code == 404


# =========================================================================== #
# 6. crop：w/h 精确输出 + 边界 + 混用冲突 + 真实 w*h 预算
# =========================================================================== #
_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _mock_slide_read(mod, dims=(2000, 1500)):
    """fake osr：read_region 返回真实 PIL 图（可编码 PNG 供尺寸断言）。"""
    osr = mock.Mock(dimensions=dims)

    def _read_region(xy, level, size):
        return Image.new("RGB", (size[0], size[1]), (200, 30, 30))

    osr.read_region.side_effect = _read_region

    def _resolve_context(*a, **k):
        return None, None

    pair = {"osr": osr}
    p1 = mock.patch.object(mod, "_get_slide", return_value=_FAKE_ENTRY)
    p2 = mock.patch.object(mod.slide_cache, "borrow_pair",
                           side_effect=lambda _e: _borrow_pair_ctx(pair))
    if mod is app_mod:
        p3 = mock.patch.object(app_mod, "_resolve_pair_context",
                               side_effect=_resolve_context)
    else:
        p3 = mock.patch.object(share_srv, "_resolve_pair",
                               side_effect=_resolve_context)
    if mod is share_srv:
        p4 = mock.patch.object(share_srv, "_region_view", side_effect=lambda pair, ctx, fp: pair["osr"])
    else:
        p4 = mock.patch.object(app_mod, "_region_view", side_effect=lambda pair, ctx, fp: pair["osr"])
    return p1, p2, p3, p4


def test_main_crop_wh_exact_output_and_guards(monkeypatch):
    _touch()
    _setup_owner()
    c = _client()
    p1, p2, p3, p4 = _mock_slide_read(app_mod, dims=(2000, 1500))
    with p1, p2, p3, p4:
        r = c.get("/api/slide/demo.svs/crop?x=100&y=50&w=320&h=170")
        assert r.status_code == 200, r.get_data(as_text=True)
        img = Image.open(io.BytesIO(r.get_data()))
        assert img.size == (320, 170)  # 精确等于合法 w/h
        assert "320x170px" in r.headers.get("Content-Disposition", "")
        # 出界 w/h → 400（不 clamp）
        r2 = c.get("/api/slide/demo.svs/crop?x=1800&y=0&w=320&h=170")
        assert r2.status_code == 400
        # size 与 w/h 混用 → 400
        r3 = c.get("/api/slide/demo.svs/crop?x=0&y=0&size=100&w=320&h=170")
        assert r3.status_code == 400
        # 旧 size 兼容分支仍走 clamp 正方形
        r4 = c.get("/api/slide/demo.svs/crop?x=1900&y=1400&size=300")
        assert r4.status_code == 200
        img4 = Image.open(io.BytesIO(r4.get_data()))
        assert img4.size == (100, 100)  # clamp 到边界（2000-1900=100）


def test_main_crop_wh_pixel_budget_real_wh(monkeypatch):
    _touch()
    _setup_owner()
    c = _client()
    # 真实 w*h 预算：4000×5000=20M > CROP_MAX_PIXELS(默认 16.7M) → 413
    p1, p2, p3, p4 = _mock_slide_read(app_mod, dims=(20000, 20000))
    with p1, p2, p3, p4, mock.patch.object(crop_guard, "CROP_MAX_PIXELS", 4096 ** 2):
        r = c.get("/api/slide/demo.svs/crop?x=0&y=0&w=4000&h=5000")
        assert r.status_code == 413
        assert r.get_json()["code"] == "crop_too_large"
        # 同像素量正方形也拒（对照旧实现 size=4472 只查单边不查 w*h 的回归）
        r2 = c.get("/api/slide/demo.svs/crop?x=0&y=0&size=40000")
        assert r2.status_code == 413


def test_share_crop_wh_exact_and_not_roi_sizes_bound(monkeypatch):
    _touch()
    _setup_owner()
    monkeypatch.setattr(share_srv, "_slide_dims_and_mpp",
                        lambda safe: (2000, 1500, 60.0, 60.0))
    # preset_only 分享的 crop 不受 roi_sizes 约束（E06 收窄口径）
    tok = share_store.create_share(["demo.svs"], 1,
                                   roi_sizes=[6.0])["token"]
    sc = _share_client()
    p1, p2, p3, p4 = _mock_slide_read(share_srv, dims=(2000, 1500))
    with p1, p2, p3, p4:
        r = sc.get("/s/%s/api/slide/demo.svs/crop?x=10&y=20&w=640&h=400" % tok)
        assert r.status_code == 200, r.get_data(as_text=True)
        img = Image.open(io.BytesIO(r.get_data()))
        assert img.size == (640, 400)
        assert "640x400px" in r.headers.get("Content-Disposition", "")
        # 出界拒绝
        r2 = sc.get("/s/%s/api/slide/demo.svs/crop?x=1500&y=0&w=640&h=400" % tok)
        assert r2.status_code == 400
        # 混用冲突
        r3 = sc.get("/s/%s/api/slide/demo.svs/crop?x=0&y=0&size=100&w=64&h=40" % tok)
        assert r3.status_code == 400
        # 旧 size 兼容
        r4 = sc.get("/s/%s/api/slide/demo.svs/crop?x=0&y=0&size=50" % tok)
        assert r4.status_code == 200


# =========================================================================== #
# 7. plugin v1 annotate（带 run grant 全链）w/h 直通
# =========================================================================== #
def test_plugin_v1_annotate_wh(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": "inst1"}, None))
    grant = {"grant_id": "g1", "slide": "demo.svs", "installation_id": "inst1",
             "created_by_user_id": "u1", "session_id": "sess1"}
    monkeypatch.setattr(app_mod, "_verify_run_grant",
                        lambda gid, slide, inst, expect_session=None: (True, ""))
    monkeypatch.setattr(app_mod, "share_store", app_mod.share_store)
    monkeypatch.setattr(app_mod.share_store, "get_run_grant", lambda gid: grant)
    monkeypatch.setattr(app_mod, "_archived_slide_names", lambda: [])
    monkeypatch.setattr(app_mod.share_store, "get_plugin_installation",
                        lambda iid: {"plugin_id": "histopilot", "version": "0"})
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_legacy_slide_revision", lambda safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI-rect", "x": 5, "y": 5, "width_px": 220, "height_px": 90,
        "note": "n", "effect_key": "ek-p1", "session_id": "sess1",
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["w"] == 220 and body["h"] == 90
    # 出界 → 400 invalid_request（不静默裁剪）
    r2 = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI-rect", "x": 990, "y": 5, "width_px": 220, "height_px": 90,
    }, headers={"X-Run-Grant": "g1"})
    assert r2.status_code == 400
    assert r2.get_json()["error"]["code"] == "invalid_request"
