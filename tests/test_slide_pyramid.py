# -*- coding: utf-8 -*-
"""服务端金字塔构建（摄取期/回填）测试。

全部图像由 tifffile 在 TEST_RUN_DIR 下合成（确定性噪声/底纹，无患者数据、
无真实切片）。覆盖：阈值/env、RGB 无损（LZW）与 JPEG 源、uint16 多通道
OME、非 OME resolution 标签（平台视图权威，不按标签编造 mpp）、流式块
读取上界、硬上限跳过、回填 dry-run 与 --apply 的 revision/记账/删除任务
拒绝。

运行（遵循工作区 TEST_RUN_DIR 约定）::

    python -m pytest tests/test_slide_pyramid.py -q
"""
import importlib.util
import io
import time
from pathlib import Path

import numpy as np
import pytest

import slide_cache
import slide_io
import slide_pyramid as sp
import slide_storage
import slide_store

pytest.importorskip("tifffile")
pytest.importorskip("zarr")
pytest.importorskip("imagecodecs")


# --------------------------------------------------------------------------- #
# 合成源（无真实数据）
# --------------------------------------------------------------------------- #
def _noise_rgb(h, w, seed=7):
    rng = np.random.default_rng(int(seed))
    return rng.integers(0, 256, (int(h), int(w), 3), dtype=np.uint8)


def _gradient_cyx(c, h, w):
    yy, xx = np.indices((int(h), int(w)), dtype=np.uint32)
    return np.stack([
        ((xx * 3 + yy * 5 + ci * 1000) % 65535).astype(np.uint16)
        for ci in range(int(c))])


def _write_rgb_lzw_ome(path, h=2000, w=1500, mpp=0.25, names=("R", "G", "B")):
    import tifffile

    data = _noise_rgb(h, w)
    meta = {"axes": "YXS", "PhysicalSizeX": mpp, "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": mpp, "PhysicalSizeYUnit": "µm"}
    if names:
        meta["Channel"] = {"Name": list(names)}
    tifffile.imwrite(str(path), data, photometric="rgb", compression="lzw",
                     ome=True, metadata=meta)
    return data


def _jpeg_tiles(rgb, quality=90, tile=512):
    from PIL import Image

    h, w = rgb.shape[:2]
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            block = np.full((tile, tile, 3), 255, np.uint8)
            part = rgb[y:min(h, y + tile), x:min(w, x + tile)]
            block[:part.shape[0], :part.shape[1]] = part
            buf = io.BytesIO()
            Image.fromarray(block, mode="RGB").save(
                buf, "JPEG", quality=int(quality), subsampling=0)
            yield buf.getvalue()


def _write_rgb_jpeg_ome(path, h=1400, w=1800, mpp=0.5):
    import tifffile

    rgb = _noise_rgb(h, w, seed=11)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(_jpeg_tiles(rgb), shape=rgb.shape, dtype="uint8",
                 tile=(512, 512), compression="jpeg",
                 photometric="ycbcr", subsampling=(1, 1),
                 metadata={"axes": "YXS",
                           "PhysicalSizeX": mpp, "PhysicalSizeXUnit": "µm",
                           "PhysicalSizeY": mpp, "PhysicalSizeYUnit": "µm"})
    return rgb


def _write_cyx_ome(path, c=3, h=1200, w=1600, mpp=0.5):
    import tifffile

    data = _gradient_cyx(c, h, w)
    tifffile.imwrite(
        str(path), data, photometric="minisblack", compression="deflate",
        ome=True, metadata={
            "axes": "CYX",
            "PhysicalSizeX": mpp, "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": mpp, "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["DAPI", "FITC", "Cy5"],
                        "Color": ["#0000FF", "#00FF00", "#FF0000"]},
        })
    return data


def _write_tiled_with_resolution(path, h=1200, w=1800, mpp=1.0):
    import tifffile

    # 10000 px/cm => 1 µm/px；非 OME（走 Resolution 标签路径）。
    data = _noise_rgb(h, w, seed=23)
    tifffile.imwrite(str(path), data, photometric="rgb", tile=(64, 64),
                     resolution=(10000, 10000), resolutionunit="CENTIMETER")
    return data


def _write_plain_rgb_lzw_with_resolution(path, h=1200, w=1600,
                                         resolution=(40000, 40000),
                                         unit="CENTIMETER"):
    import tifffile

    # 生产上报的形态：单层条带 RGB TIFF + 分辨率标签（非 OME）。
    data = _noise_rgb(h, w, seed=29)
    tifffile.imwrite(str(path), data, photometric="rgb", compression="lzw",
                     resolution=resolution, resolutionunit=unit)
    return data


def _gradient_rgb(h, w, seed=31):
    """低频渐变 + 轻噪声：每个窗口都有方差，且比纯噪声更易压缩。"""
    yy, xx = np.indices((int(h), int(w)), dtype=np.uint32)
    rng = np.random.default_rng(int(seed))
    noise = rng.integers(0, 16, (int(h), int(w), 3), dtype=np.uint8)
    base = np.stack([
        (xx * 5 + yy * 3) % 251,
        (xx * 2 + yy * 7 + 60) % 253,
        ((xx + yy) * 4 + 30) % 249,
    ], axis=2).astype(np.uint16)
    return np.clip(base + noise, 0, 255).astype(np.uint8)


