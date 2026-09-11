# -*- coding: utf-8 -*-
"""KFB 混合重封装 converter 测试（KFB Phase A）。

核心合同（docs/kfb-ingestion-converter-review.md §0.5/§10.3）：
  - 完整 256×256 JPEG tile 的压缩字节与源**逐字节一致**（禁止二次有损）；
  - 经典多 IFD 金字塔 BigTIFF；``slide_io.open_slide`` 可打开，level 数
    与尺寸符合策略；每层四角 read_region 不抛；
  - MPP 经 TIFF resolution tags 暴露（真 openslide 环境）；
  - objective 写进 ImageDescription JSON 与 manifest；
  - 失败清理 ``.part``，稳定错误码。

fixture 为合成数据（无患者数据）；openslide 在本仓可能被 _bootstrap stub
成 object（tests/_bootstrap.py），此时 ``slide_io.open_slide`` 走
TiffFileSlide fallback——两条路径都必须通过（tifffile 会把带
NewSubfileType=reduced 的经典金字塔 IFD 组成一个多 level series）。

运行：cd 项目根 && python3 -m pytest tests/test_kfb_converter.py -q
"""
import io
import json
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import pytest  # noqa: E402

numpy = pytest.importorskip("numpy")
pytest.importorskip("PIL")
tifffile = pytest.importorskip("tifffile")

from PIL import Image  # noqa: E402

import kfb.converter  # noqa: E402
import slide_io  # noqa: E402
from kfb import KfbError, build_synthetic_kfb, convert_kfb, parse_kfb  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 合成 fixture 580×300 → 层级策略（含首个 1×1 网格层）
EXPECTED_LEVELS = [(580, 300), (290, 150), (145, 75)]


def _real_openslide() -> bool:
    """_bootstrap 只在缺库时 stub；真 openslide 存在时 OpenSlide 不是 object。"""
    try:
        import openslide

        return openslide.OpenSlide is not object
    except ImportError:
        return False


@pytest.fixture()
def synth_pair(tmp_path):
    src = build_synthetic_kfb(tmp_path / "synth.kfb")
    dst = tmp_path / "out.tif"
    return str(src), str(dst)


@pytest.fixture()
def converted(synth_pair):
    src, dst = synth_pair
    manifest = convert_kfb(src, dst)
    return src, dst, manifest


# --------------------------------------------------------------------------- #
# 1. tile 压缩段字节一致性（原样复制，禁止二次有损）
# --------------------------------------------------------------------------- #
def test_full_tile_bytes_identical(converted):
    src, dst, _manifest = converted
    doc = parse_kfb(src)
    try:
        raw = open(dst, "rb").read()
        with tifffile.TiffFile(dst) as tf:
            assert tf.is_bigtiff
            assert len(tf.pages) == len(EXPECTED_LEVELS)
            for page, lv in zip(tf.pages, doc.levels):
                tiles = doc.tiles_by_level[lv.level]
                grid = {(t.row, t.col): t for t in tiles}
                offsets = page.tags["TileOffsets"].value
                counts = page.tags["TileByteCounts"].value
                expected_n = lv.tiles_across * lv.tiles_down
                assert len(offsets) == expected_n
                idx = 0
                for row in range(lv.tiles_down):
                    for col in range(lv.tiles_across):
                        tile = grid[(row, col)]
                        seg = raw[offsets[idx]: offsets[idx] + counts[idx]]
                        if tile.is_full_tile:
                            assert seg == doc.tile_payload(tile), (
                                "层 %d tile(%d,%d) 压缩段与源不一致（二次有损）"
                                % (lv.level, row, col))
                        idx += 1
    finally:
        doc.close()


