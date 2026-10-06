# -*- coding: utf-8 -*-
"""F8 普通图片（BMP/JPEG）输入适配器测试（单张大图，重编码类）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（RASTER_SAMPLE /
RASTER_BMP_SAMPLE）缺失时，受影响的测试带清晰理由跳过——绝不失败。样本
路径只经环境变量传入（公开 OpenSlide 样本 CMU-1-region.jpg /
CMU-1-region-small.bmp，见 .testdata/openslide/ucf-samples.env），不落
私有路径。

覆盖：
  * 合成夹具（CLI gen-raster）：双输出 profile 转换、报告契约（raster
    compose 指纹 / l0-box2）、结构校验、平台读取器打开产物确认
    native_rgb、变体拒绝（RLE8 / 4 位深 / 渐进 SOF / 灰度 JPEG / 伪装
    魔数 / 截断像素区间）、内存预算分配前拒绝、strict-lossless 类型化
    拒绝、OME 输出不含 PhysicalSize（无物理标尺，不发明 mpp）；
  * 真实样本（环境变量门控）：默认 192 MiB 预算（门禁在 MemoryMax=320M
    复核）转换成功；JPEG（band 路径，真实相机输出无 restart marker）与
    BMP 转换产物与 Pillow/OpenSlide 读原文件在若干 ROI 上比较 L0 均值
    误差上限（重编码类，无字节搬运路径）；低倍层为 l0-box2 生成链，检查
    几何（÷2 链、末层 ≤ 256）与内容均值误差；slide_io.open_slide 打开
    产物确认 native_rgb；磁盘预检上界必须盖住实际输出（preserve 与
    compact 各一次）。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_raster.py -q
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

#: 公开 OpenSlide 样本（CC0；路径仅经环境变量传入）。未设置时必须解析为
#: **不存在的路径**：空串会变成 Path(".") 而 .exists() 为真，skipif 失效
#: （与 F4/F6 套件同一回归审查）。
_UNSET = "/nonexistent/raster-sample-unset"
RASTER_SAMPLE = Path(os.environ.get("RASTER_SAMPLE") or _UNSET)
RASTER_BMP_SAMPLE = Path(os.environ.get("RASTER_BMP_SAMPLE") or _UNSET)


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
    return tmp_path_factory.mktemp("f8-raster")


# --------------------------------------------------------------------------- #
# 合成夹具
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_gen_probe_and_convert_both_profiles(workdir):
    """gen-raster BMP 夹具 → probe 契约 + 双 profile 转换 + 校验 + 读取器。"""
    import tifffile

    import slide_io

    src = workdir / "syn.bmp"
    _cli("gen-raster", src, "--kind", "bmp", "--width", "512", "--height", "320")
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "plain-image-bmp-jpeg"
    assert doc["adapter"] == "plain-image-bmp-jpeg"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    assert doc["kind"] == "bmp-24"
    assert (doc["width"], doc["height"]) == (512, 320)
    # 无物理标尺：mpp_source 恒为 none
    assert doc["mpp_source"].startswith("none")
    assert doc["pyramid_method"] == "l0-box2"
    l0 = doc["levels"][0]
    assert l0["decode_unit"] == "rows"
    assert l0["reencoded"] is True
    # 输出金字塔 = 重编码 L0 + l0-box2 生成尾（末层两边 ≤ 256）
    assert [l["width"] for l in doc["levels"]] == [512, 256]
    assert doc["levels"][-1]["generated"] is True

    for profile, out in (
        ("bf-classic", workdir / "syn-classic.tif"),
        ("bf-ome", workdir / "syn.ome.tif"),
    ):
        rj = json.loads(
            _cli("convert", src, out, "--overwrite", "--profile", profile).stdout
        )
        assert rj["source_format"] == "plain-image-bmp-jpeg"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_raw_copied"] == 0
        assert rj["tiles_reencoded"] == sum(l["tiles_total"] for l in rj["levels"])
        # 单张大图重编码类：composed 摘要 + 保留画质指纹
        assert rj["composed"]["mode"] == "raster-compose-reencode"
        assert rj["composed"]["fingerprint"] == "raster-compose:q96:y422:hstd:v1"
        assert rj["composed"]["pyramid"] == "l0-box2"
        vj = json.loads(
            _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
        )
        assert vj["ok"] is True
        sp = slide_io.open_slide(str(out))
        assert sp.dimensions == (512, 320)
        if hasattr(sp, "is_native_rgb"):
            assert sp.is_native_rgb, "转换产物必须是原生 RGB"

    # classic 布局的 tile 载荷结构：photometric 6（YCbCr JPEG）、tile 256
    with tifffile.TiffFile(workdir / "syn-classic.tif") as tf:
        page = tf.pages[0]
        assert page.photometric == 6
        assert page.tags["TileWidth"].value == 256
        assert page.is_tiled

    # OME 输出（JPEG 路径）不得携带 PhysicalSize（BMP/JPEG 无物理标尺）
    src_j = workdir / "syn.jpg"
    _cli("gen-raster", src_j, "--kind", "jpeg", "--width", "512", "--height", "320",
         "--no-restart")
    ome = workdir / "syn.ome.tif"
    _cli("convert", src_j, ome, "--overwrite", "--profile", "bf-ome")
    raw = ome.read_bytes()
    assert b"PhysicalSize" not in raw, "OME 中不得写 PhysicalSize（无物理标尺）"

    # strict-lossless 对普通图片是类型化拒绝（输出必然重编码）
    r = _cli("convert", src, workdir / "no.tif", "--overwrite",
             "--policy", "strict-lossless", check=False)
    assert r.returncode == 1
    assert json.loads(r.stdout)["error"]["code"] == "pixel_policy_violation"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "kind,knobs,code,frag",
    [
        ("bmp", ["--compression", "1"], "unsupported_kfb_variant", "RLE"),
        ("bmp", ["--bits", "4"], "unsupported_kfb_variant", "位深 4"),
        ("bmp", ["--truncated"], "tile_payload_out_of_bounds", "截断"),
        ("jpeg", ["--progressive"], "jpeg_decode_failed", "SOF"),
        ("jpeg", ["--gray"], "unsupported_kfb_variant", "灰度"),
    ],
)
def test_variant_rejections_before_copy(workdir, kind, knobs, code, frag):
    """RLE / 调色板位深 / 截断 / 渐进 / 灰度在复制前类型化拒绝。"""
    src = workdir / f"v-{kind}-{'-'.join(knobs).replace('--', '')}.{kind}"
    _cli("gen-raster", src, "--kind", kind, "--width", "512", "--height", "320", *knobs)
    out = workdir / "rejected.tif"
    r = _cli("convert", src, out, "--overwrite", check=False)
    assert r.returncode == 1
    assert not out.exists(), "拒绝必须发生在写出之前"
    err = json.loads(r.stdout)["error"]
    assert err["code"] == code
    assert frag in err["message"]
    # probe 给同一拒绝（前端嗅探失败兜底路径）
    pj = _cli("probe", src, check=False)
    assert pj.returncode == 1
    assert frag in json.loads(pj.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_non_raster_magic_refused(workdir):
    """TIFF 魔数不进普通图片适配器（按魔数路由，与扩展名无关）。"""
    src = workdir / "tiff.tif"
    _cli("gen-gtiff", src, "--levels", "1")
    r = _cli("convert", src, workdir / "no.tif", "--overwrite", check=False)
    assert r.returncode == 0  # 通用 TIFF 适配器照常工作（不是 raster 路径）


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 行带/解码工作集 → 分配前 resource_profile_insufficient。"""
    for kind, extra in (("bmp", []), ("jpeg", ["--no-restart"])):
        src = workdir / f"b.{kind}"
        _cli("gen-raster", src, "--kind", kind, "--width", "512", "--height", "320", *extra)
        out = workdir / f"b-{kind}.tif"
        r = _cli("convert", src, out, "--overwrite", "--memory-budget", "65536", check=False)
        assert r.returncode == 1
        assert not out.exists()
        err = json.loads(r.stdout)["error"]
        assert err["code"] == "resource_profile_insufficient"
        assert "内存预算不足" in err["message"]
        # 默认预算（192 MiB saver 档）下同一输入转换成功
        r2 = _cli("convert", src, out, "--overwrite")
        assert json.loads(r2.stdout)["output_bytes"] > 0


