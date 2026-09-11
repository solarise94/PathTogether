# -*- coding: utf-8 -*-
"""江丰/KFBIO 明场 KFB 厂商布局（生产 parser）。

与合成 ``kfb_bf_v1`` 同一 magic ``f1 01 ee ee 4b 46 42 00``，但 header 不是
version=1 合同，而是评审真实样本（KFPBL40000110032）的 tagged + 64B tile
索引。未知变体 fail-closed。

布局（little-endian，由真实样本校准）：

    0x00  8s   magic
    0x10  u32  tile_count
    0x14  u32  height_px          # 注意：高在前
    0x18  u32  width_px
    0x1c  u32  objective（整数倍率，如 20）
    0x20  4s   codec 必须为 JPEG
    0x44  u64  tile_index_offset
    0x4c  f32  mpp
    0x58  u32  tile_w = tile_h = 256

    tile_index 起连续 tile_count 条，每条 64B：
      4s  f104eeee
      u32 x_px, y_px, jpeg_w, jpeg_h
      f32 scale（20.0=level0，10.0=level1，…）
      u32 0, 0
      u32 payload_length
      i32 va                 # phys = index_offset + va
      u32 0xffffffff
      u32 20,20,20,20
      4s  ff04eeee

    关联图：f1 02 / f1 03 记录（52B 头 + JPEG），按出现顺序映射为
    overview / label / thumbnail（首张宽幅、中间近方、末张最小）。
"""

from __future__ import annotations

import math
import struct

from .errors import KfbError
from .parser import (
    HEADER_MIN_BYTES, MAGIC, MAX_DIM_PX, MAX_LEVEL_COUNT, MAX_PAYLOAD_LENGTH,
    MAX_TILE_COUNT, MIN_PAYLOAD_LENGTH, MIN_TILE_COUNT, TILE_H, TILE_W,
    KfbAssociated, KfbHeader, KfbLevel, KfbTile, _check_jpeg_bounds,
    _check_payload,
)

_TILE_REC = struct.Struct("<4sIIIIfIIIiI4I4s")
_TILE_HEAD = bytes.fromhex("f104eeee")
_TILE_TAIL = bytes.fromhex("ff04eeee")
_ASSOC_MAGICS = (
    bytes.fromhex("f102eeee"),
    bytes.fromhex("f103eeee"),
)
_ASSOC_HEAD = struct.Struct("<4sIIIII")  # magic, id, height, width, type, jpeg_len


def looks_like_vendor(mm, size) -> bool:
    """magic 已匹配后：version≠1 且 codec=JPEG、tile=256 视为厂商布局。"""
    if size < HEADER_MIN_BYTES:
        return False
    version, = struct.unpack_from("<I", mm, 0x08)
    if version == 1:
        return False
    codec = bytes(mm[0x20:0x24])
    tile, = struct.unpack_from("<I", mm, 0x58)
    return codec == b"JPEG" and tile == TILE_W


