# -*- coding: utf-8 -*-
"""江丰/KFBIO 荧光 KFBF 厂商布局（生产 parser，kfb_fl_v1 合同）。

由 4 份真实样本（KFFL02000113023，5 染料 + DAPI 共 6 通道，
SDC4/MDK/IL-1β/SOX10/CD68）校准；与明场 KFB（vendor_kfbio）同源的
tagged header，但 tile 存储为**每网格位置 6 通道灰度 JPEG 拼接**，
且深层金字塔几何不同。未知变体 fail-closed（不猜测）。

布局（little-endian，全部经真实样本逐字段验证）：

    0x00  8s   magic = f1 01 ee ee 4b 46 42 46（"....KFBF"）
    0x08  u32  version = 0
    0x0c  f32  格式版本 = 2.1
    0x10  u32  tile_count / 0x14 u32 height_px / 0x18 u32 width_px
    0x1c  u32  objective（整数倍率，40）
    0x20  4s   codec 必须为 JPEG
    0x2c  u32  扫描时间戳（unix epoch）
    0x34  u32  overview 关联图记录偏移 / 0x38 u32 label 记录偏移
    0x44  u64  tile_index_offset
    0x4c  f32  mpp（µm/px，level 0）
    0x58  u32  tile 边长 = 256
    0x5c       tagged 段：ff01eeee + u32 count + count × (u32 tag, u32 len,
               len 字节值)。关键 tag：
                 29  scanner_id（ASCII）
                 75  u32 channel_count
                 77  ptr → nch × 40B 通道名（NUL 填充）
                 79  ptr → nch × 12B 通道颜色（R/G/B 各 u32，值在低字节）
                 84  ptr → nch × f64 曝光（标定值 1..15，按毫秒解释）
                 87  ptr → nch × f64 gamma（标定值 1.0）

    tile_index 起连续 tile_count 条，每条 64B：
      4s f104eeee, u32 x_px, u32 y_px, u32 jpeg_w, u32 jpeg_h,
      f32 scale（= objective / 2^level；40=level0）, u32 0, u32 0,
      u32 len_ch0（= side[0]）, u32 va（指针块绝对偏移）,
      u32 0（明场为 0xffffffff 哨兵）, u32 side_off（side 记录绝对偏移）,
      u32 0, 0, 0, 4s ff04eeee

    va 处指针块 96B = 12 × u64：前 6 条为本 tile 各通道 JPEG payload 的
    绝对偏移；后 6 条为下一条 tile 的通道偏移（冗余回链，不使用）。
    side_off 处 side 记录 48B = 6 × u64：各通道 payload 长度。

    金字塔层级几何（与明场不同！）：
      L0        = header 宽×高（L0 底行内容可能被裁剪，缺失网格按黑填充）
      L1..L3    = floor 减半
      L4..L8    = ceil(L0/256) 精确乘 2^(8-L)（L8 = ceil(L0/256) 锚点）
      L9+       = 自 L8 起 floor 减半（max 1）
    层缺失判定：L>=1 的层要求 tile 网格 extent 与公式精确一致，否则
    invalid_tile_index（防公式误配静默错位）。

    关联图：overview（f102）/label（f103）记录各自 52B 头 + 内联 RGB JPEG；
    thumbnail 为文件末尾 52B 的 f102 记录，payload 经 *p1 二级间接（灰度）。

非可信二进制防御：所有 offset/length 显式与文件长度比较；上限复用
:mod:`kfb.parser` 常量。未知 magic/version/codec → ``unsupported_kfb_variant``。
"""

from __future__ import annotations

import math
import mmap
import os
import struct
from dataclasses import dataclass, field

from .errors import KfbError
from .parser import (
    HEADER_MIN_BYTES, MAX_DIM_PX, MAX_PAYLOAD_LENGTH, MAX_TILE_COUNT,
    MIN_PAYLOAD_LENGTH, MIN_TILE_COUNT, TILE_H, TILE_W,
    KfbAssociated, _check_jpeg_bounds, _check_payload,
)

#: 固定 magic（"....KFBF"，与明场 KFB 仅末字节 0x46/0x00 之差）
KFBF_MAGIC = bytes.fromhex("f101eeee4b464246")

