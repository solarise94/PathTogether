# -*- coding: utf-8 -*-
"""raster_slide 普通图片（BMP/JPEG）读取器测试（Wave 1）。

覆盖（docs/raster-image-compatibility-agent-plan.md §6 与读取器相关行）：
  - 基础格式：RGB/调色板/灰度 BMP（真实 256 色调色板，可独立解码）、
    RGB/灰度/CMYK JPEG、大小写 .BMP/.JPG/.JPEG 别名；
  - 方向：EXIF Orientation 1–8（非对称四角图案断言像素位置与宽高交换/镜像）；
  - 校验：空文件、垃圾字节、截断 BMP/JPEG、PNG/TIFF 伪装后缀、
    超像素上限（monkeypatch 小值）、DecompressionBomb 映射、多帧拒绝；
  - 图像接口：read_region 四角/边界/完全越界透明、thumbnail 保比例、
    DeepZoomGenerator 直连（真实 openslide 环境才有意义，最小环境跳过）；
  - 生命周期：close 幂等、构造失败不泄漏（smoke）、__del__ 兜底；
  - open_slide 分发：.bmp 返回 RasterSlide 且不落入 openslide 尝试、
    .part + format_hint、大小写不敏感、失败稳定码。

fixture 全部为 PIL 合成字节（无患者数据；真实请求样本本机不可达，
真实样本兼容性**待验收**，不以合成数据冒充）。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_raster_slide.py -q
"""
import gc
import io
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
import pytest  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

import slide_io  # noqa: E402
import raster_slide  # noqa: E402


# --------------------------------------------------------------------------- #
# 合成 fixture 工具（无外部样本）
# --------------------------------------------------------------------------- #
#: 四角基准色（EXIF 断言用，JPEG 有损 → 容差比较）
_R, _G, _B, _Y = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
_TOL = 10


def _near(px, expect):
    """JPEG 有损压缩容差比较。"""
    return all(abs(a - e) <= _TOL for a, e in zip(px[:3], expect))


def _gradient_rgb(w, h):
    """确定性 RGB 渐变图（fixture 自身定义，断言按同公式重算）。"""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 4) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
    return im


def _bmp_bytes(im):
    b = io.BytesIO()
    im.save(b, format="BMP")
    return b.getvalue()


def _jpeg_bytes(im, **kw):
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=95, **kw)
    return b.getvalue()


def _gradient_bmp(w=64, h=48):
    return _bmp_bytes(_gradient_rgb(w, h))


