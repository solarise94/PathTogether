# -*- coding: utf-8 -*-
"""F4 Leica SCN 输入适配器测试（tile 搬运类）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（SCN_SAMPLE）缺失时，受影响
的测试带清晰理由跳过——绝不失败。样本路径只经环境变量传入（公开 OpenSlide
样本 Leica-1.scn，见 .testdata/openslide/ucf-samples.env），不落私有路径。

覆盖：
  * 合成夹具（CLI gen-scn）：双输出 profile 转换、结构校验、平台读取器
    打开产物确认 native_rgb、稀疏网格填充计数、变体拒绝（荧光/非 JPEG/
    描述伪装 OME/转换器/外来厂商）、内存预算分配前拒绝；
  * 真实样本（环境变量门控）：默认 192 MiB 预算转换成功；转换产物与
    OpenSlide 读原文件在若干组织 ROI 上 **L0 像素一致**（tile 搬运类）；
    低倍层几何一致；slide_io.open_slide 打开产物确认 native_rgb。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_scn.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent

CLI_ENV = os.environ.get(
    "SLIDE_TRANSFORM_CLI",
    str(REPO / "slide-transform-core" / "target" / "release" / "slide-transform"),
)
CLI_DEBUG = REPO / "slide-transform-core" / "target" / "debug" / "slide-transform"
CLI = CLI_ENV if Path(CLI_ENV).exists() else (CLI_DEBUG if CLI_DEBUG.exists() else None)

#: 公开 OpenSlide 样本（Leica-1.scn，CC0；路径仅经环境变量传入）。
#: 未设置时必须解析为**不存在的路径**：Path("") 会变成 Path(".") 而
#: .exists() 为真，skipif 失效（回归审查 2026-10-05 #1）。
_SCN_SAMPLE_UNSET = "/nonexistent/scn-sample-unset"
SCN_SAMPLE = Path(os.environ.get("SCN_SAMPLE") or _SCN_SAMPLE_UNSET)


def _cli(*args, check=True):
    assert CLI is not None, "slide-transform CLI 未构建"
    r = subprocess.run(
        [str(CLI), *map(str, args)], capture_output=True, text=True, timeout=3600
    )
    if check and r.returncode != 0:
        raise AssertionError(f"CLI 失败: {r.stdout} {r.stderr}")
    return r


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("f4-scn")


def test_sample_env_unset_means_skip_not_fail(tmp_path):
    """回归（审查 #1）：SCN_SAMPLE 未设置时两个真实样本测试必须 skip，
    绝不能执行（曾因 Path("") → Path(".") 且 .exists()==True 而失败）。"""
    env = {k: v for k, v in os.environ.items() if k != "SCN_SAMPLE"}
    env["TMPDIR"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_slide_transform_scn.py", "-q",
         "-k", "real_sample"],
        capture_output=True, text=True, timeout=600,
        cwd=str(REPO), env=env,
    )
    assert r.returncode == 0, f"真实样本门未跳过: {r.stdout[-2000:]}"
    assert "skipped" in r.stdout, r.stdout[-500:]


# --------------------------------------------------------------------------- #
# 合成夹具
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_gen_and_convert_both_profiles(workdir):
    """gen-scn 夹具 → 双 profile 转换：报告契约、结构校验、平台读取器。"""
    import tifffile

    import slide_io

    src = workdir / "syn.scn"
    _cli("gen-scn", src, "--width", "520", "--height", "300", "--tile", "128")
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "leica-scn-jpeg"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    assert doc["illumination"] == "brightfield"
    # label image 被排除；主金字塔 3 层、严格递减
    assert doc["levels"][0]["width"] == 520
    assert [l["width"] for l in doc["levels"]] == sorted(
        (l["width"] for l in doc["levels"]), reverse=True
    )
    assert doc["associated"] and doc["associated"][0]["name"] == "label"
    assert doc["mpp_x"] == 0.5  # view(500nm)/pixels
    # 主图不等于 label 图（label 的 ifd=0 不在任何输出层里）
    assert all(l["ifd"] != 0 for l in doc["levels"])

    for profile, out in (
        ("bf-classic", workdir / "syn-classic.tif"),
        ("bf-ome", workdir / "syn.ome.tif"),
    ):
        rj = json.loads(
            _cli("convert", src, out, "--overwrite", "--profile", profile).stdout
        )
        assert rj["source_format"] == "leica-scn-jpeg"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_reencoded"] == 0
        vj = json.loads(
            _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
        )
        assert vj["ok"] is True
        sp = slide_io.open_slide(str(out))
        assert sp.dimensions == (520, 300)
        if hasattr(sp, "is_native_rgb"):
            # OME 产物走平台 TiffFileSlide；classic 产物可能回落到
            # openslide.OpenSlide（无该属性）
            assert sp.is_native_rgb, "转换产物必须是原生 RGB"

    # classic 布局的 tile 载荷结构：photometric 6（YCbCr JPEG）、tile 128
    with tifffile.TiffFile(workdir / "syn-classic.tif") as tf:
        page = tf.pages[0]
        assert page.photometric == 6
        assert page.tags["TileWidth"].value == 128
        assert page.is_tiled


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_sparse_grid_tiles_are_filled(workdir):
    """稀疏网格：(0,0) 缺失 tile 用共享白色填充块，计数进 tiles_filled。"""
    src = workdir / "sparse.scn"
    _cli("gen-scn", src, "--width", "520", "--height", "300", "--sparse")
    pj = json.loads(_cli("probe", src).stdout)
    assert pj["document"]["levels"][0]["tiles_missing"] == 2
    assert pj["estimate"]["cells_missing"] == 2
    out = workdir / "sparse.tif"
    rj = json.loads(_cli("convert", src, out, "--overwrite").stdout)
    l0 = rj["levels"][0]
    assert l0["tiles_filled"] == 2
    assert l0["tiles_raw_copied"] == l0["tiles_total"] - 2
    assert "scn_missing_tiles_filled" in rj["warnings"]
    vj = json.loads(_cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout)
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "knobs,frag",
    [
        (["--fluoro"], "荧光"),
        (["--non-jpeg"], "基线 JPEG"),
        (["--desc", "ome"], "OME-TIFF"),
        (["--desc", "converter"], "不是转换输入"),
        (["--desc", "foreign"], "不猜"),
        (["--desc", "none"], "不猜"),
    ],
)
def test_variant_rejections_before_copy(workdir, knobs, frag):
    """荧光/非 JPEG/描述伪装的 SCN 在复制前类型化拒绝。"""
    src = workdir / ("v" + "-".join(knobs).replace("--", "") + ".scn")
    _cli("gen-scn", src, *knobs)
    out = workdir / "rejected.tif"
    r = _cli("convert", src, out, "--overwrite", check=False)
    assert r.returncode == 1
    assert not out.exists(), "拒绝必须发生在写出之前"
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "unsupported_kfb_variant"
    assert frag in err["message"]
    # probe 也给同一拒绝（前端嗅探失败的兜底路径）
    pj = _cli("probe", src, check=False)
    assert pj.returncode == 1
    assert frag in json.loads(pj.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 结构预留 → 分配前 resource_profile_insufficient。"""
    src = workdir / "b.scn"
    _cli("gen-scn", src, "--width", "400", "--height", "260")
    out = workdir / "b.tif"
    r = _cli("convert", src, out, "--overwrite", "--memory-budget", "65536", check=False)
    assert r.returncode == 1
    assert not out.exists()
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "resource_profile_insufficient"
    # 默认预算（192 MiB saver 档）下同一输入转换成功
    r2 = _cli("convert", src, out, "--overwrite")
    assert json.loads(r2.stdout)["output_bytes"] > 0


