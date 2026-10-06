# -*- coding: utf-8 -*-
"""F7 Hamamatsu VMS 输入适配器测试（多文件包拼接重编码类）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（VMS_SAMPLE_DIR，含 .vms 入口 +
同目录 tile JPEG 的完整包）缺失时，受影响的测试带清晰理由跳过——绝不失败。
样本路径只经环境变量传入（公开 OpenSlide 样本 CMU-1 VMS，见
.testdata/openslide/ucf-samples.env），不落私有路径。

覆盖：
  * 合成夹具（CLI gen-vms）：双输出 profile 转换、报告契约（mosaic compose
    指纹 / l0-box2）、结构校验、平台读取器打开产物确认 native_rgb、变体拒绝
    （无 restart marker / 渐进 SOF / 缺成员列出 / VMU / 多焦面 / 路径穿越）、
    内存预算分配前拒绝；
  * 合成夹具像素门：转换产物与 OpenSlide 读原 .vms 在整层与低倍层比较
    （拼接重编码类，L0 均值误差上限；夹具含跨 tile 接缝的全局图案）；
  * 真实样本（环境变量门控）：默认 192 MiB 预算（门禁在 MemoryMax=320M
    复核）转换成功；转换产物与 OpenSlide 读原包在若干组织 ROI 上比较 L0
    均值误差上限；低倍层为 l0-box2 生成链，检查几何（÷2 链、末层 ≤ 256）
    与扫描仪自己的降采样层的均值误差；slide_io.open_slide 打开产物确认
    native_rgb。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_vms.py -q
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

#: 公开 OpenSlide 样本（CMU-1 VMS，CC0；路径仅经环境变量传入）。
#: 指向包含 .vms 入口 + 同目录 tile JPEG 的完整包目录。未设置时必须解析为
#: **不存在的路径**（与 F4/F6 套件同一回归审查）。
_VMS_SAMPLE_UNSET = "/nonexistent/vms-sample-unset"
VMS_SAMPLE_DIR = Path(os.environ.get("VMS_SAMPLE_DIR") or _VMS_SAMPLE_UNSET)


def _sample_entry(sample_dir: Path) -> Path | None:
    """完整包目录里唯一的 .vms 入口（0 或 ≥2 个 → None，由调用方断言）。"""
    if not sample_dir.exists():
        return None
    entries = sorted(sample_dir.glob("*.vms"))
    return entries[0] if len(entries) == 1 else None


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
    return tmp_path_factory.mktemp("f7-vms")


# --------------------------------------------------------------------------- #
# 合成夹具
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_gen_probe_and_convert_both_profiles(workdir):
    """gen-vms 夹具 → probe 契约 + 双 profile 转换 + 结构校验 + 读取器。"""
    import tifffile

    import slide_io

    src_dir = workdir / "syn"
    _cli("gen-vms", src_dir)
    entry = src_dir / "synthetic.vms"
    pj = json.loads(_cli("probe", entry).stdout)
    doc = pj["document"]
    assert doc["format"] == "hamamatsu-vms-bundle"
    assert doc["adapter"] == "hamamatsu-vms-bundle"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    # 拼接几何：等宽列 256×2，行高 256+144
    assert (doc["width"], doc["height"]) == (512, 400)
    assert (doc["grid_cols"], doc["grid_rows"]) == (2, 2)
    assert doc["objective"] == 40
    assert doc["mpp_source"] == "vms-physicalwidth-nm"
    assert doc["pyramid_method"] == "l0-box2"
    assert doc["map_file"] is True and doc["opt_file"] is True
    # 每 tile 的分段几何（S422：MCU 16×8，DRI = 每行 MCU 数）
    t00 = doc["tiles"][0]
    assert (t00["width"], t00["height"]) == (256, 256)
    assert t00["restart_interval"] == 16
    # segments = ⌈总 MCU / DRI⌉ = (16 宽 × 32 高) / 16 = 32
    assert t00["segments"] == ((256 // 16) * (256 // 8)) // 16
    # 关联图：macro 被检测并排除
    assert [a["name"] for a in doc["associated"]] == ["macro"]
    # 输出金字塔 = 重编码 L0 + l0-box2 生成尾
    assert [(l["width"], l["height"]) for l in doc["levels"]] == [
        (512, 400), (256, 200)]
    assert doc["levels"][-1]["generated"] is True

    for profile, out in (
        ("bf-classic", workdir / "syn-classic.tif"),
        ("bf-ome", workdir / "syn.ome.tif"),
    ):
        rj = json.loads(
            _cli("convert", entry, out, "--overwrite", "--profile", profile).stdout
        )
        assert rj["source_format"] == "hamamatsu-vms-bundle"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_raw_copied"] == 0
        assert rj["tiles_reencoded"] == sum(l["tiles_total"] for l in rj["levels"])
        # 拼接重编码类：composed 摘要 + 保留画质指纹
        assert rj["composed"]["mode"] == "mosaic-compose-reencode"
        assert rj["composed"]["fingerprint"] == "vms-mosaic-compose:q96:y422:hstd:v1"
        assert rj["composed"]["pyramid"] == "l0-box2"
        vj = json.loads(
            _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
        )
        assert vj["ok"] is True
        sp = slide_io.open_slide(str(out))
        assert sp.dimensions == (512, 400)
        if hasattr(sp, "is_native_rgb"):
            assert sp.is_native_rgb, "转换产物必须是原生 RGB"

    # classic 布局的 tile 载荷结构：photometric 6（YCbCr JPEG）、tile 256
    with tifffile.TiffFile(workdir / "syn-classic.tif") as tf:
        page = tf.pages[0]
        assert page.photometric == 6
        assert page.tags["TileWidth"].value == 256
        assert page.is_tiled

    # strict-lossless 对 VMS 是类型化拒绝（输出必然重编码）
    r = _cli("convert", entry, workdir / "no.tif", "--overwrite",
             "--policy", "strict-lossless", check=False)
    assert r.returncode == 1
    assert json.loads(r.stdout)["error"]["code"] == "pixel_policy_violation"
    # 荧光 profile 同样拒绝（VMS 恒为明场）
    r = _cli("convert", entry, workdir / "no2.tif", "--overwrite",
             "--profile", "fl-ome", check=False)
    assert r.returncode == 1
    assert "荧光" in json.loads(r.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "knobs,frag",
    [
        (["--no-restart"], "restart marker"),
        (["--progressive"], "SOF"),
        # 缺成员：一条错误列出全部缺的名字，复制/转换之前拒绝
        (["--missing-member"], "缺少成员"),
        (["--vmu"], "VMU"),
        (["--multi-layer"], "NoLayers=2"),
        (["--traversal"], "路径穿越"),
    ],
)
def test_variant_rejections_before_copy(workdir, knobs, frag):
    """无 restart/渐进/缺成员/VMU/多焦面/穿越名在复制前类型化拒绝。"""
    src_dir = workdir / ("v" + "-".join(knobs).replace("--", ""))
    _cli("gen-vms", src_dir, *knobs)
    out = workdir / "rejected.tif"
    r = _cli("convert", src_dir / "synthetic.vms", out, "--overwrite", check=False)
    assert r.returncode == 1
    assert not out.exists(), "拒绝必须发生在写出之前"
    err = json.loads(r.stdout)["error"]
    assert err["code"] in ("unsupported_kfb_variant", "jpeg_decode_failed",
                           "conversion_validation_failed")
    assert frag in err["message"]
    if knobs != ["--missing-member"]:
        # probe 给同一拒绝（前端嗅探失败兜底路径）；缺成员在 probe 同样拒绝
        pj = _cli("probe", src_dir / "synthetic.vms", check=False)
        assert pj.returncode == 1
        assert frag in json.loads(pj.stdout)["error"]["message"]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_missing_members_are_listed_together(workdir):
    """两个缺成员在同一条拒绝里列出（不是只报第一个）。"""
    src_dir = workdir / "missing2"
    _cli("gen-vms", src_dir, "--missing-member")
    # 再删一个 tile 成员，凑成两个缺失
    (src_dir / "synthetic-1-0.jpg").unlink()
    r = _cli("probe", src_dir / "synthetic.vms", check=False)
    assert r.returncode == 1
    msg = json.loads(r.stdout)["error"]["message"]
    assert "缺少成员" in msg
    assert "synthetic-1-1.jpg" in msg and "synthetic-1-0.jpg" in msg


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 常驻 tile 头/条带工作集 → 分配前 resource_profile_insufficient。"""
    src_dir = workdir / "b"
    _cli("gen-vms", src_dir)
    out = workdir / "b.tif"
    r = _cli("convert", src_dir / "synthetic.vms", out, "--overwrite",
             "--memory-budget", "65536", check=False)
    assert r.returncode == 1
    assert not out.exists()
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "resource_profile_insufficient"
    assert "内存预算不足" in err["message"]
    # 默认预算（192 MiB saver 档）下同一输入转换成功
    r2 = _cli("convert", src_dir / "synthetic.vms", out, "--overwrite")
    assert json.loads(r2.stdout)["output_bytes"] > 0


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_synthetic_pixels_match_openslide(workdir):
    """合成夹具像素门：拼接产物 == OpenSlide 读原 .vms（重编码噪声内）。

    夹具的 tile 图案是绝对拼接坐标的全局函数（跨 tile 接缝连续），任何
    错位/漏拼都会在接缝处产生远超重编码噪声的台阶。
    """
    import openslide

    import slide_io

    src_dir = workdir / "pix"
    _cli("gen-vms", src_dir)
    out = workdir / "pix.ome.tif"
    _cli("convert", src_dir / "synthetic.vms", out, "--overwrite", "--profile", "bf-ome")
    s = openslide.OpenSlide(str(src_dir / "synthetic.vms"))
    d = slide_io.open_slide(str(out))
    assert d.dimensions == s.dimensions == (512, 400)
    a = s.read_region((0, 0), 0, (512, 400)).convert("RGB")
    b = d.read_region((0, 0), 0, (512, 400)).convert("RGB")
    diff = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
    assert diff.mean() < 3.0, f"L0 mean diff {diff.mean()}"
    assert diff.max() <= 40
    # 低倍层：box2 生成层 vs OpenSlide 自己的 ÷2 层（重采样核不同，比均值）
    a1 = s.read_region((32, 24), 1, (128, 100)).convert("RGB")
    b1 = d.read_region((32, 24), 1, (128, 100)).convert("RGB")
    d1 = np.abs(np.asarray(a1, dtype=np.int16) - np.asarray(b1, dtype=np.int16))
    assert d1.mean() < 6.0, f"L1 mean diff {d1.mean()}"


