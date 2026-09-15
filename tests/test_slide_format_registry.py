# -*- coding: utf-8 -*-
"""slide_format_registry 契约测试（KFB Phase A）。

覆盖：
  - 现有 SUPPORTED_EXTS 每个扩展名的 capability 归类正确；
  - registry 词表与 app.SUPPORTED_EXTS 同步（两侧防漂移）；
  - .kfb = convert-required（canonical .tif）、.kfbf = convert-required
    （canonical .ome.tif，荧光多通道 OME-TIFF 通道，真实样本已校准）；
  - 未知扩展名 fail-closed；
  - **上传白名单未被改动**：.kfb/.kfbf 不在 app.SUPPORTED_EXTS。

运行：cd 项目根 && python3 -m pytest tests/test_slide_format_registry.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import pytest  # noqa: E402

import slide_format_registry as reg  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 现有扩展名能力归类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ext", [
    ".svs", ".tif", ".tiff", ".ndpi", ".vms", ".vmu", ".scn", ".bif",
    ".svslide", ".bmp", ".jpg", ".jpeg",
])
def test_native_single_file_exts(ext):
    info = reg.lookup("sample" + ext)
    assert info["ext"] == ext
    assert info["capability"] == reg.CAP_NATIVE_SINGLE_FILE
    assert info["canonical_ext"] is None


def test_mrxs_is_native_bundle():
    info = reg.lookup("slide.mrxs")
    assert info["capability"] == reg.CAP_NATIVE_BUNDLE
    assert info["canonical_ext"] is None
    assert info["ext"] == ".mrxs"


@pytest.mark.parametrize("filename,ext", [
    ("a.KFB", ".kfb"),            # 大小写不敏感
    ("/tmp/x/y.Kfb", ".kfb"),     # 路径取 basename
    ("切片 001.kfb", ".kfb"),      # 非 ASCII 名
])
def test_kfb_is_convert_required(filename, ext):
    info = reg.lookup(filename)
    assert info["ext"] == ext
    assert info["capability"] == reg.CAP_CONVERT_REQUIRED
    assert info["canonical_ext"] == ".tif"


def test_kfbf_is_convert_required():
    info = reg.lookup("sample.kfbf")
    assert info["ext"] == ".kfbf"
    assert info["capability"] == reg.CAP_CONVERT_REQUIRED
    assert info["canonical_ext"] == ".ome.tif"


@pytest.mark.parametrize("filename", [
    "a.zip",          # 归档：不是切片格式（解包后按成员再查）
    "a.png",
    "a.xyz123",
    "noext",
    "",
    None,
])
def test_unknown_or_archive_fail_closed(filename):
    info = reg.lookup(filename)
    assert info["capability"] == reg.CAP_UNSUPPORTED
    assert info["canonical_ext"] is None


def test_capability_values_are_the_four_contract_kinds():
    kinds = {info["capability"] for info in (
        reg.lookup("a.svs"), reg.lookup("a.mrxs"),
        reg.lookup("a.kfb"), reg.lookup("a.kfbf"), reg.lookup("a.zz"))}
    assert kinds <= {reg.CAP_NATIVE_SINGLE_FILE, reg.CAP_NATIVE_BUNDLE,
                     reg.CAP_CONVERT_REQUIRED, reg.CAP_UNSUPPORTED}


# --------------------------------------------------------------------------- #
# 2. 与 app.SUPPORTED_EXTS 同步 + 上传白名单
# --------------------------------------------------------------------------- #
#: app.py SUPPORTED_EXTS 的冻结期望（含普通图片族 bmp/jpg/jpeg；
#: **不得**加入 kfb/kfbf）
_EXPECTED_SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif",
    "svslide", "bmp", "jpg", "jpeg",
}


def test_registry_covers_supported_exts_exactly():
    """registry 登记的 native 扩展名集合 == app.SUPPORTED_EXTS（防漂移）。"""
    import app

    assert app.SUPPORTED_EXTS == _EXPECTED_SUPPORTED_EXTS
    native = {"." + e for e in app.SUPPORTED_EXTS}
    registered = {ext for ext in reg._FORMATS}  # noqa: SLF001
    assert native <= registered
    # 登记表允许额外包含 convert-required 项（kfb/kfbf）
    assert registered - native == {".kfb", ".kfbf"}


def test_upload_whitelist_unchanged_no_kfb():
    """上传白名单合同：.kfb/.kfbf 绝不进入 SUPPORTED_EXTS（Phase A 边界）。"""
    import app

    assert "kfb" not in app.SUPPORTED_EXTS
    assert "kfbf" not in app.SUPPORTED_EXTS
    # slide_io 逻辑格式词表同样不含 kfb
    import slide_io

    assert ".kfb" not in slide_io.LOGICAL_EXTS
    assert ".kfbf" not in slide_io.LOGICAL_EXTS


# --------------------------------------------------------------------------- #
# 3. 普通图片族（BMP/JPEG）：能力登记 + 产品目录 + 白名单同步
# --------------------------------------------------------------------------- #
def test_raster_image_whitelist_synced():
    """普通图片族进入上传白名单：SUPPORTED_EXTS / _upload_ext_allowed 同步。"""
    import app

    assert {"bmp", "jpg", "jpeg"} <= app.SUPPORTED_EXTS
    for name in ("a.bmp", "a.JPG", "a.jpeg"):
        assert app._upload_ext_allowed(name) is True
    # 大小写不敏感 + kfb/kfbf 走 convert-required 通道、未知仍拒绝
    assert app._upload_ext_allowed("a.BMP") is True
    assert app._upload_ext_allowed("a.kfb") is True   # convert-required 通道
    assert app._upload_ext_allowed("a.kfbf") is True  # convert-required 通道
    assert app._upload_ext_allowed("a.png") is False


def test_public_catalog_raster_image_row():
    """目录单列一条 raster-image：direct 单文件、可选上传、用户向短句。"""
    rows = {item["id"]: item for item in reg.public_catalog()}
    item = rows["raster-image"]
    assert item["display_name"] == "普通图片（BMP / JPEG）"
    assert item["extensions"] == [".bmp", ".jpg", ".jpeg"]
    assert item["capability"] == reg.CAP_NATIVE_SINGLE_FILE
    assert item["canonical_format"] is None
    assert item["bundle_required"] is False
    assert item["import_mode"] == "direct"
    assert item["selectable_for_upload"] is True
    assert item["limits"] == ["普通图片、支持像素坐标、无物理标尺"]


def test_public_catalog_user_wording_no_internal_terms():
    """用户文案不出现内部实现词汇（reader/解码库/Pillow/OpenSlide 等）。"""
    banned = ("reader", "解码", "Pillow", "OpenSlide", "openslide",
              "tifffile", "RasterSlide")
    for item in reg.public_catalog():
        text = item["display_name"] + "".join(item["limits"])
        for word in banned:
            assert word not in text, (item["id"], word)


def test_public_catalog_ids_unique_and_exts_disjoint():
    """目录 id 唯一、extensions 两两不重叠（词表不再漂移的底线）。"""
    items = reg.public_catalog()
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))
    seen = []
    for item in items:
        for ext in item["extensions"]:
            assert ext not in seen, ext
            seen.append(ext)
