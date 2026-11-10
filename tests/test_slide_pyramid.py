# -*- coding: utf-8 -*-
"""服务端金字塔构建（摄取期/回填）测试。

全部图像由 tifffile 在 TEST_RUN_DIR 下合成（确定性噪声/底纹，无患者数据、
无真实切片）。覆盖：阈值/env、RGB 无损（LZW）与 JPEG 源、uint16 多通道
OME、非 OME resolution 标签、流式块读取上界、硬上限跳过、回填 dry-run 与
--apply 的 revision/记账/删除任务拒绝。

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
