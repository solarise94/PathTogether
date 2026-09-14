# -*- coding: utf-8 -*-
"""KFBF（荧光）→ 多通道 OME-TIFF converter 测试。

核心合同（docs/kfb-ingestion-converter-review.md §8）：
  - 完整 cell 的通道 JPEG 压缩字节与源**逐字节一致**（禁止二次有损）；
  - 稀疏缺失 cell → 黑填充（warning ``sparse_fill_black``），荧光背景即黑；
  - 底行裁剪 tile → 黑底重编码（尽量复用源量化表）；
  - 输出为 tifffile 可识别的 CYX 多通道 SubIFD 金字塔 OME-TIFF，
    ``slide_io.open_slide`` 打开后通道名/颜色/mpp/objective 齐全，
    ``read_region_channels`` 像素与源图案一致（JPEG 有损容差内）；
  - 失败清理半成品，稳定错误码；no-clobber。

fixture 为合成数据（无患者数据）。

运行：cd 项目根 && python3 -m pytest tests/test_kfbf_converter.py -q
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import slide_io  # noqa: E402
from kfb import KfbError, build_synthetic_kfbf, convert_kfbf  # noqa: E402
from kfb.fixture_fl import _channel_pattern  # noqa: E402


@pytest.fixture()
def synth_kfbf(tmp_path):
    return build_synthetic_kfbf(tmp_path / "synth.kfbf")


def test_convert_synthetic_structure_and_manifest(synth_kfbf, tmp_path):
    dst = tmp_path / "synth.ome.tif"
    man = convert_kfbf(synth_kfbf, dst, min_free_bytes=0)
    assert dst.is_file()
    assert (tmp_path / "synth.ome.tif.manifest.json").is_file()
    assert man["source"]["format"] == "kfbf_kfbio_jpeg"
    assert man["source"]["fluorescence"] is True
    assert man["canonical"]["format"] == "ome-tiff"
    assert man["dimensions"] == {"width": 600, "height": 400}
    assert [lv["level"] for lv in man["levels"]] == [0, 1, 2]
    assert [(c["name"], c["color"]) for c in man["channels"]] == [
        ("DAPI", "#0000E5"), ("520", "#00FF00")]
    assert "sparse_fill_black" in man["warnings"]
    # 逐 tile 统计：L0 每通道 2 透传 + 3 重编码 + 1 黑填充
    l0 = [r for r in man["tiles"] if r["level"] == 0]
    assert all(r["tiles_raw_copied"] == 2 and r["tiles_reencoded"] == 3
               and r["cells_filled_black"] == 1 for r in l0)
    # associated 三图落盘
    assoc = tmp_path / "synth.ome.tif.associated"
    assert {p.name for p in assoc.iterdir()} == \
        {"overview.jpg", "label.jpg", "thumbnail.jpg"}
    assert sorted(a["name"] for a in man["associated"]) == \
        ["label", "overview", "thumbnail"]


def test_convert_synthetic_viewer_integration(synth_kfbf, tmp_path):
    dst = tmp_path / "synth.ome.tif"
    convert_kfbf(synth_kfbf, dst, min_free_bytes=0)
    slide = slide_io.open_slide(str(dst))
    try:
        assert type(slide).__name__ == "TiffFileSlide"
        assert slide.axes == "CYX"
        assert slide.channel_count == 2
        assert slide.level_count == 3
        assert slide.level_dimensions == ((600, 400), (300, 200), (150, 100))
        names = [c["name"] for c in slide.ome_channels]
        assert names == ["DAPI", "520"]
        assert abs(float(slide.properties["openslide.mpp-x"])
                   - 0.2506266) < 1e-6
        assert float(slide.properties["openslide.objective-power"]) == 40.0

        # 通道像素与合成图案一致（JPEG 有损容差）
        planes, geo = slide.read_region_channels((0, 0), 0, (600, 400), [0, 1])
        assert planes.shape == (2, 400, 600)
        exp = _channel_pattern(256, 256, 0, 0, 0, 0)
        assert np.abs(planes[0][0:256, 0:256] - exp).max() <= 24
        exp1 = _channel_pattern(256, 256, 1, 0, 0, 0)
        assert np.abs(planes[1][0:256, 0:256] - exp1).max() <= 24
        # 稀疏缺失 cell(0,1) → 纯黑
        assert planes[0][0:256, 256:512].max() == 0
        assert planes[1][0:256, 256:512].max() == 0
        # 底行裁剪 tile 重编码：内容区与图案一致、padding 区近黑
        # （JPEG 块效应在内容边界有少量振铃，远离边界处必须严格为 0）
        exp_b = _channel_pattern(100, 256, 0, 0, 1, 0)
        assert np.abs(planes[0][256:356, 0:256] - exp_b).max() <= 24
        assert planes[0][356:400, 0:256].max() <= 16
        assert planes[0][384:400, 0:256].max() == 0
        # 深层 level 可读
        planes_l2, _ = slide.read_region_channels((0, 0), 2, (150, 100), [0])
        assert planes_l2.shape == (1, 100, 150)
        # legacy read_region 不抛
        img = slide.read_region((0, 0), 0, (64, 64))
        assert img.size == (64, 64)
    finally:
        slide.close()


def test_passthrough_bytes_identical(synth_kfbf, tmp_path):
    """完整 cell 的通道 JPEG 与源逐字节一致（核心不可降级合同）。"""
    dst = tmp_path / "synth.ome.tif"
    convert_kfbf(synth_kfbf, dst, min_free_bytes=0)

    from kfb import parse_kfbf
    with parse_kfbf(synth_kfbf) as doc:
        full = next(t for t in doc.tiles_by_level[0]
                    if (t.row, t.col) == (0, 0))
        src_payloads = [doc.channel_payload(full, c) for c in range(2)]

    import tifffile
    with open(dst, "rb") as raw, tifffile.TiffFile(str(dst)) as tf:
        pages = list(tf.pages)
        assert len(pages) == 2
        for c in range(2):
            offsets = pages[c].tags["TileOffsets"].value
            counts = pages[c].tags["TileByteCounts"].value
            raw.seek(offsets[0])
            assert raw.read(counts[0]) == src_payloads[c]


def test_no_clobber_existing_output(synth_kfbf, tmp_path):
    dst = tmp_path / "synth.ome.tif"
    dst.write_bytes(b"FOREIGN")
    with pytest.raises(KfbError) as ei:
        convert_kfbf(synth_kfbf, dst, min_free_bytes=0)
    assert ei.value.code == "conversion_validation_failed"
    assert dst.read_bytes() == b"FOREIGN"
    assert not (tmp_path / "synth.ome.tif.part").exists()


def test_reject_non_grayscale_channel(tmp_path, monkeypatch):
    """通道 JPEG 非灰度（RGB）→ conversion_validation_failed。"""
    src = build_synthetic_kfbf(tmp_path / "rgb.kfbf")
    # 把第一个 tile 的 ch0 payload 换成 RGB JPEG（同步修正 side/len 过于
    # 繁琐——直接在 converter 的 scan 路径上注入更简单：monkeypatch
    # channel_payload 返回 RGB JPEG）
    import kfb.converter_fl as cfl
    from kfb.vendor_kfbf import KfbfDocument
    rgb = Image.new("RGB", (256, 256), (200, 30, 40))
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    rgb_bytes = buf.getvalue()
    orig = KfbfDocument.channel_payload
    state = {"done": False}

    def fake(self, tile, channel):
        if tile.level == 0 and channel == 0 and not state["done"]:
            state["done"] = True
            return rgb_bytes
        return orig(self, tile, channel)

    monkeypatch.setattr(KfbfDocument, "channel_payload", fake)
    with pytest.raises(KfbError) as ei:
        cfl.convert_kfbf(src, tmp_path / "rgb.ome.tif", min_free_bytes=0)
    assert ei.value.code == "conversion_validation_failed"


def test_cli_success_and_failure(tmp_path):
    src = build_synthetic_kfbf(tmp_path / "cli.kfbf")
    dst = tmp_path / "cli.ome.tif"
    script = Path(__file__).resolve().parent.parent / "scripts" \
        / "convert_kfbf.py"
    r = subprocess.run([sys.executable, str(script), str(src), str(dst)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout)
    assert summary["dimensions"] == {"width": 600, "height": 400}
    assert summary["channels"] == ["DAPI", "520"]
    assert dst.is_file()

    # 垃圾字节 → 退出码 1 + 稳定错误码
    bad = tmp_path / "bad.kfbf"
    bad.write_bytes(b"not-a-kfbf")
    r2 = subprocess.run([sys.executable, str(script), str(bad),
                         str(tmp_path / "bad.ome.tif")],
                        capture_output=True, text=True)
    assert r2.returncode == 1
    assert r2.stderr.strip() in (
        "unsupported_kfb_variant", "invalid_kfb_header")
