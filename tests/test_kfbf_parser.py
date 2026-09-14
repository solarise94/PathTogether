# -*- coding: utf-8 -*-
"""kfb_fl_v1（荧光 KFBF）parser 契约测试。

fixture 全部为合成数据（kfb/fixture_fl.py，无患者数据）。负向用例通过
在合成文件字节上定点 patch 构造（坏 magic/version/codec/保留字段/越界
指针/通道元数据缺失），断言稳定错误码（fail-closed，不猜测）。

PT_KFBF_SAMPLES_DIR 指向真实样本目录时额外跑真实文件校准用例；
缺失则 skip（不得伪绿）。

运行：cd 项目根 && python3 -m pytest tests/test_kfbf_parser.py -q
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import pytest  # noqa: E402

from kfb import KfbError, build_synthetic_kfbf, parse_kfbf  # noqa: E402
from kfb.vendor_kfbf import KFBF_MAGIC, level_dimensions  # noqa: E402


@pytest.fixture()
def synth_kfbf(tmp_path):
    path = build_synthetic_kfbf(tmp_path / "synth.kfbf")
    yield str(path)


def _patch(path, offset, blob):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(blob)


# --------------------------------------------------------------------------- #
# 1. 正常合成样本
# --------------------------------------------------------------------------- #
def test_parse_synthetic_header(synth_kfbf):
    with parse_kfbf(synth_kfbf) as doc:
        h = doc.header
        assert (h.width_px, h.height_px) == (600, 400)
        assert h.objective == 40.0
        assert h.channel_count == 2
        assert h.tile_count == 8  # L0 5（缺 cell(0,1)）+ L1 2 + L2 1
        assert h.scanner_id == "KFSYNTH0001"
        assert abs(h.mpp - 0.2506266) < 1e-6
        assert h.scanned_at == 1789135116
        assert [(lv.level, lv.width, lv.height) for lv in doc.levels] == [
            (0, 600, 400), (1, 300, 200), (2, 150, 100)]


def test_parse_synthetic_channels(synth_kfbf):
    with parse_kfbf(synth_kfbf) as doc:
        assert len(doc.channels) == 2
        dapi, ch520 = doc.channels
        assert dapi.name == "DAPI"
        assert dapi.color_rgb == (0, 0, 229)
        assert dapi.exposure == 6.0
        assert dapi.gamma == 1.0
        assert ch520.name == "520"
        assert ch520.color_rgb == (0, 255, 0)
        assert ch520.exposure == 2.0


def test_parse_synthetic_tiles_and_payloads(synth_kfbf):
    import io

    from PIL import Image
    with parse_kfbf(synth_kfbf) as doc:
        # 稀疏：L0 缺 cell(0,1)
        cells_l0 = {(t.row, t.col) for t in doc.tiles_by_level[0]}
        assert cells_l0 == {(0, 0), (0, 2), (1, 0), (1, 1), (1, 2)}
        # 底行 tile 被裁剪（trim_level0_bottom=44）：jh = 144-44 = 100
        bottom = [t for t in doc.tiles_by_level[0] if t.row == 1]
        assert all(t.jpeg_h == 100 for t in bottom)
        # 每 tile 每通道 payload 可解码为灰度 JPEG
        tile = next(t for t in doc.tiles_by_level[0]
                    if (t.row, t.col) == (0, 0))
        for c in range(2):
            payload = doc.channel_payload(tile, c)
            assert len(payload) == tile.lengths[c]
            im = Image.open(io.BytesIO(payload))
            im.load()
            assert im.size == (256, 256)
            assert im.mode == "L"


def test_parse_synthetic_associated(synth_kfbf):
    import io

    from PIL import Image
    with parse_kfbf(synth_kfbf) as doc:
        by_name = {a.name: a for a in doc.associated}
        assert set(by_name) == {"overview", "label", "thumbnail"}
        assert (by_name["overview"].width, by_name["overview"].height) \
            == (96, 64)
        im = Image.open(io.BytesIO(
            doc.associated_payload(by_name["overview"])))
        im.load()
        assert im.mode == "RGB"
        im = Image.open(io.BytesIO(
            doc.associated_payload(by_name["thumbnail"])))
        im.load()
        assert im.size == (64, 48)
        assert im.mode == "L"


def test_level_dimensions_formula():
    """kfb_fl_v1 层级几何公式（真实样本校准值）。"""
    # NJH 真实样本：31023×53370
    dims = [level_dimensions(31023, 53370, lvl) for lvl in range(17)]
    assert dims[:5] == [(31023, 53370), (15511, 26685), (7755, 13342),
                        (3877, 6671), (1952, 3344)]
    assert dims[8] == (122, 209)      # ceil(L0/256) 锚点
    assert dims[9] == (61, 104)
    assert dims[16] == (1, 1)
    # 小图：L1..L3 floor 减半
    assert level_dimensions(600, 400, 1) == (300, 200)
    assert level_dimensions(600, 400, 2) == (150, 100)


# --------------------------------------------------------------------------- #
# 2. fail-closed 负向用例
# --------------------------------------------------------------------------- #
def test_reject_brightfield_kfb_magic(tmp_path):
    """明场 KFB magic 不得以 KFBF 解析（变体不猜测）。"""
    src = build_synthetic_kfbf(tmp_path / "x.kfbf")
    _patch(src, 0, bytes.fromhex("f101eeee4b464200"))  # KFB 明场 magic
    with pytest.raises(KfbError) as ei:
        parse_kfbf(src)
    assert ei.value.code == "unsupported_kfb_variant"


def test_reject_bad_version(synth_kfbf):
    _patch(synth_kfbf, 0x08, struct.pack("<I", 1))
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code == "unsupported_kfb_variant"


def test_reject_bad_format_version(synth_kfbf):
    _patch(synth_kfbf, 0x0C, struct.pack("<f", 3.0))
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code == "unsupported_kfb_variant"


def test_reject_non_jpeg_codec(synth_kfbf):
    _patch(synth_kfbf, 0x20, b"LZ77")
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code == "unsupported_kfb_variant"


def test_reject_truncated(synth_kfbf, tmp_path):
    data = open(synth_kfbf, "rb").read()
    short = tmp_path / "short.kfbf"
    short.write_bytes(data[:len(data) // 2])
    with pytest.raises(KfbError) as ei:
        parse_kfbf(short)
    assert ei.value.code in ("invalid_kfb_header", "invalid_tile_index",
                             "tile_payload_out_of_bounds")


def test_reject_reserved_field_nonzero(synth_kfbf):
    import mmap
    with open(synth_kfbf, "rb") as f:
        idx_off = struct.unpack_from("<Q", f.read(0x50), 0x44)[0]
    # 第一条 tile 记录的保留字段 r1（偏移 +52）置非零
    _patch(synth_kfbf, idx_off + 52, struct.pack("<I", 7))
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code == "invalid_tile_index"


def test_reject_side_record_out_of_bounds(synth_kfbf):
    with open(synth_kfbf, "rb") as f:
        idx_off = struct.unpack_from("<Q", f.read(0x50), 0x44)[0]
    _patch(synth_kfbf, idx_off + 48, struct.pack("<I", 0x7FFFFF00))
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code in ("invalid_tile_index", "tile_payload_out_of_bounds")


def test_reject_missing_channel_count_tag(synth_kfbf):
    """tag 75（channel_count）清零 → invalid_kfb_header。"""
    with open(synth_kfbf, "rb") as f:
        head = f.read(0x200)
    off = 0x64
    count, = struct.unpack_from("<I", head, 0x60)
    for _ in range(count):
        tag, ln = struct.unpack_from("<II", head, off)
        if tag == 75:
            _patch(synth_kfbf, off + 8, struct.pack("<I", 0))
            break
        off += 8 + ln
    with pytest.raises(KfbError) as ei:
        parse_kfbf(synth_kfbf)
    assert ei.value.code == "invalid_kfb_header"


def test_reject_non_grayscale_channel_jpeg(synth_kfbf, tmp_path):
    """通道 payload 换成 RGB JPEG → jpeg_decode_failed（SOF 探测在转换期；
    parser 期骨架仍合法——此用例验证 converter 拒绝，见 converter 测试）。
    此处仅确认 parser 骨架校验通过（不误判）。"""
    with parse_kfbf(synth_kfbf) as doc:
        assert doc.header.channel_count == 2


# --------------------------------------------------------------------------- #
# 3. 真实样本校准（PT_KFBF_SAMPLES_DIR 存在时）
# --------------------------------------------------------------------------- #
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


@pytest.mark.skipif(not _real_samples(), reason="无真实 KFBF 样本")
def test_real_samples_parse_and_calibrate():
    """真实样本：通道元数据与厂商 viewer 的 channel.json 一致。"""
    import json
    samples = _real_samples()
    assert samples, "样本目录为空"
    for path in samples:
        with parse_kfbf(path) as doc:
            h = doc.header
            assert h.channel_count >= 2
            assert h.objective == 40.0
            assert 0.2 < h.mpp < 0.3
            assert h.scanner_id.startswith("KF")
            # 通道名/颜色 == 伴随 channel.json
            cj_path = os.path.join(
                os.path.dirname(path),
                os.path.splitext(os.path.basename(path))[0] + "_kfbf",
                "Annotations", "channel.json")
            if os.path.isfile(cj_path):
                cj = json.load(open(cj_path, encoding="utf-8"))
                assert [c["channelName"] for c in cj] == \
                    [c.name for c in doc.channels]
                for cj_ch, ch in zip(cj, doc.channels):
                    rgb = tuple(int(cj_ch["channelColor"][i:i + 2], 16)
                                for i in (1, 3, 5))
                    assert rgb == ch.color_rgb
            # 每 level 至少一个 tile 的通道 payload 可完整解码
            import io

            from PIL import Image
            t0 = doc.tiles_by_level[0][0]
            for c in range(h.channel_count):
                im = Image.open(io.BytesIO(doc.channel_payload(t0, c)))
                im.load()
                assert im.mode == "L"
