# -*- coding: utf-8 -*-
"""纯逻辑测试：提示词构造 / 预处理与掩码回映射 / PNG 编码（无 GPU、无权重）。"""
import base64
import io

import numpy as np
import pytest
from PIL import Image

import pathseg_infer


# ---------------------------------------------------------------- 提示词

def test_prompt_level_object_split():
    # 上游文件名语法：level+object → prompt
    prompt, level, target, region = pathseg_infer.build_prompt(
        "nuclei connective", "bile duct")
    assert prompt == "nuclei-level connective in bile duct pathology"
    assert (level, target, region) == ("nuclei", "connective", "bile duct")


def test_prompt_level_only_duplicates():
    # "nuclei" → "nuclei-level nuclei"（上游 CSV 存在该 prompt）
    prompt, level, target, _ = pathseg_infer.build_prompt("nuclei")
    assert prompt == "nuclei-level nuclei in multiple pathology"
    assert (level, target) == ("nuclei", "nuclei")


def test_prompt_vocabulary_lookup():
    # 词表收敛自上游 CSV：gland→tissue、neoplastic→nuclei、lymphocyte→cell
    assert pathseg_infer.build_prompt("gland", "colon")[0] == \
        "tissue-level gland in colon pathology"
    assert pathseg_infer.build_prompt("neoplastic cells", "breast")[0] == \
        "nuclei-level neoplastic cells in breast pathology"
    assert pathseg_infer.build_prompt("lymphocytes")[0] == \
        "cell-level lymphocytes in multiple pathology"


def test_prompt_unspecified_becomes_multiple():
    # 上游 format_string：unspecified → multiple
    assert "multiple pathology" in pathseg_infer.build_prompt("nuclei")[0]


def test_prompt_plus_and_case_normalization():
    prompt, _, _, _ = pathseg_infer.build_prompt("NUCLEI+Connective", "Bile+Duct")
    assert prompt == "nuclei-level connective in bile duct pathology"


def test_prompt_rejects_empty():
    with pytest.raises(ValueError):
        pathseg_infer.build_prompt("   ")
    with pytest.raises(ValueError):
        pathseg_infer.build_prompt(None)


# ---------------------------------------------------------------- 预处理

def test_preprocess_square_1024_identity():
    img = np.random.RandomState(0).randint(0, 256, (1024, 1024, 3), np.uint8)
    out, geom = pathseg_infer.preprocess_image(img)
    assert out.shape == (1024, 1024, 3)
    assert (geom.src_h, geom.src_w, geom.pad_y, geom.pad_x) == (1024, 1024, 0, 0)
    assert np.array_equal(out, img)  # 同尺寸跳过 resize，逐像素恒等


def test_preprocess_non_square_pads_and_maps_back():
    # 600×800（H×W）→ 宽>高：pad 行方向（y 两侧各 100）成 800×800 → resize 1024
    img = np.zeros((600, 800, 3), np.uint8)
    img[:, 400:] = 255  # 右半亮块：无 x-pad，resize 后约从 512 起
    out, geom = pathseg_infer.preprocess_image(img)
    assert out.shape == (1024, 1024, 3)
    assert (geom.src_h, geom.src_w) == (800, 800)
    assert (geom.pad_y, geom.pad_x) == (100, 0)
    # 中心行：亮区起点应落在约 512（400*1024/800）
    row = out[512, :, 0]
    bright = np.nonzero(row > 200)[0]
    assert bright.size > 0 and abs(int(bright[0]) - 512) <= 4


def test_mask_round_trip_non_square():
    import cv2  # noqa: F401 — 回映射依赖（延迟导入），确保本环境可用

    h, w = 600, 800
    img = np.zeros((h, w, 3), np.uint8)
    out, geom = pathseg_infer.preprocess_image(img)
    # 1024 坐标下的圆 → 回映射到原图坐标应仍是一个圆（面积近似）
    mask = np.zeros((1024, 1024), bool)
    yy, xx = np.ogrid[:1024, :1024]
    mask[(yy - 512) ** 2 + (xx - 512) ** 2 < 100 ** 2] = True
    back = pathseg_infer.mask_to_original_size(mask, geom)
    assert back.shape == (h, w)
    # 圆心在原图 (x=400, y=300)（1024 坐标 512 → pad 系 512*800/1024=400，
    # y 再减 pad_y=100 → 300；x 方向无 pad）
    ys, xs = np.nonzero(back)
    assert abs(xs.mean() - 400) < 3 and abs(ys.mean() - 300) < 3
    expected_area = np.pi * (100 * w / 1024.0) ** 2
    assert back.sum() == pytest.approx(expected_area, rel=0.05)


def test_mask_round_trip_odd_pad_quirk():
    # 奇数差：pad 后短 1px（上游原样），回映射仍精确裁回原尺寸
    img = np.zeros((5, 8, 3), np.uint8)
    _, geom = pathseg_infer.preprocess_image(img)
    assert (geom.src_h, geom.src_w) == (7, 8)  # pad=1 两侧，8-5=3 为奇数
    assert (geom.pad_y, geom.pad_x) == (1, 0)
    mask = np.ones((1024, 1024), bool)
    back = pathseg_infer.mask_to_original_size(mask, geom)
    assert back.shape == (5, 8) and back.all()


# ---------------------------------------------------------------- PNG/哈希

def test_encode_mask_png_round_trip():
    mask = np.zeros((64, 96), bool)
    mask[10:30, 20:80] = True
    png = pathseg_infer.encode_mask_png(mask)
    decoded = np.asarray(Image.open(io.BytesIO(png)))
    assert decoded.shape == (64, 96) and decoded.dtype == np.uint8
    assert set(np.unique(decoded)) <= {0, 255}
    assert np.array_equal(decoded == 255, mask)
    # 同一尺寸输入的 PNG 掩码即服务返回（base64 可还原）
    b64 = base64.b64encode(png).decode("ascii")
    assert np.array_equal(
        np.asarray(Image.open(io.BytesIO(base64.b64decode(b64)))) == 255, mask)


def test_file_sha256(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"hello pathsegmentor")
    import hashlib
    assert pathseg_infer.file_sha256(str(p)) == \
        hashlib.sha256(b"hello pathsegmentor").hexdigest()
