# -*- coding: utf-8 -*-
"""直传类别（direct_class）声明核验——先转换后上传阶段 1。

单一实现，供两处共用（不 import app，避免模块环）：

  - COS 摄取 worker（cos_ingest_worker）：``open_slide`` 试开**之前**核验
    创建时随任务声明的 direct_class；不符 → validation_failed 族失败，
    错误码 ``convert_in_browser``；
  - 百度分享导入（baidu_ingest）：下载落盘后复查——只放行 OME-TIFF /
    转换器 BigTIFF / 普通 TIFF（暂时直传规则），其余拒绝。

合同（docs/slide-tools/upload-convert-first-phase1.md §3）：

  direct_class 声明值（/api/ingestions 可选字段）：
    ``ome-tiff``                    TIFF 且 ImageDescription 含 OME-XML；
    ``converter-bigtiff``           本机转换工具导出的经典 BigTIFF——
                                    ImageDescription 是转换器写的 JSON，
                                    带转换器来源标记（source_format ∈
                                    CONVERTER_SOURCE_FORMATS，见
                                    convert_svs.rs/convert_bf.rs/
                                    convert_mirax.rs 的 description 写法）；
    ``legacy-direct``               无额外头级声明的普通直传（ndpi/vms/
                                    …/普通图片/普通 TIFF）——只做占位，
                                    字节合法性仍由 open_slide 裁定；
    ``unconverted-variant:svs-jp2k``  JPEG2000 编码的 Aperio SVS——第 0 层
                                    压缩必须是 33003(JP2K)/33005(JPX)。

核验**只读文件头/IFD，绝不读整个文件**：TIFF 魔数 + tifffile 的首 IFD
惰性解析（is_ome/description/compression），zip 只扫中央目录文件名。
"""

from __future__ import annotations

import json
import zipfile

#: 声明词表（``unconverted-variant:<variant>`` 按前缀匹配，variant 白名单
#: 见 _VARIANT_CHECKS）
DIRECT_CLASSES = frozenset({
    "ome-tiff",
    "converter-bigtiff",
    "legacy-direct",
})

#: unconverted-variant 白名单：变体名 → (说明, 第 0 层压缩码集合)
_VARIANT_CHECKS = {
    "svs-jp2k": ("JPEG2000 编码的 Aperio SVS", frozenset({33003, 33005})),
}

#: unconverted-variant:* 前缀
VARIANT_PREFIX = "unconverted-variant:"

#: 转换器经典 BigTIFF 的 ImageDescription JSON 里的来源标记值
#: （slide-transform-core：kfb/converter.py、convert_svs.rs、convert_mirax.rs）
CONVERTER_SOURCE_FORMATS = frozenset({
    "kfb_bf_v1",          # 明场 KFB → 经典多 IFD BigTIFF（kfb/converter.py）
    "kfb_kfbio_jpeg",     # 明场 KFB 旧版本头
    "aperio-svs-jpeg",    # SVS → classic/ome（classic 描述 JSON 带 adapter）
    "mirax-bundle",       # MRXS → 经典 BigTIFF
})

#: snniff 结果词表（actual 类别）
ACTUAL_OME = "ome-tiff"
ACTUAL_CONVERTER = "converter-bigtiff"
ACTUAL_SVS_JP2K = "svs-jp2k"
ACTUAL_TIFF_OTHER = "tiff-other"
ACTUAL_NON_TIFF = "non-tiff"

_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")

#: 好奇心防护：描述标签理论上可巨大；只读前 1 MiB 判 OME/JSON 标记
_DESC_READ_CAP = 1 << 20


def is_direct_class(value):
    """声明字符串是否在词表内（含 unconverted-variant 白名单变体）。"""
    s = str(value or "").strip().lower()
    if s in DIRECT_CLASSES:
        return True
    if s.startswith(VARIANT_PREFIX):
        return s[len(VARIANT_PREFIX):] in _VARIANT_CHECKS
    return False


