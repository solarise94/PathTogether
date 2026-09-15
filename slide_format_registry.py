# -*- coding: utf-8 -*-
"""切片格式能力注册表（KFB Phase A）。

单一事实来源：每种扩展名一个 capability，供后续上传分流 / 前端能力展示 /
转换 worker 决策复用。**不**反向 import app（slide_io 同理，避免模块环），
故扩展名词表在此复制一份，与 ``app.SUPPORTED_EXTS`` / ``slide_io.LOGICAL_EXTS``
同步维护（tests/test_slide_format_registry.py 断言两侧一致，防漂移）。

capability 语义（docs/kfb-ingestion-converter-review.md §3.1/§4）：
  - ``native-single-file``：OpenSlide/现有 reader 直接读单文件，原样保留；
  - ``native-bundle``：主文件 + 同名伴随目录（MRXS），不能当单文件入口；
  - ``convert-required``：需后台转换为 canonical 格式（明场 KFB → 经典
    多 IFD BigTIFF；荧光 KFBF → 多通道 OME-TIFF）；
  - ``unsupported``：明确不支持，fail-closed（未知扩展名）。

注意：``.kfb`` 在此登记 **不** 意味着加入上传白名单——``app.SUPPORTED_EXTS``
与前端 accept 在 Phase B/C 之前保持不变。
"""

from __future__ import annotations

CAP_NATIVE_SINGLE_FILE = "native-single-file"
CAP_NATIVE_BUNDLE = "native-bundle"
CAP_CONVERT_REQUIRED = "convert-required"
CAP_UNSUPPORTED = "unsupported"

#: 与 app.SUPPORTED_EXTS 同集（svs/tif/tiff/ndpi/mrxs/vms/vmu/scn/bif/svslide）
#: + 待转换/明确不支持的扩展名。key 为小写含点扩展名。
_FORMATS = {
    # --- 原生单文件（OpenSlide 或 TiffFileSlide 直接读） -------------------
    ".svs": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Aperio SVS；OpenSlide 原生，避免二次 JPEG 压缩",
    },
    ".tif": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "TIFF（含通用 tiled / BigTIFF）；OpenSlide generic-tiff 或 TiffFileSlide",
    },
    ".tiff": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "同 .tif",
    },
    ".ndpi": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Hamamatsu NDPI；OpenSlide 原生",
    },
    ".vms": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Sakura VMS",
    },
    ".vmu": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Sakura VMU",
    },
    ".scn": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Leica SCN",
    },
    ".bif": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Ventana BIF",
    },
    ".svslide": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "GE/Synthesys SVSlide",
    },
    # --- 普通图片族（BMP/JPEG；raster_slide.RasterSlide 直接读） -----------
    ".bmp": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 BMP；RasterSlide（Pillow）全量解码，"
                 "按真实字节格式校验伪装",
    },
    ".jpg": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 JPEG；与 .jpeg 同一解码格式的别名"
                 "（RasterSlide 按后缀互认 JPEG 字节）",
    },
    ".jpeg": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 JPEG；同 .jpg",
    },
    # --- 原生 bundle（主文件 + 伴随目录） ----------------------------------
    ".mrxs": {
        "capability": CAP_NATIVE_BUNDLE,
        "canonical_ext": None,
        "notes": "3DHISTECH MRXS；需同名伴随目录，删除/配额按 bundle 结算",
    },
    # --- 需转换（Phase A：明场 KFB 离线 spike，不接上传） -------------------
    ".kfb": {
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_ext": ".tif",
        "notes": "明场 KFB（kfb_bf_v1 合同）→ 经典多 IFD JPEG tiled BigTIFF；"
                 "仅 kfb/ 包离线转换，尚未接入上传入口",
    },
    # --- 明确需转换（荧光 KFBF：真实样本校准后的多通道 OME-TIFF 通道） ----
    ".kfbf": {
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_ext": ".ome.tif",
        "notes": "荧光 KFBF（kfb_fl_v1 合同，KFFL 真实样本校准）→ 多通道 "
                 "金字塔 OME-TIFF；保留通道名/颜色/曝光/mpp；"
                 "不得从明场路径推导",
    },
}

#: 归档扩展名：不是切片格式，解压后按成员再查注册表（zip 上传解包用）
ARCHIVE_EXTS = frozenset({".zip"})


def lookup(filename):
    """按文件名（basename/路径均可）查询格式能力。

    返回 ``{"ext": str, "capability": str, "canonical_ext": str|None,
    "notes": str}``；未知扩展名 / 无扩展名 → capability=
    ``unsupported``（ext 为小写含点后缀或 ``""``）。纯字符串级判定，
    不触碰文件系统。
    """
    s = "" if filename is None else str(filename)
    base = s.replace("\\", "/").rsplit("/", 1)[-1].lower()
    ext = ""
    if "." in base.lstrip("."):
        ext = "." + base.rsplit(".", 1)[-1]
    info = _FORMATS.get(ext)
    if info is None:
        return {
            "ext": ext,
            "capability": CAP_UNSUPPORTED,
            "canonical_ext": None,
            "notes": "未登记的扩展名；不自动猜测格式，fail-closed",
        }
    return {
        "ext": ext,
        "capability": info["capability"],
        "canonical_ext": info["canonical_ext"],
        "notes": info["notes"],
    }


