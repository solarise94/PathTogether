# -*- coding: utf-8 -*-
"""普通图片（BMP/JPEG）读取器：Pillow 全量解码 + OpenSlide duck-type 接口。

供 :func:`slide_io.open_slide` 按逻辑后缀延迟分发（``.bmp/.jpg/.jpeg``），
duck-type 出 DeepZoomGenerator 需要的接口（与 TiffFileSlide 对齐）：
``properties``（dict）、``dimensions``、``level_count``、``level_dimensions``、
``level_downsamples``、``get_best_level_for_downsample(d)``、
``read_region((x,y), level, (w,h)) -> RGBA``、``get_thumbnail(size)``、
``associated_images``、``close()``。

设计取舍（docs/raster-image-compatibility-agent-plan.md §4.2/§4.3/§4.5/§4.6）：
- **不包装 openslide.ImageSlide**：ImageSlide 只是薄包装——不做 EXIF 方向
  校正、没有「后缀 ↔ 真实字节格式」白名单校验、传入 PIL 图的所有权/关闭
  职责不清、没有像素上限钩子，且最小测试环境里 openslide 可能是 stub。
  为满足任务书合同仍需在外层补齐以上全部逻辑，包装的净收益只剩约十几行
  裁剪数学，故直接基于 Pillow 实现。
- **全量解码语义**：普通图片不是可按需读块的金字塔切片。构造时先查
  宽高/像素数/帧数（超限拒绝），再 ``load()`` 全量解码——截断文件在解码
  处稳定失败，不能只看文件头就算验证通过；解码后的 RGB 缓冲常驻直至
  close（约 ``W × H × 3`` 字节，不逐瓦片重复打开/解码文件）。
- **坐标与方向**（§4.3）：EXIF Orientation 1–8 用 ``ImageOps.exif_transpose``
  校正（BMP 无 EXIF 自然跳过），校正后宽高为准；``read_region`` 用 level-0
  像素坐标，越界区域透明 padding、完全越界返回全透明（语义与
  TiffFileSlide/OpenSlide 一致）。仅影响读取表示，原始上传字节与 SHA-256
  由上层保留。
- **颜色**（§4.2）：灰度/调色板/CMYK 转显示 RGB；带 alpha 的 BMP/P
  （``transparency``）按**白底合成**（透明处显示白色），缩略图与瓦片读
  同一常驻缓冲，保证二者一致。ICC profile 字节原样保留在
  ``properties["icc_profile"]``——不默默应用，也不假称色彩已准确校准。
- **不改 Pillow 全局开关**：``LOAD_TRUNCATED_IMAGES``/``MAX_IMAGE_PIXELS``
  保持默认；``DecompressionBombError`` 捕获后映射为稳定错误码。
- **错误契约**（§4.5）：格式不符/无法识别字节/多帧/超限 →
  ``SlideValidationError("invalid_slide", ...)``；解码/截断/IO →
  ``SlideValidationError("slide_open_failed", ..., cause_type=...)``
  （cause_type 只进日志）。不新增机器码，路由/前端不变。
"""

from __future__ import annotations

import os

from PIL import Image, ImageOps

from slide_io import SlideValidationError, logical_format_ext

#: 默认像素上限：50 MP。RGB 常驻缓冲 50M×3 ≈ 150 MB，叠加方向/颜色转换的
#: 瞬时开销后冷打开峰值 RSS 实测约 0.4 GB（8000×6000 合成 BMP，见交付
#: 报告），与默认句柄池（每张切片 6 句柄）的其它格式同量级、可运维。
#: 环境变量 ``RASTER_MAX_PIXELS`` 可覆盖（非法/非正值回退默认）。
_DEFAULT_MAX_PIXELS = 50_000_000


def _env_max_pixels():
    """读取 ``RASTER_MAX_PIXELS`` 环境变量（非法/缺省回退默认值）。"""
    raw = os.environ.get("RASTER_MAX_PIXELS")
    if not raw:
        return _DEFAULT_MAX_PIXELS
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PIXELS
    return v if v > 0 else _DEFAULT_MAX_PIXELS


#: 普通图片像素数上限（W×H；模块加载期读 env，测试可 monkeypatch 本常量）。
RASTER_MAX_PIXELS = _env_max_pixels()

#: 逻辑后缀 → 允许的真实字节格式（PIL ``img.format``）。``.jpg`` 与
#: ``.jpeg`` 是同一解码格式的别名（互认）；伪装字节按格式不符拒绝。
EXT_FORMATS = {".bmp": "BMP", ".jpg": "JPEG", ".jpeg": "JPEG"}


