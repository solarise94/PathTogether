# -*- coding: utf-8 -*-
"""单层大切片的服务端金字塔构建（摄取期 / 回填共用）。

背景（生产实况）：用户会把只有单一分辨率层的大 TIFF/OME-TIFF 直接上传
（12161×9621 / 9101×6641 RGB 8-bit）。viewer 的 OpenSlide DeepZoom 与 AI
region 路径（``slide_render.choose_read_level``）在单层切片上都只能解码
level 0；即使输出图只有 1024px，也要全量解码原图，AI 也只能看 level-0
窗口。本模块在**服务端**把这类单层文件重写为带 SubIFD 缩减层的分块
OME-TIFF，使 viewer/AI 能用真正的低分辨率层看「当前小区域 + 全片状态」。

策略（可调常量见下）：

- **触发**：``level_count == 1`` 且长边 > ``PYRAMID_MIN_LONG_EDGE``（默认
  4096）。``INGEST_PYRAMID_ENABLED=0`` 关闭；``INGEST_PYRAMID_MIN_LONG_EDGE``
  覆盖阈值。
- **层数**：每级 2× 面积平均下采样，直到长边 ≤ ``PYRAMID_TARGET_LONG_EDGE``
  （默认 **1024**）。最后一层仍保留整片概览，AI 一次读 1024px 即可获得
  全片状态；比继续降到 512 更省层数/体积，且 1024 已是 AI 输出上限。
- **分块**：512×512；不足一块的边缘 tile 按实际尺寸写。
- **压缩**：
  * 无损源（none/LZW/deflate/zstd/lzma）→ level 0 用 deflate（无损重压缩）；
  * JPEG 源（photometric=YCbCr + compression=JPEG，RGB 8-bit）→ level 0
    以 JPEG quality 95 重编码（**有损**，但只此一次；Pillow 编码 4:4:4）。
    灰度 JPEG 解码后用 deflate 保存解码结果；未知压缩一律跳过，不猜
    有损/无损。
  * 缩减层：RGB 8-bit 用 JPEG q90；其他（uint16/多通道/灰度）用 deflate。
- **流式/内存**：level 0 的 tile 生成器逐块读源（zarr/tifffile），同时把
  2× 下采样写进同级目录的临时 zarr store（level 1）；随后逐级从上一级
  store 生成下一级。最终用 tifffile 的 ``tile=`` 生成器写 SubIFD。峰值
  内存只与 tile/块有关（512² × 通道 × dtype），与全图尺寸无关（临时
  zarr 文件页不计入进程 RSS，不持整级数组）。
- **硬上限**（任一命中 → 跳过并保留原文件，``PyramidSkip.reason``）：
  * ``width × height > 2^32``；
  * 预估输出 > 3× 源文件（写出后再按实际大小复核）；
  * 目标盘 scratch 空间不足（临时 zarr + 预估输出 + 安全余量）；
  * 不支持的 axes/dtype/压缩形态（如 T/Z>1、4 通道 RGBA、JPEG2000）。
- **物理尺寸/元数据**：优先沿用 OME-XML ``PhysicalSizeX/Y``（µm）；非 OME
  源从 TIFF Resolution 标签换算；通道名/颜色从 OME ``Channel`` 复制。
- **原子性**：本模块只写调用方给出的目标路径。调用方（worker/回填脚本）
  在同一目录生成临时文件、校验后原子替换，并负责记账/内容 revision。

不在本模块做的事：数据库记账、事件、租约续期、文件替换。这些由
``cos_ingest_worker`` 与 ``scripts/build_missing_pyramids.py`` 按各自的
失败语义处理；任何构建失败都必须回退到原单层文件。
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np

#: 触发阈值：单层且长边大于该值才构建（环境变量覆盖）。
PYRAMID_MIN_LONG_EDGE = 4096
#: 最小层目标长边：缩减到 ≤ 该值即停止（最后一层即全片概览）。
PYRAMID_TARGET_LONG_EDGE = 1024
#: TIFF tile 尺寸（与 DeepZoom 512 对齐；单 tile 内存有界）。
PYRAMID_TILE = 512
#: JPEG 源 level 0 的重编码质量（仅此一次有损）。
PYRAMID_JPEG_QUALITY_LEVEL0 = 95
#: RGB 8-bit 缩减层的 JPEG 质量。
PYRAMID_JPEG_QUALITY_SUBLEVEL = 90
#: level 0 主图/缩减层像素总量上限（2^32；Cover 全仓既有硬界限口径）。
PYRAMID_MAX_PIXELS = 1 << 32
#: 预估/实际输出相对源文件的增长上限。
PYRAMID_MAX_GROWTH_RATIO = 3.0
#: scratch 盘最低安全余量。
PYRAMID_MIN_FREE_BYTES = 256 * 1024 * 1024
#: tifffile 编码缓冲上限（默认 BUFFERSIZE=256MB × 编码输出 ≈ 512MB 峰值；
#: 显式压到 32MB，保证 50k×50k 输出时进程 RSS 仍 < 512MB）。
PYRAMID_WRITE_BUFFERSIZE = 32 * 1024 * 1024

#: 认定为无损的 TIFF compression 枚举（tifffile Compression 值）。
_LOSSLESS_COMPRESSIONS = frozenset({
    1,       # NONE
    5,       # LZW
    8,       # DEFLATE（旧 Adobe 编号）
    32946,   # DEFLATE
    34925,   # LZMA
    50000,   # ZSTD
})
_COMPRESSION_JPEG = 7
_PHOTOMETRIC_RGB = 2
_PHOTOMETRIC_YCBCR = 6

_SUPPORTED_DTYPES = frozenset(("uint8", "uint16", "float32"))

#: 可重写为 OME-TIFF 的源后缀。PT 对 .tif/.tiff（含 .ome.tiff）走 OME 优先 /
#: TiffFileSlide；.svs/.ndpi 等厂商后缀在 OpenSlide 成功时优先 OpenSlide，
#: 替换后反而读不到 SubIFD 金字塔——因此不构建（保留原文件）。
_BUILDABLE_SUFFIXES = (".ome.tiff", ".ome.tif", ".tiff", ".tif")

#: 写成 OME-TIFF 时使用的物理尺寸单位（与 OME 默认一致）。
_MPP_UNIT = "µm"


class PyramidSkip(Exception):
    """确定性的「不构建」判定（保留原文件，不是故障）。

    ``reason`` 是稳定的机器词表（可进 ingestion_events detail）：

    - ``disabled`` / ``below_threshold`` / ``already_multilevel``
    - ``pixel_count_cap`` / ``estimated_output_cap`` /
      ``output_size_cap`` / ``insufficient_scratch``
    - ``unsupported_source_compression`` / ``unsupported_lossy_layout``
    - ``unsupported_axes`` / ``unsupported_dtype`` / ``unsupported_samples``
    - ``source_unreadable`` / ``deadline`` / ``lease_lost``
    - ``quota_exceeded``（调用方判定，构建后替换前）
    """

    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = str(reason)


class PyramidError(RuntimeError):
    """构建/校验失败（调用方回退原文件；不得让摄取失败）。"""


class PyramidValidationError(PyramidError):
    """输出未通过 PT 自身读取路径的校验。"""


@dataclass(frozen=True)
class PyramidResult:
    """一次成功构建的可记账结果（不含临时路径）。"""

    levels: int
    level_dimensions: tuple
    seconds: float
    bytes_before: int
    bytes_after: int
    source_lossless: bool
    level0_compression: str
    sublevel_compression: str
    method: str = "pyramid-l0-box2-subifd"
    estimated_bytes: int = 0
    bigtiff: bool = False
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 配置/触发判定
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)


def pyramid_enabled() -> bool:
    """``INGEST_PYRAMID_ENABLED``（默认开；``0``/false 关闭）。"""
    raw = os.environ.get("INGEST_PYRAMID_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def min_long_edge() -> int:
    """触发阈值（``INGEST_PYRAMID_MIN_LONG_EDGE`` 覆盖）。"""
    return max(1, _env_int("INGEST_PYRAMID_MIN_LONG_EDGE", PYRAMID_MIN_LONG_EDGE))


def _slide_info_values(slide_info):
    """从 dict / 对象 / 路径解析 (level_count, (width, height))。"""
    if isinstance(slide_info, (str, bytes, os.PathLike)):
        import slide_io

        slide = slide_io.open_slide(os.fspath(slide_info))
        try:
            return (int(getattr(slide, "level_count", 0) or 0),
                    tuple(int(v) for v in slide.dimensions))
        finally:
            try:
                slide.close()
            except Exception:  # noqa: BLE001
                pass
    if isinstance(slide_info, dict):
        get = slide_info.get
    else:
        def get(key, default=None):
            return getattr(slide_info, key, default)

    level_count = get("level_count")
    if level_count is None:
        level_count = get("levelCount")
    dims = get("dimensions")
    if dims is None:
        w = get("width")
        h = get("height")
        if w is not None and h is not None:
            dims = (w, h)
    if dims is None:
        raise ValueError("slide_info 缺少 dimensions/width/height")
    try:
        return int(level_count or 0), (int(dims[0]), int(dims[1]))
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("slide_info 形态非法：%r" % (slide_info,)) from exc


def is_buildable_source(path) -> bool:
    """源文件名是否为可安全重写为 OME-TIFF 的后缀（大小写不敏感）。"""
    name = os.path.basename(os.fspath(path)).lower()
    return any(name.endswith(suffix) for suffix in _BUILDABLE_SUFFIXES)


def needs_pyramid(slide_info) -> bool:
    """是否需要服务端金字塔。

    ``slide_info`` 可为 ``{"level_count": 1, "dimensions": (w, h)}`` 形态的
    dict、带同名字属性的对象，或切片路径（按 slide_io 试开）。判定只看
    ``level_count == 1`` 且长边 > 阈值；是否真的能构建由 :func:`build_pyramid`
    的格式/上限检查决定。
    """
    if not pyramid_enabled():
        return False
    try:
        level_count, (width, height) = _slide_info_values(slide_info)
    except Exception:  # noqa: BLE001  探测失败按「不需要」处理，绝不误伤
        return False
    if level_count != 1:
        return False
    return max(int(width), int(height)) > min_long_edge()


def level_dimensions_for(width, height, *, target=PYRAMID_TARGET_LONG_EDGE):
    """2× 下采样层尺寸序列（首项为 level 0），长边 ≤ target 停止。"""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError("尺寸必须为正")
    out = [(width, height)]
    while max(width, height) > int(target):
        width = (width + 1) // 2
        height = (height + 1) // 2
        out.append((width, height))
    return out


# --------------------------------------------------------------------------- #
# 源文件布局解析
# --------------------------------------------------------------------------- #
@dataclass
class _SourceLayout:
    axes: str          # 输出 axes：'YXS' | 'CYX' | 'YX'
    height: int
    width: int
    channels: int      # RGB=3；CYX=C；灰度=1
    dtype: str
    native_rgb: bool
    lossless: bool
    jpeg_source: bool
    mpp_x: float | None
    mpp_y: float | None
    objective_power: float | None
    channel_names: list
    channel_colors: list
    source_axes: str
    source_shape: tuple
    source_compression: int
    source_photometric: int


def _pick_series(tif):
    """主图 series 选择（与 slide_io.TiffFileSlide 同口径：YX 面积、层数）。"""
    best = 0
    best_score = (-1, -1)
    for i, series in enumerate(tif.series):
        axes = series.axes or ""
        shape = series.shape or ()
        area = 0
        try:
            if "Y" in axes and "X" in axes:
                area = int(shape[axes.index("Y")]) * int(shape[axes.index("X")])
        except (IndexError, TypeError, ValueError):
            area = 0
        if area <= 0 and len(shape) >= 2:
            try:
                area = int(shape[-2]) * int(shape[-1])
            except (IndexError, TypeError, ValueError):
                area = 0
        try:
            nlevels = len(series.levels) if series.levels else 1
        except Exception:  # noqa: BLE001
            nlevels = 1
        score = (int(area), int(nlevels))
        if score > best_score:
            best, best_score = i, score
    return best, tif.series[best]


def _rational_value(tag):
    """TIFF RATIONAL/SRATIONAL 标签 → float（缺失/非法返回 None）。"""
    if tag is None:
        return None
    value = getattr(tag, "value", tag)
    try:
        if isinstance(value, (tuple, list)):
            if len(value) == 2:
                num, den = float(value[0]), float(value[1])
                return num / den if den else None
            return float(value[0]) if value else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _source_mpp(tif, series):
    """解析源物理像素尺寸（µm）：OME PhysicalSize 优先，其次 Resolution 标签。"""
    mpp_x = mpp_y = None
    objective = None
    try:
        ome_xml = tif.ome_metadata if tif.is_ome else None
    except Exception:  # noqa: BLE001
        ome_xml = None
    if ome_xml:
        import slide_io

        mpp_x, mpp_y, objective = slide_io._parse_ome_mpp(ome_xml)  # noqa: SLF001
        # OME 元数据是权威：只要给出了任一 PhysicalSize，就不再回落到 TIFF
        # Resolution 标签（否则会把缺失的一轴编造成标签默认值）。
        if mpp_x is not None or mpp_y is not None:
            return mpp_x, mpp_y, objective
    try:
        page = series.pages[0]
        tags = page.tags
        xres = _rational_value(tags.get("XResolution"))
        yres = _rational_value(tags.get("YResolution"))
        unit = tags.get("ResolutionUnit")
        unit = getattr(unit, "value", unit)
        if xres and xres > 0 and yres and yres > 0:
            if int(unit or 2) == 3:      # centimeter
                tx, ty = 10000.0 / xres, 10000.0 / yres
            else:                        # inch（TIFF 默认）
                tx, ty = 25400.0 / xres, 25400.0 / yres
            if mpp_x is None:
                mpp_x = tx
            if mpp_y is None:
                mpp_y = ty
    except Exception:  # noqa: BLE001
        pass
    return mpp_x, mpp_y, objective


def _source_channels(tif, series_index=0):
    """OME Channel 元数据（名称/颜色）；非 OME/解析失败返回 ([], [])。"""
    names, colors = [], []
    try:
        ome_xml = tif.ome_metadata if tif.is_ome else None
    except Exception:  # noqa: BLE001
        ome_xml = None
    if not ome_xml:
        return names, colors
    import slide_io

    try:
        channels = slide_io._parse_ome_channels(  # noqa: SLF001
            ome_xml, int(series_index)) or []
    except Exception:  # noqa: BLE001
        channels = []
    for ch in channels:
        name = (ch or {}).get("name")
        color = (ch or {}).get("color")
        names.append(str(name) if name else "")
        colors.append(str(color) if color else "")
    return names, colors


def _source_layout(tif, series, *, level_count, series_index=0):
    """把 series axes/shape 收敛为受支持的三类布局；否则 PyramidalSkip。"""
    if level_count > 1:
        raise PyramidSkip("already_multilevel")
    axes = str(series.axes or "")
    shape = tuple(int(v) for v in (series.shape or ()))
    if "Y" not in axes or "X" not in axes:
        raise PyramidSkip("unsupported_axes", "缺少 Y/X 轴：%r" % axes)
    iy, ix = axes.index("Y"), axes.index("X")
    height, width = int(shape[iy]), int(shape[ix])
    dtype = str(getattr(series, "dtype", "") or "")
    if dtype not in _SUPPORTED_DTYPES:
        raise PyramidSkip("unsupported_dtype", "dtype=%s" % dtype)

    page = series.pages[0]
    # 注意：tifffile 的 COMPRESSION.NONE 是 IntEnum 但 bool() 为 False，
    # 不能用 ``x or 0`` 取值（会把未压缩误判成未知压缩 0）。
    try:
        compression = int(page.compression)
    except (TypeError, ValueError):
        compression = 0
    try:
        photometric = int(page.photometric)
    except (TypeError, ValueError):
        photometric = 0
    jpeg_source = compression == _COMPRESSION_JPEG
    lossless = compression in _LOSSLESS_COMPRESSIONS
    if not (jpeg_source or lossless):
        raise PyramidSkip("unsupported_source_compression",
                          "compression=%d" % compression)

    has_c = "C" in axes
    has_s = "S" in axes
    if has_c and has_s:
        raise PyramidSkip("unsupported_axes", "同时存在 C 与 S：%r" % axes)

    if has_s:
        samples = int(shape[axes.index("S")])
        if samples != 3:
            raise PyramidSkip("unsupported_samples",
                              "S=%d（仅支持 3 通道 RGB）" % samples)
        if "C" in axes:
            raise PyramidSkip("unsupported_axes", "C 与 S 并存：%r" % axes)
        out_axes = "YXS"
        channels = 3
        native_rgb = photometric in (_PHOTOMETRIC_RGB,
                                     _PHOTOMETRIC_YCBCR)
        if photometric == _PHOTOMETRIC_YCBCR and not jpeg_source:
            # YCbCr 只有 JPEG 解码路径才转 RGB；其它压缩下按原始 YCbCr
            # 写出会错色，直接跳过。
            raise PyramidSkip("unsupported_lossy_layout",
                              "YCbCr 但 compression=%d（仅支持 JPEG）"
                              % compression)
        if not native_rgb:
            raise PyramidSkip("unsupported_lossy_layout",
                              "S=3 但 photometric=%d" % photometric)
        if jpeg_source and dtype != "uint8":
            raise PyramidSkip("unsupported_lossy_layout",
                              "JPEG 源仅支持 uint8 RGB")
    elif has_c:
        channels = int(shape[axes.index("C")])
        if channels < 1:
            raise PyramidSkip("unsupported_axes", "C=%d" % channels)
        out_axes = "CYX" if channels > 1 else "YX"
        native_rgb = False
    else:
        channels = 1
        out_axes = "YX"
        native_rgb = False

    # 其他非空间轴只允许 size==1（T/Z/Q/P 的首平面已固定；多平面不静默丢数据）。
    for i, ax in enumerate(axes):
        if ax in ("Y", "X", "C", "S"):
            continue
        if int(shape[i]) != 1:
            raise PyramidSkip("unsupported_axes",
                              "轴 %s=%d（仅支持首平面单层）" % (ax, shape[i]))

    mpp_x, mpp_y, objective = _source_mpp(tif, series)
    names, colors = _source_channels(tif, series_index)
    if len(names) != channels:
        names = []
        colors = []
    return _SourceLayout(
        axes=out_axes, height=height, width=width, channels=channels,
        dtype=dtype, native_rgb=native_rgb, lossless=lossless,
        jpeg_source=jpeg_source, mpp_x=mpp_x, mpp_y=mpp_y,
        objective_power=objective, channel_names=names,
        channel_colors=colors, source_axes=axes, source_shape=shape,
        source_compression=compression, source_photometric=photometric,
    )


def _canonical_shape(layout, width, height):
    if layout.axes == "YXS":
        return (height, width, 3)
    if layout.axes == "CYX":
        return (layout.channels, height, width)
    return (height, width)


# --------------------------------------------------------------------------- #
# 源读取（tile 级；内存只保留一个块）
# --------------------------------------------------------------------------- #
def _open_source_zarr(tif, series_index):
    import zarr

    store = tif.aszarr(series=series_index, read_only=True)
    root = zarr.open(store, mode="r")
    if hasattr(root, "keys"):
        array = root["0"]
    else:
        array = root
    return store, root, array


def _zarr_index(axes, shape, y0, y1, x0, x1, layout, channel=None):
    """按源 axes 构造 zarr 切片（Y/X 保留 slice，其余取首 plane/全部通道）。"""
    idx = []
    for i, ax in enumerate(axes):
        if ax == "Y":
            idx.append(slice(int(y0), int(y1)))
        elif ax == "X":
            idx.append(slice(int(x0), int(x1)))
        elif ax == "C":
            if channel is not None:
                idx.append(int(channel))
            else:
                idx.append(0 if int(layout.channels) == 1
                           else slice(0, int(layout.channels)))
        elif ax == "S":
            idx.append(slice(0, 3))
        else:
            idx.append(0)
    return tuple(idx) if idx else (slice(None),)


def _read_source_block(array, layout, y0, y1, x0, x1, channel=None):
    """读取一个源块并转成规范轴序（YXS / CYX / YX）；供测试计数/限内存。

    ``channel`` 给出时只读 CYX 源的单个通道并返回 2D 块（tifffile 的
    separate-plane 写法要求逐通道 tile 顺序）。
    """
    axes = layout.source_axes
    idx = _zarr_index(axes, layout.source_shape, y0, y1, x0, x1, layout,
                      channel=channel)
    block = np.asarray(array[idx])
    present = [ax for ax in axes if ax in ("Y", "X", "C", "S")]
    if channel is not None:
        present = [ax for ax in present if ax != "C"]
    if channel is not None:
        target = ("Y", "X")
    elif layout.axes == "YXS":
        target = ("Y", "X", "S")
    elif layout.axes == "CYX":
        target = ("C", "Y", "X")
    else:
        target = ("Y", "X")
    try:
        order = [present.index(ax) for ax in target]
    except ValueError as exc:  # pragma: no cover - 布局检查已保证
        raise PyramidError("源 axes 与块维度不一致：%r" % axes) from exc
    if block.ndim != len(present):
        raise PyramidError("源块维度异常：%r vs axes=%r"
                           % (block.shape, axes))
    if order != list(range(len(order))):
        block = np.transpose(block, order)
    if layout.axes == "YX" and block.ndim == 3 and block.shape[0] == 1:
        block = block[0]
    return np.ascontiguousarray(block)


# --------------------------------------------------------------------------- #
# 下采样（2×2 面积平均；整数用四舍五入，浮点用均值）
# --------------------------------------------------------------------------- #
def _downsample_block(layout, block):
    """按布局对空间 Y/X 两维做 2×2 面积平均并恢复规范轴序。"""
    if layout.axes == "YXS":
        arr = np.transpose(block, (2, 0, 1))
        return np.transpose(_downsample2(arr), (1, 2, 0))
    return _downsample2(block)


def _downsample2(a):
    """对最后两维做 2×2 面积平均；奇数边用 edge 复制补齐。"""
    arr = np.asarray(a)
    h, w = int(arr.shape[-2]), int(arr.shape[-1])
    h2, w2 = (h + 1) // 2, (w + 1) // 2
    pad_h, pad_w = h2 * 2 - h, w2 * 2 - w
    if pad_h or pad_w:
        pad = [(0, 0)] * (arr.ndim - 2) + [(0, pad_h), (0, pad_w)]
        arr = np.pad(arr, pad, mode="edge")
    reshaped = arr.reshape(arr.shape[:-2] + (h2, 2, w2, 2))
    if arr.dtype.kind in "ui":
        total = reshaped.sum(axis=(-3, -1), dtype=np.uint64)
        out = (total + 2) // 4
        return out.astype(arr.dtype)
    total = reshaped.astype(np.float64).mean(axis=(-3, -1))
    return total.astype(arr.dtype)


# --------------------------------------------------------------------------- #
# JPEG tile 编码（tifffile 的 JPEG 写路径把输入当 YCbCr 原样存，
# 因此 RGB→JPEG 必须自行用 Pillow 编码，见 tests/_tiff_fixtures.py 说明）
# --------------------------------------------------------------------------- #
def _jpeg_tile(rgb_block, quality):
    from PIL import Image

    h, w = rgb_block.shape[:2]
    if h != PYRAMID_TILE or w != PYRAMID_TILE:
        canvas = np.full((PYRAMID_TILE, PYRAMID_TILE, 3), 255, np.uint8)
        canvas[:h, :w] = rgb_block
        rgb_block = canvas
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb_block), mode="RGB").save(
        buf, "JPEG", quality=int(quality), subsampling=0)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 输出估算/空间检查
# --------------------------------------------------------------------------- #
def estimate_output_bytes(layout, level_dims, source_bytes):
    """保守输出字节估算（用于 3× 上限与 BigTIFF/scratch 决策）。

    无损源：level 0 ≈ 源大小（无损重压缩量级）；缩减层按最坏 deflate 原量
    计。有损 JPEG 源：level 0 ≈ 1.5× 源（q95 重编码），缩减层 JPEG 按原量
    的 1/8 计。估算只用于准入；写出后再按实际大小复核并受 3× 硬上限。
    """
    raw0 = int(level_dims[0][0]) * int(level_dims[0][1]) * \
        int(layout.channels) * np.dtype(layout.dtype).itemsize
    if layout.lossless:
        base = min(max(int(source_bytes), raw0 // 20), raw0)
        sub = sum(int(w) * int(h) * int(layout.channels)
                  * np.dtype(layout.dtype).itemsize
                  for w, h in level_dims[1:])
        # 缩减层与 level 0 同源、纹理更平滑：压缩率通常不低于源。按源大小
        # 的 1/2 封顶，避免把高度可压缩的荧光源误判为 3× 超限。
        return int(base + min(sub, max(1, int(source_bytes) * 3 // 4)))
    base = int(max(int(source_bytes) * 1.5, raw0 // 10))
    sub = sum(int(w) * int(h) * 3 for w, h in level_dims[1:]) // 8
    return int(base + sub)


def _needed_scratch(layout, level_dims):
    item = np.dtype(layout.dtype).itemsize
    return sum(int(w) * int(h) * int(layout.channels) * item
               for w, h in level_dims[1:])


def _check_scratch(directory, need_bytes):
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        raise PyramidSkip("insufficient_scratch", "无法读取磁盘余量：%s" % exc)
    if free < int(need_bytes) + PYRAMID_MIN_FREE_BYTES:
        raise PyramidSkip(
            "insufficient_scratch",
            "scratch 空间不足（free=%d need=%d）" % (free, need_bytes))


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def _report(progress, stage, done, total):
    if progress is None:
        return
    try:
        progress(stage, int(done), int(total))
    except PyramidSkip:
        raise
    except Exception:  # noqa: BLE001  进度回调失败不得影响构建
        pass


def _tick(progress, stage, done, total, *, deadline, every=64):
    if deadline is not None and time.time() > float(deadline):
        raise PyramidSkip("deadline")
    if total <= 0 or done % every == 0 or done == total:
        _report(progress, stage, done, total)


def build_pyramid(src_path, dst_path, *, scratch_dir=None, progress=None,
                  deadline=None):
    """把单层大 TIFF/OME-TIFF 重写为分块 SubIFD 金字塔 OME-TIFF。

    参数：
      - ``src_path``：源文件（只读）；
      - ``dst_path``：输出文件（必须不存在；调用方给临时路径，校验后自行
        原子替换）；
      - ``scratch_dir``：临时 zarr 目录（默认 ``dst_path`` 同目录，保证
        与输出同文件系统）；
      - ``progress``：``callback(stage, done, total)``，worker 用来续租；
      - ``deadline``：epoch 秒硬期限（超过后抛 ``PyramidSkip('deadline')``）。

    返回 :class:`PyramidResult`；确定性不构建抛 :class:`PyramidSkip`，
    真实故障抛 :class:`PyramidError`。成功后临时 scratch 已清理。
    """
    if not pyramid_enabled():
        raise PyramidSkip("disabled")
    src_path = os.fspath(src_path)
    dst_path = os.fspath(dst_path)
    started = time.monotonic()
    if not os.path.isfile(src_path):
        raise PyramidSkip("source_unreadable", "源文件不存在")
    if os.path.exists(dst_path):
        raise PyramidError("输出路径已存在：%s" % dst_path)
    if not is_buildable_source(src_path):
        raise PyramidSkip("unsupported_format_ext",
                          os.path.basename(src_path))
    src_bytes = os.path.getsize(src_path)

    import tifffile

    try:
        tif = tifffile.TiffFile(src_path)
    except Exception as exc:  # noqa: BLE001
        raise PyramidSkip("source_unreadable", type(exc).__name__) from exc

    store = root = array = None
    scratch = None
    try:
        series_index, series = _pick_series(tif)
        levels = series.levels or [series]
        layout = _source_layout(tif, series, level_count=len(levels),
                                series_index=series_index)

        width, height = layout.width, layout.height
        if max(width, height) <= min_long_edge():
            raise PyramidSkip("below_threshold")
        if int(width) * int(height) > PYRAMID_MAX_PIXELS:
            raise PyramidSkip("pixel_count_cap",
                              "%dx%d > 2^32" % (width, height))

        level_dims = level_dimensions_for(width, height)
        estimated = estimate_output_bytes(layout, level_dims, src_bytes)
        if estimated > int(src_bytes * PYRAMID_MAX_GROWTH_RATIO):
            raise PyramidSkip("estimated_output_cap",
                              "est=%d src=%d" % (estimated, src_bytes))

        dst_dir = os.path.dirname(os.path.abspath(dst_path))
        os.makedirs(dst_dir, exist_ok=True)
        scratch_parent = os.fspath(scratch_dir or dst_dir)
        _check_scratch(scratch_parent,
                       _needed_scratch(layout, level_dims) + estimated)
        bigtiff = bool(estimated > 3_500_000_000)

        store, root, array = _open_source_zarr(tif, series_index)

        scratch = tempfile.mkdtemp(prefix=".pyramid-scratch-", dir=scratch_parent)
        _report(progress, "prepare", 0, max(1, len(level_dims) - 1))

        # ---- 生成 level 1..N 的临时 zarr（磁盘 store；level0 写输出时顺带
        # 产生 level1）。用 zarr 而非 numpy memmap：文件页不会计入进程 RSS，
        # 峰值内存只与单个 512 tile 相关（大图 40k×30k 也 < 512MB）。
        import zarr

        tmp_levels = [None] * len(level_dims)  # 索引 0 不使用
        for i in range(1, len(level_dims)):
            w_i, h_i = level_dims[i]
            shape = _canonical_shape(layout, w_i, h_i)
            if layout.axes == "YXS":
                chunks = (min(PYRAMID_TILE, h_i), min(PYRAMID_TILE, w_i), 3)
            elif layout.axes == "CYX":
                chunks = (1, min(PYRAMID_TILE, h_i), min(PYRAMID_TILE, w_i))
            else:
                chunks = (min(PYRAMID_TILE, h_i), min(PYRAMID_TILE, w_i))
            tmp_levels[i] = zarr.create_array(
                os.path.join(scratch, "level%d" % i),
                shape=shape, chunks=chunks, dtype=layout.dtype,
                compressors=None, overwrite=True)

        level0_jpeg = bool(layout.jpeg_source and layout.native_rgb
                           and layout.dtype == "uint8")
        level0_compression = "jpeg" if level0_jpeg else "deflate"
        sub_jpeg = bool(layout.native_rgb and layout.dtype == "uint8")
        sub_compression = "jpeg" if sub_jpeg else "deflate"

        meta = {"axes": layout.axes}
        if layout.mpp_x:
            meta["PhysicalSizeX"] = float(layout.mpp_x)
            meta["PhysicalSizeXUnit"] = _MPP_UNIT
        if layout.mpp_y:
            meta["PhysicalSizeY"] = float(layout.mpp_y)
            meta["PhysicalSizeYUnit"] = _MPP_UNIT
        if layout.channel_names:
            channel_meta = {"Name": list(layout.channel_names)}
            if layout.channel_colors and all(layout.channel_colors):
                channel_meta["Color"] = list(layout.channel_colors)
            meta["Channel"] = channel_meta

        tile = (PYRAMID_TILE, PYRAMID_TILE)

        def tile_boxes(h, w):
            rows = (h + PYRAMID_TILE - 1) // PYRAMID_TILE
            cols = (w + PYRAMID_TILE - 1) // PYRAMID_TILE
            for ty in range(rows):
                y0 = ty * PYRAMID_TILE
                y1 = min(h, y0 + PYRAMID_TILE)
                for tx in range(cols):
                    x0 = tx * PYRAMID_TILE
                    x1 = min(w, x0 + PYRAMID_TILE)
                    yield y0, y1, x0, x1

        boxes0 = list(tile_boxes(height, width))
        channels0 = int(layout.channels) if layout.axes == "CYX" else 1
        total0 = len(boxes0) * channels0

        def level0_tiles():
            """逐 512 tile 读源；顺带写 level1 临时 zarr（单次源遍历）。

            tifffile 的 planar-separate 写法按 channel-major 消费 tile；
            CYX 源逐通道读取/产出，保持同一顺序。
            """
            done = 0
            next_level = tmp_levels[1] if len(tmp_levels) > 1 else None
            if layout.axes == "CYX":
                for c in range(int(layout.channels)):
                    for y0, y1, x0, x1 in boxes0:
                        block = _read_source_block(
                            array, layout, y0, y1, x0, x1, channel=c)
                        if next_level is not None:
                            down = _downsample2(block)
                            next_level[c,
                                     y0 // 2:y0 // 2 + down.shape[0],
                                     x0 // 2:x0 // 2 + down.shape[1]] = down
                        done += 1
                        _tick(progress, "level0", done, total0,
                              deadline=deadline)
                        yield block
            else:
                for y0, y1, x0, x1 in boxes0:
                    block = _read_source_block(array, layout, y0, y1, x0, x1)
                    if next_level is not None:
                        down = _downsample_block(layout, block)
                        if layout.axes == "YXS":
                            next_level[y0 // 2:y0 // 2 + down.shape[0],
                                     x0 // 2:x0 // 2 + down.shape[1], :] = down
                        else:
                            next_level[y0 // 2:y0 // 2 + down.shape[0],
                                     x0 // 2:x0 // 2 + down.shape[1]] = down
                    done += 1
                    _tick(progress, "level0", done, total0, deadline=deadline)
                    if level0_jpeg:
                        yield _jpeg_tile(block, PYRAMID_JPEG_QUALITY_LEVEL0)
                    else:
                        yield block

        # ---- level i (i>=2) 从上一级临时 zarr 生成下一级 ---- #
        def build_next_level(i):
            prev = tmp_levels[i - 1]
            cur = tmp_levels[i]
            prev_h, prev_w = level_dims[i - 1][1], level_dims[i - 1][0]
            out_h, out_w = level_dims[i][1], level_dims[i][0]
            rows = (out_h + PYRAMID_TILE - 1) // PYRAMID_TILE
            cols = (out_w + PYRAMID_TILE - 1) // PYRAMID_TILE
            channels_i = int(layout.channels) if layout.axes == "CYX" else 1
            total = rows * cols * channels_i
            done = 0
            for ty in range(rows):
                oy0 = ty * PYRAMID_TILE
                oy1 = min(out_h, oy0 + PYRAMID_TILE)
                iy0 = oy0 * 2
                iy1 = min(prev_h, iy0 + (oy1 - oy0) * 2)
                for tx in range(cols):
                    ox0 = tx * PYRAMID_TILE
                    ox1 = min(out_w, ox0 + PYRAMID_TILE)
                    ix0 = ox0 * 2
                    ix1 = min(prev_w, ix0 + (ox1 - ox0) * 2)
                    if layout.axes == "CYX":
                        # 逐通道处理：内存与通道数无关（大 C 荧光源）。
                        for c in range(int(layout.channels)):
                            block = np.asarray(prev[c, iy0:iy1, ix0:ix1])
                            down = _downsample2(block)
                            cur[c, oy0:oy1, ox0:ox1] = down
                            done += 1
                            _tick(progress, "level%d" % i, done, total,
                                  deadline=deadline)
                        continue
                    if layout.axes == "YXS":
                        block = np.asarray(prev[iy0:iy1, ix0:ix1, :])
                        down = _downsample_block(layout, block)
                        cur[oy0:oy1, ox0:ox1, :] = down
                    else:
                        block = np.asarray(prev[iy0:iy1, ix0:ix1])
                        down = _downsample_block(layout, block)
                        cur[oy0:oy1, ox0:ox1] = down
                    done += 1
                    _tick(progress, "level%d" % i, done, total,
                          deadline=deadline)

        for i in range(2, len(level_dims)):
            build_next_level(i)
            _report(progress, "levels_built", i - 1, max(1, len(level_dims) - 1))

        # ---- 写出 OME-TIFF（每级 tile 生成器）---- #
        def level_tiles(i):
            mem = tmp_levels[i]
            out_h, out_w = level_dims[i][1], level_dims[i][0]
            boxes = list(tile_boxes(out_h, out_w))
            channels_i = int(layout.channels) if layout.axes == "CYX" else 1
            total = len(boxes) * channels_i
            done = 0
            if layout.axes == "CYX":
                for c in range(int(layout.channels)):
                    for y0, y1, x0, x1 in boxes:
                        block = np.asarray(mem[c, y0:y1, x0:x1])
                        done += 1
                        _tick(progress, "write%d" % i, done, total,
                              deadline=deadline)
                        yield block
                return
            for y0, y1, x0, x1 in boxes:
                if layout.axes == "YXS":
                    block = np.asarray(mem[y0:y1, x0:x1, :])
                else:
                    block = np.asarray(mem[y0:y1, x0:x1])
                done += 1
                _tick(progress, "write%d" % i, done, total,
                      deadline=deadline)
                if sub_jpeg:
                    yield _jpeg_tile(block, PYRAMID_JPEG_QUALITY_SUBLEVEL)
                else:
                    yield block

        with tifffile.TiffWriter(dst_path, bigtiff=bigtiff, ome=True) as tw:
            common0 = dict(
                shape=_canonical_shape(layout, width, height),
                dtype="uint8" if level0_jpeg else layout.dtype,
                tile=tile,
                compression=level0_compression,
                subifds=max(0, len(level_dims) - 1),
                metadata=meta,
                buffersize=PYRAMID_WRITE_BUFFERSIZE,
            )
            if level0_jpeg:
                common0.update(photometric="ycbcr", subsampling=(1, 1))
            else:
                common0.update(
                    photometric="rgb" if layout.native_rgb else "minisblack")
            tw.write(level0_tiles(), **common0)

            for i in range(1, len(level_dims)):
                shape = _canonical_shape(layout, level_dims[i][0],
                                        level_dims[i][1])
                sub = dict(shape=shape, dtype=layout.dtype, tile=tile,
                           subfiletype=1, compression=sub_compression,
                           buffersize=PYRAMID_WRITE_BUFFERSIZE)
                if sub_jpeg:
                    sub.update(photometric="ycbcr", subsampling=(1, 1))
                else:
                    sub.update(
                        photometric="rgb" if layout.native_rgb else "minisblack")
                tw.write(level_tiles(i), **sub)

        bytes_after = os.path.getsize(dst_path)
        if bytes_after > int(src_bytes * PYRAMID_MAX_GROWTH_RATIO):
            raise PyramidSkip("output_size_cap",
                              "out=%d src=%d" % (bytes_after, src_bytes))

        seconds = time.monotonic() - started
        _report(progress, "done", len(level_dims) - 1,
                max(1, len(level_dims) - 1))
        return PyramidResult(
            levels=len(level_dims),
            level_dimensions=tuple(level_dims),
            seconds=round(seconds, 3),
            bytes_before=int(src_bytes),
            bytes_after=int(bytes_after),
            source_lossless=bool(layout.lossless),
            level0_compression=level0_compression,
            sublevel_compression=sub_compression,
            estimated_bytes=int(estimated),
            bigtiff=bool(bigtiff),
            extra={
                "axes": layout.axes,
                "channels": int(layout.channels),
                "dtype": layout.dtype,
                "mpp_x": layout.mpp_x,
                "mpp_y": layout.mpp_y,
                "source_compression": int(layout.source_compression),
            },
        )
    finally:
        # 先释放 reader/store 引用再删 scratch（Windows 语义保守，Linux 无碍）。
        try:
            if root is not None:
                close = getattr(root, "close", None)
                if callable(close):
                    close()
            if store is not None:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
        except Exception:  # noqa: BLE001
            pass
        try:
            tif.close()
        except Exception:  # noqa: BLE001
            pass
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 输出校验（必须走 PT 自己的 slide_io 读取路径）
# --------------------------------------------------------------------------- #
def _mpp_of(slide, axis="x"):
    try:
        props = slide.properties or {}
    except Exception:  # noqa: BLE001
        return None
    raw = props.get("openslide.mpp-%s" % axis)
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sample_boxes(width, height, size=256):
    size = max(1, min(int(size), int(width), int(height)))
    boxes = [(0, 0)]
    if width > size or height > size:
        boxes.append(((width - size) // 2, (height - size) // 2))
        boxes.append((max(0, width - size), max(0, height - size)))
    seen, out = set(), []
    for box in boxes:
        if box not in seen:
            seen.add(box)
            out.append(box)
    return out


def validate_pyramid(src_path, dst_path, *, expected_dims=None,
                     lossless=None, max_mean_diff=8.0):
    """用 ``slide_io.open_slide`` 校验输出可读、层数/尺寸/mpp/像素正确。

    - level_count > 1，level-0 尺寸与源一致，每级尺寸符合 2× 序列；
    - mpp（openslide.mpp-x）在源与输出间一致（都缺失也视为一致）；
    - 3 个 level-0 采样窗口逐像素比较：无损源要求完全相等；JPEG 源允许
      平均差 ≤ ``max_mean_diff``（同一 JPEG 解码器误差量级）。

    返回 ``{"levels", "level_dimensions", "mpp_x", "mean_diff"}``。
    """
    import slide_io

    src = dst = None
    try:
        src = slide_io.open_slide(os.fspath(src_path))
        dst = slide_io.open_slide(os.fspath(dst_path))
        if int(getattr(dst, "level_count", 0) or 0) <= 1:
            raise PyramidValidationError("输出仍为单层")
        sw, sh = (int(v) for v in src.dimensions)
        dw, dh = (int(v) for v in dst.dimensions)
        if (sw, sh) != (dw, dh):
            raise PyramidValidationError(
                "level-0 尺寸不一致（src=%dx%d dst=%dx%d）"
                % (sw, sh, dw, dh))
        expected = expected_dims or tuple(
            level_dimensions_for(sw, sh))
        got = tuple((int(w), int(h)) for w, h in dst.level_dimensions)
        if got != tuple(expected):
            raise PyramidValidationError(
                "层尺寸与 2× 序列不一致（got=%r expected=%r）"
                % (got, expected))
        for axis in ("x", "y"):
            s_mpp, d_mpp = _mpp_of(src, axis), _mpp_of(dst, axis)
            if (s_mpp is None) != (d_mpp is None):
                raise PyramidValidationError(
                    "mpp-%s 存在性不一致（src=%r dst=%r）"
                    % (axis, s_mpp, d_mpp))
            if s_mpp is not None and \
                    abs(s_mpp - d_mpp) > 1e-6 * max(1.0, s_mpp):
                raise PyramidValidationError(
                    "mpp-%s 不一致（src=%r dst=%r）"
                    % (axis, s_mpp, d_mpp))
        if lossless is None:
            lossless = True
        from PIL import ImageChops

        diffs = []
        for (x, y) in _sample_boxes(sw, sh):
            box = (min(256, sw), min(256, sh))
            a = src.read_region((x, y), 0, box)
            b = dst.read_region((x, y), 0, box)
            if a.size != b.size:
                raise PyramidValidationError(
                    "采样窗口尺寸不一致（%r vs %r）" % (a.size, b.size))
            if a.mode != "RGBA":
                a = a.convert("RGBA")
            if b.mode != "RGBA":
                b = b.convert("RGBA")
            diff = ImageChops.difference(a, b)
            hist = diff.histogram()
            # 每个 band 直方图相加；只关心非零幅度。
            total, weight = 0, 0
            for band in range(4):
                start = band * 256
                for value in range(256):
                    n = hist[start + value]
                    if n:
                        total += value * n
                        weight += n
            mean = (total / weight) if weight else 0.0
            if lossless and mean != 0.0:
                raise PyramidValidationError(
                    "无损源 level-0 像素不相等（窗口 %r mean_diff=%.3f）"
                    % ((x, y), mean))
            diffs.append(mean)
        worst = max(diffs) if diffs else 0.0
        if not lossless and worst > float(max_mean_diff):
            raise PyramidValidationError(
                "JPEG 源 level-0 差异过大（mean_diff=%.2f > %.2f）"
                % (worst, max_mean_diff))
        return {
            "levels": int(dst.level_count),
            "level_dimensions": got,
            "mpp_x": _mpp_of(dst, "x"),
            "mean_diff": worst,
        }
    finally:
        for slide in (src, dst):
            if slide is not None:
                try:
                    slide.close()
                except Exception:  # noqa: BLE001
                    pass


def file_sha256(path, *, chunk_size=1024 * 1024):
    """流式计算文件 SHA-256（十六进制；原子替换后的内容 revision 用）。"""
    import hashlib

    digest = hashlib.sha256()
    with open(os.fspath(path), "rb") as fh:
        while True:
            chunk = fh.read(int(chunk_size))
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "PYRAMID_MIN_LONG_EDGE", "PYRAMID_TARGET_LONG_EDGE", "PYRAMID_TILE",
    "PYRAMID_JPEG_QUALITY_LEVEL0", "PYRAMID_JPEG_QUALITY_SUBLEVEL",
    "PYRAMID_MAX_PIXELS", "PYRAMID_MAX_GROWTH_RATIO",
    "PyramidSkip", "PyramidError", "PyramidValidationError", "PyramidResult",
    "pyramid_enabled", "min_long_edge", "needs_pyramid",
    "is_buildable_source",
    "level_dimensions_for", "estimate_output_bytes", "build_pyramid",
    "validate_pyramid", "file_sha256",
]
