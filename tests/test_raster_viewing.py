# -*- coding: utf-8 -*-
"""普通图片（BMP/JPEG）查看/缓存/渲染/分享端到端（Wave 2C）。

覆盖 docs/raster-image-compatibility-agent-plan.md §4.3/§4.4/§4.6 与 §6
「图像接口 / 分享策略 / 生命周期 / 性能」相关行（主站/分享查看闭环中本进程
可验证的部分）：

  - slide_render RGB 分支守卫：RasterSlide 恒 native_rgb，不进荧光通道流程
    （slide_image_mode / build_channel_manifest / resolve_render_context /
    canonicalize_selection / build_render_info / composite_region 防御守卫 /
    RenderedSlideView 直通）；
  - share_server 一致性：_read_metadata 对普通图片输出 mpp/objective=null、
    mpp_source="missing"（DPI/EXIF 不当 µm/px，不补 0.25/40×）；
  - 真 BMP 走 DZI / 瓦片 / 边界瓦片 / 缩略图路由（DZI 尺寸、边界瓦片、
    缩略图比例各验一点）；
  - 分享权限：缺可信 MPP 的毫米预设标注（新增与编辑两条路径）**继续拒绝**；
  - 生命周期：真实 RasterSlide 句柄进 slide_cache 后单句柄、换代/淘汰释放；
  - share_server 不再拥有独立扩展名词表（闲置 SUPPORTED_EXTS 已删除）。

fixture 全部为 PIL 合成字节（无患者数据；真实请求样本本机不可达，
真实样本兼容性**待验收**，不以合成数据冒充）。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_raster_viewing.py -q
"""
import io
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import share_store  # noqa: E402
import share_server as share_srv  # noqa: E402
import slide_cache  # noqa: E402
import slide_render  # noqa: E402
import raster_slide  # noqa: E402
from openslide.deepzoom import DeepZoomGenerator  # noqa: E402


# --------------------------------------------------------------------------- #
# 合成 fixture（与 test_raster_slide 同思路：非对称四角图案）
# --------------------------------------------------------------------------- #
_R = (255, 0, 0)
_G = (0, 255, 0)
_B = (0, 0, 255)
_Y = (255, 255, 0)


def _corner_image(w=700, h=500, block=16):
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for i in range(block):
        for j in range(block):
            im.putpixel((i, j), _R)
            im.putpixel((w - 1 - i, j), _G)
            im.putpixel((i, h - 1 - j), _B)
            im.putpixel((w - 1 - i, h - 1 - j), _Y)
    return im


def _bmp_bytes(im):
    b = io.BytesIO()
    im.save(b, format="BMP")
    return b.getvalue()


def _jpeg_bytes(im):
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=95)
    return b.getvalue()


@pytest.fixture(autouse=True)
def _isolate_caches():
    """跨用例清空 slide_cache 三层状态（本文件会用真实句柄）。"""
    def _clear():
        with slide_cache._cache_lock:
            slide_cache._slide_cache.clear()
            slide_cache._raster_lru.clear()
        with slide_cache._info_cache_lock:
            slide_cache._info_cache.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def view_env(tmp_path, monkeypatch):
    """分享端测试环境（与 test_slide_cache_generation.share_env 同形）。"""
    data_dir = tmp_path / "share-data"
    upload_dir = tmp_path / "uploads"
    data_dir.mkdir(parents=True)
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", upload_dir)
    # 缺省 flag 关：与生产默认一致（个别用例显式打开）
    monkeypatch.delenv(slide_render.FLAG_ENV, raising=False)
    share_store.set_owner_user_id("")
    share_srv.app.config["TESTING"] = True
    with share_srv.app.test_client() as c:
        yield c, upload_dir
    share_srv._tile_cache.clear()


def _share_of(upload_dir, filename, data, **kw):
    p = upload_dir / filename
    p.write_bytes(data)
    return share_store.create_share([filename], 24, **kw)["token"]