def parse_vendor(mm, size):
    """填充 (header, levels, tiles, tiles_by_level, associated)。"""
    tile_count, height, width, objective = struct.unpack_from("<IIII", mm, 0x10)
    if not (MIN_TILE_COUNT <= tile_count <= MAX_TILE_COUNT):
        raise KfbError("invalid_kfb_header", "tile_count=%d 越界" % tile_count)
    if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX):
        raise KfbError("invalid_kfb_header", "宽/高越界 (%d,%d)" % (width, height))
    if objective < 1 or objective > 100:
        raise KfbError("invalid_kfb_header", "objective=%d 非法" % objective)
    if bytes(mm[0x20:0x24]) != b"JPEG":
        raise KfbError("unsupported_kfb_variant", "非 JPEG 明场 KFB")
    tile_size, = struct.unpack_from("<I", mm, 0x58)
    if tile_size != TILE_W:
        raise KfbError("invalid_kfb_header", "tile 尺寸必须为 %d" % TILE_W)
    mpp, = struct.unpack_from("<f", mm, 0x4c)
    if not math.isfinite(mpp) or mpp <= 0:
        raise KfbError("invalid_kfb_header", "mpp=%r 非法" % (mpp,))
    index_offset, = struct.unpack_from("<Q", mm, 0x44)
    index_bytes = tile_count * _TILE_REC.size
    if index_offset < HEADER_MIN_BYTES or index_offset + index_bytes > size:
        raise KfbError("invalid_tile_index", "index_offset=%d 非法" % index_offset)

    scanner_id = _read_scanner_id(mm, size)
    useful_levels = _pyramid_levels(width, height)
    level_count = len(useful_levels)
    if not (1 <= level_count <= MAX_LEVEL_COUNT):
        raise KfbError("invalid_kfb_header", "level_count=%d 越界" % level_count)
    level_by_idx = {lv.level: lv for lv in useful_levels}

    tiles = []
    by_level = {}
    seen = set()
    for i in range(tile_count):
        rec_off = index_offset + i * _TILE_REC.size
        (magic, x_px, y_px, jpeg_w, jpeg_h, scale, z0, z1, length, va,
         sent, *_rest, tail) = _TILE_REC.unpack_from(mm, rec_off)
        if magic != _TILE_HEAD or tail != _TILE_TAIL:
            raise KfbError("invalid_tile_index", "tile[%d] 记录 magic 非法" % i)
        if z0 != 0 or z1 != 0 or sent != 0xFFFFFFFF:
            raise KfbError("invalid_tile_index", "tile[%d] 保留字段非法" % i)
        if not math.isfinite(scale) or scale <= 0:
            raise KfbError("invalid_tile_index", "tile[%d] scale=%r" % (i, scale))
        level = int(round(math.log2(float(objective) / scale)))
        if level < 0 or level not in level_by_idx:
            continue  # 1×1 之后的冗余单 tile 层不纳入 canonical
        lv = level_by_idx[level]
        if x_px % TILE_W or y_px % TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 坐标未按网格对齐" % i)
        if jpeg_w < 1 or jpeg_w > TILE_W or jpeg_h < 1 or jpeg_h > TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] jpeg 尺寸 %d×%d 非法" % (i, jpeg_w, jpeg_h))
        if x_px + jpeg_w > lv.width or y_px + jpeg_h > lv.height:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 越出层 %d 边界" % (i, level))
        if not (MIN_PAYLOAD_LENGTH <= length <= MAX_PAYLOAD_LENGTH):
            raise KfbError("invalid_tile_index",
                           "tile[%d] payload_length=%d 非法" % (i, length))
        phys = index_offset + int(va)
        _check_payload(mm, size, phys, length)
        payload = mm[phys:phys + length]
        _check_jpeg_bounds(payload)
        cell = (level, y_px // TILE_H, x_px // TILE_W)
        if cell in seen:
            raise KfbError("invalid_tile_index", "tile[%d] 网格重复" % i)
        seen.add(cell)
        tile = KfbTile(level=level, x_px=x_px, y_px=y_px, jpeg_w=jpeg_w,
                       jpeg_h=jpeg_h, payload_offset=phys, payload_length=length)
        tiles.append(tile)
        by_level.setdefault(level, []).append(tile)

    associated = _parse_associated(mm, size)
    header = KfbHeader(
        version=0, width_px=width, height_px=height,
        tile_w=TILE_W, tile_h=TILE_H, level_count=level_count,
        tile_count=len(tiles), mpp_x=float(mpp), mpp_y=float(mpp),
        objective=float(objective), scanner_id=scanner_id,
        associated_count=len(associated), index_offset=index_offset,
        brightfield=True)
    return header, useful_levels, tiles, by_level, associated


def _pyramid_levels(width, height):
    levels = []
    w, h = width, height
    for lvl in range(MAX_LEVEL_COUNT):
        lv = KfbLevel(level=lvl, width=max(1, w), height=max(1, h))
        levels.append(lv)
        if lv.tiles_across == 1 and lv.tiles_down == 1:
            break
        w = max(1, w >> 1)
        h = max(1, h >> 1)
    return levels


def _read_scanner_id(mm, size):
    """tagged 段 ff01eeee 中 tag 29（16s ASCII）。缺失则空串。"""
    if size < 0x70:
        return ""
    if bytes(mm[0x5c:0x60]) != bytes.fromhex("ff01eeee"):
        return ""
    count, = struct.unpack_from("<I", mm, 0x60)
    if count > 32:
        raise KfbError("invalid_kfb_header", "tagged 段过长")
    off = 0x64
    scanner = ""
    for _ in range(count):
        if off + 8 > size:
            raise KfbError("invalid_kfb_header", "tagged 段截断")
        tag, length = struct.unpack_from("<II", mm, off)
        off += 8
        if length > 256 or off + length > size:
            raise KfbError("invalid_kfb_header", "tagged 值越界")
        val = bytes(mm[off:off + length])
        off += length
        if tag == 29:
            scanner = val.split(b"\x00", 1)[0].decode("ascii", "replace")
    return scanner


def _parse_associated(mm, size):
    """扫描 f102/f103 记录。按面积：最大 overview、次大 label、最小 thumbnail。"""
    found = []
    seen = set()
    for magic in _ASSOC_MAGICS:
        pos = 0
        while True:
            i = mm.find(magic, pos)
            if i < 0:
                break
            pos = i + 4
            if i + 52 > size:
                continue
            _mg, _id, height, width, _typ, jpeg_len = _ASSOC_HEAD.unpack_from(mm, i)
            jpeg_off = i + 52
            if jpeg_off in seen:
                continue
            if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX
                    and MIN_PAYLOAD_LENGTH <= jpeg_len <= MAX_PAYLOAD_LENGTH
                    and jpeg_off + jpeg_len <= size):
                continue
            try:
                _check_jpeg_bounds(mm[jpeg_off:jpeg_off + jpeg_len])
            except KfbError:
                continue
            seen.add(jpeg_off)
            found.append((width * height, width, height, jpeg_off, jpeg_len))
            if len(found) >= 8:
                break
    found.sort(key=lambda t: t[0], reverse=True)
    names = ("overview", "label", "thumbnail")
    # 3 张：大/中/小；不足则按顺序截断
    picked = found[:2]
    if len(found) >= 3:
        picked.append(found[-1])
    return [
        KfbAssociated(name=names[i], payload_offset=item[3],
                      payload_length=item[4], width=item[1], height=item[2])
        for i, item in enumerate(picked)
    ]
