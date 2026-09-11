# -*- coding: utf-8 -*-
"""kfb_bf_v1 只读 parser（Phase A）。

解析明场 KFB 合同布局（docs/kfb-ingestion-converter-review.md §0.3，
little-endian）：

    0x00  8s   magic = f1 01 ee ee 4b 46 42 00
    0x08  u32  version = 1
    0x0C  u32  header_bytes（含 magic，96..4096）
    0x10  u32  width_px / 0x14 u32 height_px（level 0）
    0x18  u32  tile_w / 0x1C u32 tile_h（必须 256）
    0x20  u32  level_count（1..16）
    0x24  u32  tile_count（1..2_000_000）
    0x28  f64  mpp_x / 0x30 f64 mpp_y
    0x38  f32  objective
    0x3C  16s  scanner_id（ASCII NUL 填充）
    0x4C  u32  associated_count（0..8）
    0x50  u64  index_offset
    0x58  u32  flags（bit0=brightfield，其它位必须 0）
    0x5C  零填充至 header_bytes

    index_offset 起连续 tile_count 条（每条 32B）：
      u32 level, u32 x_px, u32 y_px, u16 jpeg_w, u16 jpeg_h,
      u64 payload_offset, u32 payload_length(1..8MiB), u32 reserved=0
    随后 associated_count 条（每条 48B）：
      16s name(label/overview/thumbnail), u64 payload_offset,
      u32 payload_length, u16 width, u16 height, u32 reserved=0,
      再零填充 12B 至 48B 步长（字段本体 36B；步长按合同固定 48）。

非可信二进制防御：全部 offset/length 加法后显式与文件长度比较（Python
int 不回绕，checked 语义即 bound-check）；硬上限见常量。未知
magic/version → ``unsupported_kfb_variant``，不猜测。
"""

from __future__ import annotations

import mmap
import math
import os
import struct
from dataclasses import dataclass, field

from .errors import KfbError

#: 固定 magic（评审真实样本同源；合成 fixture 与 parser 同一合同）
MAGIC = bytes.fromhex("f101eeee4b464200")

VERSION = 1
TILE_W = 256
TILE_H = 256

HEADER_MIN_BYTES = 96
HEADER_MAX_BYTES = 4096
MAX_DIM_PX = 200_000
MIN_LEVEL_COUNT = 1
MAX_LEVEL_COUNT = 16
MIN_TILE_COUNT = 1
MAX_TILE_COUNT = 2_000_000
MIN_ASSOCIATED_COUNT = 0
MAX_ASSOCIATED_COUNT = 8
MIN_PAYLOAD_LENGTH = 1
MAX_PAYLOAD_LENGTH = 8 * 1024 * 1024

#: tile 索引条目 32B：level,x,y,jpeg_w,jpeg_h,payload_offset,len,reserved
_TILE_ENTRY = struct.Struct("<IIIHHQII")
#: associated 索引条目：字段本体 36B + 12B 零填充 = 48B 步长（合同固定）
_ASSOC_ENTRY = struct.Struct("<16sQIHHI")
_ASSOC_PAD = struct.Struct("<12x")
_ASSOCIATED_NAMES = frozenset(("label", "overview", "thumbnail"))


@dataclass(frozen=True)
class KfbHeader:
    """kfb_bf_v1 header（已校验）。"""

    version: int
    width_px: int
    height_px: int
    tile_w: int
    tile_h: int
    level_count: int
    tile_count: int
    mpp_x: float
    mpp_y: float
    objective: float
    scanner_id: str
    associated_count: int
    index_offset: int
    brightfield: bool


@dataclass(frozen=True)
class KfbLevel:
    """层级几何：level L 尺寸 = level0 按 2^L 向下取整（奇数边 floor，
    与评审样本一致：34013→17006→8503→4251）。"""

    level: int
    width: int
    height: int

    @property
    def tiles_across(self) -> int:
        return (self.width + TILE_W - 1) // TILE_W

    @property
    def tiles_down(self) -> int:
        return (self.height + TILE_H - 1) // TILE_H


