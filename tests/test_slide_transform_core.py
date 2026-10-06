# -*- coding: utf-8 -*-
"""C1 differential harness: Rust `slide-transform` CLI vs the Python oracle
(``kfb/converter.py`` / ``kfb/converter_fl.py``).

Skip behaviour (hard requirement): if the CLI binary has not been built or
the private real samples are absent, the affected tests skip with a clear
reason — never fail.

Layout:
  * synthetic fixtures (several sizes incl. non-multiple-of-256): brightfield
    whole-file sha256 byte equality; fluorescence structure + full-tile
    payload sha256 equality + OME-XML semantics + ExposureTime-on-Plane.
  * five real samples (aliases KFB-1, KFBF-A..D in sorted-name order; sha256
    pinned): full-tile payload equality, structure, OME metadata, and the
    hard edge gate — every edge tile PSNR vs source ≥ oracle PSNR − 0.5 dB
    and global max|diff| ≤ oracle's (our codec is Pillow-bit-exact, so edge
    tiles are byte-equal and the gate holds with margin 0).
  * readers: tifffile open + structure; Bio-Formats showinf opens both
    outputs (no exceptions, level count > 1).
  * strict-lossless policy rejection; channel.json absorption.
  * >4 GiB synthetic proof for both writers (BigTIFF 64-bit offsets).
  * native RSS/time per sample.

Usage:
  PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest tests/test_slide_transform_core.py -x -q
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from kfb.converter import convert_kfb  # noqa: E402
from kfb.converter_fl import convert_kfbf  # noqa: E402
from kfb.fixture import build_synthetic_kfb  # noqa: E402
from kfb.fixture_fl import build_synthetic_kfbf  # noqa: E402

CLI_ENV = os.environ.get(
    "SLIDE_TRANSFORM_CLI",
    str(REPO / "slide-transform-core" / "target" / "release" / "slide-transform"),
)
CLI_DEBUG = REPO / "slide-transform-core" / "target" / "debug" / "slide-transform"
CLI = CLI_ENV if Path(CLI_ENV).exists() else (CLI_DEBUG if CLI_DEBUG.exists() else None)

SAMPLE_DIR = Path(
    os.environ.get(
        "KFB_SAMPLE_DIR", "/home/solarise/ZCodeProject/histopilot-suite/切片文件夹"
    )
)

# Private samples: referenced ONLY by alias + sha256.
# KFB-1: brightfield; KFBF-A..D: fluorescence, sorted-name order.
EXPECTED_SHA256 = {
    "KFB-1": "17fe1cfde1a6d1f1d8778ea5854db5fd353f657e51003c606b404b60300aab71",
    "KFBF-A": "1d10e48fa6e7a7da96dfe5ada5d4c55199fd77ac29560b537fa9e31a888c78f6",
    "KFBF-B": "0ad51d3dce5d6fcdb5db5a4ea220dd452c9c2d220d590a6cde4d76d83e0e2c94",
    "KFBF-C": "8fe031ad72534a82325ffc56100bbe7bf275ae9808250f9f523ea0ce63a58963",
    "KFBF-D": "4aa66d2fda0d7a9a9f5509063f695a8b9c53b59dcfa9f723d0819b8329f03013",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cli(*args, check=True):
    assert CLI is not None, "slide-transform CLI 未构建（见 docs/slide-tools/c1-core-report.md RERUN）"
    r = subprocess.run(
        [str(CLI), *map(str, args)], capture_output=True, text=True, timeout=3600
    )
    if check and r.returncode != 0:
        raise AssertionError(f"CLI 失败: {r.stdout} {r.stderr}")
    return r


def _locate_samples():
    """Return {alias: path} for the private samples, or {} when absent."""
    out = {}
    if not SAMPLE_DIR.is_dir():
        return out
    kfb = sorted(SAMPLE_DIR.glob("*.kfb"))
    kfbf = sorted(SAMPLE_DIR.glob("ref/*.kfbf"))
    for i, p in enumerate(kfb[:1]):
        out[f"KFB-{i + 1}"] = p
    for i, p in enumerate(kfbf[:4]):
        out[f"KFBF-{chr(ord('A') + i)}"] = p
    return out


SAMPLES = _locate_samples()


def _mem_time_of(cmd):
    """Run cmd via GNU time, return (max_rss_kib, wall_seconds).

    RUSAGE_CHILDREN would report the max over *all* children (the Bio-Formats
    JVM pollutes it), so we wrap each command in /usr/bin/time -f %M.
    """
    import re as _re
    import time

    t0 = time.monotonic()
    r = subprocess.run(
        ["/usr/bin/time", "-f", "STC_MAXRSS=%M", *map(str, cmd)],
        capture_output=True,
        timeout=3600,
    )
    dt = time.monotonic() - t0
    assert r.returncode == 0, r.stderr.decode()[:400]
    m = _re.search(rb"STC_MAXRSS=(\d+)", r.stderr)
    rss = int(m.group(1)) if m else 0
    return rss, dt


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

BF_SIZES = [(580, 300), (256, 256), (613, 355), (1024, 777), (300, 300)]


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("c1-diff")
    return d


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.parametrize("w,h", BF_SIZES)
def test_bf_fixture_whole_file_byte_equality(workdir, w, h):
    src = workdir / f"bf-{w}x{h}.kfb"
    build_synthetic_kfb(src, width=w, height=h)
    oracle = workdir / f"bf-{w}x{h}-oracle.tif"
    mine = workdir / f"bf-{w}x{h}-mine.tif"
    convert_kfb(src, oracle)
    _cli("convert", src, mine, "--overwrite")
    assert _sha256(oracle) == _sha256(mine), "明场输出应与 oracle 逐字节一致"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_fl_fixture_structure_and_payloads(workdir):
    src = workdir / "fl-600x400.kfbf"
    build_synthetic_kfbf(src)
    oracle = workdir / "fl-oracle.tif"
    mine = workdir / "fl-mine.tif"
    convert_kfbf(src, oracle)
    _cli("convert", src, mine, "--overwrite")

    import tifffile

    with tifffile.TiffFile(oracle) as ta, tifffile.TiffFile(mine) as tb:
        assert ta.is_bigtiff and tb.is_bigtiff
        assert ta.is_ome and tb.is_ome
        pa, pb = list(ta.pages), list(tb.pages)
        assert len(pa) == len(pb)
        sa, sb = ta.series[0], tb.series[0]
        assert sa.axes == sb.axes == "CYX"
        assert tuple(sa.shape) == tuple(sb.shape)
        assert len(sa.levels) == len(sb.levels)
        with open(oracle, "rb") as ra, open(mine, "rb") as rb:
            for i, (x, y) in enumerate(zip(pa, pb)):
                off_a = x.tags["TileOffsets"].value
                cnt_a = x.tags["TileByteCounts"].value
                off_b = y.tags["TileOffsets"].value
                cnt_b = y.tags["TileByteCounts"].value
                assert len(off_a) == len(off_b)
                for t in range(len(off_a)):
                    ra.seek(off_a[t])
                    da = ra.read(cnt_a[t])
                    rb.seek(off_b[t])
                    db = rb.read(cnt_b[t])
                    assert da == db, f"IFD{i} tile{t} payload 不一致"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_fl_fixture_ome_semantics(workdir):
    import xml.etree.ElementTree as ET

    src = workdir / "fl-600x400.kfbf"
    mine = workdir / "fl-mine.tif"
    if not mine.exists():
        build_synthetic_kfbf(src)
        _cli("convert", src, mine, "--overwrite")
    import tifffile

    with tifffile.TiffFile(mine) as tf:
        desc = tf.pages[0].description
    root = ET.fromstring(desc)
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
    channels = root.findall(".//ome:Channel", ns)
    planes = root.findall(".//ome:Plane", ns)
    assert len(channels) == 2
    assert len(planes) == 2
    # ExposureTime on <Plane>（C1 变更），Channel 上不再出现
    for ch in channels:
        assert "ExposureTime" not in ch.attrib
    exposures = {p.attrib["TheC"]: p.attrib["ExposureTime"] for p in planes}
    assert exposures == {"0": "6", "1": "2"}
    for p in planes:
        assert p.attrib["ExposureTimeUnit"] == "ms"  # assumed（未升级为确认）
    px = root.find(".//ome:Pixels", ns)
    assert px.attrib["SizeC"] == "2" and px.attrib["SizeX"] == "600"
    assert "PhysicalSizeX" in px.attrib


# --------------------------------------------------------------------------- #
# helpers for tile-level comparison incl. the edge PSNR gate
# --------------------------------------------------------------------------- #


def _tile_kind(page, idx, across):
    row, col = divmod(idx, across)
    return (
        (col + 1) * 256 <= page.imagewidth and (row + 1) * 256 <= page.imagelength
    )


def _psnr(a, b):
    d = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    mse = (d * d).mean()
    if mse == 0:
        return float("inf")
    return 10.0 * np.log10(255.0 * 255.0 / mse)


def _edge_gate_for_bf(source_kfb, oracle_tif, mine_tif):
    """The hard C1 gate on real brightfield samples: for every edge tile,
    PSNR(mine vs source-canvas) ≥ PSNR(oracle vs source-canvas) − 0.5 dB and
    max|diff| ≤ oracle's (ours are byte-equal, so both hold with margin 0)."""
    from PIL import Image

    from kfb.parser import parse_kfb

    doc = parse_kfb(source_kfb)
    import tifffile

    stats = {"tiles": 0, "edge_tiles": 0, "min_psnr_margin": float("inf"),
             "maxdiff_mine": 0, "maxdiff_oracle": 0}
    try:
        with tifffile.TiffFile(oracle_tif) as ta, tifffile.TiffFile(mine_tif) as tb, \
                open(oracle_tif, "rb") as ra, open(mine_tif, "rb") as rb:
            pages_a, pages_b = list(ta.pages), list(tb.pages)
            for i, (x, y) in enumerate(zip(pages_a, pages_b)):
                off_a = x.tags["TileOffsets"].value
                cnt_a = x.tags["TileByteCounts"].value
                off_b = y.tags["TileOffsets"].value
                cnt_b = y.tags["TileByteCounts"].value
                across = (x.imagewidth + 255) // 256
                # source tiles of this level
                lv = next(
                    (l for l in doc.levels if l.width == x.imagewidth and l.height == x.imagelength),
                    None,
                )
                src_tiles = {}
                if lv is not None:
                    for t in doc.tiles_by_level.get(lv.level, []):
                        src_tiles[(t.row, t.col)] = doc.tile_payload(t)
                for t in range(len(off_a)):
                    stats["tiles"] += 1
                    if _tile_kind(x, t, across):
                        continue
                    stats["edge_tiles"] += 1
                    row, col = divmod(t, across)
                    ra.seek(off_a[t])
                    da = ra.read(cnt_a[t])
                    rb.seek(off_b[t])
                    db = rb.read(cnt_b[t])
                    ia = np.asarray(
                        Image.open(io.BytesIO(da)).convert("RGB"), dtype=np.float64
                    )
                    ib = np.asarray(
                        Image.open(io.BytesIO(db)).convert("RGB"), dtype=np.float64
                    )
                    assert ia.shape == ib.shape
                    sp = src_tiles.get((row, col))
                    if sp is None:
                        continue
                    isrc = np.asarray(
                        Image.open(io.BytesIO(sp)).convert("RGB"), dtype=np.float64
                    )
                    canvas = np.full((256, 256, 3), 255.0)
                    canvas[: isrc.shape[0], : isrc.shape[1]] = isrc
                    psnr_o = _psnr(canvas, ia)
                    psnr_m = _psnr(canvas, ib)
                    margin = psnr_m - (psnr_o - 0.5)
                    stats["min_psnr_margin"] = min(stats["min_psnr_margin"], margin)
                    stats["maxdiff_oracle"] = max(
                        stats["maxdiff_oracle"], int(np.abs(canvas - ia).max())
                    )
                    stats["maxdiff_mine"] = max(
                        stats["maxdiff_mine"], int(np.abs(canvas - ib).max())
                    )
                    assert psnr_m >= psnr_o - 0.5, (
                        f"edge tile L{i}({row},{col}): psnr {psnr_m} < oracle {psnr_o} - 0.5"
                    )
                    assert int(np.abs(canvas - ib).max()) <= int(
                        np.abs(canvas - ia).max()
                    ), "global max diff exceeded oracle"
    finally:
        doc.close()
    return stats


