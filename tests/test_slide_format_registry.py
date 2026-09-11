# -*- coding: utf-8 -*-
"""slide_format_registry 契约测试（KFB Phase A）。

覆盖：
  - 现有 SUPPORTED_EXTS 每个扩展名的 capability 归类正确；
  - registry 词表与 app.SUPPORTED_EXTS 同步（两侧防漂移）；
  - .kfb = convert-required（canonical .tif）、.kfbf = unsupported；
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
    ".svslide",
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


def test_kfbf_is_unsupported():
    info = reg.lookup("sample.kfbf")
    assert info["ext"] == ".kfbf"
    assert info["capability"] == reg.CAP_UNSUPPORTED
    assert info["canonical_ext"] is None


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
# 2. 与 app.SUPPORTED_EXTS 同步 + 上传白名单未被改动
# --------------------------------------------------------------------------- #
#: app.py SUPPORTED_EXTS 的冻结期望（Phase A 合同：**不得**加入 kfb/kfbf）
_EXPECTED_SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif",
    "svslide",
}


def test_registry_covers_supported_exts_exactly():
    """registry 登记的 native 扩展名集合 == app.SUPPORTED_EXTS（防漂移）。"""
    import app

    assert app.SUPPORTED_EXTS == _EXPECTED_SUPPORTED_EXTS
    native = {"." + e for e in app.SUPPORTED_EXTS}
    registered = {ext for ext in reg._FORMATS}  # noqa: SLF001
    assert native <= registered
    # 登记表允许额外包含 convert-required / unsupported 项（kfb/kfbf）
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