@dataclass(frozen=True)
class KfbTile:
    """tile 索引条目（payload 经 :class:`KfbDocument` 的 mmap 切片读取）。"""

    level: int
    x_px: int
    y_px: int
    jpeg_w: int
    jpeg_h: int
    payload_offset: int
    payload_length: int

    @property
    def col(self) -> int:
        return self.x_px // TILE_W

    @property
    def row(self) -> int:
        return self.y_px // TILE_H

    @property
    def is_full_tile(self) -> bool:
        return self.jpeg_w == TILE_W and self.jpeg_h == TILE_H


@dataclass(frozen=True)
class KfbAssociated:
    """关联图像（label/overview/thumbnail）索引条目。"""

    name: str
    payload_offset: int
    payload_length: int
    width: int
    height: int


@dataclass
class KfbDocument:
    """解析结果：header + 层级几何 + tile 索引 + associated 索引 + mmap。

    ``levels`` 覆盖 0..level_count-1 的合同几何（尺寸由 header 推导）；
    ``tiles_by_level`` 为该层实际出现的 tile（网格完整性校验在 converter，
    缺 tile → conversion_validation_failed）。
    """

    header: KfbHeader
    levels: list
    tiles: list = field(default_factory=list)
    associated: list = field(default_factory=list)
    tiles_by_level: dict = field(default_factory=dict)
    source_size: int = 0
    _mmap: object = None

    def tile_payload(self, tile) -> bytes:
        """读取 tile 的 JPEG payload 字节（mmap 切片，不复制索引）。"""
        if self._mmap is None:
            raise KfbError("invalid_kfb_header", "文档已关闭")
        return bytes(self._mmap[tile.payload_offset:
                                tile.payload_offset + tile.payload_length])

    def associated_payload(self, item) -> bytes:
        """读取关联图像 JPEG payload 字节。"""
        if self._mmap is None:
            raise KfbError("invalid_kfb_header", "文档已关闭")
        return bytes(self._mmap[item.payload_offset:
                                item.payload_offset + item.payload_length])

    def close(self):
        m, self._mmap = self._mmap, None
        if m is not None:
            try:
                m.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --------------------------------------------------------------------------- #
# JPEG 轻量探测：SOI/EOI 边界 + SOF 尺寸/采样（不解码像素）
# --------------------------------------------------------------------------- #
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


@dataclass(frozen=True)
class JpegProbe:
    """JPEG 头部探测结果；sampling 为三分量 (h1,v1,h2,v2,h3,v3) 或 None。"""

    width: int
    height: int
    sampling: tuple


def scan_jpeg(data) -> JpegProbe:
    """扫描 JPEG 标记段，取 SOF 的尺寸与分量采样因子。

    data 非 SOI 开头 / 结构残缺 → ``jpeg_decode_failed``（KfbError）。
    只读头部标记，不解码像素，控制解码器输入规模（§9）。
    """
    n = len(data)
    if n < 4 or data[0:2] != b"\xff\xd8":
        raise KfbError("jpeg_decode_failed", "payload 不是 JPEG（缺 SOI）")
    width = height = 0
    sampling = None
    i = 2
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise KfbError("jpeg_decode_failed", "JPEG 标记流错位")
        # 跳过填充 FF
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker == 0xD9:  # EOI：头扫描止于 EOI（SOF 必在其前）
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # 无长度段
        if i + 2 > n:
            raise KfbError("jpeg_decode_failed", "JPEG 标记段截断")
        seg_len = (data[i] << 8) | data[i + 1]
        if seg_len < 2 or i + seg_len > n:
            raise KfbError("jpeg_decode_failed", "JPEG 段长度非法")
        if marker in _SOF_MARKERS:
            seg = data[i + 2: i + seg_len]
            if len(seg) < 6:
                raise KfbError("jpeg_decode_failed", "SOF 段残缺")
            height = (seg[1] << 8) | seg[2]
            width = (seg[3] << 8) | seg[4]
            ncomp = seg[5]
            if ncomp not in (1, 3) or len(seg) < 6 + 3 * ncomp:
                raise KfbError("jpeg_decode_failed", "SOF 分量数非法")
            if ncomp == 3:
                sampling = tuple(
                    (seg[6 + 3 * c + 1] >> 4, seg[6 + 3 * c + 1] & 0x0F)
                    for c in range(3))
                sampling = (sampling[0][0], sampling[0][1],
                            sampling[1][0], sampling[1][1],
                            sampling[2][0], sampling[2][1])
            break  # 只取首个 SOF
        i += seg_len
    if width <= 0 or height <= 0:
        raise KfbError("jpeg_decode_failed", "JPEG 缺少 SOF")
    return JpegProbe(width=width, height=height, sampling=sampling)