# --------------------------------------------------------------------------- #
# 1) slide_render RGB 分支守卫：普通图片绝不进荧光通道流程
# --------------------------------------------------------------------------- #
def _raster(tmp_path, name="g.bmp", w=64, h=48):
    p = tmp_path / name
    p.write_bytes(_bmp_bytes(_corner_image(w, h, block=8)))
    return raster_slide.RasterSlide(p)


def test_raster_image_mode_is_native_rgb(tmp_path):
    s = _raster(tmp_path)
    try:
        assert s.is_raster_image is True
        assert slide_render.slide_image_mode(s) == "native_rgb"
        manifest = slide_render.build_channel_manifest(s)
        assert manifest["image_mode"] == "native_rgb"
        assert manifest["channels"] == []
        assert manifest["warnings"] == []
    finally:
        s.close()


def test_raster_resolve_render_context_defaults_legacy(tmp_path):
    """缺省解析：普通图片恒走 native/legacy（ctx=None），不产生通道方案。"""
    s = _raster(tmp_path)
    try:
        ctx, fp = slide_render.resolve_render_context(
            s, safe="g.bmp", expected_revision="rev-1", flag_enabled=True)
        assert ctx is None and fp is None
    finally:
        s.close()


def test_raster_rejects_active_channels_selection(tmp_path):
    """用户提交 active_channels：native_rgb 不接受 → 稳定 invalid_render_context。"""
    s = _raster(tmp_path)
    try:
        with pytest.raises(slide_render.SlideRenderError) as ei:
            slide_render.canonicalize_selection(
                s, {"active_channels": [{
                    "index": 0, "color": "#FF0000", "alpha": 1.0,
                    "black": 0, "white": 255}]},
                asset_revision="rev-1")
        assert ei.value.code == "invalid_render_context"
    finally:
        s.close()


def test_raster_build_render_info_native_shape(tmp_path):
    """info additive：native_rgb、空 channels、native-rgb-v1 默认 context、
    不含荧光通道流程的 ``axes`` 字段。"""
    s = _raster(tmp_path)
    try:
        out = slide_render.build_render_info(
            s, asset_revision="rev-1", secret="unit-secret",
            slide_name="g.bmp", flag_enabled=True)
        assert out["image_mode"] == "native_rgb"
        assert out["channels"] == []
        assert "axes" not in out
        assert out["default_render_context"]["version"] == \
            slide_render.CONTEXT_VERSION_NATIVE_RGB
        assert out["default_render_token"]
        assert out["server_capability"]["multichannel"] is False
    finally:
        s.close()


def test_raster_composite_region_guard(tmp_path):
    """防御守卫：multichannel context 落到普通图片 → 稳定错误而非
    AttributeError（RasterSlide 没有 channel_count/read_region_channels）。"""
    s = _raster(tmp_path)
    try:
        ctx = {
            "version": slide_render.CONTEXT_VERSION_MULTICHANNEL,
            "asset_revision": "rev-1",
            "plane": {"t": 0, "z": 0},
            "active_channels": [{
                "index": 0, "color": "#FF0000", "alpha": 1.0,
                "black": 0.0, "white": 255.0, "gamma": 1.0}],
        }
        with pytest.raises(slide_render.SlideRenderError) as ei:
            slide_render.composite_region(s, ctx, (0, 0), 0, (4, 4))
        assert ei.value.code == "invalid_render_context"
        # 同一 context 经 RenderedSlideView 也不得触发通道合成
        view = slide_render.RenderedSlideView(s, ctx, fingerprint="x")
        with pytest.raises(slide_render.SlideRenderError):
            view.read_region((0, 0), 0, (4, 4))
    finally:
        s.close()


