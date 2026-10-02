# -*- coding: utf-8 -*-
"""scripts/qupath-probe 回归测试（review P2：旧 run_probe.sh/compare_regions.py
对失败一律返回 0）。

不启动 QuPath：负例用假的 <root>/bin/QuPath（bash -> 本测试生成的 python
helper），正例由 helper 生成一份与 default-probe.groovy 同构的完整证据
（小参考金字塔 TIFF + 逐级裁剪 PNG），因此全部用例在数秒内完成。
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
PROBE_DIR = REPO / "scripts" / "qupath-probe"
RUN_PROBE = PROBE_DIR / "run_probe.sh"
CHECK_PROBE = PROBE_DIR / "check_probe.py"
COMPARE_REGIONS = PROBE_DIR / "compare_regions.py"

# 与 default-probe.groovy 相同的盒子/证据生成逻辑，供测试进程与假 QuPath 共用。
PROBE_UTIL_PY = '''
import json
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image


def boxes_for_level(lw, lh, fx=0.5, fy=0.5):
    """与 default-probe.groovy 一致：center/corner/edge/tissue 全部钳制到级内。"""
    return [
        ("center", max(0, lw // 2 - 64), max(0, lh // 2 - 64), min(128, lw), min(128, lh)),
        ("corner", 0, 0, min(96, lw), min(96, lh)),
        ("edge", max(0, lw - 80), max(0, lh - 80), min(80, lw), min(80, lh)),
        ("tissue", max(0, min(lw - 128, int(fx * lw) - 64)),
                   max(0, min(lh - 128, int(fy * lh) - 64)), min(128, lw), min(128, lh)),
    ]


def write_reference(path, shape=(384, 512), levels=3, flat=False, seed=7):
    """classic 风格金字塔 TIFF（subifds），返回各级数组。"""
    rng = np.random.default_rng(seed)
    h, w = shape
    if flat:  # std ~1.4：compare_regions 里不算 textured
        base = rng.integers(126, 131, (h, w, 3)).astype(np.uint8)
    else:
        base = rng.integers(0, 256, (h, w, 3)).astype(np.uint8)
    imgs = [base]
    for _ in range(levels - 1):
        imgs.append(imgs[-1][::2, ::2])
    with tifffile.TiffWriter(str(path)) as t:
        t.write(imgs[0], photometric="rgb", subifds=levels - 1)
        for a in imgs[1:]:
            t.write(a, photometric="rgb", subfiletype=1)
    return imgs


def write_probe(out_dir, reference, fx=0.5, fy=0.5, mutate=None):
    """生成与 default-probe.groovy 同构的 probe.json + regions/*.png。"""
    out_dir = Path(out_dir)
    (out_dir / "regions").mkdir(parents=True, exist_ok=True)
    with tifffile.TiffFile(str(reference)) as tf:
        arrs = [l.asarray() for l in tf.series[0].levels]
    dims = [(a.shape[1], a.shape[0]) for a in arrs]
    reads = []
    for i, (lw, lh) in enumerate(dims):
        for name, x, y, w, h in boxes_for_level(lw, lh, fx, fy):
            crop = arrs[i][y:y + h, x:x + w, :3]
            png = "l%d-%s.png" % (i, name)
            Image.fromarray(crop).save(out_dir / "regions" / png)
            reads.append({"level": i, "name": name, "level_box": [x, y, w, h],
                          "width": int(crop.shape[1]), "height": int(crop.shape[0]),
                          "png": png})
    probe = {
        "default_server": "qupath.lib.images.servers.bioformats.BioFormatsImageServer",
        "width": dims[0][0], "height": dims[0][1],
        "resolutions": len(dims),
        "levels": [{"width": lw, "height": lh, "downsample": 2 ** i}
                   for i, (lw, lh) in enumerate(dims)],
        "is_rgb": True, "n_channels": 3, "pixel_type": "uint8",
        "region_reads": reads,
        "bioformats": {"series": 1, "resolutions": len(dims), "rgb": True,
                       "reader": "loci.formats.in.OMEPyramidReader"},
        "project_reopen": {"server": "qupath.lib.images.servers.bioformats.BioFormatsImageServer",
                           "resolutions": len(dims), "width": dims[0][0],
                           "height": dims[0][1]},
    }
    if mutate is not None:
        mutate(probe)
    (out_dir / "probe.json").write_text(json.dumps(probe, indent=1))
    return probe
'''

# 假 QuPath：bash 启动器 exec 到这个 python helper，行为由 FAKE_QUPATH_MODE 决定。
FAKE_QUPATH_PY = '''
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probeutil

argv = sys.argv[1:]  # ['script', groovy, '-a', img, ..., '-a', fy]
vals = [argv[i] for i in range(2, len(argv)) if argv[i - 1] == "-a"]
img, probe_json, regions, project, fx, fy = vals[:6]
mode = os.environ.get("FAKE_QUPATH_MODE", "ok")
ref = os.environ.get("FAKE_QUPATH_REF")

if mode == "exit3":
    sys.exit(3)
if mode == "silent":
    sys.exit(0)

out_dir = Path(probe_json).parent


def mutate(probe):
    if mode == "no-project-reopen":
        del probe["project_reopen"]
    elif mode == "resolutions-mismatch":
        probe["resolutions"] += 1


probeutil.write_probe(out_dir, ref, float(fx), float(fy), mutate=mutate)

if mode == "missing-png":
    (out_dir / "regions" / "l0-center.png").unlink()
elif mode == "png-size-mismatch":
    from PIL import Image
    with __import__("tifffile").TiffFile(ref) as tf:
        arr = tf.series[0].levels[0].asarray()
    # 比 box 宽 3px 的 PNG：与记录的 box 尺寸不一致
    Image.fromarray(arr[0:128, 0:131, :3]).save(out_dir / "regions" / "l0-center.png")
sys.exit(0)
'''


@pytest.fixture(scope="module")
def probe_util(tmp_path_factory):
    d = tmp_path_factory.mktemp("probe-util")
    (d / "probeutil.py").write_text(PROBE_UTIL_PY)
    (d / "fake-qupath.py").write_text(FAKE_QUPATH_PY)
    spec = importlib.util.spec_from_file_location("probeutil", d / "probeutil.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.dir = d
    return mod


@pytest.fixture(scope="module")
def classic_ref(probe_util, tmp_path_factory):
    p = tmp_path_factory.mktemp("ref") / "classic.tif"
    probe_util.write_reference(p)
    return p


@pytest.fixture(scope="module")
def flat_ref(probe_util, tmp_path_factory):
    p = tmp_path_factory.mktemp("flatref") / "flat.tif"
    probe_util.write_reference(p, flat=True)
    return p


def make_fake_qupath_root(tmp_path, util_dir):
    root = tmp_path / "QuPath-root"
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "QuPath"
    launcher.write_text(
        '#!/usr/bin/env bash\n'
        'exec "${FAKE_QUPATH_PY}" "${FAKE_QUPATH_HELPER}" "$@"\n')
    launcher.chmod(0o755)
    return root


def fake_qupath_env(util_dir, mode, ref):
    env = dict(os.environ)
    env.update({
        "PYTHON": sys.executable,
        "FAKE_QUPATH_PY": sys.executable,
        "FAKE_QUPATH_HELPER": str(Path(util_dir) / "fake-qupath.py"),
        "FAKE_QUPATH_MODE": mode,
        "FAKE_QUPATH_REF": str(ref),
    })
    return env


@pytest.fixture()
def fake_qupath(tmp_path, probe_util, classic_ref):
    root = make_fake_qupath_root(tmp_path, probe_util.dir)
    img = tmp_path / "slide.ome.tif"
    img.write_bytes(b"not really an image, run_probe only checks existence")

    def _run(mode, *extra, out=None):
        out = out or (tmp_path / f"out-{mode}")
        env = fake_qupath_env(probe_util.dir, mode, classic_ref)
        cmd = ["bash", str(RUN_PROBE), str(root), str(out), str(img),
               "0.5", "0.5", *extra]
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=120), out

    return _run


# ---------------------------------------------------------------- run_probe.sh

def test_run_probe_nonexistent_qupath_root_is_nonzero(tmp_path):
    img = tmp_path / "img.tif"
    img.write_bytes(b"x")
    r = subprocess.run(
        ["bash", str(RUN_PROBE), str(tmp_path / "no-such-qupath"),
         str(tmp_path / "out"), str(img)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "not found" in r.stderr or "launcher" in r.stderr


def test_run_probe_propagates_qupath_exit_code(fake_qupath):
    r, _ = fake_qupath("exit3")
    assert r.returncode == 3


def test_run_probe_fails_when_qupath_writes_nothing(fake_qupath):
    r, out = fake_qupath("silent")
    assert r.returncode != 0
    assert "probe.json not found" in r.stderr or "evidence" in r.stderr.lower()


@pytest.mark.parametrize("mode,expected_in_stderr", [
    ("no-project-reopen", "project_reopen"),
    ("resolutions-mismatch", "resolutions"),
    ("missing-png", "l0-center.png"),
    ("png-size-mismatch", "131"),
])
def test_run_probe_fails_on_incomplete_evidence(fake_qupath, mode,
                                                expected_in_stderr):
    r, _ = fake_qupath(mode)
    assert r.returncode != 0, (mode, r.stdout, r.stderr)
    assert expected_in_stderr in r.stderr, (mode, r.stderr)


def test_run_probe_accepts_complete_evidence(fake_qupath):
    r, out = fake_qupath("ok")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads((out / "probe.json").read_text())["resolutions"] == 3


def test_run_probe_forwards_checker_flags(fake_qupath):
    r, _ = fake_qupath("ok-forward", "--expect-server-substring", "BioFormats")
    assert r.returncode == 0
    r_bad, _ = fake_qupath("ok-forward2", "--expect-resolutions", "9")
    assert r_bad.returncode == 2
    assert "expected 9" in r_bad.stderr


# ---------------------------------------------------------------- check_probe.py

def run_check(probe_json, *extra):
    return subprocess.run([sys.executable, str(CHECK_PROBE), str(probe_json), *extra],
                          capture_output=True, text=True, timeout=60)


@pytest.fixture()
def good_evidence(tmp_path, probe_util, classic_ref):
    out = tmp_path / "good"
    probe_util.write_probe(out, classic_ref)
    return out


def test_check_probe_accepts_good_evidence(good_evidence):
    r = run_check(good_evidence / "probe.json")
    assert r.returncode == 0, r.stderr
    assert "resolutions=3" in r.stdout


@pytest.mark.parametrize("flag,value", [
    ("--expect-resolutions", "3"),
    ("--expect-server-substring", "BioFormats"),
])
def test_check_probe_flags_pass(good_evidence, flag, value):
    assert run_check(good_evidence / "probe.json", flag, value).returncode == 0


@pytest.mark.parametrize("flag,value,needle", [
    ("--expect-resolutions", "4", "expected 4"),
    ("--expect-server-substring", "OpenSlide", "OpenSlide"),
])
def test_check_probe_flags_fail(good_evidence, flag, value, needle):
    r = run_check(good_evidence / "probe.json", flag, value)
    assert r.returncode != 0
    assert needle in r.stderr


def test_check_probe_expect_rgb(good_evidence):
    assert run_check(good_evidence / "probe.json", "--expect-rgb").returncode == 0


def test_check_probe_rejects_unparseable_json(tmp_path):
    p = tmp_path / "probe.json"
    p.write_text("{not json")
    r = run_check(p)
    assert r.returncode == 2
    assert "unparseable" in r.stderr


def test_check_probe_rejects_missing_regions(good_evidence):
    (good_evidence / "regions" / "l1-edge.png").unlink()
    r = run_check(good_evidence / "probe.json")
    assert r.returncode != 0
    assert "l1-edge.png" in r.stderr


def test_check_probe_rejects_box_outside_level(good_evidence):
    p = good_evidence / "probe.json"
    probe = json.loads(p.read_text())
    probe["region_reads"][0]["level_box"] = [400, 300, 128, 128]  # 512x384 内越界
    p.write_text(json.dumps(probe))
    r = run_check(p)
    assert r.returncode != 0
    assert "exceeds level bounds" in r.stderr


def test_check_probe_rejects_png_size_mismatch(good_evidence):
    p = good_evidence / "probe.json"
    probe = json.loads(p.read_text())
    probe["region_reads"][0]["width"] = 131
    p.write_text(json.dumps(probe))
    r = run_check(p)
    assert r.returncode != 0
    assert "131" in r.stderr


# ------------------------------------------------------------ compare_regions.py

def run_compare(probe_json, regions, reference, out_json, *extra):
    return subprocess.run(
        [sys.executable, str(COMPARE_REGIONS), str(probe_json), str(regions),
         str(reference), str(out_json), *extra],
        capture_output=True, text=True, timeout=120)


@pytest.fixture()
def cmp_case(tmp_path, probe_util, classic_ref):
    out = tmp_path / "cmp"
    probe_util.write_probe(out, classic_ref)
    report = tmp_path / "report.json"
    return out, classic_ref, report


def test_compare_regions_exact_match_ok(cmp_case):
    out, ref, report = cmp_case
    r = run_compare(out / "probe.json", out / "regions", ref, report,
                    "--expect-regions", "12")
    assert r.returncode == 0, r.stderr
    data = json.loads(report.read_text())
    assert data["ok"] is True
    assert data["summary"]["regions"] == 12
    assert data["summary"]["max_abs_diff"] == 0


def test_compare_regions_one_pixel_off_fails(cmp_case):
    out, ref, report = cmp_case
    png = out / "regions" / "l1-tissue.png"
    arr = np.asarray(Image.open(png)).astype(np.int16)
    arr[0, 0, 0] = min(255, int(arr[0, 0, 0]) + 1)
    Image.fromarray(arr.astype(np.uint8)).save(png)
    r = run_compare(out / "probe.json", out / "regions", ref, report)
    assert r.returncode != 0
    assert "max_abs_diff" in r.stderr
    assert json.loads(report.read_text())["ok"] is False


def test_compare_regions_all_pixels_wrong_fails(cmp_case):
    out, ref, report = cmp_case
    png = out / "regions" / "l0-center.png"
    arr = np.asarray(Image.open(png))
    Image.fromarray(255 - arr).save(png)
    r = run_compare(out / "probe.json", out / "regions", ref, report)
    assert r.returncode != 0
    assert "max_abs_diff" in r.stderr


def test_compare_regions_wrong_png_shape_fails(cmp_case):
    out, ref, report = cmp_case
    png = out / "regions" / "l2-corner.png"
    arr = np.asarray(Image.open(png))[:, :-3]  # 96x96 -> 96x93
    Image.fromarray(arr).save(png)
    r = run_compare(out / "probe.json", out / "regions", ref, report)
    assert r.returncode != 0
    assert "shape" in r.stderr and "level 2/corner" in r.stderr


def test_compare_regions_box_beyond_reference_bounds_fails(cmp_case):
    out, ref, report = cmp_case
    p = out / "probe.json"
    probe = json.loads(p.read_text())
    probe["region_reads"][4]["level_box"] = [100, 100, 128, 128]  # 256x192 内越界
    p.write_text(json.dumps(probe))
    r = run_compare(p, out / "regions", ref, report)
    assert r.returncode != 0
    assert "exceeds reference level bounds" in r.stderr
    assert "level 1" in r.stderr


def test_compare_regions_missing_png_fails(cmp_case):
    out, ref, report = cmp_case
    (out / "regions" / "l0-edge.png").unlink()
    r = run_compare(out / "probe.json", out / "regions", ref, report)
    assert r.returncode != 0
    assert "l0-edge.png" in r.stderr


def test_compare_regions_level_count_mismatch_fails(cmp_case, probe_util,
                                                    tmp_path):
    out, ref, report = cmp_case
    two_level = tmp_path / "two-level.tif"
    probe_util.write_reference(two_level, levels=2)
    r = run_compare(out / "probe.json", out / "regions", two_level, report)
    assert r.returncode != 0
    assert "levels" in r.stderr and "resolutions" in r.stderr


def test_compare_regions_min_textured_unmet_fails(tmp_path, probe_util,
                                                  flat_ref):
    out = tmp_path / "flat-ev"
    probe_util.write_probe(out, flat_ref)
    r = run_compare(out / "probe.json", out / "regions", flat_ref,
                    tmp_path / "r.json", "--min-textured", "1")
    assert r.returncode != 0
    assert "textured" in r.stderr
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["summary"]["textured"] == 0


def test_compare_regions_min_textured_met_ok(cmp_case):
    out, ref, report = cmp_case
    r = run_compare(out / "probe.json", out / "regions", ref, report,
                    "--min-textured", "12")
    assert r.returncode == 0, r.stderr
    assert json.loads(report.read_text())["summary"]["textured"] == 12


def test_compare_regions_expect_regions_unmet_fails(cmp_case):
    out, ref, report = cmp_case
    r = run_compare(out / "probe.json", out / "regions", ref, report,
                    "--expect-regions", "13")
    assert r.returncode != 0
    assert "13" in r.stderr


def test_compare_regions_uncovered_level_fails(cmp_case):
    out, ref, report = cmp_case
    p = out / "probe.json"
    probe = json.loads(p.read_text())
    probe["region_reads"] = [r for r in probe["region_reads"] if r["level"] != 2]
    p.write_text(json.dumps(probe))
    r = run_compare(p, out / "regions", ref, report)
    assert r.returncode != 0
    assert "levels without a compared region: [2]" in r.stderr


def test_compare_regions_tolerates_configured_diff(cmp_case):
    out, ref, report = cmp_case
    png = out / "regions" / "l1-center.png"
    arr = np.asarray(Image.open(png)).astype(np.int16)
    arr[0, 0, 0] = min(255, int(arr[0, 0, 0]) + 1)
    Image.fromarray(arr.astype(np.uint8)).save(png)
    assert run_compare(out / "probe.json", out / "regions", ref, report,
                       "--max-abs-diff", "1").returncode == 0
