# -*- coding: utf-8 -*-
"""kfb_bf_v1 parser 契约测试（KFB Phase A）。

fixture 全部为合成数据（kfb/fixture.py，无患者数据）。负向用例通过
在合成文件字节上定点 patch 构造（截断/坏 magic/越界 offset/非 JPEG
payload/索引损坏），断言稳定错误码。

PT_KFB_SAMPLE_PATH 指向真实样本时额外跑真实文件用例；缺失则 skip
（不得伪绿，docs/kfb-ingestion-converter-review.md §0.2）。

运行：cd 项目根 && python3 -m pytest tests/test_kfb_parser.py -q
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import pytest  # noqa: E402

from kfb import KfbError, build_synthetic_kfb, parse_kfb  # noqa: E402
from kfb.parser import MAGIC  # noqa: E402


@pytest.fixture()
def synth_kfb(tmp_path):
    path = build_synthetic_kfb(tmp_path / "synth.kfb")
    yield str(path)


def _patch(path, offset, blob):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(blob)


def _first_tile(doc):
    return doc.tiles[0]


# --------------------------------------------------------------------------- #
# 1. 正常合成样本
# --------------------------------------------------------------------------- #
def test_parse_synthetic_header_and_levels(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        h = doc.header
        assert (h.version, h.width_px, h.height_px) == (1, 580, 300)
        assert (h.tile_w, h.tile_h) == (256, 256)
        assert h.level_count == 3
        assert h.tile_count == 9  # 6 + 2 + 1
        assert h.brightfield is True
        assert h.scanner_id == "PTSYNTH0001"
        assert abs(h.mpp_x - 0.4841049) < 1e-9
        assert h.objective == 20.0
        # 层级几何：level0 按 2^L 向下取整（奇数边 floor）
        assert [(l.level, l.width, l.height) for l in doc.levels] == [
            (0, 580, 300), (1, 290, 150), (2, 145, 75)]
        # tile 索引聚合
        assert len(doc.tiles) == 9
        assert {lv: len(ts) for lv, ts in doc.tiles_by_level.items()} == {
            0: 6, 1: 2, 2: 1}
        full = [t for t in doc.tiles if t.is_full_tile]
        assert len(full) == 2  # (0,0) (0,1)
        # associated：label/overview/thumbnail
        assert [a.name for a in doc.associated] == [
            "label", "overview", "thumbnail"]


def test_parse_floor_geometry_for_odd_dims(tmp_path):
    """奇数边向下取整（评审样本 34013→17006→8503→4251 的同型小图）。"""
    path = build_synthetic_kfb(tmp_path / "odd.kfb", width=1365, height=1203)
    with parse_kfb(str(path)) as doc:
        assert [(l.level, l.width, l.height) for l in doc.levels] == [
            (0, 1365, 1203), (1, 682, 601), (2, 341, 300),
            (3, 170, 150)]  # 到首个 1×1 网格层（含）


def test_tile_payload_reads_jpeg_bytes(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        tile = _first_tile(doc)
        payload = doc.tile_payload(tile)
        assert len(payload) == tile.payload_length
        assert payload[:2] == b"\xff\xd8"
        assert payload[-2:] == b"\xff\xd9"


# --------------------------------------------------------------------------- #
# 2. 损坏样本 → 稳定错误码（fail-closed）
# --------------------------------------------------------------------------- #
def test_bad_magic_rejected(synth_kfb):
    _patch(synth_kfb, 0x00, b"\x00" * 8)
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "unsupported_kfb_variant"


def test_unknown_version_rejected(synth_kfb):
    _patch(synth_kfb, 0x08, struct.pack("<I", 2))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "unsupported_kfb_variant"


def test_truncated_below_header_min(tmp_path):
    path = build_synthetic_kfb(tmp_path / "t.kfb")
    data = open(str(path), "rb").read()
    cut = tmp_path / "cut.kfb"
    cut.write_bytes(data[:50])
    with pytest.raises(KfbError) as ei:
        parse_kfb(str(cut))
    assert ei.value.code == "invalid_kfb_header"


def test_truncated_mid_index(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        idx = doc.header.index_offset
    data = open(synth_kfb, "rb").read()
    cut = synth_kfb + ".trunc"
    with open(cut, "wb") as f:
        f.write(data[: idx + 2 * 32])  # 只剩 2/9 条 tile 索引
    with pytest.raises(KfbError) as ei:
        parse_kfb(cut)
    assert ei.value.code == "invalid_tile_index"


def test_payload_offset_out_of_bounds(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        tile = _first_tile(doc)
        entry_pos = doc.header.index_offset  # tile[0] 条目
        size = os.path.getsize(synth_kfb)
    _patch(synth_kfb, entry_pos + 16, struct.pack("<Q", size + 4096))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "tile_payload_out_of_bounds"


def test_non_jpeg_payload_rejected(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        tile = _first_tile(doc)
    _patch(synth_kfb, tile.payload_offset, b"\x00\x00")
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "jpeg_decode_failed"


def test_broken_jpeg_eoi_rejected(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        tile = _first_tile(doc)
    _patch(synth_kfb, tile.payload_offset + tile.payload_length - 2,
           b"\x00\x00")
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "jpeg_decode_failed"


def test_non_256_tile_size_rejected(synth_kfb):
    _patch(synth_kfb, 0x18, struct.pack("<I", 128))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "invalid_kfb_header"


def test_undefined_flag_bits_rejected(synth_kfb):
    _patch(synth_kfb, 0x58, struct.pack("<I", 0x2))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "invalid_kfb_header"


def test_header_bytes_out_of_range(synth_kfb):
    _patch(synth_kfb, 0x0C, struct.pack("<I", 4097))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "invalid_kfb_header"


def test_tile_level_out_of_range(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        entry_pos = doc.header.index_offset
    _patch(synth_kfb, entry_pos, struct.pack("<I", 99))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "invalid_tile_index"


def test_duplicate_grid_cell_rejected(synth_kfb):
    with parse_kfb(synth_kfb) as doc:
        entry_pos = doc.header.index_offset + 1 * 32  # tile[1] 条目
    # tile[1] 是 (level0, row0, col1)；把 x_px 改成 tile[0] 的 0 → 网格重复
    _patch(synth_kfb, entry_pos + 4, struct.pack("<I", 0))
    with pytest.raises(KfbError) as ei:
        parse_kfb(synth_kfb)
    assert ei.value.code == "invalid_tile_index"


def test_missing_file_rejected(tmp_path):
    with pytest.raises(KfbError) as ei:
        parse_kfb(str(tmp_path / "nope.kfb"))
    assert ei.value.code == "invalid_kfb_header"


# --------------------------------------------------------------------------- #
# 3. 可选：真实样本（PT_KFB_SAMPLE_PATH）
# --------------------------------------------------------------------------- #
def test_real_sample_if_provided():
    """环境变量指向可读文件时解析真实样本；缺失则 skip（不伪绿）。"""
    path = os.environ.get("PT_KFB_SAMPLE_PATH", "")
    if not path or not os.path.isfile(path):
        pytest.skip("PT_KFB_SAMPLE_PATH 未设置或不可读")
    with open(path, "rb") as f:
        magic = f.read(8)
    assert magic == MAGIC, "真实样本 magic 与 kfb_bf_v1 合同不符"
    doc = parse_kfb(path)
    try:
        assert doc.header.width_px == 34013
        assert doc.header.height_px == 46152
        assert doc.header.level_count == 9
        assert abs(doc.header.mpp_x - 0.4841049) < 1e-5
        assert doc.header.scanner_id.startswith("KFPBL")
        assert doc.header.brightfield is True
        full = sum(1 for t in doc.tiles if t.is_full_tile)
        edge = sum(1 for t in doc.tiles if not t.is_full_tile)
        assert full == 31650
        assert edge >= 600
        assert [(lv.level, lv.width, lv.height) for lv in doc.levels[:4]] == [
            (0, 34013, 46152), (1, 17006, 23076),
            (2, 8503, 11538), (3, 4251, 5769)]
    finally:
        doc.close()