def _corner_image(w=24, h=24, block=8):
    """非对称四角图案：左上红 / 右上绿 / 左下蓝 / 右下黄。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for i in range(block):
        for j in range(block):
            im.putpixel((i, j), _R)
            im.putpixel((w - 1 - i, j), _G)
            im.putpixel((i, h - 1 - j), _B)
            im.putpixel((w - 1 - i, h - 1 - j), _Y)
    return im


def _palette_bmp(w=32, h=32):
    """真实 256 色调色板 BMP（非灰度调色板，读回仍为 P 模式、可独立解码）。

    任务书警示：历史上调色板用例失败过——fixture 必须构造完整 256 项目录
    调色板，且索引图案覆盖全部 256 个索引。
    """
    im = Image.new("P", (w, h))
    pal = []
    for i in range(256):
        pal += [(i * 7) % 256, (i * 13) % 256, (i * 29) % 256]
    im.putpalette(pal)
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x + y * w) % 256
    return _bmp_bytes(im), pal


def _gray_bmp(w=32, h=24):
    im = Image.new("L", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 3 + y * 7) % 256
    return _bmp_bytes(im)


def _alpha_bmp(w=4, h=2):
    """手工构造 32 位 BGRA BMP（BITMAPV4HEADER + alpha 掩码）：Pillow 不支持
    直接保存 RGBA BMP，只能按 BMP V4 规范手写字节（左半不透明红、右半全透明）。"""
    px = {}
    for y in range(h):
        for x in range(w):
            px[(x, y)] = (255, 0, 0, 255) if x < w // 2 else (0, 0, 255, 0)
    rows = []
    for y in range(h - 1, -1, -1):  # BMP 自底向上
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[(x, y)]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    pixdata = b"".join(rows)
    v4 = struct.pack(
        "<IiiHHIIiiIIIIII4s9iIII",
        108, w, h, 1, 32, 3, w * h * 4, 0, 0, 0, 0,
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,
        b"Win ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert len(v4) == 108
    hdr = struct.pack("<2sIHHI", b"BM",
                      14 + len(v4) + len(pixdata), 0, 0, 14 + len(v4))
    return hdr + v4 + pixdata


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _rgb_at(slide, x, y):
    """read_region 单像素 RGB（越界安全：单像素区域读取）。"""
    return slide.read_region((x, y), 0, (1, 1)).getpixel((0, 0))[:3]


# --------------------------------------------------------------------------- #
# 1. 基础格式与 duck-type 接口
# --------------------------------------------------------------------------- #
def test_rgb_bmp_openslide_ducktype(tmp_path):
    p = _write(tmp_path, "photo.bmp", _gradient_bmp())
    s = raster_slide.RasterSlide(p)
    try:
        assert s.is_raster_image is True
        assert s.properties["openslide.vendor"] == "raster-image"
        # 无任何 mpp/objective 键：普通图片缺物理标尺（§4.4）
        assert "openslide.mpp-x" not in s.properties
        assert "openslide.mpp-y" not in s.properties
        assert "openslide.objective-power" not in s.properties
        assert s.dimensions == (64, 48)
        assert s.level_count == 1
        assert s.level_dimensions == ((64, 48),)
        assert s.level_downsamples == (1.0,)
        assert s.get_best_level_for_downsample(100) == 0
        assert s.get_best_level_for_downsample(1) == 0
        assert s.associated_images == {}
    finally:
        s.close()


def test_rgb_bmp_read_region_pixels_exact(tmp_path):
    """无 lossless BMP：read_region 像素与 fixture 公式逐像素一致。"""
    w, h = 64, 48
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp(w, h)))
    try:
        img = s.read_region((0, 0), 0, (w, h))
        assert img.mode == "RGBA" and img.size == (w, h)
        arr = img.load()
        for y in range(0, h, 7):
            for x in range(0, w, 5):
                expect = ((x * 4) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
                assert arr[x, y][:3] == expect
                assert arr[x, y][3] == 255
    finally:
        s.close()


def test_read_region_four_corners(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp()))
    try:
        assert _rgb_at(s, 0, 0) == (0, 0, 0)
        assert _rgb_at(s, 63, 0) == (252, 0, 126)
        assert _rgb_at(s, 0, 47) == (0, 235, 94)
        assert _rgb_at(s, 63, 47) == (252, 235, 220)
    finally:
        s.close()


def test_read_region_partial_out_of_bounds_padding(tmp_path):
    """右/下越界：有效区域内容 + 越界透明 padding。"""
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp()))
    try:
        img = s.read_region((60, 0), 0, (8, 4))
        assert img.size == (8, 4)
        px = img.load()
        assert px[0, 0][:3] == (240, 0, 120)  # x=60（(60*4)%256,(60*2)%256）
        assert px[3, 0][3] == 255            # x=63 仍在图内
        assert px[4, 0] == (0, 0, 0, 0)      # x=64 越界 → 全透明
        # 左/上负坐标：内容出现在偏移 (4,4) 之后，其余透明
        img2 = s.read_region((-4, -4), 0, (8, 8))
        px2 = img2.load()
        assert px2[0, 0] == (0, 0, 0, 0)
        assert px2[4, 4][:3] == (0, 0, 0)    # (0,0) 原点
        assert px2[7, 7][:3] == (12, 15, 12)  # (3,3)
    finally:
        s.close()


def test_read_region_fully_out_of_bounds_transparent(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp()))
    try:
        img = s.read_region((500, 500), 0, (8, 8))
        assert img.mode == "RGBA"
        assert img.getextrema()[3] == (0, 0)  # alpha 全 0
    finally:
        s.close()


def test_get_thumbnail_keeps_aspect_ratio(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp(64, 48)))
    try:
        t = s.get_thumbnail((32, 32))
        assert t.mode == "RGBA"
        assert t.size == (32, 24)  # contain：保比例（64:48 → 32:24）
        t2 = s.get_thumbnail((16, 12))
        assert t2.size == (16, 12)
    finally:
        s.close()


def test_palette_bmp_full_256_palette(tmp_path):
    """真实 256 色调色板 BMP：全部索引独立可解码，转显示 RGB 正确。"""
    data, pal = _palette_bmp()
    src = Image.open(io.BytesIO(data))
    assert src.format == "BMP" and src.mode == "P"  # fixture 自检：仍是 P
    assert len(src.getpalette()) == 768
    s = raster_slide.RasterSlide(_write(tmp_path, "pal.bmp", data))
    try:
        img = s.read_region((0, 0), 0, (32, 32))
        px = img.load()
        for y in range(0, 32, 3):
            for x in range(0, 32, 3):
                idx = (x + y * 32) % 256
                expect = tuple(pal[idx * 3:idx * 3 + 3])
                assert px[x, y][:3] == expect, (x, y)
    finally:
        s.close()


def test_grayscale_bmp_converted_to_rgb(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "gray.bmp", _gray_bmp()))
    try:
        assert s.dimensions == (32, 24)
        img = s.read_region((0, 0), 0, (32, 24))
        px = img.load()
        for y in range(0, 24, 5):
            for x in range(0, 32, 5):
                v = (x * 3 + y * 7) % 256
                assert px[x, y][:3] == (v, v, v)
    finally:
        s.close()


def test_alpha_bmp_white_background_composite(tmp_path):
    """带 alpha 的 BMP：透明处按白底合成（瓦片与缩略图同一缓冲，结果一致）。"""
    s = raster_slide.RasterSlide(_write(tmp_path, "alpha.bmp", _alpha_bmp()))
    try:
        assert s.dimensions == (4, 2)
        img = s.read_region((0, 0), 0, (4, 2))
        px = img.load()
        assert px[0, 0][:3] == (255, 0, 0) and px[0, 0][3] == 255  # 不透明红
        assert px[3, 0][:3] == (255, 255, 255) and px[3, 0][3] == 255  # 白底
        t = s.get_thumbnail((8, 4))
        assert t.size == (8, 4)
        tpx = t.load()
        # 缩略图与瓦片白底一致
        assert tpx[1, 1][:3] == (255, 0, 0)
        assert tpx[6, 1][:3] == (255, 255, 255) and tpx[6, 1][3] == 255
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["RGB", "L", "CMYK"])
def test_jpeg_variants(mode, tmp_path):
    w, h = 40, 30
    src = Image.new(mode, (w, h))
    px = src.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = tuple((c + x * 2 + y) % 256
                             for c in range(len(src.getbands())))
    expected = src.convert("RGB")
    data = _jpeg_bytes(src)
    s = raster_slide.RasterSlide(_write(tmp_path, "img.jpg", data))
    try:
        assert s.dimensions == (w, h)
        img = s.read_region((0, 0), 0, (w, h)).convert("RGB")
        exp = expected.load()
        got = img.load()
        for y in range(0, h, 4):
            for x in range(0, w, 4):
                assert _near(got[x, y], exp[x, y]), (x, y)
    finally:
        s.close()


@pytest.mark.parametrize("name,kind", [
    ("p.JPG", "jpeg"), ("p.JPEG", "jpeg"), ("p.jpeg", "jpeg"), ("p.jpg", "jpeg"),
    ("photo.BMP", "bmp"), ("photo.Bmp", "bmp"), ("photo.bmp", "bmp"),
])
def test_case_and_alias_ext_names(tmp_path, name, kind):
    """大小写不敏感 + .jpg/.jpeg 互认（字节与后缀匹配）。"""
    data = (_jpeg_bytes(Image.new("RGB", (8, 6), (10, 20, 30)))
            if kind == "jpeg" else _gradient_bmp(8, 6))
    p = _write(tmp_path, name, data)
    s = slide_io.open_slide(p)
    try:
        assert isinstance(s, raster_slide.RasterSlide)
        assert s.dimensions == (8, 6)
    finally:
        s.close()


def test_icc_profile_kept_in_properties_not_applied(tmp_path):
    """ICC profile 字节保留于 properties：不默默应用、不丢弃。"""
    data = _jpeg_bytes(Image.new("RGB", (8, 6), (1, 2, 3)),
                       icc_profile=b"fake-icc-payload")
    p = _write(tmp_path, "icc.jpg", data)
    s = raster_slide.RasterSlide(p)
    try:
        assert s.properties.get("icc_profile") == b"fake-icc-payload"
    finally:
        s.close()
    p2 = _write(tmp_path, "plain.bmp", _gradient_bmp(8, 6))
    s2 = raster_slide.RasterSlide(p2)
    try:
        assert "icc_profile" not in s2.properties
    finally:
        s2.close()


# --------------------------------------------------------------------------- #
# 2. EXIF 方向 1–8（非对称图案：像素位置 + 宽高交换/镜像）
# --------------------------------------------------------------------------- #
#: 校正后 (TL, TR, BL, BR) 四角期望色（EXIF 规范语义，硬编码防循环验证）：
#:   1 原样；2 水平镜像；3 旋转 180；4 垂直镜像；
#:   5 转置（主对角线）；6 顺时针 90；7 逆时针 90 + 水平镜像；8 逆时针 90。
_EXIF_EXPECT = {
    1: (_R, _G, _B, _Y), 2: (_G, _R, _Y, _B), 3: (_Y, _B, _G, _R),
    4: (_B, _Y, _R, _G), 5: (_R, _B, _G, _Y), 6: (_B, _R, _Y, _G),
    7: (_Y, _G, _B, _R), 8: (_G, _Y, _R, _B),
}


def _jpeg_with_orientation(im, orientation):
    exif = Image.Exif()
    exif[274] = orientation  # 0x0112
    return _jpeg_bytes(im, exif=exif)


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_orientation_1_to_8_pixels(tmp_path, orientation):
    """四角像素位置按 EXIF 校正（JPEG 24×24 四角块图案）。"""
    p = _write(tmp_path, "o%d.jpg" % orientation,
               _jpeg_with_orientation(_corner_image(), orientation))
    s = raster_slide.RasterSlide(p)
    try:
        tl, tr, bl, br = _EXIF_EXPECT[orientation]
        assert _near(_rgb_at(s, 2, 2), tl)
        assert _near(_rgb_at(s, 21, 2), tr)
        assert _near(_rgb_at(s, 2, 21), bl)
        assert _near(_rgb_at(s, 21, 21), br)
    finally:
        s.close()


@pytest.mark.parametrize("orientation,dims", [
    (1, (40, 24)), (2, (40, 24)), (3, (40, 24)), (4, (40, 24)),
    (5, (24, 40)), (6, (24, 40)), (7, (24, 40)), (8, (24, 40)),
])
def test_exif_orientation_swapped_dimensions(tmp_path, orientation, dims):
    """校正后宽高为准：5–8 宽高交换，read_region 坐标系一致。"""
    p = _write(tmp_path, "d%d.jpg" % orientation,
               _jpeg_with_orientation(_corner_image(40, 24), orientation))
    s = raster_slide.RasterSlide(p)
    try:
        assert s.dimensions == dims
        assert s.level_dimensions == (dims,)
        img = s.read_region((0, 0), 0, dims)
        assert img.size == dims
        # 完全越界语义在校正后坐标系下同样成立
        assert s.read_region((dims[0] + 5, dims[1] + 5), 0, (4, 4)) \
            .getextrema()[3] == (0, 0)
    finally:
        s.close()


def test_bmp_without_exif_skips_transpose(tmp_path):
    """BMP 无 EXIF：自然跳过校正，宽高不变。"""
    p = _write(tmp_path, "plain.bmp", _gradient_bmp(40, 24))
    s = raster_slide.RasterSlide(p)
    try:
        assert s.dimensions == (40, 24)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 3. 校验：空/垃圾/截断/伪装/超限/多帧 → 稳定错误码
# --------------------------------------------------------------------------- #
def test_empty_file_rejected(tmp_path):
    p = _write(tmp_path, "empty.bmp", b"")
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"


def test_garbage_bytes_rejected(tmp_path):
    for name in ("junk.bmp", "junk.jpg"):
        p = _write(tmp_path, name, b"\x00junk-not-an-image" * 8)
        with pytest.raises(slide_io.SlideValidationError) as ei:
            raster_slide.RasterSlide(p)
        assert ei.value.code == "invalid_slide"


def test_truncated_bmp_fails_at_decode(tmp_path):
    """截断 BMP：头部可读但全量解码失败 → slide_open_failed（不是仅查头）。"""
    data = _gradient_bmp(64, 48)
    p = _write(tmp_path, "cut.bmp", data[:int(len(data) * 0.7)])
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "slide_open_failed"
    assert ei.value.cause_type  # cause_type 只进日志


def test_truncated_jpeg_fails_at_decode(tmp_path):
    data = _jpeg_bytes(_gradient_rgb(64, 48))
    p = _write(tmp_path, "cut.jpg", data[:int(len(data) * 0.5)])
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "slide_open_failed"


def test_png_disguised_as_bmp_rejected(tmp_path):
    """伪装后缀：PNG 字节挂 .bmp 名 → invalid_slide（不因解码库支持而放行）。"""
    b = io.BytesIO()
    Image.new("RGB", (8, 6), (1, 2, 3)).save(b, format="PNG")
    p = _write(tmp_path, "fake.bmp", b.getvalue())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"


def test_tiff_disguised_as_jpg_rejected(tmp_path):
    b = io.BytesIO()
    Image.new("RGB", (8, 6), (1, 2, 3)).save(b, format="TIFF")
    p = _write(tmp_path, "fake.jpg", b.getvalue())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"
    # JPEG 字节用于 .jpg 与 .jpeg 互认（反向用例）
    pj = _write(tmp_path, "real.jpeg",
                _jpeg_bytes(Image.new("RGB", (8, 6))))
    s = raster_slide.RasterSlide(pj)
    try:
        assert s.dimensions == (8, 6)
    finally:
        s.close()


def test_pixel_limit_exceeded(tmp_path, monkeypatch):
    """像素超限：invalid_slide，消息含具体 W×H 与上限值（可测试）。"""
    monkeypatch.setattr(raster_slide, "RASTER_MAX_PIXELS", 100)
    p = _write(tmp_path, "big.bmp", _gradient_bmp(64, 48))  # 3072 像素
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"
    msg = str(ei.value)
    assert "上限" in msg and "100" in msg and "64" in msg and "48" in msg


def test_decompression_bomb_error_mapped(tmp_path, monkeypatch):
    """Pillow DecompressionBomb 异常：捕获并映射为稳定 invalid_slide。

    不修改 Pillow 全局开关；此处 monkeypatch 仅为本用例触发保护路径，
    用例结束自动还原。
    """
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    p = _write(tmp_path, "bomb.bmp", _gradient_bmp(64, 48))
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"


def test_multiframe_rejected(tmp_path, monkeypatch):
    """多帧图片拒绝：invalid_slide（帧数守卫，不悄悄取首帧）。"""
    im1 = Image.new("RGB", (16, 12), (255, 0, 0))
    im2 = Image.new("RGB", (16, 12), (0, 0, 255))
    b = io.BytesIO()
    im1.save(b, format="GIF", save_all=True, append_images=[im2])
    p = _write(tmp_path, "multi.bmp", b.getvalue())
    # 真实多帧 PIL 图像仅覆盖 format 标记以通过「后缀↔字节格式」门：
    # 本用例的被测对象是 n_frames 守卫，不是格式门（格式门已有专测）。
    orig_open = Image.open

    def fake_open(fp, *a, **k):
        im = orig_open(fp, *a, **k)
        im.format = "BMP"
        return im

    monkeypatch.setattr(Image, "open", fake_open)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        raster_slide.RasterSlide(p)
    assert ei.value.code == "invalid_slide"
    assert "多帧" in str(ei.value)


# --------------------------------------------------------------------------- #
# 4. 生命周期：close 幂等 / 构造失败不泄漏 / __del__ 兜底
# --------------------------------------------------------------------------- #
def test_close_idempotent(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp()))
    assert s.dimensions == (64, 48)
    s.close()
    s.close()  # 重复 close 不抛
    s.close()


def test_constructor_failure_no_leak_smoke(tmp_path):
    """构造失败不泄漏：批量失败构造 + gc 无异常；成功句柄批量关净。"""
    for i in range(30):
        p = _write(tmp_path, "bad%d.bmp" % i, b"\x00junk" * 8)
        with pytest.raises(slide_io.SlideValidationError):
            raster_slide.RasterSlide(p)
    for i in range(30):
        p = _write(tmp_path, "ok%d.bmp" % i, _gradient_bmp(8, 6))
        s = raster_slide.RasterSlide(p)
        assert s.dimensions == (8, 6)
        s.close()
    gc.collect()


def test_del_without_close_smoke(tmp_path):
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp()))
    del s  # __del__ 兜底，不抛
    gc.collect()


# --------------------------------------------------------------------------- #
# 5. DeepZoomGenerator 直连（真实 openslide 环境；stub 环境跳过）
# --------------------------------------------------------------------------- #
def _real_openslide():
    try:
        import openslide
        import openslide.deepzoom  # noqa: F401  # 子模块需显式导入
        real = hasattr(openslide, "OpenSlide") and openslide.OpenSlide is not object
        return openslide if real else None
    except ImportError:
        return None


def test_deepzoom_generator_integration(tmp_path):
    """DeepZoomGenerator 直接消费 RasterSlide（单层由 DZG 生成查看层级）。"""
    os_mod = _real_openslide()
    if os_mod is None:
        pytest.skip("最小环境 openslide 为 stub，DeepZoom 数学无意义")
    w, h = 64, 48
    s = raster_slide.RasterSlide(_write(tmp_path, "g.bmp", _gradient_bmp(w, h)))
    try:
        dz = os_mod.deepzoom.DeepZoomGenerator(s)
        assert dz.level_count > 1
        assert dz.level_dimensions[-1] == (w, h)
        tile = dz.get_tile(dz.level_count - 1, (0, 0))
        # OpenSlide 语义：完全不透明的源返回 RGB 瓦片（与 SVS 一致）
        assert tile.mode in ("RGB", "RGBA")
        # 最高层级左上角仍是 fixture 左上角颜色（渐变方向保持）
        near = tile.convert("RGB").getpixel((2, 2))
        assert _near(near, (0, 0, 0))
        # DZI 描述 XML 可生成（现有查看链路依赖）
        assert dz.get_dzi("jpeg")  # noqa: B015
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 6. open_slide 分发（slide_io 接线）
# --------------------------------------------------------------------------- #
def test_open_slide_dispatch_bmp_to_raster(tmp_path, monkeypatch):
    """.bmp 只走 raster 分支：即使 openslide 被换成必炸桩也不被触碰。"""
    import openslide

    def boom(*a, **k):
        raise AssertionError("raster 后缀不得落入 openslide.OpenSlide 尝试")

    monkeypatch.setattr(openslide, "OpenSlide", boom)
    p = _write(tmp_path, "photo.bmp", _gradient_bmp())
    s = slide_io.open_slide(p)
    try:
        assert isinstance(s, raster_slide.RasterSlide)
        assert s.is_raster_image is True
    finally:
        s.close()


def test_open_slide_part_with_hint_bmp(tmp_path):
    """.part + format_hint：字节从实际暂存路径读取，hint 参与判定。"""
    p = tmp_path / ".uploading-a1b2c3.part"
    p.write_bytes(_gradient_bmp())
    s = slide_io.open_slide(p, format_hint="photo.bmp")
    try:
        assert isinstance(s, raster_slide.RasterSlide)
        assert s.dimensions == (64, 48)
    finally:
        s.close()


def test_open_slide_part_without_hint_fails(tmp_path):
    p = tmp_path / ".uploading-a1b2c4.part"
    p.write_bytes(_gradient_bmp())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"


def test_open_slide_raster_stable_failures(tmp_path):
    """伪装 / 截断 / 缺文件：open_slide 稳定码（不透出裸异常/路径）。"""
    b = io.BytesIO()
    Image.new("RGB", (8, 6)).save(b, format="PNG")
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(_write(tmp_path, "fake.bmp", b.getvalue()))
    assert ei.value.code == "invalid_slide"
    data = _gradient_bmp(64, 48)
    with pytest.raises(slide_io.SlideValidationError) as ei2:
        slide_io.open_slide(
            _write(tmp_path, "cut.bmp", data[:int(len(data) * 0.7)]))
    assert ei2.value.code == "slide_open_failed"
    with pytest.raises(slide_io.SlideValidationError) as ei3:
        slide_io.open_slide(tmp_path / "missing.bmp")
    assert ei3.value.code == "slide_open_failed"


def test_raster_max_pixels_env_override(monkeypatch):
    """RASTER_MAX_PIXELS 常量支持 env 覆盖（模块加载期读取）。"""
    monkeypatch.setenv("RASTER_MAX_PIXELS", "12345")
    assert raster_slide._env_max_pixels() == 12345
    monkeypatch.setenv("RASTER_MAX_PIXELS", "not-a-number")
    assert raster_slide._env_max_pixels() == raster_slide._DEFAULT_MAX_PIXELS
    monkeypatch.setenv("RASTER_MAX_PIXELS", "")
    assert raster_slide._env_max_pixels() == raster_slide._DEFAULT_MAX_PIXELS