def _write_gradient_rgb_ome(path, h=3100, w=4300, mpp=0.25):
    import tifffile

    data = _gradient_rgb(h, w)
    meta = {"axes": "YXS", "PhysicalSizeX": mpp, "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": mpp, "PhysicalSizeYUnit": "µm"}
    tifffile.imwrite(str(path), data, photometric="rgb", compression="lzw",
                     ome=True, metadata=meta)
    return data


def _write_zero_sublevel_pyramid(path, h=1200, w=1600):
    """写一个 level 0 正常、唯一 SubIFD 全零的「旧构建器形态」金字塔。"""
    import tifffile

    rgb = _gradient_rgb(h, w)
    sub = np.zeros(((int(h) + 1) // 2, (int(w) + 1) // 2, 3), np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(rgb, photometric="rgb", tile=(512, 512),
                 compression="deflate", subifds=1,
                 metadata={"axes": "YXS"})
        tw.write(sub, photometric="rgb", tile=(512, 512),
                 compression="deflate", subfiletype=1)
    return rgb


def _write_broken_pyramid_file(path, h=1200, w=1600):
    return _write_zero_sublevel_pyramid(path, h=h, w=w)


def _write_healthy_pyramid_file(path, h=1200, w=1600):
    src = Path(path).with_name(Path(path).name + ".src.tiff")
    try:
        _write_gradient_rgb_ome(src, h=h, w=w)
        sp.build_pyramid(src, path)
    finally:
        if src.exists():
            src.unlink()


def _box_reduce_rgb(rgb, out_h, out_w):
    """2× 盒式平均缩到 out_h×out_w；ceil 尺寸多出的边按 edge 复制。"""
    h, w = rgb.shape[:2]
    pad_h, pad_w = int(out_h) * 2 - h, int(out_w) * 2 - w
    arr = rgb
    if pad_h > 0 or pad_w > 0:
        arr = np.pad(arr, ((0, max(0, pad_h)), (0, max(0, pad_w)), (0, 0)),
                     mode="edge")
    blocks = arr[:int(out_h) * 2, :int(out_w) * 2].reshape(
        int(out_h), 2, int(out_w), 2, 3)
    return ((blocks.sum(axis=(1, 3), dtype=np.uint32) + 2) // 4).astype(
        np.uint8)


def _write_own_dims_pyramid(path, level_dims, *, zero_sublevels=False,
                            resample=None, shift_sublevels=0):
    """写「第三方工具形态」金字塔：层尺寸完全由调用方给出。

    缩减层内容由上一级做 2× 下采样得到（可写 floor 或 ceil 序列）：
    默认用与构建器同口径的对齐盒式平均；``resample="pil"`` 用 Pillow
    的 BOX 重采样（比例映射，模拟其它转换器的滤波器/取整差异）。
    ``zero_sublevels`` 时只写全零缩减层，模拟旧构建器写坏的文件；
    ``shift_sublevels`` 让缩减层内容来自水平错位的源（非全零但明显不符）。
    """
    import tifffile

    base_w, base_h = (int(v) for v in level_dims[0])
    levels = [_gradient_rgb(base_h, base_w)]
    prev = levels[0]
    if shift_sublevels:
        prev = np.roll(prev, int(shift_sublevels), axis=1)
    for (lw, lh) in level_dims[1:]:
        if zero_sublevels:
            prev = np.zeros((int(lh), int(lw), 3), np.uint8)
        elif resample == "pil":
            from PIL import Image

            prev = np.asarray(Image.fromarray(
                prev, "RGB").resize((int(lw), int(lh)), Image.BOX))
        else:
            prev = _box_reduce_rgb(prev, int(lh), int(lw))
        levels.append(prev)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(levels[0], photometric="rgb", tile=(512, 512),
                 compression="deflate", subifds=len(levels) - 1,
                 metadata={"axes": "YXS"})
        for level in levels[1:]:
            tw.write(level, photometric="rgb", tile=(512, 512),
                     compression="deflate", subfiletype=1)
    return levels


def _platform_mpp(path):
    """经平台读取路径（slide_io）取 (mpp_x, mpp_y)；未报告为 None。"""
    slide = slide_io.open_slide(str(path))
    try:
        props = slide.properties or {}
        out = []
        for axis in ("x", "y"):
            raw = props.get("openslide.mpp-%s" % axis)
            out.append(float(raw) if raw not in (None, "") else None)
        return tuple(out)
    finally:
        slide.close()


def _assert_same_mpp(src_mpp, dst_mpp):
    for s_val, d_val in zip(src_mpp, dst_mpp):
        if s_val is None:
            assert d_val is None
        else:
            assert d_val == pytest.approx(s_val, rel=1e-6)


def _write_unsupported_tzyx(path, t=2, c=2, z=1, h=300, w=400):
    import tifffile

    data = np.zeros((t, c, z, h, w), np.uint16)
    for ti in range(t):
        data[ti] = _gradient_cyx(c, h, w)[:, None, :, :]
    tifffile.imwrite(str(path), data, photometric="minisblack", ome=True,
                     metadata={"axes": "TCZYX"})


@pytest.fixture(autouse=True)
def _threshold(monkeypatch):
    """单测用较小合成图：阈值降到 1024（默认 4096 的行为另行断言）。"""
    monkeypatch.setenv("INGEST_PYRAMID_MIN_LONG_EDGE", "1024")
    monkeypatch.delenv("INGEST_PYRAMID_ENABLED", raising=False)
    yield


# --------------------------------------------------------------------------- #
# 阈值/env/needs_pyramid
# --------------------------------------------------------------------------- #
def test_needs_pyramid_threshold_and_env(monkeypatch, tmp_path):
    assert sp.needs_pyramid({"level_count": 1, "dimensions": (5000, 4000)})
    assert not sp.needs_pyramid({"level_count": 1, "dimensions": (1024, 1024)})
    assert not sp.needs_pyramid({"level_count": 2, "dimensions": (5000, 4000)})

    monkeypatch.setenv("INGEST_PYRAMID_MIN_LONG_EDGE", "6000")
    assert not sp.needs_pyramid(
        {"level_count": 1, "dimensions": (5000, 4000)})
    monkeypatch.setenv("INGEST_PYRAMID_ENABLED", "0")
    assert not sp.needs_pyramid(
        {"level_count": 1, "dimensions": (9000, 9000)})
    # 探测失败/畸形输入按「不需要」处理，绝不因诊断失败误伤上传。
    assert not sp.needs_pyramid({"level_count": 1})
    assert not sp.needs_pyramid("not-a-real-file.tiff")


def test_needs_pyramid_accepts_path(tmp_path):
    src = tmp_path / "small.ome.tiff"
    _write_rgb_lzw_ome(src, h=300, w=400)
    assert not sp.needs_pyramid(str(src))
    src2 = tmp_path / "big.ome.tiff"
    _write_rgb_lzw_ome(src2, h=1200, w=1500)
    assert sp.needs_pyramid(str(src2))


def test_buildable_source_suffix_guard():
    assert sp.is_buildable_source("data.tiff")
    assert sp.is_buildable_source("x.OME.TIFF")
    assert not sp.is_buildable_source("data.svs")
    assert not sp.is_buildable_source("data.ndpi")


def test_level_dimensions_halve_until_target():
    dims = sp.level_dimensions_for(12161, 9621)
    assert dims[0] == (12161, 9621)
    assert max(dims[-1]) <= sp.PYRAMID_TARGET_LONG_EDGE
    assert max(dims[-2]) > sp.PYRAMID_TARGET_LONG_EDGE
    assert dims[1] == (6081, 4811)
    assert len(dims) == 5


# --------------------------------------------------------------------------- #
# 构建：RGB 无损源 / JPEG 源 / uint16 多通道 / 非 OME resolution
# --------------------------------------------------------------------------- #
def test_build_rgb_lzw_lossless_levels_mpp_and_pixels(tmp_path):
    src = tmp_path / "rgb_lzw.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_rgb_lzw_ome(src, mpp=0.25)

    result = sp.build_pyramid(src, dst)
    assert result.levels > 1
    assert result.level0_compression == "deflate"
    assert result.sublevel_compression == "jpeg"  # RGB 8-bit 缩减层
    assert result.bytes_after > 0

    slide = slide_io.open_slide(str(dst))
    try:
        assert slide.level_count == result.levels
        assert tuple(slide.dimensions) == (1500, 2000)
        assert float(slide.properties["openslide.mpp-x"]) == pytest.approx(0.25)
        assert float(slide.properties["openslide.mpp-y"]) == pytest.approx(0.25)
        # 多级可真正读取（不是只有 IFD 标签）
        for level in range(1, slide.level_count):
            img = slide.read_region((0, 0), level, (32, 32))
            assert img.size == (32, 32)
    finally:
        slide.close()

    meta = sp.validate_pyramid(src, dst, lossless=True)
    assert meta["levels"] == result.levels
    assert meta["mpp_x"] == pytest.approx(0.25)
    assert meta["mean_diff"] == 0.0


def test_build_rgb_jpeg_reencodes_level0_and_subleves(tmp_path):
    src = tmp_path / "rgb_jpeg.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_rgb_jpeg_ome(src)

    result = sp.build_pyramid(src, dst)
    assert result.source_lossless is False
    assert result.level0_compression == "jpeg"
    assert result.sublevel_compression == "jpeg"
    meta = sp.validate_pyramid(src, dst, lossless=False)
    assert meta["mean_diff"] < 8.0  # q95 重编码，JPEG 解码误差量级

    import tifffile

    with tifffile.TiffFile(str(dst)) as tf:
        page = tf.series[0].pages[0]
        assert int(page.compression) == 7
        assert page.is_tiled
        assert int(page.tilewidth) == 512


def test_build_multichannel_uint16_preserves_channels_mpp_and_pixels(tmp_path):
    src = tmp_path / "cyx.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    data = _write_cyx_ome(src)

    result = sp.build_pyramid(src, dst)
    assert result.levels > 1
    assert result.level0_compression == "deflate"
    assert result.sublevel_compression == "deflate"  # 非 RGB 8-bit

    slide = slide_io.open_slide(str(dst))
    try:
        assert slide.channel_count == 3
        assert slide.channel_axis == "C"
        assert float(slide.properties["openslide.mpp-x"]) == pytest.approx(0.5)
        manifest = slide.channel_manifest
        assert [c.get("name") for c in manifest["channels"]] == \
            ["DAPI", "FITC", "Cy5"]
    finally:
        slide.close()

    meta = sp.validate_pyramid(src, dst, lossless=True)
    assert meta["mean_diff"] == 0.0
    # 输出 level-0 原始平面与源完全相同（无损）。
    import tifffile

    with tifffile.TiffFile(str(dst)) as tf:
        out = tf.series[0].asarray()
    assert out.shape == data.shape
    assert np.array_equal(out, data)


def test_build_single_channel_uint16_ome(tmp_path):
    src = tmp_path / "gray.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    data = _write_cyx_ome(src, c=1, h=1200, w=1600)
    result = sp.build_pyramid(src, dst)
    assert result.levels > 1
    slide = slide_io.open_slide(str(dst))
    try:
        assert slide.axes == "YX"
        assert slide.channel_count == 1
    finally:
        slide.close()
    sp.validate_pyramid(src, dst, lossless=True)
    import tifffile

    with tifffile.TiffFile(str(dst)) as tf:
        assert np.array_equal(tf.series[0].asarray(), data[0])


def test_build_nonome_resolution_tags_keep_mpp(tmp_path):
    src = tmp_path / "gray.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_tiled_with_resolution(src, mpp=1.0)
    src_slide = slide_io.open_slide(str(src))
    try:
        src_mpp = float(src_slide.properties["openslide.mpp-x"])
    finally:
        src_slide.close()

    sp.build_pyramid(src, dst)
    meta = sp.validate_pyramid(src, dst, lossless=True)
    assert meta["mpp_x"] == pytest.approx(src_mpp, rel=1e-6)


def test_build_plain_tiff_cm_resolution_keeps_platform_mpp(tmp_path):
    """生产回归：条带 LZW 明文 TIFF + 分辨率标签。

    平台视图（slide_io）对该源不报告 mpp（OpenSlide 打不开、TiffFileSlide
    无 OME PhysicalSize）。输出必须与平台视图一致（同样不报告 mpp），
    构建与校验都不得再因 mpp 存在性不一致而失败。
    """
    src = tmp_path / "plain_cm.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_plain_rgb_lzw_with_resolution(src)

    src_mpp = _platform_mpp(src)
    result = sp.build_pyramid(src, dst)  # 旧实现写 0.25 µm/px 导致校验失败
    assert result.levels > 1
    meta = sp.validate_pyramid(src, dst, lossless=True)
    dst_mpp = _platform_mpp(dst)
    _assert_same_mpp(src_mpp, dst_mpp)
    if dst_mpp[0] is None:
        assert meta["mpp_x"] is None
    else:
        assert meta["mpp_x"] == pytest.approx(dst_mpp[0], rel=1e-6)


def test_build_plain_tiff_72dpi_default_does_not_invent_mpp(tmp_path):
    """72 dpi 只是导出器默认值：不得被换算成 ≈352.8 µm/px 写进输出。"""
    src = tmp_path / "plain_72dpi.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_plain_rgb_lzw_with_resolution(src, resolution=(72, 72),
                                         unit="INCH")

    src_mpp = _platform_mpp(src)
    sp.build_pyramid(src, dst)
    meta = sp.validate_pyramid(src, dst, lossless=True)
    dst_mpp = _platform_mpp(dst)
    _assert_same_mpp(src_mpp, dst_mpp)
    # 25400/72 ≈ 352.8；输出绝不能凭空带上这个值。
    assert dst_mpp[0] is None or abs(dst_mpp[0] - 352.8) > 1.0
    assert meta["mpp_x"] is None or abs(meta["mpp_x"] - 352.8) > 1.0


