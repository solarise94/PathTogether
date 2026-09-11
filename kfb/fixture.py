# -*- coding: utf-8 -*-
"""合成 kfb_bf_v1 fixture（Phase A CI 用，无任何患者数据）。

与 :mod:`kfb.parser` 同一磁盘合同（§0.3）。默认几何 580×300、层间连续
减半，直到首个 1×1 网格层（含）为止，共 3 层：

    level 0: 580×300（3×2 网格：2 个完整 256×256 tile + 右侧/底边残缺 tile）
    level 1: 290×150（2×1 网格：两个非 256 边缘 tile）
    level 2: 145×75（1×1 单 tile）

像素为确定性渐变（无随机源），JPEG 由 Pillow 编码（默认 4:2:0 采样 +
双量化表），另生成 label/overview/thumbnail 三张小 JPEG。
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from .parser import (MAGIC, TILE_H, TILE_W, VERSION, KfbLevel)

DEFAULT_WIDTH = 580
DEFAULT_HEIGHT = 300
DEFAULT_JPEG_QUALITY = 90
DEFAULT_MPP = 0.4841049
DEFAULT_OBJECTIVE = 20.0
DEFAULT_SCANNER_ID = b"PTSYNTH0001"
HEADER_BYTES = 96
ASSOC_ENTRY_SIZE = 48  # 字段本体 36B + 12B 零填充（与 parser 合同一致）

_TILE_ENTRY = struct.Struct("<IIIHHQII")
_ASSOC_ENTRY = struct.Struct("<16sQIHHI")


def _gradient(h, w, seed=0):
    """确定性合成图案：通道间可区分、层间 seed 可区分。"""
    yy, xx = np.indices((h, w))
    r = ((xx + seed) % 256).astype(np.uint8)
    g = ((yy * 3 + seed) % 256).astype(np.uint8)
    b = (((xx + yy) // 2 + seed * 7) % 256).astype(np.uint8)
    return np.stack([r, g, b], axis=-1)


def _encode_jpeg(arr, quality):
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _assoc_image(name, w=96, h=72, seed=0):
    """关联图：label 白底黑框、overview/overview 渐变（确定性）。"""
    yy, xx = np.indices((h, w))
    if name == "label":
        arr = np.full((h, w, 3), 250, dtype=np.uint8)
        arr[0:6, :, :] = 30
        arr[-6:, :, :] = 30
        arr[:, 0:6, :] = 30
        arr[:, -6:, :] = 30
    elif name == "overview":
        arr = np.stack([
            ((xx * 255 // max(1, w - 1)) % 256).astype(np.uint8),
            ((yy * 255 // max(1, h - 1)) % 256).astype(np.uint8),
            np.full((h, w), 120, dtype=np.uint8),
        ], axis=-1)
    else:  # thumbnail
        arr = _gradient(h, w, seed=seed)
    return arr


def _level_geometry(width, height, max_levels=16):
    """按合同推导层列表：level L 尺寸 = level0 >> L（floor），
    直到（含）首个 1×1 网格层。"""
    levels = []
    for lvl in range(max_levels):
        w = max(1, width >> lvl)
        h = max(1, height >> lvl)
        levels.append(KfbLevel(level=lvl, width=w, height=h))
        if (w + TILE_W - 1) // TILE_W == 1 and (h + TILE_H - 1) // TILE_H == 1:
            break
    return levels


def build_synthetic_kfb(path, *, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                        quality=DEFAULT_JPEG_QUALITY,
                        mpp_x=DEFAULT_MPP, mpp_y=None,
                        objective=DEFAULT_OBJECTIVE,
                        scanner_id=DEFAULT_SCANNER_ID,
                        omit_tile=None, brightfield=True):
    """生成小尺寸合成 kfb_bf_v1 文件，返回 Path。

    ``omit_tile=(level, row, col)`` 可故意少写一个网格单元的 tile，
    供 converter 的 conversion_validation_failed 负向用例使用。
    """
    if mpp_y is None:
        mpp_y = mpp_x
    if isinstance(scanner_id, str):
        scanner_id = scanner_id.encode("ascii")
    scanner_id = scanner_id[:15]
    levels = _level_geometry(width, height)

    tile_entries = []   # (KfbTile 字段 tuple)
    tile_payloads = []  # bytes，顺序即文件内顺序
    assoc_entries = []
    assoc_payloads = []

    for lv in levels:
        ny, nx = lv.tiles_down, lv.tiles_across
        for row in range(ny):
            for col in range(nx):
                if omit_tile is not None and \
                        (lv.level, row, col) == tuple(omit_tile):
                    continue
                x0, y0 = col * TILE_W, row * TILE_H
                tw = min(TILE_W, lv.width - x0)
                th = min(TILE_H, lv.height - y0)
                data = _encode_jpeg(_gradient(th, tw, seed=lv.level * 17),
                                    quality)
                tile_entries.append(struct.pack(
                    _TILE_ENTRY.format, lv.level, x0, y0, tw, th,
                    0, len(data), 0))  # offset 后填
                tile_payloads.append(data)

    for i, name in enumerate(("label", "overview", "thumbnail")):
        w, h = (96, 72) if name != "thumbnail" else (72, 58)
        data = _encode_jpeg(_assoc_image(name, w, h, seed=i), quality)
        assoc_entries.append(struct.pack(
            _ASSOC_ENTRY.format, name.encode("ascii"), 0, len(data), w, h, 0))
        assoc_payloads.append(data)

    header_bytes = HEADER_BYTES
    # 布局：header → tile payloads → associated payloads → tile index →
    # associated index（payload offset 先占 0，写完后回填）
    parts = []
    cursor = header_bytes
    tile_offsets = []
    for data in tile_payloads:
        tile_offsets.append(cursor)
        parts.append(data)
        cursor += len(data)
    assoc_offsets = []
    for data in assoc_payloads:
        assoc_offsets.append(cursor)
        parts.append(data)
        cursor += len(data)
    index_offset = cursor
    for i, blob in enumerate(tile_entries):
        lvl, x, y, jw, jh, _o, plen, res = _TILE_ENTRY.unpack(blob)
        parts.append(struct.pack(_TILE_ENTRY.format, lvl, x, y, jw, jh,
                                 tile_offsets[i], plen, res))
        cursor += _TILE_ENTRY.size
    for i, blob in enumerate(assoc_entries):
        name, _o, plen, w, h, res = _ASSOC_ENTRY.unpack(blob)
        parts.append(struct.pack(_ASSOC_ENTRY.format, name, assoc_offsets[i],
                                 plen, w, h, res))
        parts.append(b"\x00" * (ASSOC_ENTRY_SIZE - _ASSOC_ENTRY.size))
        cursor += ASSOC_ENTRY_SIZE

    flags = 1 if brightfield else 0
    header = bytearray(header_bytes)
    header[0:8] = MAGIC
    struct.pack_into("<IIIIIIII", header, 0x08, VERSION, header_bytes,
                     width, height, TILE_W, TILE_H,
                     len(levels), len(tile_entries))
    struct.pack_into("<dd", header, 0x28, float(mpp_x), float(mpp_y))
    struct.pack_into("<f", header, 0x38, float(objective))
    header[0x3C:0x3C + len(scanner_id)] = scanner_id
    struct.pack_into("<I", header, 0x4C, len(assoc_entries))
    struct.pack_into("<Q", header, 0x50, index_offset)
    struct.pack_into("<I", header, 0x58, flags)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(header))
        for blob in parts:
            f.write(blob)
    return path