# --------------------------------------------------------------------------- #
# 解析入口
# --------------------------------------------------------------------------- #
def parse_kfb(path):
    """解析 kfb_bf_v1 文件，返回 :class:`KfbDocument`（mmap 只读）。

    失败抛 :class:`KfbError`（稳定码）。调用方负责 ``doc.close()``（或用
    with 语句）。
    """
    path = os.fspath(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise KfbError("invalid_kfb_header", "无法读取文件：%s" % e)
    if size < HEADER_MIN_BYTES:
        raise KfbError("invalid_kfb_header",
                       "文件过小（%d < %d）" % (size, HEADER_MIN_BYTES))

    fh = open(path, "rb")
    try:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as e:
        fh.close()
        raise KfbError("invalid_kfb_header", "mmap 失败：%s" % e)
    doc = KfbDocument(header=None, levels=[], source_size=size, _mmap=mm)
    try:
        from .vendor_kfbio import looks_like_vendor, parse_vendor
        if looks_like_vendor(mm, size):
            (doc.header, doc.levels, doc.tiles, doc.tiles_by_level,
             doc.associated) = parse_vendor(mm, size)
        else:
            doc.header = _parse_header(mm, size)
            doc.levels = [
                KfbLevel(level=lvl,
                         width=max(1, doc.header.width_px >> lvl),
                         height=max(1, doc.header.height_px >> lvl))
                for lvl in range(doc.header.level_count)
            ]
            doc.tiles, doc.tiles_by_level = _parse_tile_index(
                mm, size, doc.header, doc.levels)
            doc.associated = _parse_associated_index(
                mm, size, doc.header,
                doc.header.index_offset
                + doc.header.tile_count * _TILE_ENTRY.size)
            for tile in doc.tiles:
                _check_payload(mm, size, tile.payload_offset, tile.payload_length)
                payload = mm[tile.payload_offset:
                            tile.payload_offset + tile.payload_length]
                _check_jpeg_bounds(payload)
            for item in doc.associated:
                _check_payload(mm, size, item.payload_offset, item.payload_length)
                payload = mm[item.payload_offset:
                            item.payload_offset + item.payload_length]
                _check_jpeg_bounds(payload)
    except Exception:
        doc.close()
        fh.close()
        raise
    # mmap 持有文件句柄的引用（close 由 mmap 负责），fh 可关
    fh.close()
    return doc


def _parse_header(mm, size) -> KfbHeader:
    if bytes(mm[0:8]) != MAGIC:
        raise KfbError("unsupported_kfb_variant", "未知 KFB magic")
    version, = struct.unpack_from("<I", mm, 0x08)
    if version != VERSION:
        raise KfbError("unsupported_kfb_variant",
                       "不支持的 KFB version=%r（仅支持 %d）" % (version, VERSION))
    header_bytes, = struct.unpack_from("<I", mm, 0x0C)
    if not (HEADER_MIN_BYTES <= header_bytes <= HEADER_MAX_BYTES):
        raise KfbError("invalid_kfb_header",
                       "header_bytes=%d 越界 [%d,%d]"
                       % (header_bytes, HEADER_MIN_BYTES, HEADER_MAX_BYTES))
    if header_bytes > size:
        raise KfbError("invalid_kfb_header", "header_bytes 超出文件长度")
    (width, height, tile_w, tile_h, level_count, tile_count) = \
        struct.unpack_from("<IIIIII", mm, 0x10)
    if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX):
        raise KfbError("invalid_kfb_header", "宽/高越界 (%d,%d)" % (width, height))
    if tile_w != TILE_W or tile_h != TILE_H:
        raise KfbError("invalid_kfb_header",
                       "tile 尺寸必须为 %d×%d（得 %d×%d）"
                       % (TILE_W, TILE_H, tile_w, tile_h))
    if not (MIN_LEVEL_COUNT <= level_count <= MAX_LEVEL_COUNT):
        raise KfbError("invalid_kfb_header", "level_count=%d 越界" % level_count)
    if not (MIN_TILE_COUNT <= tile_count <= MAX_TILE_COUNT):
        raise KfbError("invalid_kfb_header", "tile_count=%d 越界" % tile_count)
    mpp_x, mpp_y = struct.unpack_from("<dd", mm, 0x28)
    objective, = struct.unpack_from("<f", mm, 0x38)
    scanner_raw, = struct.unpack_from("<16s", mm, 0x3C)
    associated_count, = struct.unpack_from("<I", mm, 0x4C)
    if not (MIN_ASSOCIATED_COUNT <= associated_count <= MAX_ASSOCIATED_COUNT):
        raise KfbError("invalid_kfb_header",
                       "associated_count=%d 越界" % associated_count)
    index_offset, = struct.unpack_from("<Q", mm, 0x50)
    flags, = struct.unpack_from("<I", mm, 0x58)
    if flags & ~0x1:
        raise KfbError("invalid_kfb_header", "flags=0x%x 含未定义位" % flags)
    for name, mpp in (("mpp_x", mpp_x), ("mpp_y", mpp_y)):
        if not math.isfinite(mpp) or mpp <= 0:
            raise KfbError("invalid_kfb_header", "%s=%r 非法" % (name, mpp))
    if not math.isfinite(objective) or objective < 0:
        raise KfbError("invalid_kfb_header", "objective=%r 非法" % objective)
    scanner_id = scanner_raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    # 索引区不得与 header 重叠（>= header_bytes）且起点在文件内
    if index_offset < header_bytes or index_offset >= size:
        raise KfbError("invalid_tile_index",
                       "index_offset=%d 非法" % index_offset)
    index_end = index_offset + tile_count * _TILE_ENTRY.size \
        + associated_count * 48
    if index_end > size:
        raise KfbError("invalid_tile_index", "索引区超出文件长度")
    return KfbHeader(
        version=version, width_px=width, height_px=height,
        tile_w=tile_w, tile_h=tile_h, level_count=level_count,
        tile_count=tile_count, mpp_x=mpp_x, mpp_y=mpp_y,
        objective=objective, scanner_id=scanner_id,
        associated_count=associated_count, index_offset=index_offset,
        brightfield=bool(flags & 0x1))