# --------------------------------------------------------------------------- #
# W4：面向产品的格式目录（public_catalog）
#
# 与 _FORMATS 的关系：_FORMATS/lookup 是**引擎判定**词表（notes 面向开发者，
# 契约测试锁定，保持原样）；public_catalog 是**产品展示**词表——独立展示表，
# 文案面向最终用户，不复用 notes（含「尚未接入上传」等已过时表述）。
# .ome.tif / .ome.tiff 不进 _FORMATS（lookup 按最后一段 .tif 归类即可），
# 但目录中单列一行，保证复合后缀对用户明确可见。
# --------------------------------------------------------------------------- #
#: OME-TIFF 复合后缀（lookup 按末段 .tif 归到 native 单文件；目录单列展示）
_OME_EXTS = (".ome.tif", ".ome.tiff")


def ome_extensions():
    """OME-TIFF 复合后缀元组（.ome.tif / .ome.tiff）。"""
    return _OME_EXTS


#: 产品目录展示表（id 唯一；extensions 不重叠）。capability 在 public_catalog
#: 里按首个扩展名回查 _FORMATS 防漂移（.ome.* 不在 _FORMATS，取声明值）。
_CATALOG_DISPLAY = (
    {
        "id": "svs",
        "display_name": "Aperio SVS",
        "extensions": [".svs"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "tif",
        "display_name": "TIFF / BigTIFF",
        "extensions": [".tif", ".tiff"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "ome-tiff",
        "display_name": "OME-TIFF",
        "extensions": [".ome.tif", ".ome.tiff"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": ["请使用完整的 .ome.tif / .ome.tiff 后缀命名，"
                   "多通道荧光元数据可被直接识别"],
        "selectable_for_upload": True,
    },
    {
        "id": "ndpi",
        "display_name": "Hamamatsu NDPI",
        "extensions": [".ndpi"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "mrxs",
        "display_name": "3DHISTECH MRXS",
        "extensions": [".mrxs"],
        "capability": CAP_NATIVE_BUNDLE,
        "canonical_format": None,
        "bundle_required": True,
        "import_mode": "bundle",
        "limits": ["需要完整包（主文件 + 同名伴随目录），请打包 zip 上传"],
        "selectable_for_upload": True,
    },
    {
        "id": "vms",
        "display_name": "Sakura VMS",
        "extensions": [".vms"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "vmu",
        "display_name": "Sakura VMU",
        "extensions": [".vmu"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "scn",
        "display_name": "Leica SCN",
        "extensions": [".scn"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "bif",
        "display_name": "Ventana BIF",
        "extensions": [".bif"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "svslide",
        "display_name": "GE / Synthesys SVSlide",
        "extensions": [".svslide"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": [],
        "selectable_for_upload": True,
    },
    {
        "id": "raster-image",
        "display_name": "普通图片（BMP / JPEG）",
        "extensions": [".bmp", ".jpg", ".jpeg"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "import_mode": "direct",
        "limits": ["普通图片、支持像素坐标、无物理标尺"],
        "selectable_for_upload": True,
    },
    {
        "id": "kfb",
        "display_name": "KFB（明场）",
        "extensions": [".kfb"],
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_format": "bigtiff",
        "bundle_required": False,
        "import_mode": "convert",
        "limits": ["上传后后台转换为 BigTIFF（明场）"],
        "selectable_for_upload": True,
    },
    {
        "id": "kfbf",
        "display_name": "KFBF（荧光）",
        "extensions": [".kfbf"],
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_format": "ome-tiff",
        "bundle_required": False,
        "import_mode": "convert",
        "limits": ["上传后后台转换为多通道 OME-TIFF（荧光）"],
        "selectable_for_upload": True,
    },
)


def public_catalog():
    """面向产品的格式目录（W4；每次返回新副本，调用方可安全改写）。

    每项字段：``id / display_name / extensions / capability /
    canonical_format(None|'bigtiff'|'ome-tiff') / bundle_required /
    import_mode('direct'|'convert'|'bundle') / limits(用户向短句) /
    selectable_for_upload``。capability 按首个扩展名回查 ``_FORMATS``
    （防两表漂移；.ome.* 复合后缀不在 _FORMATS，取声明值）。
    文案不复用 _FORMATS.notes（那是引擎判定备注，含已过时的接入状态）。
    """
    items = []
    for row in _CATALOG_DISPLAY:
        item = {
            "id": row["id"],
            "display_name": row["display_name"],
            "extensions": list(row["extensions"]),
            "capability": row["capability"],
            "canonical_format": row["canonical_format"],
            "bundle_required": row["bundle_required"],
            "import_mode": row["import_mode"],
            "limits": list(row["limits"]),
            "selectable_for_upload": row["selectable_for_upload"],
        }
        info = _FORMATS.get(item["extensions"][0])
        if info is not None:
            item["capability"] = info["capability"]
        items.append(item)
    return items
