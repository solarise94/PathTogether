# -*- coding: utf-8 -*-
"""切片格式能力注册表（KFB Phase A）。

单一事实来源：每种扩展名一个 capability，供后续上传分流 / 前端能力展示 /
转换 worker 决策复用。**不**反向 import app（slide_io 同理，避免模块环），
故扩展名词表在此复制一份，与 ``app.SUPPORTED_EXTS`` / ``slide_io.LOGICAL_EXTS``
同步维护（tests/test_slide_format_registry.py 断言两侧一致，防漂移）。

capability 语义（docs/kfb-ingestion-converter-review.md §3.1/§4）：
  - ``native-single-file``：OpenSlide/现有 reader 直接读单文件，原样保留；
  - ``native-bundle``：主文件 + 同名伴随目录（MRXS），不能当单文件入口；
  - ``convert-required``：需后台转换为 canonical 格式（Phase A 仅明场 KFB）；
  - ``unsupported``：明确不支持，fail-closed（KFBF 荧光未经样本校准）。

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
    # --- 明确不支持 --------------------------------------------------------
    ".kfbf": {
        "capability": CAP_UNSUPPORTED,
        "canonical_ext": None,
        "notes": "荧光 KFBF：无真实样本校准，fail-closed；"
                 "未来单独走多通道 OME-TIFF 通道，不得从明场路径推导",
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
