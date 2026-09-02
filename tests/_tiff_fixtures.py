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
    """普通（非 OME）单条带 TIFF 字节。"""
    _require_tifffile()
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, _gradient_rgb(h, w), photometric="rgb")
    return buf.getvalue()


def make_ome_tiff_bytes(h=64, w=96):
    """OME-TIFF 字节（tifffile ome=True，含 OME-XML ImageDescription）。"""
    _require_tifffile()
    import tifffile

    buf = io.BytesIO()
    tifffile.imwrite(buf, _gradient_rgb(h, w), photometric="rgb", ome=True)
    return buf.getvalue()