def test_edge_tiles_are_256_and_near_source(converted):
    """边缘 tile：白底补边后 256×256；原图区域 MAE 有界；padding 近白。"""
    src, dst, _manifest = converted
    doc = parse_kfb(src)
    try:
        raw = open(dst, "rb").read()
        with tifffile.TiffFile(dst) as tf:
            page = tf.pages[0]
            offsets = page.tags["TileOffsets"].value
            counts = page.tags["TileByteCounts"].value
            checked = 0
            for t in doc.tiles_by_level[0]:
                if t.is_full_tile:
                    continue
                seg = raw[offsets[t.row * 3 + t.col]:
                          offsets[t.row * 3 + t.col] + counts[t.row * 3 + t.col]]
                out = numpy.asarray(
                    Image.open(io.BytesIO(seg)).convert("RGB")).astype(float)
                assert out.shape == (256, 256, 3)
                im = Image.open(io.BytesIO(doc.tile_payload(t)))
                im.load()
                s = numpy.asarray(im.convert("RGB")).astype(float)
                mae = numpy.abs(out[:t.jpeg_h, :t.jpeg_w] - s).mean()
                assert mae < 2.0, "边缘 tile MAE=%.3f" % mae
                pad = numpy.concatenate([
                    out[:, t.jpeg_w:].ravel(), out[t.jpeg_h:].ravel()])
                assert pad.size == 0 or pad.mean() > 240.0
                checked += 1
            assert checked >= 4  # 580×300 → 4 个边缘 tile
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 2. slide_io.open_slide 读取（openslide 真/ stub 两路都必须可用）
# --------------------------------------------------------------------------- #
def test_open_slide_levels_and_corners(converted):
    _src, dst, _manifest = converted
    slide = slide_io.open_slide(dst)
    try:
        assert slide.level_count == len(EXPECTED_LEVELS)
        assert [tuple(d) for d in slide.level_dimensions] == EXPECTED_LEVELS
        downsamples = slide.level_downsamples
        assert downsamples[0] == 1.0
        for i in (1, 2):
            assert abs(downsamples[i] - 2.0 ** i) < 1e-6
        for lv in range(slide.level_count):
            w, h = slide.level_dimensions[lv]
            for loc in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                im = slide.read_region(loc, lv, (16, 16))
                assert im.size == (16, 16)
        # 中心区域可读且非空
        im = slide.read_region((100, 80), 0, (32, 32)).convert("RGB")
        assert im.size == (32, 32)
        assert numpy.asarray(im).std() > 0
        # 缩略图链路（viewer 用）
        th = slide.get_thumbnail((64, 64))
        assert th.size[0] > 0 and th.size[1] > 0
    finally:
        slide.close()


def test_mpp_via_tiff_resolution_tags(converted):
    """MPP 写 resolution tags：真 openslide 恢复为 header.mpp_x（±1e-4）。

    TiffFileSlide（stub 环境）不解析通用 TIFF 的 mpp，直接读标签校验。
    """
    _src, dst, _manifest = converted
    if _real_openslide():
        import openslide

        s = openslide.OpenSlide(dst)
        try:
            mpp = float(s.properties["openslide.mpp-x"])
            assert abs(mpp - 0.4841049) < 1e-4
        finally:
            s.close()
    with tifffile.TiffFile(dst) as tf:
        page = tf.pages[0]
        res = page.tags["XResolution"].value
        unit = page.tags["ResolutionUnit"].value
        # RATIONAL → px/cm；µm/px = 10^4 / px_per_cm
        px_per_cm = float(res[0]) / float(res[1])
        assert unit == 3  # centimeter
        assert abs(10000.0 / px_per_cm - 0.4841049) < 1e-4


def test_objective_in_imagedescription_json(converted):
    _src, dst, _manifest = converted
    with tifffile.TiffFile(dst) as tf:
        desc = tf.pages[0].tags["ImageDescription"].value
    payload = desc.rstrip("\x00")
    data = json.loads(payload)  # 结构化，不靠自由文本
    assert data["source_format"] == "kfb_bf_v1"
    assert data["objective"] == 20.0
    assert abs(data["mpp_x"] - 0.4841049) < 1e-9
    assert data["scanner_id"] == "PTSYNTH0001"


# --------------------------------------------------------------------------- #
# 3. manifest / associated / 原子性
# --------------------------------------------------------------------------- #
def test_manifest_and_associated_outputs(converted):
    src, dst, manifest = converted
    manifest_path = dst + ".manifest.json"
    assert os.path.exists(manifest_path)
    on_disk = json.loads(open(manifest_path, encoding="utf-8").read())
    assert on_disk == manifest
    # source 记录
    import hashlib

    h = hashlib.sha256(open(src, "rb").read()).hexdigest()
    assert manifest["source"]["sha256"] == h
    assert manifest["source"]["size"] == os.path.getsize(src)
    assert manifest["source"]["format"] == "kfb_bf_v1"
    # canonical 记录
    assert manifest["canonical"]["codec"] == "jpeg"
    assert manifest["canonical"]["tile_width"] == 256
    assert manifest["canonical"]["size"] == os.path.getsize(dst)
    # 层级与 mpp/objective
    assert [(l["width"], l["height"]) for l in manifest["levels"]] == \
        EXPECTED_LEVELS
    assert sum(l["tiles_raw_copied"] for l in manifest["levels"]) == 2
    assert sum(l["tiles_reencoded"] for l in manifest["levels"]) == 7
    assert abs(manifest["mpp_x"] - 0.4841049) < 1e-9
    assert manifest["objective"] == 20.0
    assert manifest["warnings"] == []
    # associated：OUT.associated/<name>.jpg，均为合法 JPEG
    assoc_dir = dst + ".associated"
    names = sorted(a["name"] for a in manifest["associated"])
    assert names == ["label", "overview", "thumbnail"]
    for item in manifest["associated"]:
        p = os.path.join(assoc_dir, item["file"])
        assert os.path.exists(p)
        im = Image.open(p)
        im.load()
        assert im.size == (item["width"], item["height"])


