# -*- coding: utf-8 -*-
"""Ventana BIF 输入适配器测试（重叠瓦片拼接重编码类，第九个输入转换器）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（BIF_SAMPLE，OpenSlide 公开
样本 OS-2.bif）缺失时，受影响的测试带清晰理由跳过——绝不失败。样本路径
只经环境变量传入（见 .testdata/openslide/ucf-samples.env），不落私有路径。

覆盖：
  * 合成夹具（CLI gen-bif）：双输出 profile 转换、报告契约（stitch
    compose 指纹 / l0-box2）、结构校验、平台读取器打开产物确认
    native_rgb、变体拒绝（JPEG2000 / LEFT 拼接走向 / 多 z 层 / 无
    EncodeInfo / 经典 TIFF / 灰度 / 稀疏瓦片）、内存预算分配前拒绝；
  * 合成夹具像素门：转换产物与 OpenSlide 读原 .bif 整层比较（拼接重编码
    类，L0 均值误差上限；夹具为整数几何——重叠/未覆盖/双 AOI 优先级都
    在整层比较里），低倍层与 OpenSlide L0 的 box2 比较；
  * 真实样本（环境变量门控）：probe 声明的拼接尺寸/mpp 与 OpenSlide 一致
    （114943×76349 / 0.2325）；默认 192 MiB 预算（门禁在 MemoryMax=320M
    复核）转换成功；本文件像素门**复用这次转换的产物**（不再转一次），
    与 OpenSlide 读原文件在若干组织 ROI 上比 L0 均值误差上限；低倍层为
    l0-box2 生成链，检查几何（÷2 链、末层 ≤ 256）与均值；slide_io.
    open_slide 打开产物确认 native_rgb。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_bif.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
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

#: 公开 OpenSlide 样本（OS-2.bif，license distributable；路径仅经环境变量
#: 传入）。未设置时必须解析为**不存在的路径**（与 VMS/NDPI 套件同一回归
#: 审查）。
_BIF_SAMPLE_UNSET = "/nonexistent/bif-sample-unset"
BIF_SAMPLE = Path(os.environ.get("BIF_SAMPLE") or _BIF_SAMPLE_UNSET)

#: 合成夹具的拼接几何（gen-bif 默认）：瓦片 256、重叠 32/24 → 步进
#: 224/232；AOI0 3×3 @ (0,0)、AOI2 2×2 @ (140,632)，未覆盖区为黑 → 704×1120
SYN_W, SYN_H = 704, 1120


def _cli(*args, check=True):
    assert CLI is not None, "slide-transform CLI 未构建"
    r = subprocess.run(
        [str(CLI), *map(str, args)], capture_output=True, text=True, timeout=7200
    )
    if check and r.returncode != 0:
        raise AssertionError(f"CLI 失败: {r.stdout} {r.stderr}")
    return r


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("bif")


# --------------------------------------------------------------------------- #
# 合成夹具
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_gen_probe_and_convert_both_profiles(workdir):
    """gen-bif 夹具 → probe 契约 + 双 profile 转换 + 结构校验 + 读取器。"""
    import tifffile

    import slide_io

    src = workdir / "syn.bif"
    _cli("gen-bif", src)
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "ventana-bif-jpeg"
    assert doc["adapter"] == "ventana-bif-jpeg"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    assert doc["tiff_kind"] == "bigtiff"
    # 拼接几何：704×1120（画布标签是 768×1280——重叠拼接后的真实尺寸）
    assert (doc["width"], doc["height"]) == (SYN_W, SYN_H)
    l0 = doc["levels"][0]
    assert (l0["canvas_w"], l0["canvas_h"]) == (768, 1280)
    assert (l0["tile_w"], l0["tile_h"]) == (256, 256)
    assert l0["advance_x"] == 224_000_000  # ×1e6（µ-px 整数表示）
    assert l0["advance_y"] == 232_000_000
    assert l0["areas"] == 2
    assert l0["tiles_present"] == 9 + 4
    assert doc["objective"] == 40
    assert doc["mpp_source"] == "bif-iscan-scanres"
    assert abs(doc["mpp_x"] - 0.2325) < 1e-12
    assert doc["pyramid_method"] == "l0-box2"
    assert doc["jpeg_tables"] is True
    # 关联图：label 与 thumbnail 被检测并排除
    names = [a["name"] for a in doc["associated"]]
    assert "label" in names and "thumbnail" in names
    # 输出金字塔 = 重编码 L0 + l0-box2 生成尾
    gen = [l for l in doc["levels"] if l.get("generated")]
    assert gen and gen[-1]["width"] <= 256

    for profile, out in (
        ("bf-classic", workdir / "syn-classic.tif"),
        ("bf-ome", workdir / "syn.ome.tif"),
    ):
        rj = json.loads(
            _cli("convert", src, out, "--overwrite", "--profile", profile).stdout
        )
        assert rj["source_format"] == "ventana-bif-jpeg"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_raw_copied"] == 0
        assert rj["tiles_reencoded"] == sum(l["tiles_total"] for l in rj["levels"])
        # 拼接重编码类：composed 摘要 + 保留画质指纹
        assert rj["composed"]["mode"] == "stitch-compose-reencode"
        assert rj["composed"]["fingerprint"] == "bif-mosaic-compose:q96:y422:hstd:v1"
        assert rj["composed"]["pyramid"] == "l0-box2"
        vj = json.loads(
            _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
        )
        assert vj["ok"] is True
        sp = slide_io.open_slide(str(out))
        assert sp.dimensions == (SYN_W, SYN_H)
        if hasattr(sp, "is_native_rgb"):
            assert sp.is_native_rgb, "转换产物必须是原生 RGB"

    # classic 布局的 tile 载荷结构：photometric 6（YCbCr JPEG）、tile 256
    with tifffile.TiffFile(workdir / "syn-classic.tif") as tf:
        page = tf.pages[0]
        assert page.photometric == 6
        assert page.tags["TileWidth"].value == 256
        assert page.is_tiled

    # strict-lossless 对 BIF 是类型化拒绝（输出必然重编码）
    r = _cli("convert", src, workdir / "no.tif", "--overwrite",
             "--policy", "strict-lossless", check=False)
    assert r.returncode == 1
    assert json.loads(r.stdout)["error"]["code"] == "pixel_policy_violation"
    # 荧光 profile 同样拒绝（BIF 恒为明场）
    r = _cli("convert", src, workdir / "no2.tif", "--overwrite",
             "--profile", "fl-ome", check=False)
    assert r.returncode == 1
    assert "荧光" in json.loads(r.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "knobs,frag",
    [
        (["--jp2k"], "JPEG 2000"),
        (["--left-direction"], "Direction"),
        (["--z-layers"], "Z-layers"),
        (["--no-xml"], "EncodeInfo"),
        (["--classic"], "BigTIFF"),
        (["--gray"], "SamplesPerPixel"),
        (["--sparse"], "稀疏"),
    ],
)
def test_variant_rejections_before_copy(workdir, knobs, frag):
    """JPEG2000/LEFT 走向/多 z/无 XML/经典 TIFF/灰度/稀疏瓦片复制前拒绝。"""
    src = workdir / ("v" + "-".join(knobs).replace("--", ""))
    _cli("gen-bif", src, *knobs)
    out = workdir / "rejected.tif"
    r = _cli("convert", src, out, "--overwrite", check=False)
    assert r.returncode == 1
    assert not out.exists(), "拒绝必须发生在写出之前"
    err = json.loads(r.stdout)["error"]
    assert err["code"] in ("unsupported_kfb_variant", "jpeg_decode_failed",
                           "conversion_validation_failed")
    assert frag in err["message"]
    # probe 给同一拒绝（前端嗅探失败兜底路径）
    pj = _cli("probe", src, check=False)
    assert pj.returncode == 1
    assert frag in json.loads(pj.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 拼接带工作集 → 分配前 resource_profile_insufficient。"""
    src = workdir / "b"
    _cli("gen-bif", src)
    out = workdir / "b.tif"
    r = _cli("convert", src, out, "--overwrite",
             "--memory-budget", "65536", check=False)
    assert r.returncode == 1
    assert not out.exists()
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "resource_profile_insufficient"
    assert "内存预算不足" in err["message"]
    # 默认预算（192 MiB saver 档）下同一输入转换成功
    r2 = _cli("convert", src, out, "--overwrite")
    assert json.loads(r2.stdout)["output_bytes"] > 0


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_synthetic_pixels_match_openslide(workdir):
    """合成夹具像素门：拼接产物 == OpenSlide 读原 .bif（重编码噪声内）。

    夹具是整数几何（重叠 32/24、位置全整数）：重叠区的优先级（AOI0 行号
    小者胜）、未覆盖区的黑色填充、双 AOI 的 Pos-Y 翻转、boustrophedon 瓦片
    编号都在这次整层比较里核对。
    """
    import openslide

    import slide_io

    src = workdir / "pix.bif"
    _cli("gen-bif", src)
    out = workdir / "pix.ome.tif"
    _cli("convert", src, out, "--overwrite", "--profile", "bf-ome")
    s = openslide.OpenSlide(str(src))
    d = slide_io.open_slide(str(out))
    assert d.dimensions == s.dimensions == (SYN_W, SYN_H)
    a = s.read_region((0, 0), 0, (SYN_W, SYN_H)).convert("RGB")
    b = d.read_region((0, 0), 0, (SYN_W, SYN_H)).convert("RGB")
    A, B = np.asarray(a, dtype=np.int16), np.asarray(b, dtype=np.int16)
    diff = np.abs(A - B)
    assert diff.mean() < 3.0, f"L0 mean diff {diff.mean()}"
    assert diff.max() <= 90
    for c in range(3):
        assert abs(A[..., c].mean() - B[..., c].mean()) < 4.0
    # 低倍层：输出层是 L0 的 box2 链——与「OpenSlide L0 的 box2」比较（同
    # 一重采样语义；夹具自带的 level=1.. 层是结构占位、内容并非 L0 的真
    # 实降采样，不能作为参照）
    h2, w2 = SYN_H // 2, SYN_W // 2
    box = (
        A[0 : 2 * h2 : 2, 0 : 2 * w2 : 2].astype(np.int32)
        + A[1 : 2 * h2 : 2, 0 : 2 * w2 : 2]
        + A[0 : 2 * h2 : 2, 1 : 2 * w2 : 2]
        + A[1 : 2 * h2 : 2, 1 : 2 * w2 : 2]
    ) // 4
    b1 = d.read_region((0, 0), 1, (w2, h2)).convert("RGB")
    d1 = np.abs(box.astype(np.int16) - np.asarray(b1, dtype=np.int16))
    assert d1.mean() < 6.0, f"L1 mean diff {d1.mean()}"