def _parse_tile_index(mm, size, header, levels):
    tiles = []
    by_level = {}
    seen_cells = set()
    level_by_idx = {lv.level: lv for lv in levels}
    offset = header.index_offset
    for i in range(header.tile_count):
        (lvl, x_px, y_px, jpeg_w, jpeg_h,
         payload_offset, payload_length, _reserved) = \
            _TILE_ENTRY.unpack_from(mm, offset + i * _TILE_ENTRY.size)
        lv = level_by_idx.get(lvl)
        if lv is None:
            raise KfbError("invalid_tile_index",
                           "tile[%d] level=%d 越界（level_count=%d）"
                           % (i, lvl, header.level_count))
        if x_px % TILE_W or y_px % TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 坐标未按 %d 网格对齐" % (i, TILE_W))
        if jpeg_w < 1 or jpeg_w > TILE_W or jpeg_h < 1 or jpeg_h > TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] jpeg 尺寸 %d×%d 非法" % (i, jpeg_w, jpeg_h))
        if x_px + jpeg_w > lv.width or y_px + jpeg_h > lv.height:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 尺寸 %d×%d 越出层 %d 边界 %d×%d"
                           % (i, jpeg_w, jpeg_h, lvl, lv.width, lv.height))
        if not (MIN_PAYLOAD_LENGTH <= payload_length <= MAX_PAYLOAD_LENGTH):
            raise KfbError("invalid_tile_index",
                           "tile[%d] payload_length=%d 非法" % (i, payload_length))
        if _reserved != 0:
            raise KfbError("invalid_tile_index",
                           "tile[%d] reserved!=0" % i)
        cell = (lvl, y_px // TILE_H, x_px // TILE_W)
        if cell in seen_cells:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 网格单元 (%d,%d,%d) 重复"
                           % (i, cell[0], cell[1], cell[2]))
        seen_cells.add(cell)
        tile = KfbTile(level=lvl, x_px=x_px, y_px=y_px, jpeg_w=jpeg_w,
                       jpeg_h=jpeg_h, payload_offset=payload_offset,
                       payload_length=payload_length)
        tiles.append(tile)
        by_level.setdefault(lvl, []).append(tile)
    if len(tiles) != header.tile_count:  # 防御：不应可达
        raise KfbError("invalid_tile_index", "tile 数与 header 不符")
    return tiles, by_level


