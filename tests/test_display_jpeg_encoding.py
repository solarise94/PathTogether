# -*- coding: utf-8 -*-
"""F2 荧光瓦片/派生图编码：display-jpeg-v2（native 4:2:0 / multichannel 4:4:4）。

规格（review-2026-09-05 F2）：
  - ``TILE_ENCODER_VERSION="display-jpeg-v2"``；native quality=82
    subsampling=2；multichannel quality=95 subsampling=0；显式传入，禁止靠
    Pillow 默认；
  - 合成高饱和细色线：4:4:4 解码后比 4:2:0 更接近预编码 RGB（像素差断言，
    不只比文件体积）；
  - 三处 tile（官方 / Demo / share）与 region 接入：multichannel 瓦片服务出
    的 JPEG 实际是 4:4:4、region 同理；缓存键含 quality/subsampling/编码版本
    （同键重复请求字节一致，native/multichannel 键互不命中）；
  - MIME 仍 image/jpeg，URL 扩展名仍 .jpeg。

运行：cd PathTogether && python3 -m pytest tests/test_display_jpeg_encoding.py -q
"""
import base64
import io
import os
import sys
from pathlib import Path

import pytest

import _bootstrap  # noqa: F401  # session 目录 + openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

from PIL import Image, JpegImagePlugin  # noqa: E402

import share_server as share_srv  # noqa: E402
import slide_cache  # noqa: E402
import slide_render  # noqa: E402
import app as app_mod  # noqa: E402
from _tiff_fixtures import make_ome_cyx_bytes, make_ome_tiff_bytes  # noqa: E402

FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"
CYX_NAME = "enc_cyx4.ome.tiff"
RGB_NAME = "enc_rgb.ome.tiff"


def _reset_caches():
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


def _client():
    return csrf_client(app_mod.app.test_client())


def _flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)


def _write(name, data):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return name


def _sampling(jpeg_bytes):
    """解码 JPEG 的实际色度抽样（0=4:4:4，2=4:2:0）。"""
    img = Image.open(io.BytesIO(jpeg_bytes))
    img.load()
    return JpegImagePlugin.get_sampling(img)


# --------------------------------------------------------------------------- #
# 1. 纯函数：encode_display_jpeg 参数契约
# --------------------------------------------------------------------------- #
def test_encoder_version_constant():
    assert slide_render.TILE_ENCODER_VERSION == "display-jpeg-v2"


def test_encode_params_native_and_multichannel():
    img = Image.new("RGB", (32, 16))
    b_nat, p_nat = slide_render.encode_display_jpeg(
        img, image_mode="native_rgb")
    b_mc, p_mc = slide_render.encode_display_jpeg(
        img, image_mode="multichannel")
    assert p_nat == {"quality": 82, "subsampling": 2,
                     "encoder_version": "display-jpeg-v2"}
    assert p_mc == {"quality": 95, "subsampling": 0,
                    "encoder_version": "display-jpeg-v2"}
    assert _sampling(b_nat) == 2 and _sampling(b_mc) == 0
    # region 派生图：quality 仍走调用方，subsampling 只随 image_mode
    _, p_ovr = slide_render.encode_display_jpeg(
        img, image_mode="multichannel", quality=85)
    assert p_ovr["quality"] == 85 and p_ovr["subsampling"] == 0


def test_encode_multichannel_closer_to_source_rgb():
    """高饱和细红/青线：4:4:4 解码比 4:2:0 更接近预编码 RGB（像素差）。"""
    import numpy as np

    arr = np.zeros((64, 256, 3), dtype=np.uint8)
    arr[:, ::4] = (255, 0, 0)    # 每 4px 一列细红线
    arr[:, 2::8] = (0, 255, 255)  # 每 8px 一列细青线
    img = Image.fromarray(arr)
    orig = arr.astype(np.int16)
    b420, _ = slide_render.encode_display_jpeg(img, image_mode="native_rgb")
    b444, _ = slide_render.encode_display_jpeg(img, image_mode="multichannel")
    e420 = np.abs(np.asarray(Image.open(io.BytesIO(b420)),
                             dtype=np.int16) - orig).mean()
    e444 = np.abs(np.asarray(Image.open(io.BytesIO(b444)),
                             dtype=np.int16) - orig).mean()
    assert e444 < e420, "4:4:4(%0.3f) 应比 4:2:0(%0.3f) 更接近源 RGB" \
        % (e444, e420)


# --------------------------------------------------------------------------- #
# 2. 瓦片接入（官方 / share）与缓存键
# --------------------------------------------------------------------------- #
def _tile_url(name=CYX_NAME):
    return "/api/slide/%s_files/0/0_0.jpeg" % name


def test_multichannel_tile_is_444_and_stable(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=4))
    c = _client()
    r1 = c.get(_tile_url())
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert r1.mimetype == "image/jpeg"
    assert _sampling(r1.data) == 0, "多通道默认瓦片必须 4:4:4"
    r2 = c.get(_tile_url())
    assert r2.status_code == 200 and r2.data == r1.data, \
        "同 context 重复请求必须命中同一缓存键（含 quality/subsampling/版本）"