def test_build_ome_physical_size_survives_platform_view(tmp_path):
    """OME PhysicalSize 经平台视图读出后必须原样留在输出（标定不变）。"""
    src = tmp_path / "mpp.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_rgb_lzw_ome(src, mpp=0.25)

    src_mpp = _platform_mpp(src)
    assert src_mpp == pytest.approx((0.25, 0.25))
    sp.build_pyramid(src, dst)
    meta = sp.validate_pyramid(src, dst, lossless=True)
    _assert_same_mpp(src_mpp, _platform_mpp(dst))
    assert meta["mpp_x"] == pytest.approx(0.25)


def test_build_populates_every_sublevel_with_real_content(tmp_path):
    """回归：level 2..N 必须等 level 1 填满后再生成，不能写出全零层。"""
    src = tmp_path / "gradient.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_gradient_rgb_ome(src)

    result = sp.build_pyramid(src, dst)
    assert result.levels >= 4  # ≥3 个 SubIFD，足以覆盖旧 bug
    meta = sp.validate_pyramid(src, dst, lossless=True)
    assert meta["sublevel_windows"] >= 3 * (result.levels - 1)
    assert meta["sublevel_max_mean_diff"] is not None
    assert meta["sublevel_max_mean_diff"] <= sp.PYRAMID_SUBLEVEL_TOLERANCE

    slide = slide_io.open_slide(str(dst))
    try:
        assert slide.level_count == result.levels
        for level in range(1, slide.level_count):
            lw, lh = slide.level_dimensions[level]
            win = min(256, lw, lh)
            actual = sp._rgba_array(
                slide.read_region((0, 0), level, (win, win)))
            # 直接由 level 0 对应区域逐级 2× 盒式下采样（与构建器同滤波器）。
            expected = sp._rgba_array(slide.read_region(
                (0, 0), 0, (win * (1 << level), win * (1 << level))))
            for _ in range(level):
                expected = sp._downsample_rgba(expected)
            assert expected.shape == actual.shape
            assert float(actual[..., :3].std()) > 1.0, \
                "level %d 为常量/全零" % level
            diff = np.abs(expected[..., :3].astype(np.int16)
                          - actual[..., :3].astype(np.int16))
            assert float(diff.mean(axis=(0, 1)).max()) <= \
                sp.PYRAMID_SUBLEVEL_TOLERANCE, "level %d 内容不符" % level
    finally:
        slide.close()


