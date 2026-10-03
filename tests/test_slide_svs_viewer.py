# -*- coding: utf-8 -*-
"""F1: the platform viewer path (``slide_io.open_slide``) reads an
SVS-derived conversion.

Runs the compiled ``slide-transform`` CLI on the public CC0 OpenSlide Aperio
sample (CMU-1-Small-Region; path overridable via ``SVS_SAMPLE``) and asserts:

* ``slide_io.open_slide`` opens the bf-ome output through the OME branch
  (TiffFileSlide): dimensions, level count and mpp from the OME-XML
  PhysicalSize written from the Aperio description MPP;
* a bounded ROI's pixels equal OpenSlide's rendering of the same source ROI
  (payload passthrough ⇒ identical through one decoder);
* the bf-classic output opens through OpenSlide (generic-tiff) with the same
  level dimensions and identical pixels.

Skip behaviour (hard requirement): missing CLI binary or missing sample ⇒
skip with a clear reason, never fail.

Usage:
  PYTHONPATH=.:tests .venv/bin/python -m pytest tests/test_slide_svs_viewer.py -q
"""

from __future__ import annotations

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
CLI = CLI_ENV if Path(CLI_ENV).exists() else None

# public CC0 OpenSlide sample (never a committed fixture; skip when absent)
SAMPLE = Path(
    os.environ.get(
        "SVS_SAMPLE",
        str(REPO.parent / ".testdata" / "openslide" / "CMU-1-Small-Region.svs"),
    )
)

pytestmark = [
    pytest.mark.skipif(CLI is None, reason="slide-transform CLI 未构建"),
    pytest.mark.skipif(
        not SAMPLE.exists(), reason=f"SVS 样本不可用（{SAMPLE} 不存在）"
    ),
]


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("svs-viewer")
    ome = out_dir / "small.bf-ome.ome.tif"
    classic = out_dir / "small.bf-classic.tif"
    for profile, out in (("bf-ome", ome), ("bf-classic", classic)):
        r = subprocess.run(
            [str(CLI), "convert", str(SAMPLE), str(out), "--overwrite", "--profile", profile],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert r.returncode == 0, f"CLI convert 失败: {r.stdout} {r.stderr}"
    return {"ome": ome, "classic": classic}


def test_open_slide_reads_svs_derived_bf_ome(converted):
    import openslide

    import slide_io

    slide = slide_io.open_slide(converted["ome"])
    assert slide.level_count == 1
    assert slide.level_dimensions[0] == (2220, 2967)
    # mpp survives into the viewer: OME-XML PhysicalSize from the Aperio
    # description (0.499 µm/px)
    mpp = getattr(slide, "mpp", None)
    if mpp is None and hasattr(slide, "properties"):
        mpp = slide.properties.get("openslide.mpp-x")
    assert mpp is not None, "SVS 衍生的 bf-ome 应携带物理标尺"
    assert abs(float(mpp) - 0.499) < 1e-3

    # bounded ROI: viewer pixels == openslide pixels of the source (same
    # decoder, byte-identical payloads)
    src = openslide.OpenSlide(str(SAMPLE))
    roi = (512, 512)
    at = (600, 700)
    got = np.asarray(slide.read_region(at, 0, roi).convert("RGB"))
    want = np.asarray(src.read_region(at, 0, roi).convert("RGB"))
    assert got.shape == want.shape
    assert int(np.abs(got.astype(int) - want.astype(int)).max()) == 0


def test_open_slide_reads_svs_derived_bf_classic(converted):
    import openslide

    import slide_io

    slide = slide_io.open_slide(converted["classic"])
    assert slide.level_count == 1
    assert slide.level_dimensions[0] == (2220, 2967)

    src = openslide.OpenSlide(str(SAMPLE))
    got = np.asarray(slide.read_region((600, 700), 0, (512, 512)).convert("RGB"))
    want = np.asarray(src.read_region((600, 700), 0, (512, 512)).convert("RGB"))
    assert int(np.abs(got.astype(int) - want.astype(int)).max()) == 0