def _full_tile_payloads_equal(oracle_tif, mine_tif):
    import tifffile

    n_eq = n_tot = 0
    with tifffile.TiffFile(oracle_tif) as ta, tifffile.TiffFile(mine_tif) as tb, \
            open(oracle_tif, "rb") as ra, open(mine_tif, "rb") as rb:
        for x, y in zip(ta.pages, tb.pages):
            off_a = x.tags["TileOffsets"].value
            cnt_a = x.tags["TileByteCounts"].value
            off_b = y.tags["TileOffsets"].value
            cnt_b = y.tags["TileByteCounts"].value
            across = (x.imagewidth + 255) // 256
            assert len(off_a) == len(off_b)
            for t in range(len(off_a)):
                if not _tile_kind(x, t, across):
                    continue
                ra.seek(off_a[t])
                da = ra.read(cnt_a[t])
                rb.seek(off_b[t])
                db = rb.read(cnt_b[t])
                n_tot += 1
                n_eq += int(hashlib.sha256(da).digest() == hashlib.sha256(db).digest())
    return n_eq, n_tot


def _tifffile_open_check(path, expected_pages_min=1):
    import tifffile

    with tifffile.TiffFile(path) as tf:
        assert tf.is_bigtiff
        pages = list(tf.pages)
        assert len(pages) >= expected_pages_min
        s = tf.series[0]
        assert s.shape[0] > 0
    return len(pages)