def test_validate_rejects_zeroed_sublevels(tmp_path):
    """新校验必须拒绝「层数/尺寸正确但缩减层全零」的旧构建器输出。"""
    broken = tmp_path / "broken.ome.tiff"
    _write_zero_sublevel_pyramid(broken)

    with pytest.raises(sp.PyramidValidationError) as ei:
        sp.validate_pyramid(broken, broken, lossless=True)
    message = str(ei.value)
    assert "level 1" in message
    assert "全零" in message or "常量" in message


# --------------------------------------------------------------------------- #
# 流式/上限/跳过
# --------------------------------------------------------------------------- #
def test_build_streams_tile_blocks_with_bounded_size(tmp_path, monkeypatch):
    src = tmp_path / "rgb.ome.tiff"
    dst = tmp_path / "out.ome.tiff"
    _write_rgb_lzw_ome(src)

    calls = {"n": 0, "max_bytes": 0}
    real = sp._read_source_block

    def counted(array, layout, y0, y1, x0, x1, channel=None):
        block = real(array, layout, y0, y1, x0, x1, channel=channel)
        calls["n"] += 1
        calls["max_bytes"] = max(calls["max_bytes"], int(block.nbytes))
        return block

    monkeypatch.setattr(sp, "_read_source_block", counted)
    sp.build_pyramid(src, dst)

    # 2000×1500 / 512 => 4×3 = 12 个 tile（逐块读，绝不整幅载入）。
    assert calls["n"] == 12
    assert calls["max_bytes"] <= 512 * 512 * 3


