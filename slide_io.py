# -*- coding: utf-8 -*-
"""切片 I/O 抽象层。

提供 ``open_slide(path)`` 工厂：优先用 OpenSlide 打开厂商格式
（SVS/NDPI/MRXS 等）；失败且为 TIFF 类（含 OME-TIFF）时回退到
``TiffFileSlide``（基于 tifffile + zarr<3 实现 OpenSlide API 子集）。

两者都 duck-type 出 DeepZoomGenerator 需要的接口：
``properties``（dict）、``dimensions``、``level_dimensions``、
``level_downsamples``、``get_best_level_for_downsample(d)``、
``read_region((x,y), level, (w,h))``，外加 ``get_thumbnail``、``close``。

设计取舍：
- tifffile.aszarr 方案：用 ``tf.aszarr(series=0)`` 整体打开，金字塔时得到
  zarr Group（key '0','1',... 为各级），单级时得到 zarr Array。这样无需
  为每级单独持有 store，统一在 close 时释放，简化生命周期。注意需配
  zarr<3（tifffile 较新版本要求 zarr>=3，本项目锁定兼容版本）。
- axes 处理：series.axes 可能是 'YXS'、'CYX'、'TCYX'、'QYX' 等。读取时
  按 axes 顺序构造索引：Y/X 取 slice，S（样本/通道）取 slice（默认全部
  再裁前 3 通道），其余维度（T/C/Q/P...）一律取 0。
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

# TIFF 扩展名（含 OME-TIFF 前缀）
_TIFF_EXTS = {".tif", ".tiff", ".ome.tif", ".ome.tiff"}


def _suffix_tiff(path) -> bool:
    """判断路径是否为 TIFF 类（含 .ome.tif/.ome.tiff）。"""
    p = str(path).lower()
    return any(p.endswith(ext) for ext in _TIFF_EXTS)


def _is_ome_tiff(path) -> bool:
    """快速嗅探是否为 OME-TIFF（读首部描述标签，失败按 False）。"""
    try:
        import tifffile

        with tifffile.TiffFile(str(path)) as tf:
            return bool(tf.is_ome)
    except Exception:  # noqa: BLE001
        return False


def open_slide(path):
    """工厂函数：打开切片，返回 OpenSlide 或 TiffFileSlide。

    策略（TIFF 类文件，含 .ome.tif/.ome.tiff）：
    1. OME-TIFF 优先走 TiffFileSlide——OpenSlide 的 generic-tiff 驱动
       虽然能打开 OME-TIFF，但只认 level 0（不识别 SubIFD 金字塔），
       且不解析 OME-XML 的 PhysicalSize（mpp 丢失）；
    2. 其余 TIFF 先试 openslide.OpenSlide（lazy import，使无 openslide
       库的机器也能单独使用 TiffFileSlide），失败回退 TiffFileSlide；
    3. OME-TIFF 若 TiffFileSlide 失败（异形 axes 等），回退 OpenSlide 保底。
    都失败抛出最后一个异常。
    """
    last_exc = None

    if _suffix_tiff(path) and _is_ome_tiff(path):
        try:
            return TiffFileSlide(path)
        except Exception as e:  # noqa: BLE001
            last_exc = e

    try:
        import openslide  # lazy import

        return openslide.OpenSlide(str(path))
    except Exception as e:  # noqa: BLE001  OpenSlide 可能抛各种底层异常
        last_exc = e

    if _suffix_tiff(path):
        try:
            return TiffFileSlide(path)
        except Exception as e:  # noqa: BLE001
            last_exc = e

    raise last_exc


# --------------------------------------------------------------------------- #
# OME-TIFF 元数据解析
# --------------------------------------------------------------------------- #
def _parse_ome_mpp(ome_xml: str):
    """解析 OME-XML，返回 (mpp_x, mpp_y, objective_power)。

    PhysicalSizeX/Y 单位换算成 µm：nm/1000、mm*1000、µm 原值、cm*10000、m*1e6。
    未提供单位时按 µm 处理（OME 默认）。Objective/NominalMagnification 尽力解析。
    解析失败返回 (None, None, None)。
    """
    if not ome_xml:
        return None, None, None
    try:
        root = ET.fromstring(ome_xml)
    except ET.ParseError:
        return None, None, None

    # OME-XML 命名空间
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"

    def _find_attr(elem, name):
        return elem.get(name) if elem is not None else None

    # Pixels 元素含 PhysicalSizeX/Y 与单位
    pixels = root.find(f".//{ns}Pixels")
    mpp_x = mpp_y = None
    if pixels is not None:
        psx = _to_float(pixels.get("PhysicalSizeX"))
        psy = _to_float(pixels.get("PhysicalSizeY"))
        ux = pixels.get("PhysicalSizeXUnit")
        uy = pixels.get("PhysicalSizeYUnit")
        mpp_x = _unit_to_um(psx, ux)
        mpp_y = _unit_to_um(psy, uy)

    # NominalMagnification：在 Instrument/Objective 下
    objective_power = None
    obj = root.find(f".//{ns}Objective")
    if obj is not None:
        objective_power = _to_float(obj.get("NominalMagnification"))

    return mpp_x, mpp_y, objective_power


def _unit_to_um(value, unit):
    """把 PhysicalSize 值按单位换算成 µm。单位缺失按 µm。"""
    v = _to_float(value)
    if v is None or v <= 0:
        return None
    u = (unit or "").strip().lower()
    if u in ("nm",):
        return v / 1000.0
    if u in ("mm",):
        return v * 1000.0
    if u in ("cm",):
        return v * 10000.0
    if u in ("m",):
        return v * 1_000_000.0
    # µm / µm / 默认
    return v


def _to_float(v):
    """安全转 float。"""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# TiffFileSlide
# --------------------------------------------------------------------------- #
class TiffFileSlide:
    """基于 tifffile + zarr 实现 OpenSlide API 子集（duck-typing）。

    供 openslide.deepzoom.DeepZoomGenerator 使用。非金字塔大图只有 1 级
    是可以接受的（DZG 会自动 read+resize），代码里已兼容。
    """

    VENDOR = "generic-tiff"

    def __init__(self, path):
        import tifffile  # lazy import

        self._path = str(path)
        # 先置默认值，确保 close()/__del__ 在初始化中途异常时也安全
        self._tf = None
        self._store = None
        self._zroot = None
        self._zarrays = []
        self._levels = []
        self._axes = "YXS"
        self._level_dims = []
        self._level_ds = []
        self.properties = {}

        self._tf = tifffile.TiffFile(self._path)
        # 选像素数最多的 series（多 series TIFF 取主图）
        self._series = max(
            self._tf.series,
            key=lambda s: int(np.prod(s.shape)) if s.shape else 0,
        )
        self._levels = self._series.levels or [self._series]
        self._axes = self._series.axes or "YXS"

        # 打开 zarr store：金字塔 → Group（key '0'/'1'/...），单级 → Array
        # 定位所选 series 在 tf.series 中的序号（避免依赖对象的 ==/hash）
        sidx = 0
        for i, s in enumerate(self._tf.series):
            if s is self._series:
                sidx = i
                break
        self._store = self._tf.aszarr(series=sidx)
        import zarr  # lazy import

        self._zroot = zarr.open(self._store, mode="r")
        if hasattr(self._zroot, "keys"):
            # Group：按 key '0','1',... 取各级 Array
            self._zarrays = []
            for i in range(len(self._levels)):
                arr = self._zroot[str(i)]
                self._zarrays.append(arr)
        else:
            self._zarrays = [self._zroot]

        # 尺寸与降采样
        self._level_dims = []
        for lv in self._levels:
            h, w = self._axes_shape_hw(lv.axes, lv.shape)
            self._level_dims.append((int(w), int(h)))
        base_w, base_h = self._level_dims[0]
        self._level_ds = [1.0]
        for (w, _h) in self._level_dims[1:]:
            self._level_ds.append(float(base_w) / float(w) if w else 1.0)

        # properties（OpenSlide 风格）
        self.properties = self._build_properties()

    # ---------- axes / 形状辅助 ----------
    def _axes_shape_hw(self, axes, shape):
        """从 axes 字符串 + shape 取 (height, width)。

        Y → height，X → width。未找到时按 shape 末两维兜底。
        """
        try:
            iy = axes.index("Y")
            ix = axes.index("X")
            return int(shape[iy]), int(shape[ix])
        except ValueError:
            # 兜底：取最后两维 (H, W)
            if len(shape) >= 2:
                return int(shape[-2]), int(shape[-1])
            return int(shape[0]), int(shape[0])

    def _build_index(self, axes, shape, y0, y1, x0, x1):
        """为 zarr 切片构造各轴索引：Y/X 取 slice，S 取 slice（前 3 通道），
        其余维度取 0。返回 tuple。
        """
        idx = []
        for i, ax in enumerate(axes):
            if ax == "Y":
                idx.append(slice(int(y0), int(y1)))
            elif ax == "X":
                idx.append(slice(int(x0), int(x1)))
            elif ax == "S":
                # 样本/通道：取前 3 通道当 RGB（不足 3 则取全部）
                n = int(shape[i]) if i < len(shape) else 1
                idx.append(slice(0, min(3, n)))
            else:
                # T/C/Q/P 等非空间维度：取第 0 帧
                idx.append(0)
        return tuple(idx) if idx else (slice(None),)

    # ---------- properties ----------
    def _build_properties(self):
        props = {
            "openslide.vendor": self.VENDOR,
        }
        # OME-TIFF：解析 mpp 与倍率
        is_ome = bool(getattr(self._tf, "is_ome", False))
        if is_ome:
            try:
                ome_xml = self._tf.ome_metadata
            except Exception:  # noqa: BLE001
                ome_xml = None
            if ome_xml:
                mpp_x, mpp_y, obj = _parse_ome_mpp(ome_xml)
                if mpp_x is not None:
                    props["openslide.mpp-x"] = repr(mpp_x)
                if mpp_y is not None:
                    props["openslide.mpp-y"] = repr(mpp_y)
                if obj is not None:
                    props["openslide.objective-power"] = repr(obj)
        return props

    # ---------- OpenSlide 兼容属性 ----------
    @property
    def level_count(self):
        return len(self._level_dims)

    @property
    def dimensions(self):
        return self._level_dims[0]

    @property
    def level_dimensions(self):
        return tuple(self._level_dims)

    @property
    def level_downsamples(self):
        return tuple(self._level_ds)

    def get_best_level_for_downsample(self, downsample):
        """返回 downsample <= d 的最大 level；没有则返回最小 downsample 的 level。

        与 OpenSlide 语义一致。
        """
        d = float(downsample)
        best = 0
        for i, ds in enumerate(self._level_ds):
            if ds <= d + 1e-6:
                best = i
        return best

    # ---------- 区域读取 ----------
    def read_region(self, location, level, size):
        """读取区域，返回 PIL RGBA，尺寸恰好 (w,h)。

        location=(x, y) 是 level-0 坐标，需换算到该 level 坐标（与 OpenSlide
        语义一致）。越界部分用透明像素 padding；完全越界返回全透明。
        """
        x0, y0 = location
        w, h = size
        level = int(level)
        if level < 0 or level >= len(self._level_dims):
            level = max(0, min(level, len(self._level_dims) - 1))

        lw, lh = self._level_dims[level]
        ds = self._level_ds[level] if level < len(self._level_ds) else 1.0
        # level-0 坐标换算到该 level 坐标
        lx = int(x0 / ds) if ds else int(x0)
        ly = int(y0 / ds) if ds else int(y0)

        # 目标区域在该 level 的有效范围（与图像求交）
        sx = max(0, lx)
        sy = max(0, ly)
        ex = min(lw, lx + w)
        ey = min(lh, ly + h)
        valid_w = max(0, ex - sx)
        valid_h = max(0, ey - sy)

        # 目标 RGBA 画布（全透明）
        out = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))

        if valid_w <= 0 or valid_h <= 0:
            return out  # 完全越界：直接返回全透明

        lv = self._levels[level]
        axes = lv.axes or self._axes
        idx = self._build_index(axes, lv.shape, sy, ey, sx, ex)
        arr = self._zarrays[level][idx]

        # 归一化为 (H, W, 3) uint8 RGB
        rgb = self._to_rgb_u8(arr, axes)
        if rgb is None:
            return out
        # 贴到画布对应偏移（处理左/上越界 padding）
        paste_x = int(sx - lx)
        paste_y = int(sy - ly)
        region_img = Image.fromarray(rgb, mode="RGB").convert("RGBA")
        out.paste(region_img, (paste_x, paste_y))
        return out

    def _to_rgb_u8(self, arr, axes):
        """把读取到的 numpy 数组归一化为 (H, W, 3) uint8 RGB。

        - 无 S 轴（灰度）→ 复制到 3 通道；
        - samples>=3 取前 3 通道；
        - dtype：uint8 直用；uint16 右移 8；其他归一化到 0-255。
        返回 (H,W,3) uint8，或 None（空）。
        """
        if arr is None:
            return None
        a = np.asarray(arr)
        if a.size == 0:
            return None

        # 判断轴序：优先用本次读取对应的 axes（series 级 axes）
        has_s = "S" in (axes or "")
        # 读取后数组维度 = 非零维（T/C 等被整数索引压掉）
        # 按 axes 过滤掉被标量索引的轴，得到剩余轴顺序
        remain_axes = [c for c in (axes or "") if c in "YXS"]
        # 若 axes 不足以描述（兜底），按数组形状推断
        if len(remain_axes) != a.ndim:
            remain_axes = self._infer_axes(a.ndim)

        # 把数组重排成 (H, W, C?)：按 remain_axes 顺序 transpose
        order = []
        for target in ("Y", "X", "S"):
            if target in remain_axes:
                order.append(remain_axes.index(target))
        a = np.transpose(a, order)
        # 现在轴顺序固定为 Y, X, (S?)
        if a.ndim == 2:
            h, w = a.shape
            channels = 1
        elif a.ndim == 3:
            h, w, channels = a.shape
        else:
            # 意外维度：压扁到 2D 后当灰度
            a = a.reshape(a.shape[0], -1) if a.ndim >= 2 else a
            h, w = a.shape
            channels = 1

        # dtype 归一化到 uint8
        if a.dtype == np.uint8:
            data = a
        elif a.dtype == np.uint16:
            data = (a >> 8).astype(np.uint8)
        else:
            mn = float(a.min()) if a.size else 0.0
            mx = float(a.max()) if a.size else 0.0
            if mx > mn:
                data = ((a.astype(np.float64) - mn) / (mx - mn) * 255.0).astype(np.uint8)
            else:
                data = np.zeros(a.shape, dtype=np.uint8)

        # 通道处理
        if channels >= 3:
            data = data[..., :3]
        elif channels == 1:
            data = np.repeat(data[..., np.newaxis], 3, axis=2)
        else:  # channels == 2：补到 3
            pad = np.zeros(data.shape[:-1] + (3 - channels,), dtype=data.dtype)
            data = np.concatenate([data, pad], axis=2)
        return np.ascontiguousarray(data)

    @staticmethod
    def _infer_axes(ndim):
        """axes 不足时按 ndim 推断：2→YX，3→YXS。"""
        if ndim == 2:
            return "YX"
        return "YXS"

    # ---------- 缩略图 ----------
    def get_thumbnail(self, size):
        """从最小 level 读整图缩放，返回 PIL RGBA。"""
        w, h = size
        # 最小 level（分辨率最低）
        idx = len(self._levels) - 1
        lw, lh = self._level_dims[idx]
        # 直接读整图（level-0 原点）
        img = self.read_region((0, 0), idx, (lw, lh))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # 缩放到目标尺寸（保持比例，contain 在 size 内）
        tw, th = img.size
        scale = min(float(w) / tw if tw else 1.0, float(h) / th if th else 1.0)
        new_w = max(1, int(round(tw * scale)))
        new_h = max(1, int(round(th * scale)))
        return img.resize((new_w, new_h), Image.LANCZOS)

    # ---------- 生命周期 ----------
    def close(self):
        """释放 zarr store 与 tifffile 句柄。顺序：先 store 再 TiffFile。"""
        # 释放对 zarr array/group 的引用
        self._zarrays = None
        self._zroot = None
        if self._store is not None:
            try:
                close = getattr(self._store, "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001
                pass
            self._store = None
        if self._tf is not None:
            try:
                self._tf.close()
            except Exception:  # noqa: BLE001
                pass
            self._tf = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
