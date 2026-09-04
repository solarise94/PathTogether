# -*- coding: utf-8 -*-
"""A0 上传修复测试的合成 TIFF fixture（无患者数据）。

用 tifffile 在内存生成最小真实 TIFF / OME-TIFF 字节（合成渐变图案），供
test_slide_io / test_upload_guard / test_upload_v2 的"真验证"用例复用。
不落盘、不含任何真实样本数据；tifffile/zarr 缺失时 ``pytest.skip`` 提示
（CI 镜像按 requirements.txt 安装 tifffile==2024.5.22 + zarr<3）。

注意：``.part`` 场景的断言依赖 TiffFileSlide（tifffile）真实可导入；openslide
在本仓测试自举（tests/_bootstrap.py）中被 stub 成 ``object``——普通 TIFF 走
TiffFileSlide fallback 仍真实打开，"OpenSlide 原生 SVS" 仅能测 stub 语义
（稳定失败码），这一点在各用例 docstring 中注明。
"""
import io

import pytest


def _gradient_rgb(h=64, w=96):
    """合成 RGB 渐变图（确定性强，无随机/患者数据）。"""
    import numpy as np

    img = (np.indices((h, w)).sum(axis=0) % 256).astype(np.uint8)
    return np.stack([img, img[::-1], img[:, ::-1]], axis=-1)


def _require_tifffile():
    try:
        import tifffile  # noqa: F401
    except ImportError as e:  # pragma: no cover
        pytest.skip("tifffile 未安装（requirements.txt 锁定 2024.5.22）: %s" % e)


def make_tiff_bytes(h=64, w=96):
    """普通（非 OME）tiled TIFF 字节。

    tile=(32,32)：真 openslide（CI 经 openslide-bin 安装）的 generic-tiff
    driver 只接受 tiled 图，条带式会被判 OpenSlideUnsupportedFormatError；
    tifffile/PIL/TiffFileSlide 对 tiled 兼容，其余消费方不受影响。
    """
    _require_tifffile()
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, _gradient_rgb(h, w), photometric="rgb",
                     tile=(32, 32))
    return buf.getvalue()


def make_ome_tiff_bytes(h=64, w=96):
    """OME-TIFF 字节（tifffile ome=True，含 OME-XML ImageDescription）。"""
    _require_tifffile()
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, _gradient_rgb(h, w), photometric="rgb", ome=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Batch 2：合成多通道 OME fixture（无患者数据；确定性生成）
# --------------------------------------------------------------------------- #
# 全部经真实 tifffile 2024.5.22 写出再由 TiffFileSlide 读回；axes/level 形态
# 已逐一生成验证（不得 mock axes）：
#   - RGB(ome=True)            → series axes 'YXS'（photometric=RGB, S=3）
#   - YX/CYX/TCZYX(ome=True)   → series axes 'YX'/'CYX'/'TCZYX'
#   - OME 金字塔(subifds)      → series.levels 各级 axes 与基级一致（size-1 轴
#                                 会被 tifffile 从 level axes 中省略，如
#                                 TCZYX(Z=1) 的 level axes 为 'TCYX'）
#   - 显式 Color/多文件 OME    → 手写 OME-XML 走 description=（必须
#                                 metadata=None，否则第二页起会写入 shaped
#                                 JSON 描述，is_shaped 先于 is_ome 命中，
#                                 series 退化为 shaped/QYX——2024.5.22 实测）。
def _pattern2d(h=64, w=96, offset=0):
    """确定性二维底纹（无随机、无患者数据）：x + 3y + offset。"""
    import numpy as np

    yy, xx = np.indices((int(h), int(w)))
    return (xx + 3 * yy + int(offset)).astype(np.int64)