def test_build_skips_below_threshold(tmp_path):
    src = tmp_path / "small.ome.tiff"
    _write_rgb_lzw_ome(src, h=300, w=400)
    with pytest.raises(sp.PyramidSkip) as ei:
        sp.build_pyramid(src, tmp_path / "out.ome.tiff")
    assert ei.value.reason == "below_threshold"


def test_build_skips_unsupported_axes(tmp_path):
    src = tmp_path / "tzyx.ome.tiff"
    _write_unsupported_tzyx(src)
    with pytest.raises(sp.PyramidSkip) as ei:
        sp.build_pyramid(src, tmp_path / "out.ome.tiff")
    assert ei.value.reason == "unsupported_axes"


def test_build_skips_estimated_output_cap(tmp_path, monkeypatch):
    src = tmp_path / "rgb.ome.tiff"
    _write_rgb_lzw_ome(src, h=1200, w=1500)
    monkeypatch.setattr(sp, "estimate_output_bytes",
                        lambda *_a, **_k: 10 ** 12)
    with pytest.raises(sp.PyramidSkip) as ei:
        sp.build_pyramid(src, tmp_path / "out.ome.tiff")
    assert ei.value.reason == "estimated_output_cap"


def test_build_skips_pixel_cap(tmp_path, monkeypatch):
    src = tmp_path / "rgb.ome.tiff"
    _write_rgb_lzw_ome(src, h=1200, w=1500)
    monkeypatch.setattr(sp, "PYRAMID_MAX_PIXELS", 10)
    with pytest.raises(sp.PyramidSkip) as ei:
        sp.build_pyramid(src, tmp_path / "out.ome.tiff")
    assert ei.value.reason == "pixel_count_cap"