#: 标定格式版本（0x0c f32；4 份样本一致 2.1）
FORMAT_VERSION = 2.1
_FORMAT_VERSION_EPS = 1e-4

MAX_CHANNEL_COUNT = 16
MAX_LEVEL_COUNT = 24
MAX_TAG_COUNT = 64
MAX_TAG_VALUE = 4096

CHANNEL_NAME_BYTES = 40      # tag77：每通道名 40B NUL 填充
_CHANNEL_COLOR_BYTES = 12    # tag79：R/G/B 各 u32（值在低字节）

_TILE_REC = struct.Struct("<4s" + "I" * 4 + "f" + "I" * 9 + "4s")
_TILE_HEAD = bytes.fromhex("f104eeee")
_TILE_TAIL = bytes.fromhex("ff04eeee")
_PTR_BLOCK_BYTES = 96        # 12 × u64
_ASSOC_HEAD = struct.Struct("<4sIIIII")
_ASSOC_REC_BYTES = 52
_TAGGED_MAGIC = bytes.fromhex("ff01eeee")


@dataclass(frozen=True)
class KfbfChannel:
    """单通道元数据（索引序 = 文件存储序 = tag77 名称序）。"""

    index: int
    name: str
    color_rgb: tuple          # (r, g, b) 0..255
    exposure: float           # 原始标定值（按毫秒解释，见 manifest 警告）
    gamma: float


@dataclass(frozen=True)
class KfbfHeader:
    """kfb_fl_v1 header（已校验）。"""

    width_px: int
    height_px: int
    objective: float
    tile_count: int
    channel_count: int
    mpp: float
    scanner_id: str
    index_offset: int
    scanned_at: int           # unix 时间戳（0=未知）


@dataclass(frozen=True)
class KfbfLevel:
    """层级几何（公式见模块 docstring；width/height 为该层画布尺寸）。"""

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
class KfbfTile:
    """tile 索引条目（一个网格位置 × 全部通道）。

    ``ptr_offset`` 指向 96B 指针块（前 6 条为各通道 payload 绝对偏移）；
    ``lengths`` 为各通道 payload 长度（side 记录，已校验与 len_ch0 一致）。
    """

    level: int
    x_px: int
    y_px: int
    jpeg_w: int
    jpeg_h: int
    ptr_offset: int
    payload_offsets: tuple    # nch × u64
    lengths: tuple            # nch × u64

    @property
    def col(self) -> int:
        return self.x_px // TILE_W

    @property
    def row(self) -> int:
        return self.y_px // TILE_H

    def cell_size(self, lv) -> tuple:
        """该网格 cell 在 TIFF 画布上的尺寸（右/底边裁剪）。"""
        return (min(TILE_W, lv.width - self.x_px),
                min(TILE_H, lv.height - self.y_px))

    def is_full_cell(self, lv) -> bool:
        """JPEG 尺寸恰好等于 cell 尺寸（可直接透传）。"""
        return (self.jpeg_w, self.jpeg_h) == self.cell_size(lv)


@dataclass
class KfbfDocument:
    """解析结果：header + 通道元数据 + 层级几何 + tile 索引 + mmap。"""

    header: KfbfHeader
    channels: list            # [KfbfChannel] × channel_count
    levels: list              # [KfbfLevel]，level 连续 0..N-1
    tiles: list = field(default_factory=list)
    tiles_by_level: dict = field(default_factory=dict)
    associated: list = field(default_factory=list)
    source_size: int = 0
    _mmap: object = None

    def channel_payload(self, tile, channel) -> bytes:
        """读取 tile 指定通道的 JPEG payload 字节（mmap 切片）。"""
        if self._mmap is None:
            raise KfbError("invalid_kfb_header", "文档已关闭")
        off = tile.payload_offsets[channel]
        length = tile.lengths[channel]
        return bytes(self._mmap[off:off + length])

    def associated_payload(self, item) -> bytes:
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
# 层级几何公式（kfb_fl_v1，真实样本校准）
# --------------------------------------------------------------------------- #
def level_dimensions(width, height, level):
    """kfb_fl_v1 层级画布尺寸。

    L0 = header；L1..L3 = floor 减半；L4..L8 = ceil(L0/256) × 2^(8-L)；
    L9+ = 自 L8 floor 减半（max 1）。
    """
    if level == 0:
        return width, height
    if level <= 3:
        return max(1, width >> level), max(1, height >> level)
    w8 = (width + TILE_W - 1) // TILE_W
    h8 = (height + TILE_H - 1) // TILE_H
    if level <= 8:
        return w8 << (8 - level), h8 << (8 - level)
    lw, lh = w8, h8
    for _ in range(level - 8):
        lw = max(1, lw >> 1)
        lh = max(1, lh >> 1)
    return lw, lh