# --------------------------------------------------------------------------- #
# 真实样本（BIF_SAMPLE 环境变量门控；公开 OpenSlide 样本 OS-2.bif）
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 拼接坐标；OS-2.bif 拼接后 114943×76349）——避开左缘
#: 未覆盖带（x < 940 为黑）与边缘，全部落在 AOI2 的连续覆盖内（实测低倍
#: 灰度均值 146/170/170）
REAL_ROIS = [
    (110080, 68608, 512, 512),
    (63488, 68608, 512, 512),
    (4608, 41984, 512, 512),
]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not (BIF_SAMPLE.exists() and BIF_SAMPLE.suffix.lower() == ".bif"),
    reason="BIF_SAMPLE 不存在（真实样本门跳过）",
)
def test_real_sample_probe_and_default_budget_conversion(workdir):
    """真实样本：probe 与 OpenSlide 逐项一致；默认 192 MiB 预算下转换成功
    （门禁在 MemoryMax=320M 复核）。"""
    src = BIF_SAMPLE
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "ventana-bif-jpeg"
    # 拼接尺寸与 OpenSlide 一致（画布标签 128000×82960 是重叠前的网格）
    assert (doc["width"], doc["height"]) == (114943, 76349)
    l0 = doc["levels"][0]
    assert (l0["canvas_w"], l0["canvas_h"]) == (128000, 82960)
    assert (l0["tile_w"], l0["tile_h"]) == (1024, 1360)
    assert l0["areas"] == 2
    assert l0["tiles_present"] == 95 * 61 + 11 * 17  # AOI2 95×61 + AOI0 11×17
    # 源自己的降采样层只作为结构信息列出（输出层是 l0-box2 生成链）
    assert len(doc["source_levels"]) == 9
    assert doc["source_levels"][0]["canvas_w"] == 64000
    assert abs(doc["mpp_x"] - 0.2325) < 1e-9
    assert doc["objective"] == 40
    assert any(a["name"] == "label" for a in doc["associated"])
    assert any(a["name"] == "thumbnail" for a in doc["associated"])

    out = workdir / "os2.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(src), str(out),
            # 8.8 GPx 的拼接样本：重叠瓦片流式 band 解码 + 全瓦片重编码远超
            # 默认 600 s 墙钟（内存预算仍是默认 192 MiB，由 320M 上限复核）
            "--overwrite", "--profile", "bf-ome", "--timeout", "7200",
        ],
        capture_output=True, text=True, timeout=14400,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "ventana-bif-jpeg"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_raw_copied"] == 0
    assert rj["composed"]["fingerprint"] == "bif-mosaic-compose:q96:y422:hstd:v1"
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    # 审查回归：磁盘预检上界必须是实际上界的真上界（浏览器磁盘预检按上界
    # 预留 OPFS 配额；与 VMS/NDPI 套件同一断言）
    est = json.loads(_cli("probe", src).stdout)["estimate"]
    assert est["output_upper_bound_bytes"] >= rj["output_bytes"], (
        f"output_upper_bound {est['output_upper_bound_bytes']} < actual {rj['output_bytes']}")
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not (BIF_SAMPLE.exists() and BIF_SAMPLE.suffix.lower() == ".bif"),
    reason="BIF_SAMPLE 不存在（真实样本像素门跳过）",
)
def test_real_sample_l0_mean_error_and_pyramid_geometry(workdir):
    """重编码类硬门：L0 均值误差上限 + 低倍层几何。

    本适配器没有逐字节搬运路径：L0 是各源瓦片按记录位置流式 band 解码拼
    接、再按 q96 4:2:2 重编码——与 OpenSlide 读原文件（亚像素双线性插值的
    拼接渲染）在组织 ROI 上比均值误差（量级 = 一次高画质 JPEG 生成损失 +
    整数落位 vs 双线性的半像素差）；低倍输出层是 l0-box2 生成链（÷2 链，
    末层 ≤ 256），与扫描仪自己的降采样层只比均值（重采样核不同）。
    像素门**复用上面 320M 门转换的产物**，不再转换一次。
    """
    import openslide

    import slide_io

    out = workdir / "os2.ome.tif"
    # The 320M conversion test above already produced this exact output
    # (same input, bf-ome, preserve); convert only when run on its own.
    if not out.exists():
        _cli("convert", BIF_SAMPLE, out, "--overwrite", "--profile", "bf-ome",
             "--timeout", "7200")
    src = openslide.OpenSlide(str(BIF_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "BIF 转换产物必须是原生 RGB"
    assert src.dimensions == dst.dimensions == (114943, 76349)

    # 低倍层几何：输出 = l0-box2 生成链（114943 → … → ≤256），与 probe 一致
    probe = json.loads(_cli("probe", BIF_SAMPLE).stdout)["document"]
    assert dst.level_count == len(probe["levels"])
    for level, lv in enumerate(probe["levels"]):
        assert dst.level_dimensions[level] == (lv["width"], lv["height"]), level
    last_w, last_h = dst.level_dimensions[-1]
    assert max(last_w, last_h) <= 256

    # L0 组织 ROI：均值误差上限与 NDPI/VMS 同档（6.0）。重叠优先级与
    # OpenSlide 一致（(row,col) 较大者胜出），落位取小数位置的最近整数
    # （round）——实测 2.030/2.109/3.932（修复前优先级翻转时为
    # 9.378/6.805/5.010）；通道均值差 ≤ 0.26。
    for (x, y, w, h) in REAL_ROIS:
        a = src.read_region((x, y), 0, (w, h)).convert("RGB")
        b = dst.read_region((x, y), 0, (w, h)).convert("RGB")
        d = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
        assert d.mean() < 6.0, f"ROI ({x},{y}) L0 均值误差 {d.mean():.3f} 超上限"
        for c in range(3):
            assert abs(np.asarray(a)[..., c].mean() - np.asarray(b)[..., c].mean()) < 4.0

    # 低倍层（l0-box2 生成 vs 扫描仪自己的降采样层）：均值误差上限按
    # 修复后实测给（余量 13-19%）：层 1 实测 7.100 → 8.0、层 2 实测
    # 8.581 → 10.0、层 4 实测 10.968 → 13.0。read_region 坐标是 level-0
    # 坐标、尺寸是目标层坐标（openslide 语义）；深层误差主项是重采样核
    # 不同（box2 链 vs 扫描仪金字塔），L0 落位的亚像素差向下传播。
    x, y, w, h = 45000, 40000, 8192, 8192
    for out_level, src_level, bound in ((1, 1, 8.0), (2, 2, 10.0), (4, 4, 13.0)):
        scale = 2 ** out_level
        a = src.read_region((x, y), src_level,
                            (w // scale, h // scale)).convert("RGB")
        b = dst.read_region((x, y), out_level,
                            (w // scale, h // scale)).convert("RGB")
        assert a.size == b.size
        d = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
        assert d.mean() < bound, (
            f"输出层 {out_level} vs 源层 {src_level} 均值误差 {d.mean():.3f} 超上限 {bound}"
        )