def test_build_honours_deadline(tmp_path):
    src = tmp_path / "rgb.ome.tiff"
    _write_rgb_lzw_ome(src)
    with pytest.raises(sp.PyramidSkip) as ei:
        sp.build_pyramid(src, tmp_path / "out.ome.tiff",
                         deadline=time.time() - 1)
    assert ei.value.reason == "deadline"


# --------------------------------------------------------------------------- #
# 回填工具（dry-run / apply / 拒绝）
# --------------------------------------------------------------------------- #
def _load_backfill():
    path = (Path(__file__).resolve().parents[1] / "scripts"
            / "build_missing_pyramids.py")
    spec = importlib.util.spec_from_file_location("build_missing_pyramids",
                                                  str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_user(user_id):
    import psycopg

    import pg_store

    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at) VALUES (%s, %s, %s, 'user', false, "
                    "now()) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id, user_id))
    finally:
        conn.close()


def _mk_ready_slide(slide_id, tmp_path, writer, fmt="tiff", **kw):
    """在 UPLOAD_DIR/objects/<sid>/data.<ext> 写真实文件并置 ready+revision。"""
    _mk_user("u_pyr")
    desc = slide_store.allocate_slide(
        "u_pyr", "synthetic." + fmt, fmt, display_name="synthetic")
    path = slide_storage.resolve_descriptor_path(desc)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer(path, **kw)
    size = path.stat().st_size
    assert slide_store.mark_ready(desc.slide_id, accounted_bytes=size)
    slide_store.record_revision(desc.slide_id,
                                "sha256:%s" % ("0" * 16))
    return slide_store.resolve_slide_id(desc.slide_id), path


def test_backfill_dry_run_lists_only_qualifying(tmp_path):
    mod = _load_backfill()
    big, _ = _mk_ready_slide("sld_pyrbig", tmp_path, _write_rgb_lzw_ome,
                             h=1500, w=1800)
    small, _ = _mk_ready_slide("sld_pyrsmall", tmp_path, _write_rgb_lzw_ome,
                               h=300, w=400)
    items = list(mod._eligible_candidates())
    ids = {it["slide_id"] for it in items}
    assert big.slide_id in ids
    assert small.slide_id not in ids
    item = next(it for it in items if it["slide_id"] == big.slide_id)
    assert item["width"] == 1800 and item["height"] == 1500
    assert item["bytes"] > 0


def test_backfill_apply_bumps_revision_and_accounted(tmp_path):
    mod = _load_backfill()
    desc, path = _mk_ready_slide("sld_pyrapply", tmp_path,
                                 _write_rgb_lzw_ome, h=1500, w=1800)
    before_size = path.stat().st_size
    before_rev = desc.revision
    before_sig = slide_cache.signature_of(path)
    code, payload = mod.rebuild_one(desc.slide_id)
    assert code == 0, payload
    after = slide_store.resolve_slide_id(desc.slide_id)
    assert after.revision != before_rev
    # 文件签名（mtime:size）是 slide_cache generation 的失效依据。
    assert slide_cache.signature_of(path) != before_sig
    assert int(after.accounted_bytes) == int(payload["bytes_after"])
    assert after.accounted_bytes != before_size
    slide = slide_io.open_slide(str(path))
    try:
        assert slide.level_count > 1
    finally:
        slide.close()


def test_backfill_apply_refuses_delete_job(tmp_path):
    mod = _load_backfill()
    desc, _path = _mk_ready_slide("sld_pyrdelete", tmp_path,
                                  _write_rgb_lzw_ome, h=1500, w=1800)
    slide_store.enqueue_delete_job(desc.slide_id, requested_by="u_pyr")
    code, payload = mod.rebuild_one(desc.slide_id)
    assert code == 1
    assert payload["refused"].startswith("delete_job_exists")


def test_backfill_apply_refuses_not_eligible(tmp_path):
    mod = _load_backfill()
    desc, _path = _mk_ready_slide("sld_pyrsmall2", tmp_path,
                                  _write_rgb_lzw_ome, h=300, w=400)
    code, payload = mod.rebuild_one(desc.slide_id)
    assert code == 1
    assert payload["refused"] == "not_eligible"


# --------------------------------------------------------------------------- #
# 修复模式（--repair）：只读列出 / 拒绝健康文件 / 重建旧构建器坏金字塔
# --------------------------------------------------------------------------- #
def test_repair_dry_run_lists_only_broken_pyramids(tmp_path):
    mod = _load_backfill()
    broken, broken_path = _mk_ready_slide(
        "sld_pyrbroken", tmp_path, _write_broken_pyramid_file)
    healthy, healthy_path = _mk_ready_slide(
        "sld_pyrhealthy", tmp_path, _write_healthy_pyramid_file)
    single, single_path = _mk_ready_slide(
        "sld_pyrsingle", tmp_path, _write_gradient_rgb_ome)
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in (broken_path, healthy_path, single_path)}

    items = list(mod._repair_candidates())
    assert {it["slide_id"] for it in items} == {broken.slide_id}
    item = items[0]
    assert item["width"] == 1600 and item["height"] == 1200
    assert item["level_count"] == 2
    assert item["bytes"] == broken_path.stat().st_size
    # 只读：三个文件的内容/时间戳都不变。
    for path, sig in before.items():
        assert (path.stat().st_mtime_ns, path.stat().st_size) == sig
    # 健康金字塔与单层大图都不在候选里。
    assert healthy.slide_id not in {it["slide_id"] for it in items}
    assert single.slide_id not in {it["slide_id"] for it in items}