def test_native_tile_is_420(monkeypatch):
    """RGB 切片（或 flag 关）瓦片仍 native 4:2:0、q82。"""
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    r = _client().get(_tile_url(RGB_NAME))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _sampling(r.data) == 2


def test_tile_fp_key_separates_native_and_multichannel():
    """缓存键必须含 quality/subsampling/encoder_version：两 mode 键互异。"""
    k_nat = app_mod._tile_fp_key("a", 1, "fp", 0, 0, 0,
                                 image_mode="native_rgb")
    k_mc = app_mod._tile_fp_key("a", 1, "fp", 0, 0, 0,
                                image_mode="multichannel")
    assert k_nat != k_mc
    assert k_nat[-3:] == (82, 2, "display-jpeg-v2")
    assert k_mc[-3:] == (95, 0, "display-jpeg-v2")
    sk_nat = share_srv._tile_fp_key("a", 1, "fp", 0, 0, 0,
                                    image_mode="native_rgb")
    assert sk_nat == k_nat, "share 与主站缓存键同一形状"


def test_share_tile_is_444(monkeypatch):
    import share_store

    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=4))
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data")
    monkeypatch.setattr(share_store, "SHARE_FILE",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data"
                        / "shares.json")
    share = share_store.create_share([CYX_NAME], 24)
    sc = share_srv.app.test_client()
    r = sc.get("/s/%s/api/slide/%s_files/0/0_0.jpeg"
               % (share["token"], CYX_NAME))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.mimetype == "image/jpeg"
    assert r.headers.get("Cache-Control")
    assert _sampling(r.data) == 0, "分享端多通道瓦片必须 4:4:4"


# --------------------------------------------------------------------------- #
# 3. region 接入：multichannel context → 4:4:4；native → 4:2:0
# --------------------------------------------------------------------------- #
def test_region_multichannel_is_444_native_is_420(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    r_mc = c.get("/api/slide/%s/region?x=0&y=0&w=16&h=16" % CYX_NAME)
    assert r_mc.status_code == 200, r_mc.get_data(as_text=True)
    body = r_mc.get_json()
    assert body["mime"] == "image/jpeg"
    assert _sampling(base64.b64decode(body["image_base64"])) == 0, \
        "多通道 context region 必须 4:4:4"
    r_nat = c.get("/api/slide/%s/region?x=0&y=0&w=16&h=16" % RGB_NAME)
    assert r_nat.status_code == 200, r_nat.get_data(as_text=True)
    assert _sampling(base64.b64decode(r_nat.get_json()["image_base64"])) == 2


def test_image_mode_from_context_native_rgb_not_multichannel():
    assert slide_render.image_mode_from_context(None) == "native_rgb"
    assert slide_render.image_mode_from_context(
        {"version": "native-rgb-v1"}) == "native_rgb"
    assert slide_render.image_mode_from_context(
        {"version": "multichannel-additive-v1"}) == "multichannel"


# --------------------------------------------------------------------------- #
# 4. region encoder 身份回显（P1-2）：encoder.image_mode / encoder.subsampling
# --------------------------------------------------------------------------- #
def _internal_region_body(slide, **kw):
    headers = {"X-AI-Internal-Token": app_mod.AI_INTERNAL_TOKEN}
    r = _client().post("/internal/ai/region", headers=headers,
                       json=dict({"slide": slide, "x": 0, "y": 0,
                                  "w": 16, "h": 16}, **kw))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def test_internal_region_encoder_identity_native(monkeypatch):
    """无 context：encoder 如实回报 native_rgb/4:2:0，与实际色度抽样一致。"""
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    body = _internal_region_body(RGB_NAME)
    enc = body["encoder"]
    assert enc["image_mode"] == "native_rgb"
    assert enc["subsampling"] == "4:2:0"
    # 回报身份 = 实际编码：解码后抽样确为 4:2:0
    assert _sampling(base64.b64decode(body["image_base64"])) == 2
    # 64×96 fixture 默认 out 1568 → 请求分辨率高于源 → upsampled=true
    assert body["upsampled"] is True


def test_internal_region_encoder_identity_multichannel(monkeypatch):
    """multichannel context：encoder 如实回报 multichannel/4:4:4。"""
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    r = _client().post("/api/slide/%s/render-context" % CYX_NAME,
                       json={"active_channels": [{"index": 0}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    ctx = r.get_json()["render_context"]
    body = _internal_region_body(CYX_NAME, render_context=ctx)
    enc = body["encoder"]
    assert enc["image_mode"] == "multichannel"
    assert enc["subsampling"] == "4:4:4"
    # 回报身份 = 实际编码：解码后抽样确为 4:4:4
    assert _sampling(base64.b64decode(body["image_base64"])) == 0


def test_rgb_tile_stays_420_when_flag_on(monkeypatch):
    """flag 开时 RGB 仍带 native-rgb-v1 context，不得误编 4:4:4。"""
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    r = c.get("/api/slide/%s_files/0/0_0.jpeg" % RGB_NAME)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _sampling(r.data) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
