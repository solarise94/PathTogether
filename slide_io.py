# -*- coding: utf-8 -*-
"""切片 I/O 抽象层。

提供 ``open_slide(path, *, format_hint=None)`` 工厂：优先用 OpenSlide 打开厂商格式
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

# --------------------------------------------------------------------------- #
# 逻辑格式识别与稳定错误契约（上传修复 A0）
# --------------------------------------------------------------------------- #
#: 逻辑扩展名词表。与 app.SUPPORTED_EXTS 同集（slide_io 是底层模块，不反向
#: import app，故此处自带常量；两处需同步维护）。**按长度降序**排列以保证
#: 最长匹配（``x.ome.tiff`` 识别为 ``.ome.tiff`` 而非 ``.tiff``）。
LOGICAL_EXTS = (
    ".ome.tiff", ".ome.tif",
    ".svslide",
    ".tiff", ".ndpi", ".mrxs",
    ".svs", ".vms", ".vmu", ".scn", ".bif", ".tif",
)

#: 其中的 TIFF 类（走 OME 优先 / TiffFileSlide fallback 的子集）
_TIFF_LOGICAL_EXTS = frozenset((".ome.tiff", ".ome.tif", ".tiff", ".tif"))

#: SlideValidationError.code 的固定词表
VALID_SLIDE_ERROR_CODES = frozenset((
    "invalid_slide", "slide_open_unsupported", "slide_open_failed",
))


class SlideValidationError(ValueError):
    """切片验证失败（携带稳定机器码，供路由映射统一的 HTTP/JSON 契约）。

    code 固定为 :data:`VALID_SLIDE_ERROR_CODES` 之一：
      - ``invalid_slide``：逻辑格式不在允许列表，或字节明显无效
        （tifffile 拒绝把字节当 TIFF 解析等）；
      - ``slide_open_unsupported``：OpenSlide 与允许的 TIFF fallback 都明确
        表示不支持该格式；
      - ``slide_open_failed``：解析器/IO 在允许格式内异常（损坏、截断、
        读取失败、未知异常等）。

    ``cause_type`` 是底层异常类型名（只进日志，不透出给前端）。
    兼容历史：继承 ValueError，脚本型调用方（import_slides）按 ValueError
    捕获仍成立。
    """

    def __init__(self, code, message=None, *, cause_type=None):
        super().__init__(message or code)
        self.code = code if code in VALID_SLIDE_ERROR_CODES else "slide_open_failed"
        self.cause_type = cause_type or ""


def logical_format_ext(name) -> str:
    """从逻辑文件名识别支持的切片扩展名（最长匹配）；不支持返回 ``""``。

    只做**字符串级**判定，不触碰文件系统：输入通常是已净化的 basename
    （V1 ``safe`` / V2 ``safe_name``），也可以是完整路径（取最后一段 basename，
    供 ZIP 成员的真实相对路径/绝对路径使用）。显式拒绝：NUL 字节、URL
    （``scheme://``）、目录样式（以路径分隔符结尾）、MIME 字符串
    （形如 ``image/tiff``——basename 无点后缀，天然不匹配）。
    """
    if name is None:
        return ""
    s = str(name)
    if not s or "\x00" in s or "://" in s:
        return ""
    if s.endswith("/") or s.endswith("\\"):
        return ""  # 目录样式
    base = s.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in LOGICAL_EXTS:  # 已按长度降序：最长匹配优先
        if base.endswith(ext):
            return ext
    return ""


def _suffix_tiff(path) -> bool:
    """判断路径是否为 TIFF 类（含 .ome.tif/.ome.tiff）。"""
    return logical_format_ext(path) in _TIFF_LOGICAL_EXTS


# 底层异常 → 稳定错误码的分类信号 ------------------------------------------------
def _exc_kind(e) -> str:
    """把底层打开异常归类为 ``unsupported`` / ``invalid_bytes`` / ``other``。

    - openslide 的 ``OpenSlideUnsupportedFormatError``（按类型名识别，避免
      依赖 openslide 已安装）：解析器明确不支持该格式；
    - tifffile 的 ``TiffFileError``：字节根本无法按 TIFF 解析 → 字节明显无效；
    - 其余（IOError、stub/缺库、解析中途损坏等）：other。
    """
    cls = type(e)
    name = cls.__name__
    mod = (getattr(cls, "__module__", "") or "").lower()
    if "openslide" in mod and "unsupportedformat" in name.lower():
        return "unsupported"
    if "tifffile" in mod and name == "TiffFileError":
        return "invalid_bytes"
    return "other"


def _classify_open_failure(verdicts):
    """全部打开尝试失败后的稳定错误码裁定。

    ``verdicts`` 是 ``[(kind, exc), ...]``。规则（与 SlideValidationError
    docstring 的码表一致）：
      1. 任一尝试给出"字节明显无效"信号 → ``invalid_slide``；
      2. 至少一次尝试且全部是"明确不支持"→ ``slide_open_unsupported``；
      3. 其余（损坏/截断/IO/缺库/未知）→ ``slide_open_failed``。
    ``cause_type`` 取分类信号最强（invalid_bytes > unsupported > other）的
    首个异常类型名；只进日志，不透出给前端。返回 ``(code, cause_type)``。
    """
    kinds = [k for k, _e in verdicts]
    cause = ""
    for priority in ("invalid_bytes", "unsupported", "other"):
        for k, e in verdicts:
            if k == priority and e is not None:
                cause = type(e).__name__
                break
        if cause:
            break
    if "invalid_bytes" in kinds:
        return "invalid_slide", cause
    if verdicts and all(k == "unsupported" for k in kinds):
        return "slide_open_unsupported", cause
    return "slide_open_failed", cause


def _is_ome_tiff(path) -> bool:
    """快速嗅探是否为 OME-TIFF（读首部描述标签，失败按 False）。"""
    try:
        import tifffile

        with tifffile.TiffFile(str(path)) as tf:
            return bool(tf.is_ome)
    except Exception:  # noqa: BLE001
        return False


def open_slide(path, *, format_hint=None):
    """工厂函数：打开切片，返回 OpenSlide 或 TiffFileSlide。

    策略（TIFF 类文件，含 .ome.tif/.ome.tiff）：
    1. OME-TIFF 优先走 TiffFileSlide——OpenSlide 的 generic-tiff 驱动
       虽然能打开 OME-TIFF，但只认 level 0（不识别 SubIFD 金字塔），
       且不解析 OME-XML 的 PhysicalSize（mpp 丢失）；
    2. 其余 TIFF 先试 openslide.OpenSlide（lazy import，使无 openslide
       库的机器也能单独使用 TiffFileSlide），失败回退 TiffFileSlide；
    3. OME-TIFF 若 TiffFileSlide 失败（异形 axes 等），回退 OpenSlide 保底。

    上传修复 A0：
    - **实际字节永远从 ``path`` 读取**；``format_hint`` 只参与逻辑格式判定
      （V1 把文件暂存为 ``.uploading-*.part``：调用方传净化后的原始 basename；
      V2 传 task 的 ``safe_name``。``.part`` 后缀本身永远不参与判定）。
    - OME 优先分支与普通 TIFF fallback **都**按 ``format_hint or path`` 的
      逻辑扩展名判定（修复只看 ``.part`` 后缀导致两个 TIFF 分支都被跳过、
      仅剩 OpenSlide 尝试的问题）。
    - 失败抛 :class:`SlideValidationError`（稳定机器码，见其 docstring），
      不再透出裸底层异常；分类规则见 :func:`_classify_open_failure`。
    """
    ext = logical_format_ext(format_hint if format_hint else path)
    if not ext:
        raise SlideValidationError(
            "invalid_slide", "不支持的切片格式（按逻辑文件名判定）",
            cause_type="LogicalFormatRejected")
    if os.path.isdir(str(path)):
        raise SlideValidationError(
            "invalid_slide", "切片路径是目录", cause_type="IsADirectoryError")

    verdicts = []  # [(kind, exc)]：每次失败尝试的分类信号

    if ext in _TIFF_LOGICAL_EXTS and _is_ome_tiff(str(path)):
        try:
            return TiffFileSlide(path)
        except Exception as e:  # noqa: BLE001
            verdicts.append((_exc_kind(e), e))

    try:
        import openslide  # lazy import

        return openslide.OpenSlide(str(path))
    except Exception as e:  # noqa: BLE001  OpenSlide 可能抛各种底层异常
        verdicts.append((_exc_kind(e), e))

    if ext in _TIFF_LOGICAL_EXTS:
        try:
            return TiffFileSlide(path)
        except Exception as e:  # noqa: BLE001
            verdicts.append((_exc_kind(e), e))

    code, cause = _classify_open_failure(verdicts)
    raise SlideValidationError(
        code, "切片打开失败（%s）" % code, cause_type=cause)


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
