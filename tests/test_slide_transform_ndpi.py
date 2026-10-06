# -*- coding: utf-8 -*-
"""F6 Hamamatsu NDPI 输入适配器测试（拼接重编码类）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（NDPI_SAMPLE）缺失时，受影响
的测试带清晰理由跳过——绝不失败。样本路径只经环境变量传入（公开 OpenSlide
样本 CMU-1.ndpi，见 .testdata/openslide/ucf-samples.env），不落私有路径。

覆盖：
  * 合成夹具（CLI gen-ndpi）：双输出 profile 转换、报告契约（segment
    compose 指纹 / l0-box2）、结构校验、平台读取器打开产物确认 native_rgb、
    变体拒绝（JPEG2000 / 渐进 SOF / 无 restart marker / 非 Hamamatsu
    Make / ExtraSamples）、内存预算分配前拒绝；
  * 真实样本（环境变量门控）：默认 192 MiB 预算（门禁在 MemoryMax=320M
    复核）转换成功；转换产物与 OpenSlide 读原文件在若干组织 ROI 上比较
    L0 均值误差上限（重编码类，无字节搬运路径）；低倍层为 l0-box2 生成链，
    检查几何（÷2 链、末层 ≤ 256）与内容均值误差；slide_io.open_slide 打开
    产物确认 native_rgb。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_ndpi.py -q
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

#: 公开 OpenSlide 样本（CMU-1.ndpi，CC0；路径仅经环境变量传入）。
#: 未设置时必须解析为**不存在的路径**：Path("") 会变成 Path(".") 而
#: .exists() 为真，skipif 失效（与 F4 套件同一回归审查）。
_NDPI_SAMPLE_UNSET = "/nonexistent/ndpi-sample-unset"
NDPI_SAMPLE = Path(os.environ.get("NDPI_SAMPLE") or _NDPI_SAMPLE_UNSET)


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
    return tmp_path_factory.mktemp("f6-ndpi")


# --------------------------------------------------------------------------- #
# 合成夹具
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_gen_probe_and_convert_both_profiles(workdir):
    """gen-ndpi 夹具 → probe 契约 + 双 profile 转换 + 结构校验 + 读取器。"""
    import tifffile

    import slide_io

    src = workdir / "syn.ndpi"
    _cli("gen-ndpi", src, "--width", "512", "--height", "320", "--levels", "2",
         "--associated")
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "hamamatsu-ndpi-jpeg"
    assert doc["adapter"] == "hamamatsu-ndpi-jpeg"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    assert doc["tiff_kind"] == "classic"
    assert (doc["width"], doc["height"]) == (512, 320)
    assert doc["objective"] == 20
    assert doc["mpp_x"] == 0.4990
    assert doc["pyramid_method"] == "l0-box2"
    # L0 层带分段几何；macro/focusmap 被检测并排除
    l0 = doc["levels"][0]
    assert l0["restart_interval"] > 0
    assert l0["segments"] > 0
    assert [a["name"] for a in doc["associated"]] == ["macro", "focusmap"]
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
        assert rj["source_format"] == "hamamatsu-ndpi-jpeg"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_raw_copied"] == 0
        assert rj["tiles_reencoded"] == sum(l["tiles_total"] for l in rj["levels"])
        # 拼接重编码类：composed 摘要 + 保留画质指纹
        assert rj["composed"]["mode"] == "segment-compose-reencode"
        assert rj["composed"]["fingerprint"] == "ndpi-segment-compose:q96:y422:hstd:v1"
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

    # strict-lossless 对 NDPI 是类型化拒绝（输出必然重编码）
    r = _cli("convert", src, workdir / "no.tif", "--overwrite",
             "--policy", "strict-lossless", check=False)
    assert r.returncode == 1
    assert json.loads(r.stdout)["error"]["code"] == "pixel_policy_violation"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "knobs,frag",
    [
        (["--jp2k"], "JPEG 2000"),
        (["--progressive"], "SOF"),
        (["--no-restart"], "restart marker"),
        # 非 Hamamatsu 的 Make 不路由进 NDPI 适配器：落到通用 TIFF 适配器的
        # 条带变体拒绝（同样是复制前类型化拒绝）
        (["--make", "OtherScanner"], "带状存储"),
    ],
)
def test_variant_rejections_before_copy(workdir, knobs, frag):
    """JP2K/渐进/无 restart marker/伪装 Make 的 NDPI 在复制前类型化拒绝。"""
    src = workdir / ("v" + "-".join(knobs).replace("--", "") + ".ndpi")
    _cli("gen-ndpi", src, "--width", "512", "--height", "320", "--levels", "1", *knobs)
    out = workdir / "rejected.tif"
    r = _cli("convert", src, out, "--overwrite", check=False)
    assert r.returncode == 1
    assert not out.exists(), "拒绝必须发生在写出之前"
    err = json.loads(r.stdout)["error"]
    assert err["code"] in ("unsupported_kfb_variant", "jpeg_decode_failed")
    assert frag in err["message"]
    # probe 给同一拒绝（前端嗅探失败兜底路径）
    pj = _cli("probe", src, check=False)
    assert pj.returncode == 1
    assert frag in json.loads(pj.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 条带/分段工作集 → 分配前 resource_profile_insufficient。"""
    src = workdir / "b.ndpi"
    _cli("gen-ndpi", src, "--width", "512", "--height", "320", "--levels", "1")
    out = workdir / "b.tif"
    r = _cli("convert", src, out, "--overwrite", "--memory-budget", "65536", check=False)
    assert r.returncode == 1
    assert not out.exists()
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "resource_profile_insufficient"
    assert "内存预算不足" in err["message"]
    # 默认预算（192 MiB saver 档）下同一输入转换成功
    r2 = _cli("convert", src, out, "--overwrite")
    assert json.loads(r2.stdout)["output_bytes"] > 0


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_l0_tiles_match_whole_strip_decode(workdir):
    """合成夹具像素门：分段解码拼接的 L0 == 整层条带解码（OpenSlide 语义）。

    gen-ndpi 的条带由独立编码的 restart 段拼装，OpenSlide/libjpeg 整条带
    解码与适配器的分段解码必须逐像素一致（DC 预测器每段复位）。
    """
    import openslide

    import slide_io

    src = workdir / "pix.ndpi"
    # restart_rows 默认 1（真实条带 DRI ≤ 每行 MCU 数，OpenSlide 校验）
    _cli("gen-ndpi", src, "--width", "512", "--height", "320", "--levels", "1")
    out = workdir / "pix.ome.tif"
    _cli("convert", src, out, "--overwrite", "--profile", "bf-ome")
    s = openslide.OpenSlide(str(src))
    d = slide_io.open_slide(str(out))
    assert d.dimensions == s.dimensions == (512, 320)
    box = (0, 0, 512, 320)  # 合成夹具整层都在 ROI 内
    a = s.read_region((box[0], box[1]), 0, (box[2], box[3])).convert("RGB")
    b = d.read_region((box[0], box[1]), 0, (box[2], box[3])).convert("RGB")
    aa = np.asarray(a, dtype=np.int16)
    bb = np.asarray(b, dtype=np.int16)
    diff = np.abs(aa - bb)
    # 有损重编码（q96 4:2:2）差异：远小于裁剪/错位/通道交换
    assert diff.mean() < 6.0, f"mean diff {diff.mean()}"
    for c in range(3):
        assert abs(aa[..., c].mean() - bb[..., c].mean()) < 4.0


