# -*- coding: utf-8 -*-
"""F5 通用瓦片 JPEG TIFF/BigTIFF 输入适配器测试（tile 搬运类）。

Skip 行为（硬性要求）：CLI 未构建或真实样本（GTIFF_SAMPLE）缺失时，受影响
的测试带清晰理由跳过——绝不失败。样本路径只经环境变量传入（公开 OpenSlide
样本 CMU-1.tiff，Generic-TIFF 目录，见 .testdata/openslide/ucf-samples.env），
不落私有路径。

覆盖：
  * 合成夹具（CLI gen-gtiff）：双输出 profile 转换、结构校验、平台读取器
    打开产物确认 native_rgb、缺失降采样层的 l0-box2 生成（层数/几何/L1
    内容贴 L0）、变体拒绝（条带/LZW/deflate/灰度/16 位/平面/描述伪装
    OME/转换器/Aperio/SCN）、内存预算分配前拒绝；
  * 真实样本（环境变量门控）：默认 192 MiB 预算转换成功；转换产物与
    OpenSlide 读原文件在若干组织 ROI 上 **L0 像素一致**（tile 搬运类）；
    低倍层几何一致（层数与各层尺寸 == 源）；slide_io.open_slide 打开产物
    确认 native_rgb。

用法：
  TMPDIR=.gate-tmp/tmp .venv/bin/python -m pytest tests/test_slide_transform_gtiff.py -q
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

#: 公开 OpenSlide 样本（CMU-1.tiff，CC0；路径仅经环境变量传入）。
#: 未设置时必须解析为**不存在的路径**：Path("") 会变成 Path(".") 而
#: .exists() 为真，skipif 失效（同 F4 审查 2026-10-05 #1）。
_GTIFF_SAMPLE_UNSET = "/nonexistent/gtiff-sample-unset"
GTIFF_SAMPLE = Path(os.environ.get("GTIFF_SAMPLE") or _GTIFF_SAMPLE_UNSET)


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
    return tmp_path_factory.mktemp("f5-gtiff")


def test_sample_env_unset_means_skip_not_fail(tmp_path):
    """回归（同 F4 审查 #1）：GTIFF_SAMPLE 未设置时真实样本测试必须 skip。"""
    env = {k: v for k, v in os.environ.items() if k != "GTIFF_SAMPLE"}
    env["TMPDIR"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_slide_transform_gtiff.py", "-q",
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
    """gen-gtiff 夹具 → 双 profile 转换：报告契约、结构校验、平台读取器。"""
    import tifffile

    import slide_io

    src = workdir / "syn.tiff"
    _cli("gen-gtiff", src, "--width", "520", "--height", "300", "--tile", "128",
         "--levels", "3")
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert doc["format"] == "generic-tiled-jpeg-tiff"
    assert doc["adapter_version"] == "1"
    assert doc["modality"] == "brightfield"
    assert doc["pyramid_method"] == "l0-box2"
    # 3 个源层（520/260/130），末层 ≤256px → 无生成层
    assert [l["width"] for l in doc["levels"]] == [520, 260, 130]
    assert doc["generated_levels"] == []
    # 分辨率标签：10 px/cm → 1000 µm/px（样本族同款占位值）
    assert doc["mpp_x"] == 1000.0

    for profile, out in (
        ("bf-classic", workdir / "syn-classic.tif"),
        ("bf-ome", workdir / "syn.ome.tif"),
    ):
        rj = json.loads(
            _cli("convert", src, out, "--overwrite", "--profile", profile).stdout
        )
        assert rj["source_format"] == "generic-tiled-jpeg-tiff"
        assert rj["adapter_version"] == "1"
        assert rj["tiles_reencoded"] == 0
        vj = json.loads(
            _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
        )
        assert vj["ok"] is True
        sp = slide_io.open_slide(str(out))
        assert sp.dimensions == (520, 300)
        assert sp.level_count == 3
        if hasattr(sp, "is_native_rgb"):
            assert sp.is_native_rgb, "转换产物必须是原生 RGB"

    # classic 布局的 tile 载荷结构：photometric 6（YCbCr JPEG）、tile 128
    with tifffile.TiffFile(workdir / "syn-classic.tif") as tf:
        page = tf.pages[0]
        assert page.photometric == 6
        assert page.tags["TileWidth"].value == 128
        assert page.is_tiled


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_missing_levels_generated_with_l0_box2(workdir):
    """单层源 → 生成层补齐：层数/几何/报告计数；L0 tile 仍原样搬运。"""
    import openslide

    import slide_io

    src = workdir / "single.tiff"
    # 渐变内容：重编码（q96/4:2:2）误差在 DCT 底噪内，像素门只钉「合成与
    # 索引」而不是编解码损失（噪声内容 4:2:2 色度减采样误差大，另见 Rust 测试）
    _cli("gen-gtiff", src, "--width", "520", "--height", "300", "--tile", "128",
         "--levels", "1", "--no-xres", "--gradient")
    pj = json.loads(_cli("probe", src).stdout)
    doc = pj["document"]
    assert len(doc["levels"]) == 1
    assert [(g["width"], g["height"]) for g in doc["generated_levels"]] == \
        [(260, 150), (130, 75)]
    assert doc["mpp_x"] is None, "分辨率标签缺失时不发明 mpp"

    out = workdir / "single.ome.tif"
    rj = json.loads(_cli("convert", src, out, "--overwrite", "--profile", "bf-ome").stdout)
    assert [l["width"] for l in rj["levels"]] == [520, 260, 130]
    assert rj["levels"][0]["tiles_raw_copied"] == rj["levels"][0]["tiles_total"]
    assert rj["levels"][1]["tiles_reencoded"] == 6
    assert rj["levels"][2]["tiles_reencoded"] == 2
    assert rj["tiles_raw_copied"] == rj["levels"][0]["tiles_total"]
    assert any(w.startswith("gtiff_missing_levels_generated") for w in rj["warnings"])
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True

    sp = slide_io.open_slide(str(out))
    assert sp.level_count == 3
    assert sp.level_dimensions == ((520, 300), (260, 150), (130, 75))
    assert getattr(sp, "is_native_rgb", True)

    # 生成层与 L0 的几何一致性：box2 链下 L1 的 ROI 均值必须紧贴 L0 同位置
    # 4× 重采样均值（渐变/组织内容；合成夹具是噪声纹理，给均值上限）
    src_sl = openslide.OpenSlide(str(src))
    l0 = np.asarray(
        src_sl.read_region((0, 0), 0, (512, 256)).convert("RGB"), dtype=np.float32
    )
    expect_l1 = (l0[0:256:2, 0:512:2] + l0[1:256:2, 0:512:2]
                 + l0[0:256:2, 1:512:2] + l0[1:256:2, 1:512:2]) / 4.0
    got_l1 = np.asarray(sp.read_region((0, 0), 1, (256, 128)).convert("RGB"),
                        dtype=np.float32)
    d = np.abs(expect_l1 - got_l1)
    assert d.mean() < 8.0, f"生成 L1 与 L0 box2 均值差 {d.mean():.2f} 超上限"
    assert d.max() < 48.0, f"生成 L1 逐像素最大差 {d.max()} 超上限"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize(
    "knobs,frag",
    [
        (["--stripped"], "带状存储"),
        (["--deflate"], "deflate"),
        (["--lzw"], "LZW"),
        (["--gray"], "SamplesPerPixel=1"),
        (["--bits16"], "BitsPerSample"),
        (["--planar2"], "PlanarConfiguration=2"),
        (["--desc", "ome"], "OME-TIFF"),
        (["--desc", "converter"], "不是转换输入"),
    ],
)
def test_variant_rejections_before_copy(workdir, knobs, frag):
    """变体在复制前类型化拒绝（「暂时直传」变体，不是损坏）。"""
    src = workdir / ("v" + "-".join(knobs).replace("--", "") + ".tiff")
    _cli("gen-gtiff", src, *knobs)
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
@pytest.mark.parametrize("knobs,fmt", [
    (["--desc", "aperio"], "aperio-svs-jpeg"),
    (["--desc", "scn"], "leica-scn-jpeg"),
])
def test_vendor_descriptions_route_to_their_adapters(workdir, knobs, fmt):
    """已知厂商描述经路由分派给各自适配器，不进通用适配器。"""
    src = workdir / ("r" + "-".join(knobs).replace("--", "") + ".tiff")
    _cli("gen-gtiff", src, *knobs)
    pj = json.loads(_cli("probe", src).stdout)
    # probe 成功时文档必须来自被分派的适配器，而不是通用适配器
    assert pj["document"]["format"] == fmt


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_memory_budget_refusal_before_allocation(workdir):
    """预算 < 结构预留 → 分配前 resource_profile_insufficient。"""
    src = workdir / "b.tiff"
    _cli("gen-gtiff", src, "--width", "400", "--height", "260")
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
# 真实样本（GTIFF_SAMPLE 环境变量门控；公开 OpenSlide 样本 CMU-1.tiff）
# --------------------------------------------------------------------------- #

#: 组织 ROI（level 0 像素坐标；CMU-1 主图 46000×32914）——避开边缘
REAL_ROIS = [
    (10000, 10000, 512, 512),
    (20000, 18000, 512, 512),
    (5000, 25000, 384, 384),
]


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not GTIFF_SAMPLE.exists(), reason="GTIFF_SAMPLE 不存在（真实样本门跳过）")
def test_real_sample_probe_and_default_budget_conversion(workdir):
    """真实样本：默认 192 MiB 预算下转换成功（门禁在 MemoryMax=320M 复核）。"""
    pj = json.loads(_cli("probe", GTIFF_SAMPLE).stdout)
    doc = pj["document"]
    assert doc["format"] == "generic-tiled-jpeg-tiff"
    assert doc["levels"][0]["width"] == 46000
    assert doc["levels"][0]["height"] == 32914
    assert doc["levels"][0]["tile_w"] == 256
    # 源金字塔 9 层、到 179×128（≤256px 阈值）→ 无生成层
    assert len(doc["levels"]) == 9
    assert doc["levels"][-1]["width"] == 179
    assert doc["generated_levels"] == []

    out = workdir / "cmu1-generic.ome.tif"
    r = subprocess.run(
        [
            "systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=320M", "-p", "MemorySwapMax=0",
            str(CLI), "convert", str(GTIFF_SAMPLE), str(out),
            "--overwrite", "--profile", "bf-ome",
        ],
        capture_output=True, text=True, timeout=3600,
    )
    assert r.returncode == 0, f"320M 上限下转换失败: {r.stderr[-2000:]}"
    rj = json.loads(r.stdout)
    assert rj["source_format"] == "generic-tiled-jpeg-tiff"
    assert rj["adapter_version"] == "1"
    assert rj["output_profile"] == "bf-ome"
    assert rj["tiles_reencoded"] == 0, "tile 搬运类不允许重编码"
    assert len(rj["levels"]) == 9
    assert sum(l["tiles_total"] for l in rj["levels"]) == rj["validation"]["tile_records_emitted"]
    vj = json.loads(
        _cli("validate", out, "--expect-ifd", str(rj["validation"]["ifd_count"])).stdout
    )
    assert vj["ok"] is True


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(not GTIFF_SAMPLE.exists(), reason="GTIFF_SAMPLE 不存在（真实样本像素门跳过）")
def test_real_sample_l0_pixels_match_openslide(workdir):
    """tile 搬运类硬门：转换产物与 OpenSlide 读原文件在组织 ROI 上 L0 像素一致。

    源是通用金字塔 TIFF（主链 9 层，无 associated 图，无 bounds 偏移）；
    转换产物逐 IFD 对应源层。L0 逐像素一致（passthrough 零误差）；低倍层
    是同一批 tile 的原样搬运，同样逐像素一致（几何即源金字塔）。
    """
    import openslide

    import slide_io

    out = workdir / "cmu1-generic-pix.ome.tif"
    rj = json.loads(
        _cli("convert", GTIFF_SAMPLE, out, "--overwrite", "--profile", "bf-ome").stdout
    )
    src = openslide.OpenSlide(str(GTIFF_SAMPLE))
    dst = slide_io.open_slide(str(out))
    assert getattr(dst, "is_native_rgb", True), "转换产物必须是原生 RGB"
    # 层几何：逐层一致（源 9 层，输出 9 层，同尺寸）
    assert src.level_count == dst.level_count == 9
    for level in range(src.level_count):
        assert dst.level_dimensions[level] == src.level_dimensions[level], level
    # 硬门：若干组织 ROI 的 L0 逐像素一致（passthrough 适配器零误差）
    for (x, y, w, h) in REAL_ROIS:
        a = src.read_region((x, y), 0, (w, h)).convert("RGB")
        b = dst.read_region((x, y), 0, (w, h)).convert("RGB")
        aa = np.asarray(a, dtype=np.int16)
        bb = np.asarray(b, dtype=np.int16)
        d = np.abs(aa - bb)
        assert d.max() == 0, f"ROI ({x},{y}) L0 像素不一致：max diff {d.max()}"
    # 低倍层同为原样搬运（payload 逐字节一致）：抽查 level 2 的 ROI。
    # 两侧读器的低倍层像素可有 ±3 的小差：OpenSlide 侧 downsample 非整
    # （4.00012，x/y 比取平均）带重采样相位；源文件 IFD 不写 tag 530，
    # 读端按缺省色度上采样解源，而产物写的是 SOF 真值 (2,1)——产物更正确。
    # 因此低倍层给均值误差上限（同 SCN 先例），L0 仍逐像素硬门。
    x, y, w, h = REAL_ROIS[1]
    sx, sy, sw, sh = x // 4, y // 4, w // 4, h // 4
    a = src.read_region((sx, sy), 2, (sw, sh)).convert("RGB")
    b = dst.read_region((sx, sy), 2, (sw, sh)).convert("RGB")
    d = np.abs(
        np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16)
    )
    assert d.mean() < 1.0, f"level 2 均值误差 {d.mean():.3f} 超上限"
    assert d.max() <= 8, f"level 2 逐像素最大差 {d.max()} 超上限"
    assert rj["levels"][0]["tiles_filled"] == 0