def looks_like_kfbf(mm, size) -> bool:
    """magic 匹配 + version==0 + codec=JPEG + tile=256（不解析全 header）。"""
    if size < HEADER_MIN_BYTES:
        return False
    if bytes(mm[0:8]) != KFBF_MAGIC:
        return False
    version, = struct.unpack_from("<I", mm, 0x08)
    if version != 0:
        return False
    codec = bytes(mm[0x20:0x24])
    tile, = struct.unpack_from("<I", mm, 0x58)
    return codec == b"JPEG" and tile == TILE_W


def parse_kfbf(path):
    """解析 kfb_fl_v1 文件，返回 :class:`KfbfDocument`（mmap 只读）。

    失败抛 :class:`KfbError`（稳定码）。调用方负责 ``doc.close()``
    （或用 with 语句）。
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
    doc = None
    try:
        header, tags = _parse_header(mm, size)
        channels = _parse_channels(mm, size, header, tags)
        levels, tiles, by_level = _parse_tile_index(mm, size, header)
        associated = _parse_associated(mm, size)
        doc = KfbfDocument(
            header=header, channels=channels, levels=levels, tiles=tiles,
            tiles_by_level=by_level, associated=associated,
            source_size=size, _mmap=mm)
    except Exception:
        mm.close()
        fh.close()
        raise
    fh.close()  # mmap 持有文件引用
    return doc


# --------------------------------------------------------------------------- #
# header + tagged 段
# --------------------------------------------------------------------------- #
def _parse_header(mm, size):
    if bytes(mm[0:8]) != KFBF_MAGIC:
        raise KfbError("unsupported_kfb_variant", "未知 KFBF magic")
    version, = struct.unpack_from("<I", mm, 0x08)
    if version != 0:
        raise KfbError("unsupported_kfb_variant",
                       "不支持的 KFBF version=%r（仅支持 0）" % (version,))
    fmt_version, = struct.unpack_from("<f", mm, 0x0C)
    if abs(fmt_version - FORMAT_VERSION) > _FORMAT_VERSION_EPS:
        raise KfbError("unsupported_kfb_variant",
                       "不支持的 KFBF 格式版本 %r（标定 %.1f）"
                       % (fmt_version, FORMAT_VERSION))
    tile_count, height, width, objective = struct.unpack_from("<IIII", mm, 0x10)
    if not (MIN_TILE_COUNT <= tile_count <= MAX_TILE_COUNT):
        raise KfbError("invalid_kfb_header", "tile_count=%d 越界" % tile_count)
    if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX):
        raise KfbError("invalid_kfb_header", "宽/高越界 (%d,%d)" % (width, height))
    if not (1 <= objective <= 100):
        raise KfbError("invalid_kfb_header", "objective=%d 非法" % objective)
    if bytes(mm[0x20:0x24]) != b"JPEG":
        raise KfbError("unsupported_kfb_variant", "非 JPEG 荧光 KFBF")
    scanned_at, = struct.unpack_from("<I", mm, 0x2C)
    index_offset, = struct.unpack_from("<Q", mm, 0x44)
    mpp, = struct.unpack_from("<f", mm, 0x4C)
    if not math.isfinite(mpp) or mpp <= 0:
        raise KfbError("invalid_kfb_header", "mpp=%r 非法" % (mpp,))
    tile_size, = struct.unpack_from("<I", mm, 0x58)
    if tile_size != TILE_W:
        raise KfbError("invalid_kfb_header", "tile 尺寸必须为 %d" % TILE_W)
    index_bytes = tile_count * _TILE_REC.size
    if index_offset < HEADER_MIN_BYTES or index_offset + index_bytes > size:
        raise KfbError("invalid_tile_index",
                       "index_offset=%d 非法" % index_offset)

    tags = _parse_tagged(mm, size)
    raw_nch = tags.get(75)
    if raw_nch is None or len(raw_nch) != 4:
        raise KfbError("invalid_kfb_header", "tag 75（channel_count）缺失")
    channel_count, = struct.unpack("<I", raw_nch)
    if not (1 <= channel_count <= MAX_CHANNEL_COUNT):
        raise KfbError("invalid_kfb_header",
                       "channel_count=%d 越界" % channel_count)
    scanner_id = ""
    if 29 in tags:
        scanner_id = tags[29].split(b"\x00", 1)[0].decode("ascii", "replace")
    header = KfbfHeader(
        width_px=width, height_px=height, objective=float(objective),
        tile_count=tile_count, channel_count=channel_count, mpp=float(mpp),
        scanner_id=scanner_id, index_offset=index_offset,
        scanned_at=scanned_at)
    return header, tags


def _parse_tagged(mm, size):
    """0x5c tagged 段 → {tag: value_bytes}。结构性损坏 → invalid_kfb_header。"""
    if size < 0x68 or bytes(mm[0x5C:0x60]) != _TAGGED_MAGIC:
        raise KfbError("invalid_kfb_header", "缺 tagged 段（0x5c ff01eeee）")
    count, = struct.unpack_from("<I", mm, 0x60)
    if count > MAX_TAG_COUNT:
        raise KfbError("invalid_kfb_header", "tagged 段过长（%d）" % count)
    tags = {}
    off = 0x64
    for _ in range(count):
        if off + 8 > size:
            raise KfbError("invalid_kfb_header", "tagged 段截断")
        tag, length = struct.unpack_from("<II", mm, off)
        off += 8
        if length > MAX_TAG_VALUE or off + length > size:
            raise KfbError("invalid_kfb_header", "tagged 值越界")
        tags[tag] = bytes(mm[off:off + length])
        off += length
    return tags


def _tag_ptr(mm, size, tags, tag, want_len):
    """取 tag 的 u64 指针并校验目标区在文件内，返回数据字节。"""
    raw = tags.get(tag)
    if raw is None or len(raw) != 8:
        raise KfbError("metadata_missing_required",
                       "tag %d 缺失/形态非法" % tag)
    ptr, = struct.unpack("<Q", raw)
    if ptr + want_len > size:
        raise KfbError("invalid_kfb_header",
                       "tag %d 数据区 [%d,%d) 越界" % (tag, ptr, ptr + want_len))
    return bytes(mm[ptr:ptr + want_len])


def _parse_channels(mm, size, header, tags):
    nch = header.channel_count
    names_raw = _tag_ptr(mm, size, tags, 77, nch * CHANNEL_NAME_BYTES)
    colors_raw = _tag_ptr(mm, size, tags, 79, nch * _CHANNEL_COLOR_BYTES)
    expo_raw = _tag_ptr(mm, size, tags, 84, nch * 8)
    gamma_raw = tags.get(87)
    gammas = None
    if gamma_raw is not None and len(gamma_raw) == 8:
        gptr, = struct.unpack("<Q", gamma_raw)
        if gptr + nch * 8 <= size:
            gammas = struct.unpack_from("<%dd" % nch, mm, gptr)

    channels = []
    for c in range(nch):
        name = names_raw[c * CHANNEL_NAME_BYTES:(c + 1) * CHANNEL_NAME_BYTES]
        name = name.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if not name:
            raise KfbError("metadata_missing_required", "通道 %d 名为空" % c)
        r, g, b = struct.unpack_from("<III", colors_raw, c * _CHANNEL_COLOR_BYTES)
        for v in (r, g, b):
            if v > 255:
                raise KfbError("invalid_kfb_header",
                               "通道 %d 颜色分量 %d 越界" % (c, v))
        exposure, = struct.unpack_from("<d", expo_raw, c * 8)
        if not (exposure == exposure) or exposure <= 0:
            raise KfbError("metadata_missing_required",
                           "通道 %d 曝光 %r 非法" % (c, exposure))
        gamma = 1.0
        if gammas is not None:
            gamma = float(gammas[c])
            if not (gamma == gamma) or gamma <= 0:
                gamma = 1.0
        channels.append(KfbfChannel(
            index=c, name=name, color_rgb=(r, g, b),
            exposure=float(exposure), gamma=gamma))
    return channels


# --------------------------------------------------------------------------- #
# tile 索引
# --------------------------------------------------------------------------- #
def _parse_tile_index(mm, size, header):
    nch = header.channel_count
    tiles = []
    by_level = {}
    seen = set()
    max_level = -1
    for i in range(header.tile_count):
        (magic, x_px, y_px, jpeg_w, jpeg_h, scale, z0, z1, len_ch0, va,
         sent, side_off, r1, r2, r3, tail) = \
            _TILE_REC.unpack_from(mm, header.index_offset + i * _TILE_REC.size)
        if magic != _TILE_HEAD or tail != _TILE_TAIL:
            raise KfbError("invalid_tile_index", "tile[%d] 记录 magic 非法" % i)
        if z0 or z1 or sent or r1 or r2 or r3:
            raise KfbError("invalid_tile_index", "tile[%d] 保留字段非零" % i)
        if not math.isfinite(scale) or scale <= 0:
            raise KfbError("invalid_tile_index", "tile[%d] scale=%r" % (i, scale))
        # scale 必须是 objective/2^level（在 f32 容差内）
        level = int(round(math.log2(header.objective / scale)))
        if level < 0 or level > MAX_LEVEL_COUNT \
                or abs(header.objective / (2.0 ** level) - scale) \
                > max(1e-7, scale * 1e-5):
            raise KfbError("invalid_tile_index",
                           "tile[%d] scale=%r 不是 objective/2^level"
                           % (i, scale))
        lw, lh = level_dimensions(header.width_px, header.height_px, level)
        if x_px % TILE_W or y_px % TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 坐标未按 %d 网格对齐" % (i, TILE_W))
        if jpeg_w < 1 or jpeg_w > TILE_W or jpeg_h < 1 or jpeg_h > TILE_H:
            raise KfbError("invalid_tile_index",
                           "tile[%d] jpeg 尺寸 %d×%d 非法" % (i, jpeg_w, jpeg_h))
        if x_px + jpeg_w > lw or y_px + jpeg_h > lh:
            raise KfbError("invalid_tile_index",
                           "tile[%d] %d×%d 越出层 %d 边界 %d×%d"
                           % (i, jpeg_w, jpeg_h, level, lw, lh))
        cell = (level, y_px // TILE_H, x_px // TILE_W)
        if cell in seen:
            raise KfbError("invalid_tile_index",
                           "tile[%d] 网格单元 %r 重复" % (i, cell))
        seen.add(cell)

        # side 记录：nch × u64 长度；首条必须等于记录的 len_ch0
        _check_payload(mm, size, side_off, 8 * nch)
        lengths = struct.unpack_from("<%dQ" % nch, mm, side_off)
        if lengths[0] != len_ch0:
            raise KfbError("invalid_tile_index",
                           "tile[%d] len_ch0=%d 与 side[0]=%d 不一致"
                           % (i, len_ch0, lengths[0]))
        for c, ln in enumerate(lengths):
            if not (MIN_PAYLOAD_LENGTH <= ln <= MAX_PAYLOAD_LENGTH):
                raise KfbError("invalid_tile_index",
                               "tile[%d] 通道 %d 长度 %d 非法" % (i, c, ln))

        # 指针块：前 nch 条为本 tile 各通道 payload 绝对偏移
        _check_payload(mm, size, va, _PTR_BLOCK_BYTES)
        if nch * 8 > _PTR_BLOCK_BYTES:
            raise KfbError("invalid_tile_index",
                           "channel_count=%d 超出指针块容量" % nch)
        offsets = struct.unpack_from("<%dQ" % nch, mm, va)
        for c in range(nch):
            _check_payload(mm, size, offsets[c], lengths[c])
            _check_jpeg_bounds(mm[offsets[c]:offsets[c] + lengths[c]])

        tile = KfbfTile(level=level, x_px=x_px, y_px=y_px,
                        jpeg_w=jpeg_w, jpeg_h=jpeg_h, ptr_offset=va,
                        payload_offsets=offsets, lengths=lengths)
        tiles.append(tile)
        by_level.setdefault(level, []).append(tile)
        max_level = max(max_level, level)

    if max_level < 0:
        raise KfbError("invalid_tile_index", "无任何 tile")
    # 层必须连续 0..max_level（中间缺整层 fail clearly）
    levels = []
    for lvl in range(max_level + 1):
        lw, lh = level_dimensions(header.width_px, header.height_px, lvl)
        lv = KfbfLevel(level=lvl, width=lw, height=lh)
        if lvl not in by_level:
            raise KfbError("conversion_validation_failed",
                           "层 %d 在索引中无任何 tile" % lvl)
        levels.append(lv)
    # L>=1：tile 网格 extent 必须与公式精确一致（防公式误配静默错位）
    for lvl in range(1, max_level + 1):
        lv = levels[lvl]
        ext_w = max(t.x_px + t.jpeg_w for t in by_level[lvl])
        ext_h = max(t.y_px + t.jpeg_h for t in by_level[lvl])
        if (ext_w, ext_h) != (lv.width, lv.height):
            raise KfbError(
                "invalid_tile_index",
                "层 %d 网格 extent %d×%d 与公式 %d×%d 不一致"
                % (lvl, ext_w, ext_h, lv.width, lv.height))
    return levels, tiles, by_level


# --------------------------------------------------------------------------- #
# 关联图：overview/label（header 指针 + 内联 JPEG）与 thumbnail（EOF 间接）
# --------------------------------------------------------------------------- #
def _parse_associated(mm, size):
    out = []
    overview_off, label_off = struct.unpack_from("<II", mm, 0x34)
    for name, rec_off in (("overview", overview_off), ("label", label_off)):
        item = _parse_inline_assoc(mm, size, rec_off)
        if item is not None:
            out.append((name, item))
    thumb = _parse_eof_thumbnail(mm, size)
    if thumb is not None:
        out.append(("thumbnail", thumb))
    return [KfbAssociated(name=name, payload_offset=item[0],
                          payload_length=item[1], width=item[2],
                          height=item[3])
            for name, item in out]


def _parse_inline_assoc(mm, size, rec_off):
    """f102/f103 记录（52B）+ 内联 JPEG。返回 (payload_off, len, w, h)。"""
    if rec_off < HEADER_MIN_BYTES or rec_off + _ASSOC_REC_BYTES > size:
        return None
    magic = bytes(mm[rec_off:rec_off + 4])
    if magic not in (bytes.fromhex("f102eeee"), bytes.fromhex("f103eeee")):
        return None
    tail = bytes(mm[rec_off + 48:rec_off + 52])
    if tail != bytes.fromhex("ff02eeee") and tail != bytes.fromhex("ff03eeee"):
        return None
    _mg, _id, height, width, _typ, jlen = _ASSOC_HEAD.unpack_from(mm, rec_off)
    payload_off = rec_off + _ASSOC_REC_BYTES
    if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX
            and MIN_PAYLOAD_LENGTH <= jlen <= MAX_PAYLOAD_LENGTH
            and payload_off + jlen <= size):
        return None
    try:
        _check_jpeg_bounds(mm[payload_off:payload_off + jlen])
    except KfbError:
        return None
    return (payload_off, jlen, width, height)


def _parse_eof_thumbnail(mm, size):
    """文件末尾 52B 的 f102 记录；payload 经 *p1 二级间接。"""
    if size < _ASSOC_REC_BYTES:
        return None
    rec_off = size - _ASSOC_REC_BYTES
    if bytes(mm[rec_off:rec_off + 4]) != bytes.fromhex("f102eeee"):
        return None
    if bytes(mm[rec_off + 48:rec_off + 52]) != bytes.fromhex("ff02eeee"):
        return None
    _mg, _id, height, width, _typ, jlen = _ASSOC_HEAD.unpack_from(mm, rec_off)
    p1, _p2 = struct.unpack_from("<QQ", mm, rec_off + 24)
    if not (1 <= width <= MAX_DIM_PX and 1 <= height <= MAX_DIM_PX
            and MIN_PAYLOAD_LENGTH <= jlen <= MAX_PAYLOAD_LENGTH
            and p1 + 8 <= size):
        return None
    payload_off, = struct.unpack_from("<Q", mm, p1)
    if payload_off + jlen > size:
        return None
    try:
        _check_jpeg_bounds(mm[payload_off:payload_off + jlen])
    except KfbError:
        return None
    return (payload_off, jlen, width, height)
