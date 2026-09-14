# -*- coding: utf-8 -*-
"""KFBF 真实样本端到端校准（不得伪绿：无样本则 skip）。

PT_KFBF_SAMPLES_DIR 指向含 .kfbf 的目录（默认 ../切片文件夹/ref）。
对每个样本：parse → convert → 输出可被 slide_io 打开且通道/层级/mpp
完整 → 随机抽查 level0 若干满格 tile 的通道像素与源**逐字节一致**
（JPEG 透传合同）。

输出较大（与源同量级）：默认写 pytest tmp_path；本机 home 有配额时可用
PT_KFBF_OUT_DIR 指到大空间目录（如 /tmp）。

运行：cd 项目根 && python3 -m pytest tests/test_kfbf_real_samples.py -q
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import slide_io  # noqa: E402
from kfb import convert_kfbf, parse_kfbf  # noqa: E402

_SAMPLES_DIR = os.environ.get(
    "PT_KFBF_SAMPLES_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "..", "切片文件夹", "ref"))


def _real_samples():
    if not os.path.isdir(_SAMPLES_DIR):
        return []
    return sorted(
        os.path.join(_SAMPLES_DIR, f)
        for f in os.listdir(_SAMPLES_DIR) if f.lower().endswith(".kfbf"))


SAMPLES = _real_samples()

pytestmark = pytest.mark.skipif(not SAMPLES, reason="无真实 KFBF 样本")


@pytest.mark.parametrize("path", SAMPLES,
                         ids=[os.path.basename(p)[:8] for p in SAMPLES])
def test_real_sample_end_to_end(path, tmp_path):
    with parse_kfbf(path) as doc:
        nch = doc.header.channel_count
        nlevels = len(doc.levels)
        w0, h0 = doc.header.width_px, doc.header.height_px
        # 抽查 level0 的 3 个满格 tile
        probes = [t for t in doc.tiles_by_level[0]
                  if t.jpeg_w == 256 and t.jpeg_h == 256]
        assert probes
        probes = probes[:: max(1, len(probes) // 3)][:3]
        src_pixels = {}
        for t in probes:
            for c in range(nch):
                im = Image.open(io.BytesIO(doc.channel_payload(t, c)))
                im.load()
                src_pixels[(t.x_px, t.y_px, c)] = np.asarray(im)

    dst_dir = os.environ.get("PT_KFBF_OUT_DIR") or str(tmp_path)
    dst = os.path.join(
        dst_dir,
        os.path.splitext(os.path.basename(path))[0] + ".ome.tif")
    man = convert_kfbf(path, dst, min_free_bytes=0)
    assert man["dimensions"] == {"width": w0, "height": h0}
    assert len(man["levels"]) == nlevels
    assert len(man["channels"]) == nch

    slide = slide_io.open_slide(str(dst))
    try:
        assert slide.channel_count == nch
        assert slide.level_count == nlevels
        assert slide.dimensions == (w0, h0)
        assert abs(float(slide.properties["openslide.mpp-x"])
                   - man["mpp_x"]) < 1e-6
        for (x, y, c), exp in src_pixels.items():
            planes, _ = slide.read_region_channels((x, y), 0, (256, 256), [c])
            assert np.array_equal(planes[0], exp), \
                "tile(%d,%d) 通道 %d 像素非逐字节一致" % (x, y, c)
    finally:
        slide.close()