def variant_of(value):
    """unconverted-variant:<variant> → variant；其他 → None。"""
    s = str(value or "").strip().lower()
    if s.startswith(VARIANT_PREFIX):
        return s[len(VARIANT_PREFIX):]
    return None


def _is_tiff_magic(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in _TIFF_MAGICS
    except OSError:
        return False


def _converter_marked(desc_text):
    """描述文本是否带转换器来源标记（JSON `source_format` 白名单值）。"""
    if not desc_text:
        return False
    # 描述以 NUL 结尾（转换器约定），且是紧凑 JSON——截到 NUL 再解析
    body = desc_text.split("\x00", 1)[0].strip()
    if not body.startswith("{"):
        return False
    try:
        obj = json.loads(body)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    return str(obj.get("source_format") or "") in CONVERTER_SOURCE_FORMATS


def sniff_tiff_class(path):
    """按文件头/首 IFD 嗅探实际类别。

    返回 ACTUAL_* 常量之一。只读魔数与首 IFD（tifffile 惰性解析；
    is_ome/description/compression 都不触碰金字塔数据）。
    """
    if not _is_tiff_magic(path):
        return ACTUAL_NON_TIFF
    try:
        import tifffile

        with tifffile.TiffFile(str(path)) as tf:
            if bool(getattr(tf, "is_ome", False)):
                return ACTUAL_OME
            page0 = tf.pages[0] if tf.pages else None
            if page0 is None:
                return ACTUAL_TIFF_OTHER
            desc = ""
            try:
                desc = page0.description or ""
            except Exception:  # noqa: BLE001 — 描述标签损坏不猜测
                desc = ""
            if desc and len(desc) <= _DESC_READ_CAP and _converter_marked(desc):
                return ACTUAL_CONVERTER
            try:
                compression = int(page0.compression or 0)
            except Exception:  # noqa: BLE001
                compression = 0
            if compression in (33003, 33005):
                return ACTUAL_SVS_JP2K
            return ACTUAL_TIFF_OTHER
    except Exception:  # noqa: BLE001 — 非法/截断字节按非 TIFF 处理，
        return ACTUAL_NON_TIFF  # 声明核验判 False；open_slide 再给终审


def declaration_matches(path, declared_class, filename=None):
    """声明 vs 实际字节：True=相符（可继续 open_slide 终审）。

    - ``legacy-direct``：无头级合同 → True（字节合法性由 open_slide 裁定）；
    - ``ome-tiff``：必须 is_ome；
    - ``converter-bigtiff``：描述必须带转换器来源标记；
    - ``unconverted-variant:svs-jp2k``：第 0 层压缩 ∈ {33003, 33005}；
    - 头部读不出来（损坏/截断）→ False（具体声明无法证实即不放行；
      未声明 legacy 的任务不受影响，仍由 open_slide 给 validation_failed）。
    """
    declared = str(declared_class or "").strip().lower()
    if not declared:
        return True   # 未声明（存量任务/店级测试直建）：不做头级核验
    if declared == "legacy-direct":
        return True
    if not is_direct_class(declared):
        return False
    variant = variant_of(declared)
    if variant is not None:
        _label, compressions = _VARIANT_CHECKS[variant]
        # 复用嗅探的压缩判定：svs-jp2k 的 actual 即代表第 0 层压缩命中
        return sniff_tiff_class(path) == ACTUAL_SVS_JP2K
    actual = sniff_tiff_class(path)
    if declared == ACTUAL_OME:
        return actual == ACTUAL_OME
    if declared == ACTUAL_CONVERTER:
        return actual == ACTUAL_CONVERTER
    return False


def zip_contains_bundle_entry(path):
    """zip 中央目录里是否含 MRXS 主入口（.mrxs）。

    先转换后上传阶段 1 关闭「zip 中含 MRXS 包」的上传：只扫中央目录文件名
    （不触碰成员字节）。目录条目以 / 结尾，天然不匹配后缀。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if str(name).lower().endswith(".mrxs"):
                    return True
    except Exception:  # noqa: BLE001 — 坏 zip 交给解包路径给稳定错误码
        return False
    return False
