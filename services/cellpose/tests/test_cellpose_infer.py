# -*- coding: utf-8 -*-
"""cellpose_infer 纯逻辑测试（无 GPU/权重/torch）：换算、统计、16-bit PNG。"""
import base64
import hashlib
import io

import numpy as np
import pytest
from PIL import Image

import cellpose_infer


# ---------------------------------------------------------------- 直径换算

def test_diameter_px_basic():
    assert cellpose_infer.diameter_px(7.0, 0.25) == pytest.approx(28.0)
    assert cellpose_infer.diameter_px(7.0, 0.484) == pytest.approx(14.4628, rel=1e-3)


def test_diameter_px_mpp_null_returns_none():
    assert cellpose_infer.diameter_px(7.0, None) is None


@pytest.mark.parametrize("diameter_um,mpp", [
    (-1.0, 0.25), (0.0, 0.25), (7.0, -0.1), (7.0, 0.0),
])
def test_diameter_px_invalid_raises(diameter_um, mpp):
    with pytest.raises(ValueError):
        cellpose_infer.diameter_px(diameter_um, mpp)


# ---------------------------------------------------------------- 实例统计

def _two_blob_masks():
    """标签图：1 = 左上 3x4，2 = 右下 5x5（面积 20 > 12，排序应 2 在前）。"""
    masks = np.zeros((20, 30), dtype=np.int32)
    masks[2:5, 3:7] = 1
    masks[10:15, 20:25] = 2
    return masks


def test_instance_stats_geometry_and_order():
    instances, areas = cellpose_infer.instance_stats(_two_blob_masks())
    assert [i["area_px"] for i in instances] == [25, 12]  # 面积降序
    by_area = {i["area_px"]: i for i in instances}
    assert by_area[12]["bbox"] == [3, 2, 4, 3]     # [x, y, w, h]
    assert by_area[12]["centroid"] == [4.5, 3.0]   # x 均值, y 均值
    assert by_area[25]["bbox"] == [20, 10, 5, 5]
    assert areas.tolist() == [12.0, 25.0]          # 升序面积数组


def test_instance_stats_empty():
    instances, areas = cellpose_infer.instance_stats(np.zeros((8, 8), np.int32))
    assert instances == []
    assert areas.size == 0


def test_area_summary_quantiles():
    areas = np.arange(1.0, 101.0)  # 1..100
    s = cellpose_infer.area_summary(areas)
    assert s["min"] == 1.0 and s["max"] == 100.0
    assert s["median"] == 50.5
    # area_summary 保留 1 位小数（round-half-even：25.75→25.8, 75.25→75.2）
    assert s["p25"] == pytest.approx(25.8)
    assert s["p75"] == pytest.approx(75.2)


def test_area_summary_empty_all_none():
    assert cellpose_infer.area_summary(np.array([])) == {
        "min": None, "p25": None, "median": None, "p75": None, "max": None}


# ---------------------------------------------------------------- 16-bit PNG

def _decode_labels_png(png_bytes):
    """16-bit PNG 解码（IHDR 位深校验与 Pillow 版本无关）。

    16-bit 灰度 PNG 打开后的 mode 因 Pillow 版本而异（9.4 报 "I"，
    ≥10 报 "I;16"），故直接校验 IHDR bit depth == 16。
    """
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert png_bytes[12:16] == b"IHDR"
    assert png_bytes[24] == 16  # bit depth
    image = Image.open(io.BytesIO(png_bytes))
    return np.asarray(image).astype(np.int64)


def test_encode_labels_png_roundtrip():
    masks = _two_blob_masks()
    png, truncated = cellpose_infer.encode_labels_png(masks)
    assert truncated is False
    out = _decode_labels_png(png)
    assert set(np.unique(out)) == {0, 1, 2}
    assert np.array_equal(out, masks)


def test_encode_labels_png_truncates_to_16bit():
    # 65540 个 1px 实例 → 超出 16-bit 上限，保留 65535 个并重编号
    masks = np.arange(1, 65541, dtype=np.int32).reshape(6554, 10)
    png, truncated = cellpose_infer.encode_labels_png(masks)
    assert truncated is True
    out = _decode_labels_png(png)
    labels = np.unique(out)
    assert labels.max() <= cellpose_infer.LABEL_MAP_MAX
    assert (labels == np.arange(0, 65536)).all()  # 0 背景 + 连续 1..65535
    assert int((out > 0).sum()) == 65535          # 只丢 5 个实例


# ---------------------------------------------------------------- SHA-256

def test_file_sha256_matches_hashlib(tmp_path):
    data = b"cellpose weights payload" * 4096  # 跨多个 chunk
    path = tmp_path / "w.bin"
    path.write_bytes(data)
    assert cellpose_infer.file_sha256(str(path)) == \
        hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 模型加载校验

def test_load_model_rejects_wrong_sha256(tmp_path):
    """expected_sha256 不符 → 拒绝加载（在 import torch 之前即失败）。"""
    path = tmp_path / "fake.bin"
    path.write_bytes(b"not real weights")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        cellpose_infer.load_model(str(path), "0" * 64)


def test_default_constants_match_contract():
    assert cellpose_infer.DEFAULT_DIAMETER_UM == 7.0
    assert cellpose_infer.DEFAULT_FLOW_THRESHOLD == 0.4
    assert cellpose_infer.DEFAULT_CELLPROB_THRESHOLD == 0.0
    assert cellpose_infer.MAX_IMAGE_EDGE == 2048
    assert set(cellpose_infer.PRESET_WEIGHT_NAMES) == {"cpsam", "cpsam_v2"}
