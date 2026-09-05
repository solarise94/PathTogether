# -*- coding: utf-8 -*-
"""Batch 3 路由 / render_token / 缓存 / 派生图 API 测试。

规格：docs/agent-fix-sse-summary-and-multichannel-pseudocolor-2026-09-04.md
§6.1（info additive 字段）、§6.2（render-context 规范化端点）、§6.3（资源端点
render=<token>）、§7.3（generation + fingerprint cache key）、§7.4（解码前
拒绝 / 像素预算×通道数）、§15.2（PATHTOGETHER_MULTICHANNEL_ENABLED flag）。

原则（与 test_slide_render 一致）：
  - 全部切片由真实 tifffile 2024.5.22 写出（tests/_tiff_fixtures），无患者数据；
  - render_token 验签、revision 绑定、cache key 均走真实 HTTP 路由；
  - 主站 / Demo / share / plugin v1 / internal 各访问面同一 fixture 同一 context
    必须得到相同 fingerprint。

运行：cd PathTogether && python -m pytest tests/test_render_context_api.py -q
"""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import _bootstrap  # noqa: F401  # session 目录 + openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import slide_cache  # noqa: E402
import slide_render  # noqa: E402
import app as app_mod  # noqa: E402
from _tiff_fixtures import (  # noqa: E402
    make_ome_cyx_bytes,
    make_ome_tiff_bytes,
)

FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"
CYX_NAME = "cyx4.ome.tiff"


# --------------------------------------------------------------------------- #
# 隔离与夹具
# --------------------------------------------------------------------------- #
def _reset_caches():
    """清空三层缓存（跨用例隔离）：句柄池 / info / 主站与分享端瓦片 / 统计。"""
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()
    with app_mod._tile_cache_lock:
        app_mod._tile_cache.clear()
    with share_srv._tile_cache_lock:
        share_srv._tile_cache.clear()
    slide_render.reset_caches()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    app_mod.app.config["TESTING"] = True
    share_srv.app.config["TESTING"] = True
    _reset_caches()
    yield
    _reset_caches()


def _write(name, data):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return name


def _write_cyx(name=CYX_NAME, c=4, **kw):
    return _write(name, make_ome_cyx_bytes(c=c, **kw))


def _replace_cyx(name=CYX_NAME, c=2):
    """同名替换（不同通道数，内容必不同）。"""
    _write(name, make_ome_cyx_bytes(c=c))


def _client():
    """主站 test client：写方法自动附带 CSRF token（与真实前端一致）。"""
    return csrf_client(app_mod.app.test_client())


def _flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)


def _get_info(name=CYX_NAME):
    r = _client().get("/api/slide/%s/info" % name)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _post_context(body, name=CYX_NAME):
    return _client().post("/api/slide/%s/render-context" % name, json=body)


def _default_tile_url(name=CYX_NAME, level=0, x=0, y=0):
    return "/api/slide/%s_files/%d/%d_%d.jpeg" % (name, level, x, y)


def _ctx_from_selection(selection, info=None, revision=None, name=CYX_NAME):
    """经 render-context 端点把用户选择规范化为 canonical context。"""
    if info is None:
        info = _get_info(name)
    revision = revision or info["asset_revision"]
    r = _post_context({"active_channels": selection}, name=name)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["render_context_fingerprint"] == \
        out["render_context"]["fingerprint"]
    return out


# --------------------------------------------------------------------------- #
# 1. RGB info 回归（§6.1：旧字段仍在；RGB 无通道面板字段）
# --------------------------------------------------------------------------- #
def test_rgb_info_native_rgb_fields_and_legacy_keys(monkeypatch):
    _flag_on(monkeypatch)
    name = _write("rgb.ome.tiff", make_ome_tiff_bytes())
    info = _get_info(name)
    assert info["image_mode"] == "native_rgb"
    assert info["channels"] == []
    assert info["default_render_context"]["version"] == "native-rgb-v1"
    assert info["default_render_context"]["active_channels"] == []
    assert info["default_render_token"]
    # 旧字段全部仍在（additive）
    for key in ("name", "width", "height", "mpp_x", "mpp_y", "mpp_source",
                "size_bytes", "alias", "note", "public"):
        assert key in info, "旧 info 字段 %s 不得丢失" % key
    # fingerprint 是 64 位小写 hex
    fp = info["default_render_context"]["fingerprint"]
    assert len(fp) == 64 and all(ch in "0123456789abcdef" for ch in fp)


