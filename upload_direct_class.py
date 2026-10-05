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

内存合同（审查修复）：**所有读取都有界**——魔数 8 字节、IFD0 表
≤ 1 MiB、描述标签 ≤ _DESC_READ_CAP（1 MiB，从标签偏移处定长读取）。
不用 tifffile 的 page.description / is_ome（它们会把任意大的
ImageDescription 全量读入内存——craft 出的描述可以和文件一样大，构成
worker/百度路径的 OOM 面）。ZIP 检查只扫中央目录文件名。

内容级关闭策略（防扩展名伪装，worker 用）：JPEG 编码 Aperio SVS 把扩展名
改成 .tif/.tiff 后，创建闸（只看扩展名）放行、open_slide 按内容打开——
``enforcement_failure`` 对 tif/tiff 名 + 无声明/legacy-direct 的任务按
描述中的 Aperio 厂商标记 + 压缩 7 识别并拒绝（unconverted-variant:svs-jp2k
与 ome/converter 声明路径不受影响——后者已由 declaration_matches 裁定）。
"""

from __future__ import annotations

import json
import struct
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

#: sniff 结果词表（actual 类别）
ACTUAL_OME = "ome-tiff"
ACTUAL_CONVERTER = "converter-bigtiff"
ACTUAL_SVS_JP2K = "svs-jp2k"
ACTUAL_TIFF_OTHER = "tiff-other"
ACTUAL_NON_TIFF = "non-tiff"

#: 描述标签的定长读取上限（OME-XML 头与转换器 JSON 都在头部；绝不按标签
#: 声明的 count 全量读取——count 可以和文件一样大）
_DESC_READ_CAP = 1 << 20

#: IFD0 表读取上限（防御：条目数声明可以很大）
_IFD_READ_CAP = 1 << 20

#: TIFF 标签字节数：2(BYTE)/3(SHORT)/4(LONG)/16(LONG8)——本模块只消费这些
_TYPE_UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 16: 8}

#: Aperio SVS 的厂商标记（openslide svs 读取器同判据：描述标识 Aperio）
_APERIO_MARKER = "aperio"

#: SVS 的 JPEG 编码压缩码（Aperio 明场）；33003/33005 是 JP2K/JPX
_JPEG_COMPRESSION = 7


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


# --------------------------------------------------------------------------- #
# 有界 TIFF 头解析（魔数 + IFD0；classic 与 BigTIFF、双端序）
# --------------------------------------------------------------------------- #
def _parse_tiff_head(fh):
    """解析魔数与 IFD0 条目。返回 dict 或 None（非 TIFF/坏头）。

    每个条目 → tag: (type, count, data_offset|None, inline_bytes|None)。
    内联值（total ≤ 值域字段）直接带字节；否则带 data_offset（绝不在此处
    读取数据体）。全部读取有界：头 8/16 字节 + IFD 表 ≤ _IFD_READ_CAP。
    """
    head = fh.read(8)
    if len(head) < 8:
        return None
    if head[:2] == b"II":
        bo = "<"
    elif head[:2] == b"MM":
        bo = ">"
    else:
        return None
    magic = struct.unpack(bo + "H", head[2:4])[0]
    if magic == 42:
        bigtiff = False
    elif magic == 43:
        bigtiff = True
    else:
        return None

    if bigtiff:
        # 头布局（16B）：II(2) 43(2) offsetsizes(2,须为 2) reserved(2) IFD 偏移(8)
        if len(head) < 6 or head[4] != 2:
            return None
        extra = fh.read(8)
        if len(extra) < 8:
            return None
        ifd_off = struct.unpack(bo + "Q", extra)[0]
        ifd_cnt_size, entry_size, value_size = 8, 20, 8
        ifd_cnt_fmt, field_cnt_fmt = "Q", "Q"    # 条目数与条目内 count 同宽
    else:
        ifd_off = struct.unpack(bo + "I", head[4:8])[0]
        # classic：IFD 条目数是 2 字节 SHORT；条目内的 count 字段是 4 字节
        ifd_cnt_size, entry_size, value_size = 2, 12, 4
        ifd_cnt_fmt, field_cnt_fmt = "H", "I"

    fh.seek(ifd_off)
    cnt_raw = fh.read(ifd_cnt_size)
    if len(cnt_raw) < ifd_cnt_size:
        return None
    count = struct.unpack(bo + ifd_cnt_fmt, cnt_raw)[0]
    if count > _IFD_READ_CAP // entry_size:
        return None                              # 防御：条目数声明离谱
    raw = fh.read(count * entry_size)
    entries = {}
    # 条目内布局（classic 12B / bigtiff 20B）：
    #   tag [0:2] | type [2:4] | count [4:4+cnt_w] | value [val_off:val_off+value_size]
    cnt_w = 8 if bigtiff else 4
    val_off = 4 + cnt_w
    for i in range(count):
        e = raw[i * entry_size:(i + 1) * entry_size]
        if len(e) < entry_size:
            break
        tag = struct.unpack(bo + "H", e[0:2])[0]
        typ = struct.unpack(bo + "H", e[2:4])[0]
        cnt = struct.unpack(bo + ("Q" if bigtiff else "I"), e[4:val_off])[0]
        vfield = e[val_off:val_off + value_size]
        unit = _TYPE_UNIT.get(typ)
        total = unit * cnt if unit else None
        if total is not None and total <= value_size:
            entries[tag] = (typ, cnt, None, vfield[:total])     # 内联
        else:
            entries[tag] = (typ, cnt,
                            struct.unpack(bo + ("Q" if bigtiff else "I"),
                                          vfield)[0], None)      # 偏移
    return {"bo": bo, "bigtiff": bigtiff, "entries": entries}


def _inline_uint(entry, bo):
    """内联数值（SHORT/LONG/LONG8 的第一个值；本模块只对压缩码用）。"""
    typ, cnt, offset, inline = entry
    if offset is not None or cnt < 1 or inline is None:
        return 0
    width = _TYPE_UNIT.get(typ)
    if width not in (2, 4, 8):
        return 0
    try:
        return struct.unpack(bo + {2: "H", 4: "I", 8: "Q"}[width],
                             inline[:width])[0]
    except struct.error:
        return 0


def _bounded_text(fh, bo, entry, bigtiff):
    """描述标签（ASCII）的定长读取（≤ _DESC_READ_CAP）→ str。

    只按 offset + min(count, cap) 读取——count 声明可以任意大（可以和
    文件一样大），绝不按声明全量读。
    """
    typ, cnt, offset, inline = entry
    if typ != 2:
        return ""
    if inline is not None:
        data = inline
    else:
        if offset is None:
            return ""
        cap = min(cnt, _DESC_READ_CAP)
        fh.seek(offset)
        data = fh.read(cap)
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 解码失败按无标记处理
        return ""


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


def _looks_like_ome_xml(text):
    """有界 OME-XML 判定：XML 声明开头 + OME 标记在头部窗口内
    （OME-XML 的根元素紧跟 XML 声明，1 MiB 窗口覆盖任何真实文件）。"""
    if not text:
        return False
    head = text.lstrip()[:4096]
    return head.startswith("<?xml") and "OME" in head[:2048]


def _is_aperio_svs(desc_text, compression):
    """JPEG 编码 Aperio SVS：描述标识 Aperio（openslide svs 同判据）且
    第 0 层压缩为 JPEG(7)。JP2K 压缩不算（svs-jp2k 是放行的暂时变体）。"""
    if compression != _JPEG_COMPRESSION:
        return False
    return _APERIO_MARKER in (desc_text or "").lower()


def sniff_tiff_class(path):
    """按文件头/首 IFD 嗅探实际类别（**全部读取有界**）。

    返回 ACTUAL_* 常量之一。只读魔数、IFD0 表与描述标签前
    _DESC_READ_CAP 字节；绝不按标签声明的 count 全量读取。
    """
    try:
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return ACTUAL_NON_TIFF
            bo, entries = head["bo"], head["entries"]
            desc_entry = entries.get(270)
            desc = ""
            if desc_entry is not None:
                desc = _bounded_text(fh, bo, desc_entry, head["bigtiff"])
            if _looks_like_ome_xml(desc):
                return ACTUAL_OME
            if _converter_marked(desc):
                return ACTUAL_CONVERTER
            comp_entry = entries.get(259)
            compression = _inline_uint(comp_entry, bo) if comp_entry else 0
            if compression in (33003, 33005):
                return ACTUAL_SVS_JP2K
            return ACTUAL_TIFF_OTHER
    except OSError:
        return ACTUAL_NON_TIFF


def is_aperio_svs_jpeg(path):
    """内容级判定：JPEG 编码 Aperio SVS（厂商标记 + 压缩 7）。

    「JPEG 编码 .svs 关闭直传」的内容级执行点：SVS 改名 .tif 后创建闸
    （扩展名级）放行、open_slide 按内容打开——worker 在发布前用它拒绝。
    """
    try:
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return False
            desc_entry = head["entries"].get(270)
            desc = (_bounded_text(fh, head["bo"], desc_entry, head["bigtiff"])
                    if desc_entry is not None else "")
            comp_entry = head["entries"].get(259)
            compression = _inline_uint(comp_entry, head["bo"]) \
                if comp_entry else 0
            return _is_aperio_svs(desc, compression)
    except OSError:
        return False


def enforcement_failure(path, declared_class, format_ext):
    """阶段 1 内容级关闭策略（worker 用，declaration_matches 之后调用）。

    返回错误码（目前仅 ``convert_in_browser``）或 None：
      仅对 .tif/.tiff 名 + 无声明/legacy-direct 的任务做 Aperio-SVS-JPEG
      内容检查（伪装扩展名绕过创建闸的唯一现实通道——ndpi/vms 等本就是
      暂时开放的格式）。其余声明路径由 declaration_matches 裁定；JP2K
      变体是放行的暂时直传，不做此检查。
    """
    declared = str(declared_class or "").strip().lower()
    if declared not in ("", "legacy-direct"):
        return None
    ext = str(format_ext or "").lstrip(".").lower()
    if ext not in ("tif", "tiff"):
        return None
    if is_aperio_svs_jpeg(path):
        return "convert_in_browser"
    return None


def declaration_matches(path, declared_class, filename=None):
    """声明 vs 实际字节：True=相符（可继续 open_slide 终审）。

    - ``legacy-direct``：无头级合同 → True（字节合法性由 open_slide 裁定；
      Aperio-SVS 伪装在 enforcement_failure 做内容级执行）；
    - ``ome-tiff``：描述必须含 OME-XML（有界判定）；
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
        # 复用嗅探的压缩判定：svs-jp2k 的 actual 即代表第 0 层压缩命中
        return sniff_tiff_class(path) == ACTUAL_SVS_JP2K
    actual = sniff_tiff_class(path)
    if declared == ACTUAL_OME:
        return actual == ACTUAL_OME
    if declared == ACTUAL_CONVERTER:
        return actual == ACTUAL_CONVERTER
    return False


#: zip 内视为关闭的成员后缀：MRXS 包（本阶段 zip-MRXS 关闭）与 .svs
#: （直传关闭；zip 成员无法逐个声明 JP2K 例外——svs 请直传并带声明）
_ZIP_CLOSED_SUFFIXES = (".mrxs", ".svs")


def zip_closed_format_entries(path):
    """zip 中央目录里本阶段关闭的成员（.mrxs / .svs）→ 排序文件名列表。

    只扫中央目录文件名（不触碰成员字节）。目录条目以 / 结尾，天然不匹配
    后缀。空列表 = 无关闭成员（其余成员照旧走解包/验证/发布）。
    """
    out = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                low = str(name).lower()
                if low.endswith(_ZIP_CLOSED_SUFFIXES):
                    out.append(str(name))
    except Exception:  # noqa: BLE001 — 坏 zip 交给解包路径给稳定错误码
        return []
    return sorted(out)


def zip_contains_bundle_entry(path):
    """兼容别名：zip 内是否含 MRXS 主入口（阶段 1 起请用
    :func:`zip_closed_format_entries`，它同时覆盖 .svs 成员）。"""
    return bool(zip_closed_format_entries(path))