def test_repair_refuses_healthy_and_single_level(tmp_path):
    mod = _load_backfill()
    healthy, healthy_path = _mk_ready_slide(
        "sld_pyrhealthy2", tmp_path, _write_healthy_pyramid_file)
    single, _ = _mk_ready_slide("sld_pyrsingle2", tmp_path,
                                _write_gradient_rgb_ome)
    before = (healthy_path.stat().st_mtime_ns, healthy_path.stat().st_size)

    code, payload = mod.repair_one(healthy.slide_id)
    assert code == 1
    assert payload["refused"] == "not_broken"
    code, payload = mod.repair_one(single.slide_id)
    assert code == 1
    assert payload["refused"] == "not_broken"
    assert (healthy_path.stat().st_mtime_ns,
            healthy_path.stat().st_size) == before


def test_repair_rebuilds_broken_pyramid_and_accounts(tmp_path, monkeypatch):
    mod = _load_backfill()
    desc, path = _mk_ready_slide("sld_pyrrepair", tmp_path,
                                 _write_broken_pyramid_file)
    before_size = path.stat().st_size
    before_rev = desc.revision
    before_sig = slide_cache.signature_of(path)
    broken_slide = slide_io.open_slide(str(path))
    try:
        old_level0 = np.asarray(
            broken_slide.read_region((0, 0), 0, (512, 512)).convert("RGB"))
    finally:
        broken_slide.close()

    calls = []
    real = slide_store.record_pyramid_rebuild

    def spy(slide_id, **kwargs):
        calls.append((slide_id, dict(kwargs)))
        return real(slide_id, **kwargs)

    monkeypatch.setattr(slide_store, "record_pyramid_rebuild", spy)
    code, payload = mod.repair_one(desc.slide_id)
    assert code == 0, payload
    # 复用正常 --apply 的记账原语（expected_bytes = 修复前磁盘大小）。
    assert calls and calls[0][0] == desc.slide_id
    assert int(calls[0][1]["expected_bytes"]) == before_size

    after = slide_store.resolve_slide_id(desc.slide_id)
    assert after.revision != before_rev
    assert int(after.accounted_bytes) == int(payload["bytes_after"])
    assert slide_cache.signature_of(path) != before_sig

    meta = sp.validate_pyramid(path, path, lossless=True)
    assert meta["sublevel_windows"] >= 3
    assert meta["sublevel_max_mean_diff"] <= sp.PYRAMID_SUBLEVEL_TOLERANCE
    slide = slide_io.open_slide(str(path))
    try:
        assert slide.level_count > 1
        new_level0 = np.asarray(
            slide.read_region((0, 0), 0, (512, 512)).convert("RGB"))
        # level 0 只做无损提取/重写：修复不能改变原有像素。
        assert np.array_equal(new_level0, old_level0)
        for level in range(1, slide.level_count):
            lw, lh = slide.level_dimensions[level]
            img = np.asarray(slide.read_region(
                (0, 0), level, (min(64, lw), min(64, lh))).convert("RGB"))
            assert float(img.std()) > 1.0
    finally:
        slide.close()
    # 所有临时文件（level-0 提取/构建输出/备份）都已清理。
    parent = path.parent
    assert not list(parent.glob(".pyramid-*"))
    assert not list(parent.glob(".pyramid-scratch-*"))


#: 第三方工具形态的健康金字塔：层序列与本地构建器不同（少一层 / floor /
#: ceil 取整），但每层内容都是上一级的正确 2× 下采样。
_NON_BUILDER_HEALTHY_DIMS = {
    # 构建器会写 3 层 (2400,1200)→(1200,600)→(600,300)；这里少最后一层。
    "fewer_levels": (((2400, 1200), (1200, 600)), {}),
    # 构建器用 ceil：(2301,1151)→(1151,576)→(576,288)；这里 floor 到
    # (1150,575)，层数也少一层。
    "floor_rounding": (((2301, 1151), (1150, 575)), {}),
    # 构建器序列的第二层相同，但只写到 ceil 的 (1151,576) 就停止。
    "ceil_rounding": (((2301, 1151), (1151, 576)), {}),
    # 其它转换器的滤波器/比例映射（PIL BOX）与对齐盒式参考有差异，
    # 仍必须判为健康（修复容差要覆盖跨实现重采样）。
    "pil_resample": (((2301, 1151), (1150, 575), (575, 287)),
                     {"resample": "pil"}),
}