# --------------------------------------------------------------------------- #
# 真实样本（SCN_SAMPLE 环境变量门控；公开 OpenSlide 样本 Leica-1.scn）
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 像素坐标；Leica-1 主图 36832×38432）——避开边缘与
#: 标签区，落在组织内
REAL_ROIS = [
    (10000, 10000, 512, 512),
    (20000, 18000, 512, 512),
    (5000, 25000, 384, 384),
]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not SCN_SAMPLE.exists(), reason="SCN_SAMPLE 不存在（真实样本门跳过）")
def test_real_sample_probe_and_default_budget_conversion(workdir):
    """真实样本：默认 192 MiB 预算下转换成功（门禁在 MemoryMax=320M 复核）。"""
    pj = json.loads(_cli("probe", SCN_SAMPLE).stdout)
    doc = pj["document"]
    assert doc["format"] == "leica-scn-jpeg"
    assert doc["illumination"] == "brightfield"
    assert doc["levels"][0]["width"] == 36832
    assert doc["levels"][0]["height"] == 38432
    assert doc["levels"][0]["tile_w"] == 512
    assert len(doc["levels"]) == 5
    assert doc["associated"], "label/preview image 应被检测并排除"
    assert all(a["width"] < doc["levels"][0]["width"] for a in doc["associated"])
    # view(nm)/pixels ⇒ ~0.5 µm/px
    assert abs(doc["mpp_x"] - 0.5) < 0.01

    out = workdir / "leica1.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(SCN_SAMPLE), str(out),
            "--overwrite", "--profile", "bf-ome",
        ],
        capture_output=True, text=True, timeout=3600,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "leica-scn-jpeg"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_reencoded"] == 0
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not SCN_SAMPLE.exists(), reason="SCN_SAMPLE 不存在（真实样本像素门跳过）")
def test_real_sample_l0_pixels_match_openslide(workdir):
    """tile 搬运类硬门：转换产物与 OpenSlide 读原文件在组织 ROI 上 L0 像素一致。

    OpenSlide 把 SCN 呈现为覆盖整个 collection 视图的画布，主图按
    openslide.bounds-x/y 偏移贴入；本转换器输出主图 ROI 本体。对比时源侧
    坐标加 bounds 偏移。低倍层是 4× 重采样的画布金字塔（与 ROI 本地金字
    塔存在亚像素相位差），只做几何一致 + 均值误差上限检查。
    """
    import openslide

    import slide_io

    out = workdir / "leica1-pix.ome.tif"
    rj = json.loads(
        _cli("convert", SCN_SAMPLE, out, "--overwrite", "--profile", "bf-ome").stdout
    )
    src = openslide.OpenSlide(str(SCN_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "SCN 转换产物必须是原生 RGB"
    bx = int(src.properties["openslide.bounds-x"])
    by = int(src.properties["openslide.bounds-y"])
    bw = int(src.properties["openslide.bounds-width"])
    bh = int(src.properties["openslide.bounds-height"])
    assert src.dimensions == (53130, 153470)
    assert (bw, bh) == dst.dimensions == (36832, 38432)
    # 低倍层几何：层数一致；输出各层 = SCN XML <dimension> 声明的主图
    # ROI 金字塔（SCN 每层尺寸由扫描仪声明，不是简单 ceil(÷4^k)）；
    # 源画布层是 OpenSlide 对 collection 视图自己的构造，只比层数
    probe = json.loads(_cli("probe", SCN_SAMPLE).stdout)["document"]
    assert src.level_count == dst.level_count == len(probe["levels"])
    for level, lv in enumerate(probe["levels"]):
        assert dst.level_dimensions[level] == (lv["width"], lv["height"]), level
    # 硬门：若干组织 ROI 的 L0 逐像素一致（passthrough 适配器零误差）
    for (x, y, w, h) in REAL_ROIS:
        a = src.read_region((bx + x, by + y), 0, (w, h)).convert("RGB")
        b = dst.read_region((x, y), 0, (w, h)).convert("RGB")
        aa = np.asarray(a, dtype=np.int16)
        bb = np.asarray(b, dtype=np.int16)
        d = np.abs(aa - bb)
        assert d.max() == 0, f"ROI ({x},{y}) L0 像素不一致：max diff {d.max()}"
    # 低倍层：画布金字塔与 ROI 本地金字塔存在 4× 重采样相位差——几何已查，
    # 内容给均值误差上限（亚像素相位差下的组织内容必须仍然紧贴）
    x, y, w, h = REAL_ROIS[1]
    for level in (1, 2):
        a = src.read_region((bx + x, by + y), level, (w, h)).convert("RGB")
        b = dst.read_region((x, y), level, (w, h)).convert("RGB")
        assert a.size == b.size
        d = np.abs(
            np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16)
        )
        assert d.mean() < 2.0, f"level {level} 均值误差 {d.mean():.3f} 超上限"
    assert rj["levels"][0]["tiles_filled"] == 0, "Leica-1 网格完整，不应有填充"