def _parse_associated_index(mm, size, header, assoc_offset):
    out = []
    for i in range(header.associated_count):
        base = assoc_offset + i * 48
        (name_raw, payload_offset, payload_length, width, height,
         _reserved) = _ASSOC_ENTRY.unpack_from(mm, base)
        _ASSOC_PAD.unpack_from(mm, base + _ASSOC_ENTRY.size)
        name = name_raw.split(b"\x00", 1)[0].decode("ascii", "replace")
        if name not in _ASSOCIATED_NAMES:
            raise KfbError("invalid_kfb_header",
                           "associated[%d] 名 %r 不在 %s"
                           % (i, name, sorted(_ASSOCIATED_NAMES)))
        if _reserved != 0:
            raise KfbError("invalid_kfb_header", "associated[%d] reserved!=0" % i)
        if width < 1 or height < 1 or width > MAX_DIM_PX or height > MAX_DIM_PX:
            raise KfbError("invalid_kfb_header",
                           "associated[%d] 尺寸 %d×%d 非法" % (i, width, height))
        if not (MIN_PAYLOAD_LENGTH <= payload_length <= MAX_PAYLOAD_LENGTH):
            raise KfbError("invalid_kfb_header",
                           "associated[%d] payload_length=%d 非法"
                           % (i, payload_length))
        out.append(KfbAssociated(name=name, payload_offset=payload_offset,
                                 payload_length=payload_length,
                                 width=width, height=height))
    return out


def _check_payload(mm, size, offset, length):
    """checked：offset+length 必须完整落在文件内。"""
    if offset < 0 or length < 0:
        raise KfbError("tile_payload_out_of_bounds",
                       "负 offset/length（%d,%d）" % (offset, length))
    end = offset + length
    if offset >= size or end > size:
        raise KfbError("tile_payload_out_of_bounds",
                       "payload [%d,%d) 越出文件长度 %d" % (offset, end, size))


def _check_jpeg_bounds(payload):
    """SOI..EOI 骨架检查（完整解码在转换期按需进行）。"""
    if len(payload) < 4 or payload[0:2] != b"\xff\xd8" \
            or payload[-2:] != b"\xff\xd9":
        raise KfbError("jpeg_decode_failed", "payload JPEG 骨架残缺（SOI/EOI）")