class RasterSlide:
    """BMP/JPEG 普通图片的 OpenSlide duck-type 读取器（单层、全量解码）。

    - ``properties`` 仅含 ``"openslide.vendor" = "raster-image"``（及可选
      ``"icc_profile"`` 字节）；**不含**任何 mpp/objective 键——普通图片
      缺物理标尺，DPI/EXIF 分辨率不得当作组织的 µm/px。
    - ``level_count = 1``：查看层级由现有 DeepZoomGenerator 生成。
    - 类属性 ``is_raster_image = True`` 供上层识别普通图片实例。
    - ``close()`` 幂等；构造中途失败不持有资源；``__del__`` 兜底。
    """

    VENDOR = "raster-image"
    is_raster_image = True

    def __init__(self, path, *, expected_format=None):
        """打开并全量解码普通图片。

        ``expected_format``：允许的真实字节格式（``"BMP"``/``"JPEG"``），
        由 :func:`slide_io.open_slide` 按逻辑后缀传入（``.part + format_hint``
        时路径后缀不可用，必须显式传）；缺省按 ``path`` 后缀推导，推导不出
        拒绝。失败抛 :class:`slide_io.SlideValidationError`（稳定码）。
        """
        self._path = str(path)
        self._img = None
        self._size = (0, 0)
        self._closed = False
        self.properties = {"openslide.vendor": self.VENDOR}

        if expected_format is None:
            expected_format = EXT_FORMATS.get(logical_format_ext(self._path))
            if not expected_format:
                raise SlideValidationError(
                    "invalid_slide",
                    "普通图片后缀无法识别（支持 .bmp/.jpg/.jpeg）",
                    cause_type="RasterExtRejected")

        # ---- lazy 头部打开（只读文件头，不解码像素） ------------------------
        try:
            img = Image.open(self._path)
        except Image.DecompressionBombError as e:
            # 声明尺寸触发解压炸弹保护：按超限稳定拒绝（不关保护）
            raise SlideValidationError(
                "invalid_slide", "普通图片像素超过解压保护上限",
                cause_type="DecompressionBombError") from e
        except Image.UnidentifiedImageError as e:
            raise SlideValidationError(
                "invalid_slide",
                "无法识别的图片字节（%s）" % expected_format,
                cause_type="UnidentifiedImageError") from e
        except OSError as e:
            raise SlideValidationError(
                "slide_open_failed", "图片读取失败",
                cause_type=type(e).__name__) from e

        try:
            self._open_decode(img, expected_format)
        except SlideValidationError:
            raise
        except Exception as e:  # noqa: BLE001  未知异常收敛稳定码
            raise SlideValidationError(
                "slide_open_failed", "图片解码失败",
                cause_type=type(e).__name__) from e

    # ---- 打开/解码流水（仅供 __init__；拆出便于异常归因清晰） ------------
    def _open_decode(self, img, expected_format):
        # 1) 真实字节格式校验：伪装（PNG/TIFF 字节等）→ invalid_slide
        fmt = (getattr(img, "format", None) or "").upper()
        if fmt != expected_format:
            raise SlideValidationError(
                "invalid_slide",
                "图片真实格式（%s）与扩展名要求（%s）不符"
                % (fmt or "unknown", expected_format),
                cause_type="RasterFormatMismatch")

        # 2) 解码前查宽高/像素数（§4.6：完整解码前检查）
        w, h = img.size
        if w * h > RASTER_MAX_PIXELS:
            raise SlideValidationError(
                "invalid_slide",
                "普通图片像素超过上限：%d×%d=%d（上限 %d 像素）"
                % (w, h, w * h, RASTER_MAX_PIXELS),
                cause_type="RasterPixelLimit")

        # 3) 多帧拒绝：首版仅支持单帧（不悄悄取首帧）
        n_frames = getattr(img, "n_frames", 1)
        if n_frames > 1:
            raise SlideValidationError(
                "invalid_slide",
                "普通图片包含多帧（n_frames=%d），仅支持单帧" % n_frames,
                cause_type="RasterMultiframe")

        # 4) 全量解码：截断/损坏文件在此稳定失败
        try:
            img.load()
        except Image.DecompressionBombError as e:
            raise SlideValidationError(
                "invalid_slide", "普通图片像素超过解压保护上限",
                cause_type="DecompressionBombError") from e
        except Exception as e:  # noqa: BLE001  截断/损坏/IO → slide_open_failed
            raise SlideValidationError(
                "slide_open_failed", "图片解码失败（可能截断或损坏）",
                cause_type=type(e).__name__) from e

        # 5) ICC profile 字节：保留（不默默应用、不假称色彩已校准）
        icc = img.info.get("icc_profile")
        if icc:
            self.properties["icc_profile"] = icc

        # 6) EXIF 方向校正（覆盖 1–8）。无方向标签/方向为 1 时**跳过**：
        #    exif_transpose 无条件复制整图，对无 EXIF 的大 BMP 是白费的
        #    W×H×3 瞬时副本；确需旋转/镜像时才转置，并尽早释放原图缓冲。
        try:
            orientation = img.getexif().get(0x0112)
        except Exception:  # noqa: BLE001  EXIF 读不出按无方向处理
            orientation = None
        if orientation in (None, 1):
            oriented = img
        else:
            try:
                oriented = ImageOps.exif_transpose(img)
            except Image.DecompressionBombError as e:
                raise SlideValidationError(
                    "invalid_slide", "普通图片像素超过解压保护上限",
                    cause_type="DecompressionBombError") from e
            except Exception as e:  # noqa: BLE001  EXIF 损坏 → 不建立错误坐标系
                raise SlideValidationError(
                    "slide_open_failed", "EXIF 方向校正失败",
                    cause_type=type(e).__name__) from e
            img.close()  # 转置副本已独立，原图解码缓冲尽快释放
            img = None

        # 7) 颜色归一为显示 RGB；带 alpha 的 BMP/P → 白底合成（§4.2）
        self._img = self._to_display_rgb(oriented)
        self._size = self._img.size

    @staticmethod
    def _to_display_rgb(img):
        """灰度/调色板/CMYK → RGB；透明像素按白底合成，返回常驻 RGB 缓冲。"""
        mode = img.mode
        has_alpha = mode in ("RGBA", "LA", "PA") or (
            mode == "P" and "transparency" in img.info)
        if has_alpha:
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            return bg
        if mode != "RGB":
            return img.convert("RGB")
        return img

    # ---- OpenSlide 兼容属性/方法（与 TiffFileSlide 语义对齐） ------------
    @property
    def dimensions(self):
        return self._size

    @property
    def level_count(self):
        return 1

    @property
    def level_dimensions(self):
        return (self._size,)

    @property
    def level_downsamples(self):
        return (1.0,)

    def get_best_level_for_downsample(self, downsample):
        """单层图片恒返回 0（层级由 DeepZoomGenerator 生成）。"""
        return 0

    def read_region(self, location, level, size):
        """读取区域，返回 PIL RGBA，尺寸恰好 (w, h)。

        ``location=(x, y)`` 是 level-0 坐标（EXIF 校正后坐标系）。越界部分
        透明 padding；完全越界返回全透明（OpenSlide 语义）。level 仅支持 0
        （越界 level 收敛到 0，与 TiffFileSlide 一致）。
        """
        if self._img is None:
            raise ValueError("RasterSlide 已关闭")
        x0, y0 = location
        w, h = size
        w = int(w)
        h = int(h)
        sx = max(0, int(x0))
        sy = max(0, int(y0))
        ex = min(self._size[0], int(x0) + w)
        ey = min(self._size[1], int(y0) + h)
        valid_w = max(0, ex - sx)
        valid_h = max(0, ey - sy)

        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if valid_w <= 0 or valid_h <= 0:
            return out  # 完全越界：全透明
        crop = self._img.crop((sx, sy, ex, ey)).convert("RGBA")
        out.paste(crop, (sx - int(x0), sy - int(y0)))
        return out

    def get_thumbnail(self, size):
        """整图缩放（保比例 contain），返回 PIL RGBA——与瓦片同一常驻缓冲。

        先缩 RGB 再转 RGBA：白底合成已在常驻缓冲完成，两种顺序结果相同，
        但近上限图片可省一次全尺寸 RGBA 瞬时副本（峰值内存实测约省 0.3×）。
        """
        if self._img is None:
            raise ValueError("RasterSlide 已关闭")
        w, h = size
        tw, th = self._size
        scale = min(float(w) / tw if tw else 1.0, float(h) / th if th else 1.0)
        new_w = max(1, int(round(tw * scale)))
        new_h = max(1, int(round(th * scale)))
        return self._img.resize((new_w, new_h), Image.LANCZOS).convert("RGBA")

    @property
    def associated_images(self):
        """普通图片无关联图（空 dict，与现有消费者容忍度一致）。"""
        return {}

    # ---- 生命周期 --------------------------------------------------------
    def close(self):
        """释放常驻解码缓冲（幂等；不持有文件句柄）。"""
        self._img = None
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