def make_ome_cyx_bytes(c=4, h=64, w=96, dtype="uint16", names=None,
                       colors=None, tile=None, pyramid=0):
    """多通道 CYX OME-TIFF。

    第 c 通道像素 = 底纹 + c*1500（通道间可区分）；names/colors 为
    OME Channel Name/Color（十六进制 "#RRGGBB" 形态，tifffile 原生支持）。
    pyramid>0 时用 subifds 写 OME 金字塔（每级减半）。
    """
    _require_tifffile()
    import numpy as np
    import tifffile

    base = _pattern2d(h, w)
    planes = []
    for ci in range(int(c)):
        v = base + 1500 * ci
        if dtype == "uint8":
            v = v % 256
        planes.append(v.astype(dtype))
    data = np.stack(planes)
    metadata = {"axes": "CYX"}
    if names or colors:
        metadata["Channel"] = {}
        if names:
            metadata["Channel"]["Name"] = list(names)
        if colors:
            metadata["Channel"]["Color"] = list(colors)
    buf = io.BytesIO()
    if int(pyramid) > 0:
        with tifffile.TiffWriter(buf, bigtiff=True, ome=True) as tif:
            tif.write(data, photometric="minisblack", subifds=int(pyramid),
                      metadata=metadata, tile=tile)
            step = 2
            for _ in range(int(pyramid)):
                sl = (slice(None),) + (slice(None, None, step),) * 2
                tif.write(data[sl], subfiletype=1, photometric="minisblack",
                          tile=tile)
                step *= 2
    else:
        tifffile.imwrite(buf, data, photometric="minisblack", ome=True,
                         metadata=metadata, tile=tile)
    return buf.getvalue()


def make_ome_gray_yx_bytes(h=64, w=96, dtype="uint16"):
    """单通道 YX OME-TIFF（无 C 轴灰度）。"""
    _require_tifffile()
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, _pattern2d(h, w).astype(dtype),
                     photometric="minisblack", ome=True,
                     metadata={"axes": "YX"})
    return buf.getvalue()


def make_ome_tczyx_bytes(t=1, c=3, z=1, h=32, w=40, dtype="uint16"):
    """TCZYX OME-TIFF；数据使 (t=0,z=0) 平面与 CYX 单文件同构可互验。"""
    _require_tifffile()
    import numpy as np
    import tifffile

    base = _pattern2d(h, w)
    data = np.empty((int(t), int(c), int(z), int(h), int(w)), dtype=dtype)
    for ti in range(int(t)):
        for ci in range(int(c)):
            for zi in range(int(z)):
                v = base + 1500 * ci + 30000 * ti + 7000 * zi
                if dtype == "uint8":
                    v = v % 256
                data[ti, ci, zi] = v.astype(dtype)
    buf = io.BytesIO()
    tifffile.imwrite(buf, data, photometric="minisblack", ome=True,
                     metadata={"axes": "TCZYX"})
    return buf.getvalue()


def make_ome_uint8_cyx_bytes(c=2, h=64, w=96):
    """uint8 多通道 CYX（dtype 有效范围回退用例）。"""
    return make_ome_cyx_bytes(c=c, h=h, w=w, dtype="uint8")


def make_ome_float_cyx_bytes(c=2, h=32, w=40, nan=False, inf=False,
                             const=False):
    """float32 多通道 CYX；可注入 NaN/Inf/常量（统计口径用例）。"""
    _require_tifffile()
    import numpy as np
    import tifffile

    base = _pattern2d(h, w).astype(np.float32)
    if const:
        base = np.full((int(h), int(w)), 3.25, dtype=np.float32)
    else:
        base = base * 1.5
    planes = [base * (ci + 1) for ci in range(int(c))]
    if nan:
        planes[0][0, 0] = np.float32("nan")
    if inf:
        planes[0][0, 1] = np.float32("inf")
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.stack(planes), photometric="minisblack",
                     ome=True, metadata={"axes": "CYX"})
    return buf.getvalue()


# 手写 OME-XML 头（与 tifffile 2024.5.22 输出同构；tifffile 按描述内容识别
# OME：description 末尾为 'OME>' 即 is_ome=True）
_OME_HDR = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://www.openmicroscopy.org/Schemas/OME/2016-06 '
    'http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd" '
    'UUID="urn:uuid:12345678-1234-1234-1234-123456789abc" '
    'Creator="test-fixture">')


def _write_ome_description_xml(data, xml):
    """按手写 OME-XML 写 OME-TIFF（description= + metadata=None，见上注）。"""
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, data, photometric="minisblack", description=xml,
                     metadata=None)
    return buf.getvalue()