def test_raster_rendered_view_passthrough_native(tmp_path):
    """native/无 context：RenderedSlideView 直通底层像素与缩略图。"""
    s = _raster(tmp_path)
    try:
        view = slide_render.RenderedSlideView(s, None,
                                              fingerprint="native")
        direct = s.read_region((0, 0), 0, (8, 6))
        via_view = view.read_region((0, 0), 0, (8, 6))
        assert list(direct.getdata()) == list(via_view.getdata())
        thumb = view.get_thumbnail((32, 32))
        assert thumb.mode == "RGBA"
        assert max(thumb.size) <= 32
        # 左上角仍是 fixture 红块（坐标系未被动过；LANCZOS 重采样留容差）
        px = thumb.convert("RGB").getpixel((1, 1))
        assert all(abs(a - b) <= 6 for a, b in zip(px, _R)), px
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 2) share_server 一致性：元数据 / DZI / 瓦片 / 缩略图（真 BMP）
# --------------------------------------------------------------------------- #
def test_share_slides_and_info_metadata_missing_mpp(view_env):
    """普通图片：mpp_x/mpp_y/objective=null、mpp_source="missing"（列表+详情）。"""
    c, upload_dir = view_env
    token = _share_of(upload_dir, "photo.bmp",
                      _bmp_bytes(_corner_image(64, 48, block=8)))
    r = c.get("/s/%s/api/slides" % token)
    assert r.status_code == 200
    item = r.get_json()[0]
    assert item["exists"] is True
    assert (item["width"], item["height"]) == (64, 48)
    assert item["mpp_x"] is None and item["mpp_y"] is None
    assert item["objective"] is None
    assert item["mpp_source"] == "missing"

    r2 = c.get("/s/%s/api/slide/photo.bmp/info" % token)
    assert r2.status_code == 200
    info = r2.get_json()
    assert (info["width"], info["height"]) == (64, 48)
    assert info["mpp_x"] is None and info["mpp_y"] is None
    assert info["objective"] is None
    assert info["mpp_source"] == "missing"


def test_share_raster_dzi_tile_boundary_thumbnail(view_env):
    """真 BMP 走完 DZI/瓦片路由：DZI 尺寸、最高层与右/下边界瓦片、缩略图比例。"""
    c, upload_dir = view_env
    w, h = 700, 600
    token = _share_of(upload_dir, "photo.bmp", _bmp_bytes(_corner_image(w, h)))

    # DZI：Size 必须等于校正后 level-0 尺寸
    rd = c.get("/s/%s/api/slide/photo.bmp.dzi" % token)
    assert rd.status_code == 200
    xml = rd.get_data(as_text=True)
    assert 'Width="%d"' % w in xml and 'Height="%d"' % h in xml
    assert 'TileSize="%d"' % share_srv.DZ_TILE_SIZE in xml

    # 最高层（1:1）左上角瓦片 + 右/下边界瓦片
    s = raster_slide.RasterSlide(upload_dir / "photo.bmp")
    try:
        dz = DeepZoomGenerator(s, tile_size=share_srv.DZ_TILE_SIZE,
                               overlap=share_srv.DZ_OVERLAP,
                               limit_bounds=True)
        top = dz.level_count - 1
        cols, rows = dz.level_tiles[top]
        assert cols >= 2 and rows >= 2  # 700×500 在 512 瓦片下确有边界瓦片
    finally:
        s.close()
    for (x, y) in ((0, 0), (cols - 1, 0), (0, rows - 1), (cols - 1, rows - 1)):
        url = "/s/%s/api/slide/photo.bmp_files/%d/%d_%d.jpeg" % (
            token, top, x, y)
        rt = c.get(url)
        assert rt.status_code == 200, (x, y, rt.get_data(as_text=True))
        tile = Image.open(io.BytesIO(rt.data))
        assert tile.format == "JPEG"
        # overlap 使边缘瓦片最多 tile_size + 2*overlap 像素
        assert tile.size[0] <= share_srv.DZ_TILE_SIZE + 2 * share_srv.DZ_OVERLAP
        assert tile.size[1] <= share_srv.DZ_TILE_SIZE + 2 * share_srv.DZ_OVERLAP
    # 左上角瓦片内容：非对称图案左上角为红（JPEG 容差比较）
    tl = Image.open(io.BytesIO(c.get(
        "/s/%s/api/slide/photo.bmp_files/%d/0_0.jpeg" % (token, top)).data))
    px = tl.convert("RGB").getpixel((2, 2))
    assert all(abs(a - b) <= 40 for a, b in zip(px, _R)), px

    # 缩略图：保比例 contain（700:500 → 400×286），JPEG 可解码
    rth = c.get("/s/%s/api/slide/photo.bmp/thumbnail" % token)
    assert rth.status_code == 200
    thumb = Image.open(io.BytesIO(rth.data))
    assert thumb.format == "JPEG"
    tw, th = thumb.size
    assert max(tw, th) == 400
    assert abs(tw / th - w / h) <= 0.02