# --------------------------------------------------------------------------- #
# 真实样本（NDPI_SAMPLE 环境变量门控；公开 OpenSlide 样本 CMU-1.ndpi）
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 像素坐标；CMU-1.ndpi 主图 51200×38144）——避开边缘，
#: 落在组织内
REAL_ROIS = [
    (20000, 15000, 512, 512),
    (30000, 20000, 512, 512),
    (10000, 8000, 384, 384),
]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not NDPI_SAMPLE.exists(), reason="NDPI_SAMPLE 不存在（真实样本门跳过）")
def test_real_sample_probe_and_default_budget_conversion(workdir):
    """真实样本：默认 192 MiB 预算下转换成功（门禁在 MemoryMax=320M 复核）。"""
    pj = json.loads(_cli("probe", NDPI_SAMPLE).stdout)
    doc = pj["document"]
    assert doc["format"] == "hamamatsu-ndpi-jpeg"
    assert (doc["width"], doc["height"]) == (51200, 38144)
    assert doc["objective"] == 20
    l0 = doc["levels"][0]
    assert l0["restart_interval"] == 256
    assert l0["segments"] == 119200
    # 关联图：macro 被检测并排除
    assert any(a["name"] == "macro" for a in doc["associated"])

    out = workdir / "cmu1-ndpi.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(NDPI_SAMPLE), str(out),
            # 2 GPx 的整层条带样本：分段解码 + 全瓦片重编码远超默认 600 s
            # 墙钟（内存预算仍是默认 192 MiB，由 320M 上限复核）
            "--overwrite", "--profile", "bf-ome", "--timeout", "3600",
        ],
        capture_output=True, text=True, timeout=7200,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "hamamatsu-ndpi-jpeg"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_raw_copied"] == 0
    assert rj["composed"]["fingerprint"] == "ndpi-segment-compose:q96:y422:hstd:v1"
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    # 审查回归（medium）：磁盘预检上界必须是实际上界的真上界——真实样本
    # preserve 输出 ≈ 2.74× 源条带字节，2× 上界曾被突破（浏览器磁盘闸按
    # 该上界预留 OPFS 配额）
    est = json.loads(_cli("probe", NDPI_SAMPLE).stdout)["estimate"]
    assert est["output_upper_bound_bytes"] >= rj["output_bytes"], (
        f"output_upper_bound {est['output_upper_bound_bytes']} < actual {rj['output_bytes']}")
    rj_c = json.loads(
        _cli("convert", NDPI_SAMPLE, workdir / "cmu1-ndpi-compact.ome.tif",
             "--overwrite", "--profile", "bf-ome", "--encoding", "compact",
             "--timeout", "3600").stdout)
    est_c = est["compact_upper_bound_bytes"]
    assert est_c >= rj_c["output_bytes"], (
        f"compact_upper_bound {est_c} < actual {rj_c['output_bytes']}")
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not NDPI_SAMPLE.exists(), reason="NDPI_SAMPLE 不存在（真实样本像素门跳过）")
def test_real_sample_l0_mean_error_and_pyramid_geometry(workdir):
    """重编码类硬门：L0 均值误差上限 + 低倍层几何。

    本适配器没有逐字节搬运路径：L0 是源条带分段解码后按 q96 4:2:2 的
    重编码——与 OpenSlide 读原文件在组织 ROI 上比均值误差（量级 = 一次
    高画质 JPEG 生成损失）；低倍输出层是 l0-box2 生成链（÷2 链，末层
    ≤ 256），与扫描仪自己的降采样层只比均值（重采样核不同）。
    """
    import openslide

    import slide_io

    out = workdir / "cmu1-ndpi.ome.tif"
    # The 320M conversion test above already produced this exact output
    # (same input, bf-ome, preserve); convert only when run on its own.
    if not out.exists():
        _cli("convert", NDPI_SAMPLE, out, "--overwrite", "--profile", "bf-ome",
             "--timeout", "3600")
    src = openslide.OpenSlide(str(NDPI_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "NDPI 转换产物必须是原生 RGB"
    assert src.dimensions == dst.dimensions == (51200, 38144)

    # 低倍层几何：输出 = l0-box2 生成链（51200 → … → ≤256），与 probe 一致
    probe = json.loads(_cli("probe", NDPI_SAMPLE).stdout)["document"]
    assert dst.level_count == len(probe["levels"])
    for level, lv in enumerate(probe["levels"]):
        assert dst.level_dimensions[level] == (lv["width"], lv["height"]), level
    last_w, last_h = dst.level_dimensions[-1]
    assert max(last_w, last_h) <= 256

    # L0 组织 ROI：重编码均值误差上限（一次高画质生成损失）
    for (x, y, w, h) in REAL_ROIS:
        a = src.read_region((x, y), 0, (w, h)).convert("RGB")
        b = dst.read_region((x, y), 0, (w, h)).convert("RGB")
        d = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
        assert d.mean() < 6.0, f"ROI ({x},{y}) L0 均值误差 {d.mean():.3f} 超上限"
        assert d.max() <= 255
        for c in range(3):
            assert abs(np.asarray(a)[..., c].mean() - np.asarray(b)[..., c].mean()) < 4.0

    # 低倍层（l0-box2 生成 vs 扫描仪自己的降采样层）：均值误差上限。
    # 源 level 1 = 12800×9536（÷4）；输出 level 2 同尺寸（box2²）——
    # 重采样核不同（box 平均 vs 扫描器），组织内容必须仍然紧贴。
    x, y, w, h = 16000, 12000, 1024, 1024
    for out_level, src_level in ((2, 1), (4, 2)):
        a = src.read_region((x // 4 ** src_level, y // 4 ** src_level),
                            src_level, (w // 64, h // 64)).convert("RGB")
        b = dst.read_region((x // 2 ** out_level, y // 2 ** out_level),
                            out_level, (w // 64, h // 64)).convert("RGB")
        assert a.size == b.size
        d = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
        assert d.mean() < 12.0, (
            f"输出层 {out_level} vs 源层 {src_level} 均值误差 {d.mean():.3f} 超上限"
        )