# --------------------------------------------------------------------------- #
# 真实样本（VMS_SAMPLE_DIR 环境变量门控；公开 OpenSlide 样本 CMU-1 VMS）
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 像素坐标；CMU-1 VMS 主图 102400×76288）——避开边缘，
#: 全部落在组织内（实测灰度均值 ≈ 194）
REAL_ROIS = [
    (30000, 25000, 512, 512),
    (60000, 35000, 512, 512),
    (45000, 45000, 384, 384),
]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    _sample_entry(VMS_SAMPLE_DIR) is None,
    reason="VMS_SAMPLE_DIR 不存在或没有唯一 .vms 入口（真实样本门跳过）",
)
def test_real_sample_probe_and_default_budget_conversion(workdir):
    """真实样本：默认 192 MiB 预算下转换成功（门禁在 MemoryMax=320M 复核）。"""
    entry = _sample_entry(VMS_SAMPLE_DIR)
    pj = json.loads(_cli("probe", entry).stdout)
    doc = pj["document"]
    assert doc["format"] == "hamamatsu-vms-bundle"
    assert (doc["width"], doc["height"]) == (102400, 76288)
    assert (doc["grid_cols"], doc["grid_rows"]) == (2, 2)
    assert doc["objective"] == 40
    # mpp = PhysicalWidth/(1000·宽)，与 OpenSlide 同式
    assert abs(doc["mpp_x"] - 23367500 / (1000 * 102400)) < 1e-9
    assert abs(doc["mpp_y"] - 17357904 / (1000 * 76288)) < 1e-9
    assert doc["map_file"] is True and doc["opt_file"] is True
    for t in doc["tiles"]:
        assert t["restart_interval"] == 512
    # 关联图：macro 被检测并排除
    assert any(a["name"] == "macro" for a in doc["associated"])

    out = workdir / "cmu1-vms.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(entry), str(out),
            # 7.8 GPx 的拼接样本：逐 tile 分段解码 + 全瓦片重编码远超默认
            # 600 s 墙钟（内存预算仍是默认 192 MiB，由 320M 上限复核）
            "--overwrite", "--profile", "bf-ome", "--timeout", "7200",
        ],
        capture_output=True, text=True, timeout=14400,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "hamamatsu-vms-bundle"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_raw_copied"] == 0
    assert rj["composed"]["fingerprint"] == "vms-mosaic-compose:q96:y422:hstd:v1"
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    # 审查回归：磁盘预检上界必须是实际上界的真上界（浏览器磁盘预检按上界
    # 预留 OPFS 配额；与 NDPI 套件同一断言）
    est = json.loads(_cli("probe", entry).stdout)["estimate"]
    assert est["output_upper_bound_bytes"] >= rj["output_bytes"], (
        f"output_upper_bound {est['output_upper_bound_bytes']} < actual {rj['output_bytes']}")
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    _sample_entry(VMS_SAMPLE_DIR) is None,
    reason="VMS_SAMPLE_DIR 不存在或没有唯一 .vms 入口（真实样本像素门跳过）",
)
def test_real_sample_l0_mean_error_and_pyramid_geometry(workdir):
    """重编码类硬门：L0 均值误差上限 + 低倍层几何。

    本适配器没有逐字节搬运路径：L0 是各 tile 分段解码后按真实位置拼接、
    再按 q96 4:2:2 重编码——与 OpenSlide 读原包在组织 ROI 上比均值误差
    （量级 = 一次高画质 JPEG 生成损失）；低倍输出层是 l0-box2 生成链
    （÷2 链，末层 ≤ 256），与扫描仪自己的降采样层只比均值（重采样核不同）。
    """
    import openslide

    import slide_io

    entry = _sample_entry(VMS_SAMPLE_DIR)
    out = workdir / "cmu1-vms.ome.tif"
    # The 320M conversion test above already produced this exact output
    # (same input, bf-ome, preserve); convert only when run on its own.
    if not out.exists():
        _cli("convert", entry, out, "--overwrite", "--profile", "bf-ome",
             "--timeout", "7200")
    src = openslide.OpenSlide(str(entry))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "VMS 转换产物必须是原生 RGB"
    assert src.dimensions == dst.dimensions == (102400, 76288)

    # 低倍层几何：输出 = l0-box2 生成链（102400 → … → 400×298），与 probe 一致
    probe = json.loads(_cli("probe", entry).stdout)["document"]
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
        for c in range(3):
            assert abs(np.asarray(a)[..., c].mean() - np.asarray(b)[..., c].mean()) < 4.0

    # 低倍层（l0-box2 生成 vs 扫描仪自己的降采样层）：均值误差上限。
    # 源 level 2 = 25600×19072（÷4）；输出 level 2 同尺寸（box2²）——
    # 重采样核不同（box 平均 vs 扫描器），组织内容必须仍然紧贴。
    x, y, w, h = 16000, 12000, 1024, 1024
    for out_level, src_level in ((2, 2), (4, 4)):
        scale = 2 ** out_level
        a = src.read_region((x // scale, y // scale), src_level,
                            (w // scale, h // scale)).convert("RGB")
        b = dst.read_region((x // scale, y // scale), out_level,
                            (w // scale, h // scale)).convert("RGB")
        assert a.size == b.size
        d = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))
        assert d.mean() < 12.0, (
            f"输出层 {out_level} vs 源层 {src_level} 均值误差 {d.mean():.3f} 超上限"
        )