def _mk_own_dims_slide(slide_id, tmp_path, dims, *, zero=False, **writer_kw):
    return _mk_ready_slide(
        slide_id, tmp_path, _write_own_dims_pyramid,
        level_dims=tuple(dims), zero_sublevels=zero, **writer_kw)


def test_non_builder_dimension_sequence_is_healthy_for_repair(tmp_path):
    """层数/取整与构建器不同、内容健康的金字塔不算损坏（不列出/不改写）。"""
    mod = _load_backfill()
    paths = {}
    for name, (dims, writer_kw) in _NON_BUILDER_HEALTHY_DIMS.items():
        desc, path = _mk_own_dims_slide("sld_pyr_nb_" + name, tmp_path, dims,
                                        **writer_kw)
        paths[name] = (desc, path)
        # 内容校验（按文件自身尺寸）通过。
        meta = sp.validate_sublevel_content(path)
        assert meta["compared_levels"] == len(dims) - 1
        assert meta["sublevel_max_mean_diff"] <= \
            sp.PYRAMID_REPAIR_SUBLEVEL_TOLERANCE
        # 旧的严格校验确实会因结构差异拒绝它——这正是误报来源。
        with pytest.raises(sp.PyramidValidationError):
            sp.validate_pyramid(path, path)
        # 修复列表不含它；--repair --slide-id 以 not_broken 拒绝且不改文件。
        sig = (path.stat().st_mtime_ns, path.stat().st_size)
        code, payload = mod.repair_one(desc.slide_id)
        assert code == 1, payload
        assert payload["refused"] == "not_broken"
        assert (path.stat().st_mtime_ns, path.stat().st_size) == sig

    listed = {it["slide_id"] for it in mod._repair_candidates()}
    assert listed.isdisjoint({desc.slide_id for desc, _ in paths.values()})


def test_zeroed_sublevels_with_non_builder_dims_are_repairable(tmp_path):
    """层序列不同但缩减层全零的文件仍应被列出，并可修复。"""
    mod = _load_backfill()
    dims = _NON_BUILDER_HEALTHY_DIMS["floor_rounding"][0]
    desc, path = _mk_own_dims_slide("sld_pyr_nb_zero", tmp_path, dims,
                                    zero=True)
    with pytest.raises(sp.PyramidValidationError):
        sp.validate_sublevel_content(path)
    listed = {it["slide_id"]: it for it in mod._repair_candidates()}
    assert desc.slide_id in listed
    assert "常量/全零" in listed[desc.slide_id]["validation_error"]

    broken_slide = slide_io.open_slide(str(path))
    try:
        old_level0 = np.asarray(
            broken_slide.read_region((0, 0), 0, (512, 512)).convert("RGB"))
    finally:
        broken_slide.close()

    code, payload = mod.repair_one(desc.slide_id)
    assert code == 0, payload
    meta = sp.validate_pyramid(path, path, lossless=True)
    assert meta["sublevel_max_mean_diff"] <= sp.PYRAMID_SUBLEVEL_TOLERANCE
    slide = slide_io.open_slide(str(path))
    try:
        assert slide.level_count > 1
        new_level0 = np.asarray(
            slide.read_region((0, 0), 0, (512, 512)).convert("RGB"))
        # level 0 无损提取：修复不改变原有像素。
        assert np.array_equal(new_level0, old_level0)
        for level in range(1, slide.level_count):
            lw, lh = slide.level_dimensions[level]
            img = np.asarray(slide.read_region(
                (0, 0), level, (min(64, lw), min(64, lh))).convert("RGB"))
            assert float(img.std()) > 1.0
    finally:
        slide.close()


def test_zeroed_ceil_pyramid_with_fewer_levels_is_repairable(tmp_path):
    """ceil 取整且少一层、缩减层全零：内容失败仍能识别并修复。"""
    mod = _load_backfill()
    dims = _NON_BUILDER_HEALTHY_DIMS["ceil_rounding"][0]
    desc, path = _mk_own_dims_slide("sld_pyr_nb_zero_ceil", tmp_path, dims,
                                    zero=True)
    listed = {it["slide_id"] for it in mod._repair_candidates()}
    assert desc.slide_id in listed
    code, payload = mod.repair_one(desc.slide_id)
    assert code == 0, payload
    sp.validate_pyramid(path, path, lossless=True)


def test_repair_detects_nonzero_but_mismatched_sublevel(tmp_path):
    """缩减层非全零但与上一级内容明显不符：仍是内容失败（可修复）。"""
    mod = _load_backfill()
    dims = ((2301, 1151), (1150, 575))
    desc, _path = _mk_own_dims_slide("sld_pyr_nb_wrong", tmp_path, dims,
                                     shift_sublevels=767)
    with pytest.raises(sp.PyramidValidationError):
        sp.validate_sublevel_content(_path)
    listed = {it["slide_id"] for it in mod._repair_candidates()}
    assert desc.slide_id in listed