def make_ome_explicit_color_bytes(h=32, w=40):
    """显式 OME RGBA 颜色 fixture（有符号 int 形态，含边界值）。

    4 通道 Channel 元素（RGBA 语义：u32 = signed & 0xffffffff，高字节 r、
    低字节 a）：
      0: Color="-1"        → u32=0xFFFFFFFF → #FFFFFF alpha=255（显式白，OME 来源）
      1: Color="-256"      → u32=0xFFFFFF00 → #FFFFFF alpha=0（默认关闭）
      2: 无 Color 无 Name  → 默认色卡 + 「通道 N」
      3: Color="abc"       → 非整数 → 视为缺失 + warning + 默认色卡
    """
    channels = "".join([
        '<Channel ID="Channel:0:0" SamplesPerPixel="1" Name="DAPI" '
        'Color="-1"><LightPath/></Channel>',
        '<Channel ID="Channel:0:1" SamplesPerPixel="1" Name="FITC" '
        'Color="-256"><LightPath/></Channel>',
        '<Channel ID="Channel:0:2" SamplesPerPixel="1"><LightPath/></Channel>',
        '<Channel ID="Channel:0:3" SamplesPerPixel="1" Name="Ch3" '
        'Color="abc"><LightPath/></Channel>',
    ])
    xml = (
        _OME_HDR
        + '<Image ID="Image:0" Name="SIGNED">'
        + '<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" '
        + 'SizeX="%d" SizeY="%d" SizeC="4" SizeZ="1" SizeT="1">' % (w, h)
        + channels
        + '<TiffData IFD="0" PlaneCount="4"/></Pixels></Image></OME>')
    import numpy as np

    data = np.stack([(_pattern2d(h, w) + 1500 * ci).astype(np.uint16)
                     for ci in range(4)])
    return _write_ome_description_xml(data, xml)


def make_ome_multiseries_bytes():
    """多 series OME：主图 YX 64x96（面积 6144）+ 辅助 CYX(12) 32x32。

    辅助 series 全 shape 乘积（12*32*32=12288）大于主图（6144）——旧
    np.prod 选法会选错；按 Y*X 主空间面积应选主图。
    """
    _require_tifffile()
    import numpy as np
    import tifffile

    buf = io.BytesIO()
    with tifffile.TiffWriter(buf, bigtiff=True, ome=True) as tif:
        tif.write(_pattern2d(64, 96).astype(np.uint16),
                  photometric="minisblack",
                  metadata={"axes": "YX", "Name": "MAIN"})
        tif.write(np.stack([(_pattern2d(32, 32) + 100 * ci).astype(np.uint16)
                            for ci in range(12)]),
                  photometric="minisblack",
                  metadata={"axes": "CYX", "Name": "AUX"})
    return buf.getvalue()


def make_ome_multifile_bytes(h=32, w=40):
    """multi-file OME（TiffData 引用外部 FileName）——必须稳定拒绝。"""
    xml = (
        _OME_HDR
        + '<Image ID="Image:0" Name="MULTI">'
        + '<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" '
        + 'SizeX="%d" SizeY="%d" SizeC="1" SizeZ="1" SizeT="2">' % (w, h)
        + '<Channel ID="Channel:0:0" SamplesPerPixel="1" Name="C0">'
        + '<LightPath/></Channel>'
        + '<TiffData IFD="0" PlaneCount="1"><UUID FileName="other_part.ome.tiff">'
        + 'urn:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</UUID></TiffData>'
        + '<TiffData IFD="1" PlaneCount="1"><UUID FileName="other_part.ome.tiff">'
        + 'urn:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</UUID></TiffData>'
        + '</Pixels></Image></OME>')
    import numpy as np

    data = np.stack([_pattern2d(h, w).astype(np.uint16),
                     (_pattern2d(h, w) + 1).astype(np.uint16)])
    return _write_ome_description_xml(data, xml)


def make_ome_cyx_pyramid_bytes(c=2, h=128, w=128, levels=2, tile=(32, 32)):
    """带 subifds 金字塔的 CYX OME-TIFF（全局统计「最低层采样」用例）。"""
    return make_ome_cyx_bytes(c=c, h=h, w=w, dtype="uint16",
                              tile=tile, pyramid=int(levels))
