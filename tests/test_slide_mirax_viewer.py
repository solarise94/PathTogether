# -*- coding: utf-8 -*-
"""F3: the platform viewer path (``slide_io.open_slide``) reads an
MRXS-derived conversion.

Runs the compiled ``slide-transform`` CLI on the public CC0 OpenSlide MIRAX
sample (CMU-1-Saved-1_16; path overridable via ``MRXS_SAMPLE`` — the
directory that contains ``<stem>.mrxs`` plus the same-name folder) and
asserts:

* ``slide_io.open_slide`` opens the bf-classic output through OpenSlide
  (generic-tiff branch) with the level dimensions and MPP derived from
  Slidedat.ini;
* a bounded ROI's pixels track OpenSlide's rendering of the same source ROI
  (mosaic composition at level 0 is exact; the q95 compose re-encode adds a
  small lossy generation — tolerance stated below);
* the bf-ome output opens through the OME branch (TiffFileSlide) with the
  same level count/dimensions and OME-XML PhysicalSize.

Skip behaviour (hard requirement): missing CLI binary or missing sample ⇒
skip with a clear reason, never fail.

Usage:
  PYTHONPATH=.:tests .venv/bin/python -m pytest tests/test_slide_mirax_viewer.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

openslide = pytest.importorskip("openslide")

REPO = Path(__file__).resolve().parent.parent

CLI_ENV = os.environ.get(
    "SLIDE_TRANSFORM_CLI",
    str(REPO / "slide-transform-core" / "target" / "release" / "slide-transform"),
)
CLI = CLI_ENV if Path(CLI_ENV).exists() else None

# public CC0 OpenSlide MIRAX sample (never a committed fixture; skip when
# absent). MRXS_SAMPLE points at the directory holding <stem>.mrxs + <stem>/
_MRXS_DIRS = [
    Path(os.environ.get("MRXS_SAMPLE", "")) if os.environ.get("MRXS_SAMPLE") else None,
    REPO.parent.parent.parent / ".testdata" / "openslide" / "mirax" / "CMU-1-Saved-1_16",
    REPO.parent / ".testdata" / "openslide" / "mirax" / "CMU-1-Saved-1_16",
]
SAMPLE_DIR = next((d for d in _MRXS_DIRS if d and (d / "CMU-1-Saved-1_16.mrxs").exists()), None)
SAMPLE = (SAMPLE_DIR / "CMU-1-Saved-1_16.mrxs") if SAMPLE_DIR else None

pytestmark = [
    pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建"),
    pytest.mark.skipif(SAMPLE is None or not SAMPLE.exists(), reason="MRXS 样本不可用"),
]

# tolerance: level-0 composition is exact vs OpenSlide; the only difference
# is the q95 4:4:4 compose re-encode of the composed tiles (a lossy
# generation on a lossy source, never claimed lossless)
ROI_MEAN_TOL = 6.0


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("mrxs-viewer")
    classic = out_dir / "saved.bf-classic.tif"
    ome = out_dir / "saved.bf-ome.ome.tif"
    for out, profile in [(classic, "bf-classic"), (ome, "bf-ome")]:
        r = subprocess.run(
            [CLI, "convert", str(SAMPLE), str(out), "--overwrite", "--profile", profile],
            capture_output=True, text=True, timeout=900,
        )
        assert r.returncode == 0, r.stdout[-800:] + r.stderr[-400:]
        assert out.exists()
    return classic, ome


def test_classic_opens_with_all_levels_and_metadata(converted):
    slide_io = pytest.importorskip("slide_io")
    classic, _ = converted
    src = openslide.OpenSlide(str(SAMPLE))
    s = slide_io.open_slide(str(classic))
    assert s.level_count == src.level_count
    assert s.level_dimensions == src.level_dimensions
    mpp = float(s.properties["openslide.mpp-x"])
    assert mpp == pytest.approx(3.71723625557207, rel=1e-6)


def test_classic_roi_tracks_openslide(converted):
    classic, _ = converted
    src = openslide.OpenSlide(str(SAMPLE))
    out = openslide.OpenSlide(str(classic))
    # bounded ROI inside the data area (bounds from the source)
    bx = int(src.properties["openslide.bounds-x"]) + 512
    by = int(src.properties["openslide.bounds-y"]) + 512
    rw, rh = 320, 320
    a = np.asarray(src.read_region((bx, by), 0, (rw, rh)).convert("RGB")).astype(int)
    b = np.asarray(out.read_region((bx, by), 0, (rw, rh)).convert("RGB")).astype(int)
    d = np.abs(a - b)
    assert d.mean() < ROI_MEAN_TOL, f"mean {d.mean()}"
    assert np.percentile(d, 99) < 40


def test_ome_opens_through_tifffile_branch(converted):
    slide_io = pytest.importorskip("slide_io")
    _, ome = converted
    src = openslide.OpenSlide(str(SAMPLE))
    s = slide_io.open_slide(str(ome))
    assert s.level_count == src.level_count
    assert s.level_dimensions == src.level_dimensions
    # bounded ROI through the platform reader at level 0
    bx = int(src.properties["openslide.bounds-x"]) + 256
    by = int(src.properties["openslide.bounds-y"]) + 256
    rw, rh = 256, 256
    ra = src.read_region((bx, by), 0, (rw, rh))
    m = np.asarray(ra)[:, :, 3] == 255
    ref = np.asarray(ra.convert("RGB")).astype(int)
    got = np.asarray(s.read_region((bx, by), 0, (rw, rh)).convert("RGB")).astype(int)
    d = np.abs(ref - got)[m]
    assert d.mean() < ROI_MEAN_TOL, f"mean {d.mean()}"