def test_atomic_promote_no_part_left(converted):
    _src, dst, _manifest = converted
    assert os.path.exists(dst)
    assert not os.path.exists(dst + ".part")
    assert not os.path.exists(dst + ".manifest.json.part")


def test_refuse_existing_output(synth_pair):
    src, dst = synth_pair
    convert_kfb(src, dst)
    with pytest.raises(KfbError) as ei:
        convert_kfb(src, dst)
    assert ei.value.code == "conversion_validation_failed"


# --------------------------------------------------------------------------- #
# 4. 负向：稳定错误码 + 半成品清理
# --------------------------------------------------------------------------- #
def test_missing_tile_fails_validation(tmp_path):
    src = build_synthetic_kfb(tmp_path / "gap.kfb", omit_tile=(0, 1, 1))
    dst = str(tmp_path / "gap.tif")
    with pytest.raises(KfbError) as ei:
        convert_kfb(str(src), dst)
    assert ei.value.code == "conversion_validation_failed"
    assert not os.path.exists(dst)
    assert not os.path.exists(dst + ".part")


def test_non_brightfield_rejected(tmp_path):
    src = build_synthetic_kfb(tmp_path / "fl.kfb", brightfield=False)
    with pytest.raises(KfbError) as ei:
        convert_kfb(str(src), str(tmp_path / "fl.tif"))
    assert ei.value.code == "unsupported_kfb_variant"


def test_timeout_guard(synth_pair):
    src, dst = synth_pair
    with pytest.raises(KfbError) as ei:
        convert_kfb(src, dst, timeout_seconds=-1.0)
    assert ei.value.code == "conversion_timeout"
    assert not os.path.exists(dst + ".part")


def test_output_too_large_guard(synth_pair):
    src, dst = synth_pair
    with pytest.raises(KfbError) as ei:
        convert_kfb(src, dst, max_output_bytes=100)
    assert ei.value.code == "conversion_output_too_large"
    assert not os.path.exists(dst + ".part")


def test_disk_low_guard(synth_pair):
    src, dst = synth_pair
    with pytest.raises(KfbError) as ei:
        convert_kfb(src, dst, min_free_bytes=10 ** 15)
    assert ei.value.code == "conversion_disk_low"


def test_qtables_fallback_warning(synth_pair, monkeypatch):
    """量化表拿不到时 quality=95 兜底并记 warning（输出仍须可用）。"""
    src, dst = synth_pair
    monkeypatch.setattr(kfb.converter, "_extract_qtables", lambda im: None)
    manifest = convert_kfb(src, dst)
    assert "edge_reencode_fallback_q95" in manifest["warnings"]
    slide = slide_io.open_slide(dst)
    try:
        assert slide.level_count == len(EXPECTED_LEVELS)
        slide.read_region((0, 0), 0, (16, 16))
    finally:
        slide.close()


# --------------------------------------------------------------------------- #
# 5. CLI
# --------------------------------------------------------------------------- #
def test_cli_roundtrip(synth_pair):
    src, dst = synth_pair
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "scripts", "convert_kfb.py"),
         src, dst],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["levels"] == 3
    assert os.path.exists(dst)
    assert os.path.exists(dst + ".manifest.json")


def test_cli_stable_error_code(tmp_path):
    src = tmp_path / "bad.kfb"
    src.write_bytes(b"\x00" * 128)
    dst = str(tmp_path / "bad.tif")
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "scripts", "convert_kfb.py"),
         str(src), dst],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 1
    assert proc.stderr.strip() == "unsupported_kfb_variant"
    assert not os.path.exists(dst)


# --------------------------------------------------------------------------- #
# 6. 可选：真实样本转换（PT_KFB_SAMPLE_PATH）
# --------------------------------------------------------------------------- #
def test_real_sample_convert_if_provided():
    path = os.environ.get("PT_KFB_SAMPLE_PATH", "")
    if not path or not os.path.isfile(path):
        pytest.skip("PT_KFB_SAMPLE_PATH 未设置或不可读")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="kfb-real-") as tmp:
        dst = os.path.join(tmp, "real.tif")
        manifest = convert_kfb(path, dst)
        slide = slide_io.open_slide(dst)
        try:
            assert slide.level_count == len(manifest["levels"])
            w, h = slide.level_dimensions[0]
            assert (w, h) == (manifest["dimensions"]["width"],
                              manifest["dimensions"]["height"])
            for lv in range(slide.level_count):
                lw, lh = slide.level_dimensions[lv]
                for loc in ((0, 0), (lw - 1, 0), (0, lh - 1), (lw - 1, lh - 1)):
                    slide.read_region(loc, lv, (8, 8))
        finally:
            slide.close()