def test_rgb_info_flag_off_hides_channel_fields(monkeypatch):
    _flag_off(monkeypatch)
    name = _write("rgb2.ome.tiff", make_ome_tiff_bytes())
    info = _get_info(name)
    assert "channels" not in info
    assert "default_render_token" not in info
    assert info["server_capability"]["multichannel"] is False


def test_rgb_tile_flag_on_pixel_identical_to_flag_off(monkeypatch):
    """RGB 老切片 flag 开时走 native 直通：瓦片像素与旧路径一致。"""
    name = _write("rgb3.ome.tiff", make_ome_tiff_bytes())
    _flag_off(monkeypatch)
    b_off = _client().get(_default_tile_url(name)).data
    _flag_on(monkeypatch)
    b_on = _client().get(_default_tile_url(name)).data
    assert b_off == b_on, "RGB 切片 flag 开关下瓦片像素必须一致"


# --------------------------------------------------------------------------- #
# 2. 多通道 info（flag 开：channels / default_render_token）
# --------------------------------------------------------------------------- #
def test_multichannel_info_flag_on(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    info = _get_info()
    assert info["image_mode"] == "multichannel"
    assert info["axes"] == "CYX"
    assert len(info["channels"]) == 4
    ch0 = info["channels"][0]
    assert ch0["index"] == 0 and ch0["name"] == "通道 1"
    assert ch0["color_source"] == "default"
    assert info["plane"] == {"t": 0, "z": 0, "size_t": 1, "size_z": 1,
                             "policy": "first-plane-v1"}
    drc = info["default_render_context"]
    assert drc["version"] == "multichannel-additive-v1"
    assert drc["asset_revision"] == info["asset_revision"]
    assert len(drc["active_channels"]) == 4  # 默认启用前 4 个
    assert info["default_render_token"]
    assert info["deepzoom"]["tile_size"] == app_mod.DZ_TILE_SIZE
    assert info["deepzoom"]["overlap"] == app_mod.DZ_OVERLAP
    assert isinstance(info["warnings"], list)


def test_multichannel_info_flag_off(monkeypatch):
    _flag_off(monkeypatch)
    _write_cyx()
    info = _get_info()
    assert "channels" not in info
    assert "default_render_token" not in info
    assert info["server_capability"]["multichannel"] is False


# --------------------------------------------------------------------------- #
# 3. POST render-context：规范化 + fingerprint 顺序无关
# --------------------------------------------------------------------------- #
def test_post_render_context_canonical_and_order_independent(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    info = _get_info()
    a = _post_context({"active_channels": [{"index": 0}, {"index": 2}]})
    assert a.status_code == 200, a.get_data(as_text=True)
    # 相同语义不同顺序（含 dict 键序）→ 同 fingerprint
    b = _client().post(
        "/api/slide/%s/render-context" % CYX_NAME,
        data=json.dumps({"active_channels": [
            {"gamma": 1.0, "index": 2}, {"index": 0}]}),
        content_type="application/json")
    assert b.status_code == 200
    ja, jb = a.get_json(), b.get_json()
    assert ja["render_context_fingerprint"] == jb["render_context_fingerprint"]
    canon = ja["render_context"]
    assert [c["index"] for c in canon["active_channels"]] == [0, 2]
    assert canon["asset_revision"] == info["asset_revision"]
    assert ja["render_token"]
    # 颜色缺省时由服务端 manifest 补齐（确定性色卡）
    assert canon["active_channels"][0]["color"] == "#00FFFF"
    assert canon["active_channels"][1]["color"] == "#FFD166"


def test_post_render_context_color_change_alters_fingerprint(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    a = _ctx_from_selection([{"index": 0}])
    b = _ctx_from_selection([{"index": 0, "color": "#FF0000"}])
    assert a["render_context_fingerprint"] != b["render_context_fingerprint"]
    # black/white 变化同样必须变
    c = _ctx_from_selection([{"index": 0, "black": 1.0}])
    assert c["render_context_fingerprint"] != a["render_context_fingerprint"]


# --------------------------------------------------------------------------- #
# 4. 解码前 4xx（稳定码）
# --------------------------------------------------------------------------- #
def test_nine_channels_rejected_with_channel_limit(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx(c=12)
    selection = [{"index": i} for i in range(9)]
    r = _post_context({"active_channels": selection}, name=CYX_NAME)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert (r.get_json() or {}).get("code") == "render_channel_limit"


def test_out_of_range_index_and_nan_rejected(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    r = _post_context({"active_channels": [{"index": 99}]})
    assert r.status_code == 400
    assert (r.get_json() or {}).get("code") == "render_channel_out_of_range"
    r2 = _post_context({"active_channels": [{"index": 0,
                                             "black": float("nan"),
                                             "white": 10.0}]})
    assert r2.status_code == 400
    assert (r2.get_json() or {}).get("code") == "invalid_render_context"


def test_tampered_token_rejected_before_decode(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    tok = _ctx_from_selection([{"index": 0}])["render_token"]
    body, _sig = tok.rsplit(".", 1)
    bad = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + _sig
    before = slide_render.metrics_snapshot()["composite_built"]
    r = _client().get(_default_tile_url() + "?render=" + bad)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert (r.get_json() or {}).get("code") == "invalid_render_context"
    assert slide_render.metrics_snapshot()["context_verify_fail"] >= 1
    assert slide_render.metrics_snapshot()["composite_built"] == before


def test_flag_off_ignores_render_token(monkeypatch):
    """flag 关：资源端点忽略 render token（走 legacy），不因坏 token 报错。"""
    _write_cyx()
    _flag_on(monkeypatch)
    tok = _ctx_from_selection([{"index": 0}])["render_token"]
    _flag_off(monkeypatch)
    r = _client().get(_default_tile_url() + "?render=" + "not-a-token")
    assert r.status_code == 200, r.get_data(as_text=True)
    # POST 规范化端点 flag 关：稳定 403
    rc = _post_context({"active_channels": [{"index": 0}]})
    assert rc.status_code == 403
    assert (rc.get_json() or {}).get("code") == "multichannel_disabled"


# --------------------------------------------------------------------------- #
# 5. tile 带 render=：与默认不同且稳定；两种 context 不串色
# --------------------------------------------------------------------------- #
def test_tile_two_contexts_differ_and_stable(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    ca = _ctx_from_selection([{"index": 0}])
    cb = _ctx_from_selection([{"index": 1}])
    c = _client()
    url = _default_tile_url()
    a1 = c.get(url + "?render=" + ca["render_token"]).data
    b1 = c.get(url + "?render=" + cb["render_token"]).data
    a2 = c.get(url + "?render=" + ca["render_token"]).data
    assert a1 != b1, "两种 context 的同一瓦片不得串色"
    assert a1 == a2, "同 context 重复请求必须稳定（cache key 含 fingerprint）"
    d1 = c.get(url).data  # 默认 context（前 4 通道）
    assert d1 not in (a1, b1)


def test_thumbnail_and_crop_follow_context(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    ca = _ctx_from_selection([{"index": 0}])
    c = _client()
    t_default = c.get("/api/slide/%s/thumbnail" % CYX_NAME).data
    t_tok = c.get("/api/slide/%s/thumbnail?render=%s"
                  % (CYX_NAME, ca["render_token"])).data
    assert t_default != t_tok, "缩略图必须与瓦片同 context"

    cr = c.get("/api/slide/%s/crop?x=0&y=0&size=16&render=%s"
               % (CYX_NAME, ca["render_token"]))
    assert cr.status_code == 200, cr.get_data(as_text=True)
    fp8 = ca["render_context_fingerprint"][:8]
    assert fp8 in cr.headers.get("Content-Disposition", ""), \
        "crop 下载文件名必须含 render fingerprint 前 8 位"
    # 默认（多通道默认 context）crop 同样带 fp8
    cr2 = c.get("/api/slide/%s/crop?x=0&y=0&size=16" % CYX_NAME)
    assert cr2.status_code == 200
    assert cr2.get_json() is None  # 是图像不是 JSON 错误
    d_fp = _get_info()["default_render_context"]["fingerprint"][:8]
    assert d_fp in cr2.headers.get("Content-Disposition", "")


def test_region_endpoint_accepts_render_token(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    ca = _ctx_from_selection([{"index": 0}])
    r = _client().get("/api/slide/%s/region?x=0&y=0&w=16&h=16&render=%s"
                      % (CYX_NAME, ca["render_token"]))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["render_context_fingerprint"] == \
        ca["render_context_fingerprint"]


# --------------------------------------------------------------------------- #
# 6. 同名替换：旧 token 409，新 generation 不命中旧 cache
# --------------------------------------------------------------------------- #
def test_replacement_conflicts_old_token_and_cache(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    tok = _ctx_from_selection([{"index": 0}])["render_token"]
    c = _client()
    url = _default_tile_url()
    old_tile = c.get(url + "?render=" + tok).data
    assert old_tile
    _replace_cyx()  # 同名替换为 2 通道内容
    r = c.get(url + "?render=" + tok)
    assert r.status_code == 409, r.get_data(as_text=True)
    assert (r.get_json() or {}).get("code") == "slide_revision_conflict"
    # 无 render 的默认瓦片：新代内容，不得命中旧 cache（4ch 默认 ≠ 2ch 默认）
    new_default = c.get(url).data
    assert new_default != old_tile


# --------------------------------------------------------------------------- #
# 7. Demo / share / 主站同 fixture 同 context → 相同 fingerprint
# --------------------------------------------------------------------------- #
def _demo_client_with_capability(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_ENABLED", "1")
    share_store.set_slide_meta(CYX_NAME)
    slide_id = share_store.get_slide_id(CYX_NAME)
    import demo_store
    demo_store.catalog_add(slide_id, added_by="owner-test")
    client = _client()
    r = client.get("/api/demo/config")
    assert r.status_code == 200, r.get_data(as_text=True)
    return client, slide_id


def test_demo_info_context_and_fingerprint_match_main(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    client, slide_id = _demo_client_with_capability(monkeypatch)
    main_info = _get_info()
    r = client.get("/api/demo/slides/%s/info" % slide_id)
    assert r.status_code == 200, r.get_data(as_text=True)
    demo_info = r.get_json()
    assert demo_info["image_mode"] == "multichannel"
    assert demo_info["default_render_context"]["fingerprint"] == \
        main_info["default_render_context"]["fingerprint"]
    # Demo render-context 端点
    rc = client.post("/api/demo/slides/%s/render-context" % slide_id,
                     json={"active_channels": [{"index": 0}]})
    assert rc.status_code == 200, rc.get_data(as_text=True)
    main_rc = _post_context({"active_channels": [{"index": 0}]})
    assert rc.get_json()["render_context_fingerprint"] == \
        main_rc.get_json()["render_context_fingerprint"]
    # Demo 瓦片带 token：200 且与主站同 token 内容一致
    td = client.get("/api/demo/slides/%s_files/0/0_0.jpeg?render=%s"
                    % (slide_id, main_rc.get_json()["render_token"]))
    assert td.status_code == 200, td.get_data(as_text=True)
    tm = _client().get(_default_tile_url() + "?render=%s"
                       % main_rc.get_json()["render_token"])
    assert td.data == tm.data


def _share_env(monkeypatch, name=CYX_NAME):
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data")
    monkeypatch.setattr(share_store, "SHARE_FILE",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data"
                        / "shares.json")
    share = share_store.create_share([name], 24)
    return share["token"]


def test_share_face_matches_main_fingerprint(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    token = _share_env(monkeypatch)
    sc = share_srv.app.test_client()
    main_info = _get_info()
    # 分享端单切片 info（新增 additive 路由）
    r = sc.get("/s/%s/api/slide/%s/info" % (token, CYX_NAME))
    assert r.status_code == 200, r.get_data(as_text=True)
    s_info = r.get_json()
    assert s_info["default_render_context"]["fingerprint"] == \
        main_info["default_render_context"]["fingerprint"]
    # 分享端 render-context 端点
    rc = sc.post("/s/%s/api/slide/%s/render-context" % (token, CYX_NAME),
                 json={"active_channels": [{"index": 0}]})
    assert rc.status_code == 200, rc.get_data(as_text=True)
    s_rc = rc.get_json()
    main_rc = _post_context({"active_channels": [{"index": 0}]})
    assert s_rc["render_context_fingerprint"] == \
        main_rc.get_json()["render_context_fingerprint"]
    # 分享端瓦片带 token 与主站一致
    ts = sc.get("/s/%s/api/slide/%s_files/0/0_0.jpeg?render=%s"
                % (token, CYX_NAME, s_rc["render_token"]))
    assert ts.status_code == 200, ts.get_data(as_text=True)
    tm = _client().get(_default_tile_url() + "?render=%s"
                       % main_rc.get_json()["render_token"])
    assert ts.data == tm.data
    # 分享端越权：不在该 share 的切片 403
    _write("other.ome.tiff", make_ome_cyx_bytes(c=2))
    denied = sc.post("/s/%s/api/slide/other.ome.tiff/render-context" % token,
                     json={"active_channels": [{"index": 0}]})
    assert denied.status_code == 403


# --------------------------------------------------------------------------- #
# 8. plugin v1 region 与 internal/ai/region 的 render_context
# --------------------------------------------------------------------------- #
def _plugin_bootstrap():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            raw = str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": raw})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def test_plugin_v1_region_with_render_context(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    tok = _plugin_bootstrap()
    ca = _ctx_from_selection([{"index": 0}])
    headers = {"Authorization": "Bearer " + tok}
    r = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers=headers,
        json={"x": 0, "y": 0, "w": 16, "h": 16, "out_w": 8, "out_h": 8,
              "render_context": ca["render_context"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["render_context_fingerprint"] == \
        ca["render_context_fingerprint"]
    # 旧 revision 的 context → 409
    stale = dict(ca["render_context"])
    stale["asset_revision"] = "0:0"
    r2 = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers=headers,
        json={"x": 0, "y": 0, "w": 16, "h": 16, "out_w": 8, "out_h": 8,
              "render_context": stale})
    assert r2.status_code == 409, r2.get_data(as_text=True)
    err = (r2.get_json() or {}).get("error") or {}
    assert err.get("code") == "slide_revision_conflict"
    # 越界通道 → 400（解码前）
    bad = dict(ca["render_context"])
    bad["active_channels"] = [{"index": 77, "color": "#00FFFF", "alpha": 1.0,
                               "black": 0.0, "white": 100.0, "gamma": 1.0}]
    r3 = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers=headers,
        json={"x": 0, "y": 0, "w": 16, "h": 16, "out_w": 8, "out_h": 8,
              "render_context": bad})
    assert r3.status_code == 400, r3.get_data(as_text=True)
    assert ((r3.get_json() or {}).get("error") or {}).get("code") == \
        "render_channel_out_of_range"


def test_plugin_v1_region_encoder_identity_binary_and_json(monkeypatch):
    """P1-2：encoder 携带本次响应**实际**编码身份，两条 transport 同源同值。

    multichannel context → ("multichannel","4:4:4")；同请求去掉 context →
    ("native_rgb","4:2:0")。X-Region-Upsampled 同头透出（小尺寸 fixture
    默认 out 1568 超过源像素 → 放大 true）。
    """
    _flag_on(monkeypatch)
    _write_cyx()
    tok = _plugin_bootstrap()
    ca = _ctx_from_selection([{"index": 0}])
    body = {"x": 0, "y": 0, "w": 16, "h": 16,
            "render_context": ca["render_context"]}
    # 二进制路径：X-Region-Encoder / X-Region-Upsampled 头
    rb = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/octet-stream"},
        json=body)
    assert rb.status_code == 200, rb.get_data(as_text=True)
    assert rb.headers["Content-Type"] == "application/octet-stream"
    enc = json.loads(rb.headers["X-Region-Encoder"])
    assert enc["image_mode"] == "multichannel"
    assert enc["subsampling"] == "4:4:4"
    assert json.loads(rb.headers["X-Region-Upsampled"]) is True
    # 同参数 JSON 路径：encoder 字段与二进制头同值
    rj = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers={"Authorization": "Bearer " + tok}, json=body)
    assert rj.status_code == 200, rj.get_data(as_text=True)
    jb = rj.get_json()
    assert jb["encoder"]["image_mode"] == "multichannel"
    assert jb["encoder"]["subsampling"] == "4:4:4"
    assert jb["upsampled"] is True
    # 同请求去掉 context：native 编码（真实编码路径，非猜测）
    native = dict(body)
    native.pop("render_context")
    rn = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers={"Authorization": "Bearer " + tok}, json=native)
    assert rn.status_code == 200, rn.get_data(as_text=True)
    nb = rn.get_json()
    assert nb["encoder"]["image_mode"] == "native_rgb"
    assert nb["encoder"]["subsampling"] == "4:2:0"


def test_plugin_region_pixel_budget_scales_with_channels(monkeypatch):
    """8 通道不能当 1 通道计费：小预算下 1 通道放行、8 通道 429。"""
    _flag_on(monkeypatch)
    _write_cyx()
    _write("cyx8.ome.tiff", make_ome_cyx_bytes(c=8))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 10000)
    tok = _plugin_bootstrap()
    headers = {"Authorization": "Bearer " + tok}
    ca8 = _ctx_from_selection([{"index": i} for i in range(8)],
                              name="cyx8.ome.tiff")
    ca1 = _ctx_from_selection([{"index": 0}])
    body = {"x": 0, "y": 0, "w": 64, "h": 64, "out_w": 8, "out_h": 8}
    r1 = _client().post(
        "/api/plugin/v1/slides/%s/regions" % CYX_NAME,
        headers=headers, json=dict(body, render_context=ca1["render_context"]))
    assert r1.status_code == 200, r1.get_data(as_text=True)
    r8 = _client().post(
        "/api/plugin/v1/slides/%s/regions" % "cyx8.ome.tiff",
        headers=headers,
        json=dict(body, render_context=ca8["render_context"]))
    assert r8.status_code == 429, r8.get_data(as_text=True)
    err = (r8.get_json() or {}).get("error") or {}
    assert err.get("code") == "rate_limited"


def test_internal_ai_region_with_render_context(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    ca = _ctx_from_selection([{"index": 0}])
    headers = {"X-AI-Internal-Token": app_mod.AI_INTERNAL_TOKEN}
    r = _client().post(
        "/internal/ai/region", headers=headers,
        json={"slide": CYX_NAME, "x": 0, "y": 0, "w": 16, "h": 16,
              "out_w": 8, "out_h": 8, "render_context": ca["render_context"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["render_context_fingerprint"] == \
        ca["render_context_fingerprint"]


# --------------------------------------------------------------------------- #
# 9. 并发：同 context 统计只算一次
# --------------------------------------------------------------------------- #
def test_concurrent_default_context_stats_computed_once(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    slide_render.reset_caches()
    c = _client()
    url = _default_tile_url()
    barrier = threading.Barrier(20)

    def _hit(_):
        barrier.wait()
        r = c.get(url)
        assert r.status_code == 200
        return r.data

    with ThreadPoolExecutor(max_workers=20) as ex:
        bodies = list(ex.map(_hit, range(20)))
    assert len(set(bodies)) == 1, "同 context 同瓦片必须字节一致"
    m = slide_render.metrics_snapshot()
    assert m["stats_computed"] == 4, "4 通道统计必须只各算一次：%s" % m
    assert m["stats_cache_miss"] == 4
    # 后续并发借用必须复用统计（命中数依赖调度，但至少同 key 等待方复用）
    assert m["stats_cache_hit"] >= 1


# --------------------------------------------------------------------------- #
# 10. token 跨 worker / 独立进程可验证（无共享内存）
# --------------------------------------------------------------------------- #
def test_token_roundtrip_with_derived_key_only(monkeypatch):
    """sign/verify 只依赖应用 secret 派生 key：新构造的 secret 相同即可验证。"""
    _flag_on(monkeypatch)
    _write_cyx()
    tok = _ctx_from_selection([{"index": 0}])["render_token"]
    secret = app_mod.app.secret_key
    payload = slide_render.verify_render_token(tok, secret)
    assert payload is not None
    assert payload["rev"] == app_mod._legacy_slide_revision(CYX_NAME)
    assert payload["slide"] == CYX_NAME
    assert payload["ctx"]["active_channels"][0]["index"] == 0
    # secret 换掉 → 失效（稳定码路径，不抛异常）
    assert slide_render.verify_render_token(tok, "rotated-secret") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