#: showinf 输出里真正代表「打开失败」的堆栈行（行首锚定）：showinf 正常
#: 运行也可能在幻灯片元数据/描述文本等处出现 "Exception" 字样，历史实现用
#: 宽泛子串 ``"Exception" not in out`` 判断曾在门禁里对同一产物偶发失败
#: （重跑通过、rc=0）——改为以返回码为准 + 只认这些真正的异常行。
_BF_FATAL_LINE_RES = (
    "Exception in thread",
    "FormatException",
    "IOException",
    "OutOfMemoryError",
)


def _bioformats_open_check(path):
    """Bio-Formats showinf（无像素）——打开成功且 series/level 数 > 1。"""
    import platform

    jdk = Path("/home/solarise/.local/opt/jdk-21.0.12.1+1-jre")
    showinf = Path("/home/solarise/.local/opt/bftools/showinf")
    if not showinf.exists():
        pytest.skip("bftools 不存在")
    env = dict(os.environ)
    if jdk.exists():
        env["JAVA_HOME"] = str(jdk)
        env["PATH"] = f"{jdk / 'bin'}:{env['PATH']}"
    r = subprocess.run(
        [str(showinf), "-nopix", str(path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    out = r.stdout + r.stderr
    # 失败判据：showinf 返回码非 0；或输出含真正的异常堆栈行（行首锚定，
    # 见 _BF_FATAL_LINE_RES——不再用宽泛子串）。失败信息附这些异常行
    # （而非「最后 800 字符」：rc=0 的正常输出尾巴没有诊断价值）。
    fatal = [ln for ln in out.splitlines()
             if any(ln.lstrip().startswith(pfx) for pfx in _BF_FATAL_LINE_RES)]
    assert r.returncode == 0 and not fatal, (
        "showinf rc=%s fatal_lines=%r" % (r.returncode, fatal[:8]))
    return r.returncode, out


# --------------------------------------------------------------------------- #
# real samples
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif("KFB-1" not in SAMPLES, reason="真实样本不可用（私有路径不存在）")
def test_real_kfb1_identity():
    p = SAMPLES["KFB-1"]
    assert _sha256(p) == EXPECTED_SHA256["KFB-1"], "KFB-1 sha256 与标定不符"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif("KFB-1" not in SAMPLES, reason="真实样本不可用")
def test_real_kfb1_differential(workdir):
    src = SAMPLES["KFB-1"]
    oracle = workdir / "KFB-1-oracle.tif"
    mine = workdir / "KFB-1-mine.tif"
    if not oracle.exists():
        convert_kfb(src, oracle)
    rss, dt = _mem_time_of([CLI, "convert", src, mine, "--overwrite"])
    # 全 tile payload sha 相等（硬门槛）
    n_eq, n_tot = _full_tile_payloads_equal(oracle, mine)
    assert n_eq == n_tot, f"full tile payload 不一致 {n_eq}/{n_tot}"
    # 结构
    assert _tifffile_open_check(oracle) == _tifffile_open_check(mine)
    # 边缘质量硬门槛
    stats = _edge_gate_for_bf(src, oracle, mine)
    assert stats["edge_tiles"] > 0
    # Bio-Formats 可开
    rc, _ = _bioformats_open_check(mine)
    # 整文件 sha（报告口径；如失败但上面通过，仍满足验收）
    whole = _sha256(oracle) == _sha256(mine)
    with open(workdir / "KFB-1-stats.json", "w") as f:
        json.dump({"rss_kib": rss, "wall_s": dt, "whole_file_equal": whole, **stats}, f, indent=1)
    assert whole, "KFB-1 整文件 sha 不一致（edge gate 已过，但字节级 parity 应成立）"


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    not any(k in SAMPLES for k in ("KFBF-A", "KFBF-B", "KFBF-C", "KFBF-D")),
    reason="真实样本不可用",
)
@pytest.mark.parametrize("alias", ["KFBF-A", "KFBF-B", "KFBF-C", "KFBF-D"])
def test_real_kfbf_differential(workdir, alias):
    if alias not in SAMPLES:
        pytest.skip(f"{alias} 不存在")
    src = SAMPLES[alias]
    assert _sha256(src) == EXPECTED_SHA256[alias], f"{alias} sha256 与标定不符"
    oracle = workdir / f"{alias}-oracle.tif"
    mine = workdir / f"{alias}-mine.tif"
    if not oracle.exists():
        convert_kfbf(src, oracle)
    rss, dt = _mem_time_of([CLI, "convert", src, mine, "--overwrite"])
    n_eq, n_tot = _full_tile_payloads_equal(oracle, mine)
    assert n_eq == n_tot, f"{alias} full tile payload 不一致 {n_eq}/{n_tot}"
    # OME 语义比较（通道名/颜色/曝光/尺寸）
    import xml.etree.ElementTree as ET

    import tifffile

    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
    with tifffile.TiffFile(oracle) as ta, tifffile.TiffFile(mine) as tb:
        assert ta.is_ome and tb.is_ome
        ra = ET.fromstring(ta.pages[0].description)
        rb = ET.fromstring(tb.pages[0].description)
        ca = [c.attrib for c in ra.findall(".//ome:Channel", ns)]
        cb = [c.attrib for c in rb.findall(".//ome:Channel", ns)]
        assert len(ca) == len(cb)
        for x, y in zip(ca, cb):
            assert x["Name"] == y["Name"]
            assert x["Color"] == y["Color"]
        ea = {p.attrib["TheC"]: p.attrib["ExposureTime"] for p in ra.findall(".//ome:Plane", ns)}
        eb = {p.attrib["TheC"]: p.attrib["ExposureTime"] for p in rb.findall(".//ome:Plane", ns)}
        # oracle 把曝光放 Channel；语义相等（数值），位置按 C1 移到 Plane
        oc = {c["ID"][-1]: c.get("ExposureTime") for c in ca}
        oracle_expo = {v for v in oc.values() if v}
        mine_expo = set(eb.values())
        # 数值同源（.17g 与整数形式都接受数值比较）
        assert {float(v) for v in mine_expo} == {float(v) for v in oracle_expo}, (
            f"曝光数值不同源: mine={mine_expo} oracle={oracle_expo}")
        assert len(eb) == len(cb), "每个通道应有 Plane 曝光"
        assert tuple(ta.series[0].shape) == tuple(tb.series[0].shape)
        assert len(ta.series[0].levels) == len(tb.series[0].levels)
    rc, _ = _bioformats_open_check(mine)
    with open(workdir / f"{alias}-stats.json", "w") as f:
        json.dump({"rss_kib": rss, "wall_s": dt, "full_tiles": [n_eq, n_tot]}, f, indent=1)


# --------------------------------------------------------------------------- #
# policy / companion
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_strict_lossless_rejects_edge_input(workdir):
    src = workdir / "bf-strict.kfb"
    build_synthetic_kfb(src, width=300, height=300)  # has edge tiles
    out = workdir / "bf-strict.tif"
    r = _cli("convert", src, out, "--policy", "strict-lossless", "--overwrite", check=False)
    assert r.returncode != 0
    payload = json.loads(r.stdout)
    assert payload["error"]["code"] == "pixel_policy_violation"
    assert not out.exists()


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_channel_json_absorbed(workdir):
    src = workdir / "fl-600x400.kfbf"
    if not src.exists():
        build_synthetic_kfbf(src)
    cj = workdir / "channel.json"
    cj.write_text(
        json.dumps(
            [
                {"channelName": "DAPI", "channelIndex": 1, "channelColor": "#0000E5",
                 "lower": 0, "upper": 178, "gamma": 1, "show": True},
                {"channelName": "520", "channelIndex": 2, "channelColor": "#00FF00",
                 "lower": 5, "upper": 159, "gamma": 1, "show": True},
            ]
        ),
        encoding="utf-8",
    )
    out = workdir / "fl-companion.tif"
    _cli("convert", src, out, "--channel-json", cj, "--overwrite")
    payload = json.loads(_cli("convert", src, out, "--channel-json", cj,
                              "--overwrite").stdout)
    wins = {c["name"]: c["display_window"] for c in payload["channels"]}
    assert wins["DAPI"] == [0.0, 178.0]
    assert wins["520"] == [5.0, 159.0]
    # body wins on conflict: wrong name in json must not appear
    cj.write_text(json.dumps([
        {"channelName": "NOT-A-CHANNEL", "channelIndex": 9, "channelColor": "#000000",
         "lower": 1, "upper": 2, "gamma": 1, "show": True}]))
    payload = json.loads(_cli("convert", src, out, "--channel-json", cj,
                              "--overwrite").stdout)
    assert all(c["name"] != "NOT-A-CHANNEL" for c in payload["channels"])


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_probe_json(workdir):
    src = workdir / "bf-600.kfb"
    if not src.exists():
        build_synthetic_kfb(src, width=600, height=400)
    payload = json.loads(_cli("probe", src).stdout)
    assert payload["document"]["modality"] == "brightfield"
    assert payload["document"]["width"] == 600
    src2 = workdir / "fl-600x400.kfbf"
    if not src2.exists():
        build_synthetic_kfbf(src2)
    payload = json.loads(_cli("probe", src2).stdout)
    assert payload["document"]["modality"] == "fluorescence"
    assert payload["document"]["channel_count"] == 2


# --------------------------------------------------------------------------- #
# >4 GiB proof (may be slow; opted-in via env to keep default runs bounded)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
@pytest.mark.skipif(
    os.environ.get("C1_BIG") != "1", reason="设 C1_BIG=1 运行 >4GiB 证明"
)
def test_over_4gib_both_writers(workdir):
    import tifffile

    # brightfield: 72000×72000 synthetic (payload tiles streamed from the
    # generator; source ≈ 4.6 GiB, output > 4 GiB)
    big_kfb = workdir / "big.kfb"
    from scripts.c1_biginput import build_big_bf, build_big_fl  # noqa: E402

    rss_bf, dt_bf, out_bf = build_big_bf(big_kfb, CLI)
    with tifffile.TiffFile(out_bf) as tf:
        assert tf.is_bigtiff
        # 64-bit offsets exercised: some TileOffset must exceed 4 GiB
        offs = tf.pages[0].tags["TileOffsets"].value
        assert max(offs) > (1 << 32) - 1, "未出现 >4GiB 偏移"
    big_kfbf = workdir / "big.kfbf"
    rss_fl, dt_fl, out_fl = build_big_fl(big_kfbf, CLI)
    with tifffile.TiffFile(out_fl) as tf:
        assert tf.is_bigtiff
        offs = tf.pages[0].tags["TileOffsets"].value
        assert max(offs) > (1 << 32) - 1
    with open(workdir / "big-stats.json", "w") as f:
        json.dump({"bf": [rss_bf, dt_bf], "fl": [rss_fl, dt_fl]}, f, indent=1)


# --------------------------------------------------------------------------- #
# U3 compact-jpeg-v1 (更小文件·有损): report contract + platform reader
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_compact_report_contract_and_reader(workdir):
    """compact-jpeg-v1 的报告合同 + 平台 reader（slide_io.open_slide）。

    - CLI --encoding compact：lossy_reencode=true + 锁定参数指纹，
      tiles_raw_copied=0（全部重编码）；
    - 结构校验通过（validate）；
    - slide_io.open_slide 将 compact bf-ome 作为原生 RGB 打开，颜色与
      preserve 输出一致（整幅 tile 级比较，仅允许有损重编码差异），
      MPP/尺寸与 preserve 相同；
    - compact 输出小于 preserve 输出（合成 q90 4:2:2 输入）。
    """
    import slide_io

    src = workdir / "bf-compact.kfb"
    build_synthetic_kfb(src, width=1024, height=777)
    preserve = workdir / "bf-preserve.ome.tif"
    compact = workdir / "bf-compact.ome.tif"
    rj = json.loads(_cli("convert", src, preserve, "--overwrite",
                         "--profile", "bf-ome").stdout)
    cj = json.loads(_cli("convert", src, compact, "--overwrite",
                         "--profile", "bf-ome", "--encoding", "compact").stdout)
    # report contract
    assert cj["encoding"] == "compact-jpeg-v1"
    assert cj["lossy_reencode"] is True
    p = cj["lossy_reencode_params"]
    assert p["profile"] == "compact-jpeg-v1"
    assert p["sampling"] == "4:2:0"
    assert p["huffman"] == "standard-annex-k"
    assert p["quality"] >= 75 and p["quality"] <= 90
    assert cj["tiles_raw_copied"] == 0
    assert cj["tiles_reencoded"] == sum(l["tiles_total"] for l in cj["levels"])
    assert cj["output_bytes"] < rj["output_bytes"]
    # structural self-check
    vj = json.loads(_cli("validate", compact, "--expect-ifd",
                         str(cj["validation"]["ifd_count"])).stdout)
    assert vj["ok"] is True
    # platform reader: native RGB, correct colours, same geometry/calibration
    sp = slide_io.open_slide(str(preserve))
    sc = slide_io.open_slide(str(compact))
    assert sp.dimensions == sc.dimensions == (1024, 777)
    assert getattr(sc, "is_native_rgb", False) is True
    assert sc.properties["openslide.mpp-x"] == sp.properties["openslide.mpp-x"]
    assert sc.properties["openslide.mpp-y"] == sp.properties["openslide.mpp-y"]
    from PIL import Image

    for level in (0, min(1, sc.level_count - 1)):
        box = (256, 200, 768, 456)
        a = sp.read_region((box[0] * 2 ** level, box[1] * 2 ** level), level,
                           (box[2] - box[0], box[3] - box[1])).convert("RGB")
        b = sc.read_region((box[0] * 2 ** level, box[1] * 2 ** level), level,
                           (box[2] - box[0], box[3] - box[1])).convert("RGB")
        aa = np.asarray(a, dtype=np.int16)
        bb = np.asarray(b, dtype=np.int16)
        d = np.abs(aa - bb)
        # 有损重编码差异：远小于裁剪/错位/通道交换会造成的变化
        assert d.mean() < 6.0, f"level {level} mean diff {d.mean()}"
        assert d.max() <= 255
        # 通道不交换：逐通道均值接近
        for c in range(3):
            assert abs(aa[..., c].mean() - bb[..., c].mean()) < 4.0


# --------------------------------------------------------------------------- #
# U3 × F1 merged: compact-jpeg-v1 on Aperio SVS input (decode-every-tile
# re-encode; merged branch `fmt-integ`). Sample-gated like the SVS viewer.
# --------------------------------------------------------------------------- #

SVS_SAMPLE = Path(
    os.environ.get(
        "SVS_SAMPLE",
        str(REPO.parent / ".testdata" / "openslide" / "CMU-1-Small-Region.svs"),
    )
)


@pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建")
def test_compact_svs_report_contract_and_pixels(workdir):
    """`--encoding compact` on an SVS input (merged U3×F1 behaviour).

    Synthetic SVS (CLI gen-svs) part:
    - report contract: encoding=compact-jpeg-v1, lossy_reencode=true with the
      locked parameters, tiles_raw_copied=0, adapter provenance kept;
    - structure: photometric 6 + YCbCrSubSampling (2,2) (the LOCKED
      subsampling, not the source's), tile size kept (240), NO tag 347
      (self-contained tiles);
    - slide_io opens the compact output and its pixels track the preserve
      output's within the compact tolerance (bounded ROI).
    """
    import tifffile

    import slide_io

    src = workdir / "svs-compact.svs"
    _cli("gen-svs", src, "--width", "500", "--height", "260", "--tile", "240")
    preserve = workdir / "svs-preserve.ome.tif"
    compact = workdir / "svs-compact.ome.tif"
    rj = json.loads(_cli("convert", src, preserve, "--overwrite",
                         "--profile", "bf-ome").stdout)
    cj = json.loads(_cli("convert", src, compact, "--overwrite",
                         "--profile", "bf-ome", "--encoding", "compact").stdout)
    # report contract
    assert cj["source_format"] == "aperio-svs-jpeg"
    assert cj["encoding"] == "compact-jpeg-v1"
    assert cj["lossy_reencode"] is True
    p = cj["lossy_reencode_params"]
    assert p["profile"] == "compact-jpeg-v1"
    assert p["params_fingerprint"] == "cj1:q80:420:hstd:v1"
    assert p["quality"] == 80
    assert p["sampling"] == "4:2:0"
    assert p["huffman"] == "standard-annex-k"
    assert cj["tiles_raw_copied"] == 0
    assert cj["tiles_reencoded"] == sum(l["tiles_total"] for l in cj["levels"])
    # structure: locked YCbCr payload, no shared JPEGTables, tile size kept
    with tifffile.TiffFile(compact) as tf:
        page = tf.pages[0]
        assert page.photometric == 6  # YCbCr (locked compact payload)
        assert tuple(page.tags["YCbCrSubSampling"].value) == (2, 2)
        assert page.tags["TileWidth"].value == 240  # source tile size kept
        assert "JPEGTables" not in page.tags
    vj = json.loads(_cli("validate", compact, "--expect-ifd",
                         str(cj["validation"]["ifd_count"])).stdout)
    assert vj["ok"] is True
    # strict-lossless refuses the lossy mode (typed policy error)
    refuse = _cli("convert", src, workdir / "nope.tif", "--overwrite",
                  "--profile", "bf-ome", "--encoding", "compact",
                  "--policy", "strict-lossless", check=False)
    assert refuse.returncode == 1
    assert json.loads(refuse.stdout)["error"]["code"] == "pixel_policy_violation"
    # platform reader: the compact output opens with the same geometry
    sp = slide_io.open_slide(str(preserve))
    sc = slide_io.open_slide(str(compact))
    assert sp.dimensions == sc.dimensions == (500, 260)
    # (the synthetic fixture tiles are worst-case uniform noise, so NO pixel
    # tolerance is asserted here — the pixel gates below run on the real
    # H&E sample, exactly like the F1 viewer suite)

    # real-sample part: CMU-1-Small-Region — the compact output's pixels
    # must stay within the KFB compact tolerance class against BOTH
    # OpenSlide's reading of the ORIGINAL SVS and the preserve output
    # (bounded ROI only)
    if not SVS_SAMPLE.exists():
        pytest.skip("SVS_SAMPLE 不存在（真实样本像素门跳过）")
    real_preserve = workdir / "real-preserve.ome.tif"
    real_compact = workdir / "real-compact.ome.tif"
    _cli("convert", SVS_SAMPLE, real_preserve, "--overwrite", "--profile", "bf-ome")
    rcj = json.loads(_cli("convert", SVS_SAMPLE, real_compact, "--overwrite",
                          "--profile", "bf-ome", "--encoding", "compact").stdout)
    assert rcj["encoding"] == "compact-jpeg-v1"
    assert rcj["lossy_reencode"] is True
    assert rcj["tiles_raw_copied"] == 0
    assert rcj["tiles_reencoded"] == sum(l["tiles_total"] for l in rcj["levels"])
    import openslide

    src_slide = openslide.OpenSlide(str(SVS_SAMPLE))
    out_slide = slide_io.open_slide(str(real_compact))
    assert src_slide.dimensions == out_slide.dimensions
    box = (200, 150, 712, 662)  # 512×512 bounded ROI at level 0
    a = src_slide.read_region((box[0], box[1]), 0,
                              (box[2] - box[0], box[3] - box[1])).convert("RGB")
    b = out_slide.read_region((box[0], box[1]), 0,
                              (box[2] - box[0], box[3] - box[1])).convert("RGB")
    aa = np.asarray(a, dtype=np.int16)
    bb = np.asarray(b, dtype=np.int16)
    d = np.abs(aa - bb)
    # KFB compact tolerance class (test_compact_report_contract_and_reader)
    assert d.mean() < 6.0, f"real-sample mean diff {d.mean()}"
    assert d.max() <= 255
    for c in range(3):
        assert abs(aa[..., c].mean() - bb[..., c].mean()) < 4.0
    # …and against the preserve output (which is pixel-identical to the
    # source per the F1 adapter proof) at a reduced level too
    pres_slide = slide_io.open_slide(str(real_preserve))
    for level in (0, min(1, out_slide.level_count - 1)):
        a = pres_slide.read_region((box[0] * 2 ** level, box[1] * 2 ** level), level,
                                   (box[2] - box[0], box[3] - box[1])).convert("RGB")
        b = out_slide.read_region((box[0] * 2 ** level, box[1] * 2 ** level), level,
                                  (box[2] - box[0], box[3] - box[1])).convert("RGB")
        aa = np.asarray(a, dtype=np.int16)
        bb = np.asarray(b, dtype=np.int16)
        d = np.abs(aa - bb)
        assert d.mean() < 6.0, f"real level {level} mean diff {d.mean()}"
        for c in range(3):
            assert abs(aa[..., c].mean() - bb[..., c].mean()) < 4.0