def test_share_raster_jpeg_viewing(view_env):
    """.jpg 同链路：info 缺标尺 + 缩略图可用。"""
    c, upload_dir = view_env
    token = _share_of(upload_dir, "photo.jpg",
                      _jpeg_bytes(_corner_image(400, 200, block=10)))
    info = c.get("/s/%s/api/slide/photo.jpg/info" % token).get_json()
    assert (info["width"], info["height"]) == (400, 200)
    assert info["mpp_source"] == "missing" and info["objective"] is None
    rth = c.get("/s/%s/api/slide/photo.jpg/thumbnail" % token)
    assert rth.status_code == 200
    thumb = Image.open(io.BytesIO(rth.data))
    assert max(thumb.size) == 400
    assert abs(thumb.size[0] / thumb.size[1] - 2.0) <= 0.02


# --------------------------------------------------------------------------- #
# 3) 分享权限：缺可信 MPP 的毫米预设标注继续拒绝（§4.4 锁定，不得放宽）
# --------------------------------------------------------------------------- #
def test_share_raster_preset_mm_rect_rejected(view_env):
    """preset_only 分享 + 普通图片（无 MPP）：新增 rect 403，文案指向缺标尺。"""
    c, upload_dir = view_env
    token = _share_of(upload_dir, "photo.bmp",
                      _bmp_bytes(_corner_image(64, 48, block=8)))
    r = c.post("/s/%s/api/roi" % token,
               json={"slide": "photo.bmp", "type": "rect", "label": "V",
                     "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0})
    assert r.status_code == 403, r.get_data(as_text=True)
    assert "缺少可信 MPP" in r.get_json()["error"]


def test_share_raster_preset_mm_rect_edit_rejected(view_env):
    """编辑路径同样拒绝：不能借编辑绕过「缺可信 MPP」的预设尺寸核验。"""
    c, upload_dir = view_env
    token = _share_of(upload_dir, "photo.bmp",
                      _bmp_bytes(_corner_image(64, 48, block=8)))
    # 直接落一条 rect（策略校验在路由层，store 只做数值校验）
    roi = share_store.add_roi(token, "photo.bmp", "V", type="rect",
                              x=0, y=0, w=100, h=100)
    r = c.patch("/s/%s/api/roi/%s" % (token, roi["index"]),
                json={"geom": {"x": 0, "y": 0, "w": 100, "h": 100}})
    assert r.status_code == 403, r.get_data(as_text=True)
    assert "缺少可信 MPP" in r.get_json()["error"]


def test_reject_preset_rect_mm_unit_locks_missing_mpp(view_env):
    """单元锁定：_reject_preset_rect_mm 对缺可信 MPP 一律 (msg, 403)。"""
    _c, upload_dir = view_env
    (upload_dir / "photo.bmp").write_bytes(
        _bmp_bytes(_corner_image(64, 48, block=8)))
    token = share_store.create_share(["photo.bmp"], 24)["token"]
    share = share_store.get_share(token)
    for declared in (6.0, None):
        reject = share_srv._reject_preset_rect_mm(
            share, "photo.bmp", 100, 100, declared_mm=declared)
        assert reject is not None
        msg, status = reject
        assert status == 403
        assert "缺少可信 MPP" in msg


# --------------------------------------------------------------------------- #
# 4) 多通道 flag 开：普通图片仍走 RGB 显示路径（token/瓦片像素一致）
# --------------------------------------------------------------------------- #
def test_share_raster_with_multichannel_flag_on(view_env, monkeypatch):
    c, upload_dir = view_env
    monkeypatch.setenv(slide_render.FLAG_ENV, "1")
    token = _share_of(upload_dir, "photo.bmp",
                      _bmp_bytes(_corner_image(64, 48, block=8)))
    info = c.get("/s/%s/api/slide/photo.bmp/info" % token).get_json()
    assert info["image_mode"] == "native_rgb"
    assert info["channels"] == []
    assert "axes" not in info
    assert info["default_render_context"]["version"] == \
        slide_render.CONTEXT_VERSION_NATIVE_RGB
    assert info["server_capability"]["multichannel"] is False

    # render-context POST 带 active_channels → 400 invalid_render_context
    r = c.post("/s/%s/api/slide/photo.bmp/render-context" % token,
               json={"active_channels": [{
                   "index": 0, "color": "#FF0000", "alpha": 1.0,
                   "black": 0, "white": 255}]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_render_context"

    # info 签发的 native token 拉瓦片：像素与无 token 瓦片一致（未进通道合成）
    plain = c.get("/s/%s/api/slide/photo.bmp_files/0/0_0.jpeg" % token)
    assert plain.status_code == 200
    tok_tile = c.get("/s/%s/api/slide/photo.bmp_files/0/0_0.jpeg?render=%s"
                     % (token, info["default_render_token"]))
    assert tok_tile.status_code == 200
    p1 = Image.open(io.BytesIO(plain.data)).convert("RGB")
    p2 = Image.open(io.BytesIO(tok_tile.data)).convert("RGB")
    assert p1.size == p2.size
    assert list(p1.getdata()) == list(p2.getdata())


# --------------------------------------------------------------------------- #
# 5) 生命周期：真实句柄的缓存行为（单句柄 / LRU 淘汰释放）
# --------------------------------------------------------------------------- #
def test_raster_cache_real_single_handle(tmp_path):
    """真实 RasterSlide 进 slide_cache：并发借用只解码一次（created_handles=1）。"""
    p = tmp_path / "real.bmp"
    p.write_bytes(_bmp_bytes(_corner_image(64, 48, block=8)))
    entry = slide_cache.get_slide("real.bmp", p)
    assert entry["raster"] is True
    for _ in range(3):
        with slide_cache.borrow_pair(entry) as pair:
            assert pair["osr"].dimensions == (64, 48)
            assert pair["dz"].get_dzi("jpeg")  # noqa: B015  DZI 可生成
    assert entry["created_handles"] == 1


def test_raster_cache_real_lru_eviction_releases_decoder(tmp_path, monkeypatch):
    """真实句柄的 LRU 淘汰：被逐条目的解码缓冲被释放（_closed）。"""
    monkeypatch.setattr(slide_cache, "RASTER_CACHE_MAX_ENTRIES", 1)
    p0 = tmp_path / "real0.bmp"
    p0.write_bytes(_bmp_bytes(_corner_image(32, 24, block=8)))
    e0 = slide_cache.get_slide("real0.bmp", p0)
    with slide_cache.borrow_pair(e0) as pair:
        osr = pair["osr"]
    assert osr._closed is False  # 已归还池内，仍存活复用

    p1 = tmp_path / "real1.bmp"
    p1.write_bytes(_bmp_bytes(_corner_image(32, 24, block=8)))
    slide_cache.get_slide("real1.bmp", p1)  # 超预算 → real0 被 LRU 淘汰
    assert "real0.bmp" not in slide_cache._slide_cache
    assert osr._closed is True, "淘汰必须释放解码缓冲"


def test_share_server_has_no_own_ext_vocabulary():
    """词表收敛：分享端不拥有独立扩展名词表（闲置 SUPPORTED_EXTS 已删除），
    格式能力以 slide_format_registry / slide_io 为唯一来源。"""
    assert not hasattr(share_srv, "SUPPORTED_EXTS")