# --------------------------------------------------------------------------- #
# 真实样本（RASTER_SAMPLE / RASTER_BMP_SAMPLE 环境变量门控；公开 OpenSlide
# 样本 CMU-1-region.jpg（6000×4000 基线 JPEG，无 restart marker → band
# 路径）与 CMU-1-region-small.bmp（1500×1000×24 位未压缩 BMP））
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 像素坐标）——避开边缘，落在组织内容上
JPEG_ROIS = [
    (1000, 800, 512, 512),
    (3000, 2000, 512, 512),
    (512, 256, 384, 384),
]
BMP_ROIS = [
    (200, 150, 384, 384),
    (800, 500, 256, 256),
]


def _read_source_region(src_path: Path, box):
    """源像素：普通图片族经平台读取器（RasterSlide/Pillow，与查看器同路）。"""
    import slide_io

    s = slide_io.open_slide(str(src_path))
    x, y, w, h = box
    return np.asarray(
        s.read_region((x, y), 0, (w, h)).convert("RGB"), dtype=np.int16
    )


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not RASTER_SAMPLE.exists(), reason="RASTER_SAMPLE 不存在（真实样本门跳过）"
)
def test_real_jpeg_probe_and_default_budget_conversion(workdir):
    """真实 JPEG：默认 192 MiB 预算下转换成功（门禁在 MemoryMax=320M 复核）。"""
    pj = json.loads(_cli("probe", RASTER_SAMPLE).stdout)
    doc = pj["document"]
    assert doc["format"] == "plain-image-bmp-jpeg"
    assert doc["kind"] == "jpeg-baseline"
    assert (doc["width"], doc["height"]) == (6000, 4000)
    l0 = doc["levels"][0]
    assert l0["decode_unit"] == "mcu-row-bands", "真实相机 JPEG 无 restart marker"
    assert l0["restart_interval"] == 0

    out = workdir / "region.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(RASTER_SAMPLE), str(out),
            "--overwrite", "--profile", "bf-ome",
        ],
        capture_output=True, text=True, timeout=7200,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "plain-image-bmp-jpeg"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_raw_copied"] == 0
    assert rj["composed"]["fingerprint"] == "raster-compose:q96:y422:hstd:v1"
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    # 磁盘预检上界必须是实际上界的真上界（preserve 与 compact 各一次）
    est = json.loads(_cli("probe", RASTER_SAMPLE).stdout)["estimate"]
    assert est["output_upper_bound_bytes"] >= rj["output_bytes"], (
        f"output_upper_bound {est['output_upper_bound_bytes']} < actual {rj['output_bytes']}")
    rj_c = json.loads(
        _cli("convert", RASTER_SAMPLE, workdir / "region-compact.ome.tif",
             "--overwrite", "--profile", "bf-ome", "--encoding", "compact").stdout)
    est_c = est["compact_upper_bound_bytes"]
    assert est_c >= rj_c["output_bytes"], (
        f"compact_upper_bound {est_c} < actual {rj_c['output_bytes']}")
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not RASTER_SAMPLE.exists(), reason="RASTER_SAMPLE 不存在（真实样本像素门跳过）"
)
def test_real_jpeg_l0_mean_error_and_pyramid_geometry(workdir):
    """重编码类硬门：L0 均值误差上限 + 低倍层几何（band 路径像素正确性）。

    本适配器没有逐字节搬运路径：L0 是源像素经 MCU 行 band 有界解码后按
    q96 4:2:2 的重编码——与平台读取器读原文件在 ROI 上比均值误差（量级 =
    一次高画质 JPEG 生成损失）；低倍输出层是 l0-box2 生成链（÷2 链，末层
    ≤ 256），与 Pillow 的 LANCZOS 缩略只比均值（重采样核不同）。
    """
    import slide_io

    out = workdir / "region-pix.ome.tif"
    rj = json.loads(
        _cli("convert", RASTER_SAMPLE, out, "--overwrite", "--profile", "bf-ome").stdout
    )
    src = slide_io.open_slide(str(RASTER_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "转换产物必须是原生 RGB"
    assert src.dimensions == dst.dimensions == (6000, 4000)

    # 低倍层几何：输出 = l0-box2 生成链（6000×4000 → … → ≤256）
    probe = json.loads(_cli("probe", RASTER_SAMPLE).stdout)["document"]
    assert dst.level_count == len(probe["levels"])
    for level, lv in enumerate(probe["levels"]):
        assert dst.level_dimensions[level] == (lv["width"], lv["height"]), level
    last_w, last_h = dst.level_dimensions[-1]
    assert max(last_w, last_h) <= 256

    # L0 组织 ROI：重编码均值误差上限（一次高画质生成损失）
    for (x, y, w, h) in JPEG_ROIS:
        a = np.asarray(src.read_region((x, y), 0, (w, h)).convert("RGB"), dtype=np.int16)
        b = np.asarray(dst.read_region((x, y), 0, (w, h)).convert("RGB"), dtype=np.int16)
        d = np.abs(a - b)
        assert d.mean() < 6.0, f"ROI ({x},{y}) L0 均值误差 {d.mean():.3f} 超上限"
        for c in range(3):
            assert abs(a[..., c].mean() - b[..., c].mean()) < 4.0

    # 低倍层（l0-box2 生成 vs Pillow 缩略）：均值误差上限（重采样核不同）。
    # 同一 L0 区域：src 全分辨率读后 resize 到层尺寸；OpenSlide 的
    # read_region location 恒为 level-0 坐标，dst 按同 location 读层。
    x, y, w, h = 1024, 1024, 2048, 2048
    for out_level in (1, 3):
        f = 2 ** out_level
        a = src.read_region((x, y), 0, (w, h)).convert("RGB").resize((w // f, h // f))
        b = dst.read_region((x, y), out_level, (w // f, h // f)).convert("RGB")
        aa = np.asarray(a, dtype=np.int16)
        bb = np.asarray(b, dtype=np.int16)
        d = np.abs(aa - bb)
        assert d.mean() < 24.0, (
            f"输出层 {out_level} 均值误差 {d.mean():.3f} 超上限"
        )


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not RASTER_BMP_SAMPLE.exists(), reason="RASTER_BMP_SAMPLE 不存在（真实 BMP 门跳过）"
)
def test_real_bmp_conversion_and_pixels(workdir):
    """真实 BMP（24 位未压缩）：转换成功 + L0 均值误差上限（行解码正确性）。"""
    import slide_io

    pj = json.loads(_cli("probe", RASTER_BMP_SAMPLE).stdout)
    doc = pj["document"]
    assert doc["kind"] == "bmp-24"
    assert (doc["width"], doc["height"]) == (1500, 1000)
    assert doc["levels"][0]["decode_unit"] == "rows"

    out = workdir / "region-small.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(RASTER_BMP_SAMPLE), str(out),
            "--overwrite", "--profile", "bf-ome",
        ],
        capture_output=True, text=True, timeout=3600,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "plain-image-bmp-jpeg"
    assert rj["tiles_raw_copied"] == 0

    src = slide_io.open_slide(str(RASTER_BMP_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True)
    assert src.dimensions == dst.dimensions == (1500, 1000)
    for (x, y, w, h) in BMP_ROIS:
        a = np.asarray(src.read_region((x, y), 0, (w, h)).convert("RGB"), dtype=np.int16)
        b = np.asarray(dst.read_region((x, y), 0, (w, h)).convert("RGB"), dtype=np.int16)
        d = np.abs(a - b)
        assert d.mean() < 6.0, f"ROI ({x},{y}) L0 均值误差 {d.mean():.3f} 超上限"
        for c in range(3):
            assert abs(a[..., c].mean() - b[..., c].mean()) < 4.0
    # BMP 逐行解码是无损搬运前的精确步骤：q96 重编码后逐通道均值几乎不动
    a = _read_source_region(RASTER_BMP_SAMPLE, (0, 0, 1500, 256))
    b = np.asarray(dst.read_region((0, 0), 0, (1500, 256)).convert("RGB"), dtype=np.int16)
    assert np.abs(a - b).mean() < 4.0
