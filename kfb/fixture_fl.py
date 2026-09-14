# -*- coding: utf-8 -*-
"""合成 kfb_fl_v1（荧光 KFBF）fixture（CI 用，无任何患者数据）。

与 :mod:`kfb.vendor_kfbf` 同一磁盘合同（字段布局见该模块 docstring）。
默认几何 600×400、2 通道（DAPI / 520）、floor 减半至首个 1×1 网格
层（含）共 3 层：

    level 0: 600×400（3×2 网格；cell(0,1) 故意缺失 → 黑填充路径）
    level 1: 300×200（2×1 网格，右列残缺 tile）
    level 2: 150×100（1×1 单 tile）

像素为确定性渐变（通道/层/tile 可区分），灰度 JPEG 由 Pillow 编码。
附带 overview（f102 内联 RGB）、label（f103 内联 RGB）、thumbnail
（EOF 记录 + 二级间接灰度 JPEG）。
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from .parser import TILE_H, TILE_W
from .vendor_kfbf import (
    CHANNEL_NAME_BYTES, FORMAT_VERSION, KFBF_MAGIC, level_dimensions,
)

DEFAULT_WIDTH = 600
DEFAULT_HEIGHT = 400
DEFAULT_MPP = 0.2506266
DEFAULT_OBJECTIVE = 40
DEFAULT_SCANNER_ID = b"KFSYNTH0001"
DEFAULT_JPEG_QUALITY = 90

#: 默认通道：(名字, RGB, 曝光 ms, gamma)
DEFAULT_CHANNELS = (
    ("DAPI", (0, 0, 229), 6.0, 1.0),
    ("520", (0, 255, 0), 2.0, 1.0),
)

#: 默认故意缺失的 level-0 网格 cell（row, col）——测试黑填充
DEFAULT_MISSING_CELLS = frozenset(((0, 1),))

_TAGGED_MAGIC = bytes.fromhex("ff01eeee")
_TILE_HEAD = bytes.fromhex("f104eeee")
_TILE_TAIL = bytes.fromhex("ff04eeee")
_ASSOC_MAGICS = {"overview": bytes.fromhex("f102eeee"),
                 "label": bytes.fromhex("f103eeee")}
_ASSOC_TAILS = {"overview": bytes.fromhex("ff02eeee"),
                "label": bytes.fromhex("ff03eeee")}
_TILE_REC = struct.Struct("<4s" + "I" * 4 + "f" + "I" * 9 + "4s")
_ASSOC_HEAD = struct.Struct("<4sIIIII")


def _channel_pattern(h, w, channel, level, row, col):
    """确定性合成图案：通道/层/tile 可区分（无随机源）。"""
    yy, xx = np.indices((h, w))
    return ((xx * 3 + yy * 5 + channel * 40 + level * 17
             + row * 7 + col * 11) % 256).astype(np.uint8)


def _encode_gray_jpeg(arr, quality=DEFAULT_JPEG_QUALITY):
    buf = io.BytesIO()
    Image.fromarray(arr, "L").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _encode_rgb_jpeg(arr, quality=DEFAULT_JPEG_QUALITY):
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _assoc_rgb(kind, w=96, h=64):
    yy, xx = np.indices((h, w))
    if kind == "label":
        arr = np.full((h, w, 3), 250, dtype=np.uint8)
        arr[0:4, :, :] = 30
        arr[-4:, :, :] = 30
        arr[:, 0:4, :] = 30
        arr[:, -4:, :] = 30
    else:  # overview：渐变
        arr = np.stack([
            ((xx * 255 // max(1, w - 1)) % 256).astype(np.uint8),
            ((yy * 255 // max(1, h - 1)) % 256).astype(np.uint8),
            np.full((h, w), 120, dtype=np.uint8),
        ], axis=-1)
    return arr


def build_synthetic_kfbf(path, *, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                         mpp=DEFAULT_MPP, objective=DEFAULT_OBJECTIVE,
                         scanner_id=DEFAULT_SCANNER_ID,
                         channels=DEFAULT_CHANNELS,
                         missing_cells=DEFAULT_MISSING_CELLS,
                         trim_level0_bottom=44,
                         scanned_at=1789135116):
    """写出合成 KFBF 文件，返回 Path。

    ``missing_cells``：level 0 故意不写的网格 cell（(row, col)），
    用于覆盖转换器的稀疏黑填充路径。
    ``trim_level0_bottom``：level 0 底行 tile 的 JPEG 高度比 cell 矮
    这么多像素（模拟厂商底行内容裁剪），覆盖转换器的黑底重编码路径；
    0 = 不裁剪。
    """
    path = Path(path)
    nch = len(channels)

    # ---- 金字塔几何（止于首个 1×1 网格层，含）--------------------------
    level_dims = []
    for lvl in range(24):
        lw, lh = level_dimensions(width, height, lvl)
        level_dims.append((lw, lh))
        if lw <= TILE_W and lh <= TILE_H:
            break

    # ---- tile payload（先编码，得到长度）--------------------------------
    # tiles[lvl] = [ (x, y, jw, jh, [ch_jpeg_bytes]) ]
    tiles = []
    for lvl, (lw, lh) in enumerate(level_dims):
        ta = (lw + TILE_W - 1) // TILE_W
        td = (lh + TILE_H - 1) // TILE_H
        for row in range(td):
            for col in range(ta):
                if lvl == 0 and (row, col) in missing_cells:
                    continue
                jw = min(TILE_W, lw - col * TILE_W)
                jh = min(TILE_H, lh - row * TILE_H)
                if lvl == 0 and trim_level0_bottom and row == td - 1:
                    jh = max(1, jh - int(trim_level0_bottom))
                payloads = [
                    _encode_gray_jpeg(
                        _channel_pattern(jh, jw, c, lvl, row, col))
                    for c in range(nch)
                ]
                tiles.append((lvl, col * TILE_W, row * TILE_H, jw, jh,
                              payloads))

    # ---- 关联图 ---------------------------------------------------------
    overview_jpeg = _encode_rgb_jpeg(_assoc_rgb("overview"))
    label_jpeg = _encode_rgb_jpeg(_assoc_rgb("label", w=64, h=64))
    thumb_arr = _channel_pattern(48, 64, 0, 0, 0, 0)
    thumbnail_jpeg = _encode_gray_jpeg(thumb_arr)

    # ---- header + tagged + 元数据区 ------------------------------------
    header = bytearray(0x5C)
    header[0x00:0x08] = KFBF_MAGIC
    struct.pack_into("<I", header, 0x08, 0)                     # version
    struct.pack_into("<f", header, 0x0C, FORMAT_VERSION)
    struct.pack_into("<I", header, 0x10, len(tiles))
    struct.pack_into("<I", header, 0x14, height)
    struct.pack_into("<I", header, 0x18, width)
    struct.pack_into("<I", header, 0x1C, objective)
    header[0x20:0x24] = b"JPEG"
    struct.pack_into("<I", header, 0x2C, scanned_at)
    # 0x34/0x38 overview/label 记录偏移、0x44 index_offset 布局后回填
    struct.pack_into("<f", header, 0x4C, mpp)
    struct.pack_into("<I", header, 0x58, TILE_W)

    blob = bytearray(header)

    # tagged 段（0x5c 起）：先算值区布局（值内联存指针/标量）
    meta_blocks = []  # (bytes) 顺序即写入顺序
    names_blob = b"".join(
        name.encode("utf-8")[:CHANNEL_NAME_BYTES - 1].ljust(
            CHANNEL_NAME_BYTES, b"\x00")
        for name, _c, _e, _g in channels)
    colors_blob = b"".join(
        struct.pack("<III", *rgb) for _n, rgb, _e, _g in channels)
    expo_blob = b"".join(
        struct.pack("<d", expo) for _n, _c, expo, _g in channels)
    gamma_blob = b"".join(
        struct.pack("<d", gamma) for _n, _c, _e, gamma in channels)

    # tagged 区段长度：magic4 + count4 + Σ(8 + len)
    tag_specs = [
        (29, scanner_id),                       # scanner id（内联）
        (75, struct.pack("<I", nch)),           # channel_count（内联）
        (77, 8), (79, 8), (84, 8), (87, 8),     # 指针（布局后回填）
    ]
    tagged_len = 8 + sum(8 + (len(v) if isinstance(v, bytes) else v)
                         for _t, v in tag_specs)
    meta_base = 0x5C + tagged_len
    # 元数据块紧跟 tagged 段
    ptrs = {}
    cursor = meta_base
    for tag, blk in ((77, names_blob), (79, colors_blob),
                     (84, expo_blob), (87, gamma_blob)):
        ptrs[tag] = cursor
        meta_blocks.append(blk)
        cursor += len(blk)

    tagged = bytearray()
    tagged += _TAGGED_MAGIC
    tagged += struct.pack("<I", len(tag_specs))
    for tag, val in tag_specs:
        if isinstance(val, bytes):
            tagged += struct.pack("<II", tag, len(val)) + val
        else:
            tagged += struct.pack("<II", tag, 8) + struct.pack("<Q", ptrs[tag])
    assert len(tagged) == tagged_len
    blob += tagged
    for blk in meta_blocks:
        blob += blk

    # ---- 关联图记录（内联 JPEG）-----------------------------------------
    overview_rec_off = len(blob)
    blob += _ASSOC_HEAD.pack(bytes(_ASSOC_MAGICS["overview"]), 1,
                             64, 96, 3, len(overview_jpeg))
    blob += struct.pack("<QQ", 0, 0) + b"\x00" * 8
    blob += _ASSOC_TAILS["overview"]
    blob += overview_jpeg
    label_rec_off = len(blob)
    blob += _ASSOC_HEAD.pack(bytes(_ASSOC_MAGICS["label"]), 1,
                             64, 64, 3, len(label_jpeg))
    blob += struct.pack("<QQ", 0, 0) + b"\x00" * 8
    blob += _ASSOC_TAILS["label"]
    blob += label_jpeg

    # thumbnail：payload 在前，EOF 记录经 *p1 间接
    thumb_payload_off = len(blob)
    blob += thumbnail_jpeg
    thumb_ptr_cell_off = len(blob)
    blob += struct.pack("<Q", thumb_payload_off)

    # ---- tile payload + 指针块 + side 记录 ------------------------------
    # 每条 tile：6 通道 payload（顺序写）→ 96B 指针块 → 48B side 记录
    index_entries = []
    for lvl, x, y, jw, jh, payloads in tiles:
        offsets = []
        for data in payloads:
            offsets.append(len(blob))
            blob += data
        ptr_block_off = len(blob)
        # 96B：前 6 条本 tile 通道偏移（nch 之外补 0），后 6 条全 0
        ptrs12 = list(offsets) + [0] * (6 - nch) + [0] * 6
        blob += struct.pack("<12Q", *ptrs12)
        side_off = len(blob)
        lengths = [len(d) for d in payloads]
        blob += struct.pack("<6Q", *(lengths + [0] * (6 - nch)))
        scale = objective / (2.0 ** lvl)
        index_entries.append(_TILE_REC.pack(
            _TILE_HEAD, x, y, jw, jh, scale,
            0, 0, lengths[0], ptr_block_off, 0, side_off, 0, 0, 0,
            _TILE_TAIL))

    # ---- tile 索引 + EOF thumbnail 记录 ---------------------------------
    index_offset = len(blob)
    for entry in index_entries:
        blob += entry
    thumb_rec = bytearray()
    thumb_rec += _ASSOC_HEAD.pack(bytes(_ASSOC_MAGICS["overview"]), 1,
                                  48, 64, 1, len(thumbnail_jpeg))
    thumb_rec += struct.pack("<QQ", thumb_ptr_cell_off, 0) + b"\x00" * 8
    thumb_rec += _ASSOC_TAILS["overview"]
    assert len(thumb_rec) == 52
    blob += thumb_rec

    # ---- 回填 header 指针 ------------------------------------------------
    struct.pack_into("<I", blob, 0x34, overview_rec_off)
    struct.pack_into("<I", blob, 0x38, label_rec_off)
    struct.pack_into("<Q", blob, 0x44, index_offset)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(blob))
    return path
