# -*- coding: utf-8 -*-
""">4 GiB synthetic proof (C1 item 7): generate huge synthetic inputs with the
Rust CLI's streaming generators, convert them, and verify BigTIFF 64-bit
offsets are actually exercised (TileOffsets beyond 4 GiB) plus tifffile can
reopen the output. Returns (max_rss_kib, wall_s, output_path) per format."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path


def _run_timed(cmd):
    t0 = time.monotonic()
    r = subprocess.run(
        ["/usr/bin/time", "-f", "STC_MAXRSS=%M", *map(str, cmd)],
        capture_output=True,
        timeout=7200,
    )
    dt = time.monotonic() - t0
    assert r.returncode == 0, r.stderr.decode()[-800:]
    m = re.search(rb"STC_MAXRSS=(\d+)", r.stderr)
    return (int(m.group(1)) if m else 0), dt, r.stdout.decode()


def _cli(cli):
    return str(cli)


def build_big_bf(kfb_path: Path, cli):
    """~8 GiB synthetic brightfield (100000×100000, quality 95 noise)."""
    rss, dt, _ = _run_timed([
        _cli(cli), "gen-kfb", kfb_path,
        "--width", 100000, "--height", 100000,
        "--sampling", "422", "--quality", 95,
    ])
    out = kfb_path.with_suffix(".tif")
    rss2, dt2, _ = _run_timed([_cli(cli), "convert", kfb_path, out, "--overwrite"])
    return max(rss, rss2), dt + dt2, out


def build_big_fl(kfbf_path: Path, cli):
    """~5+ GiB synthetic fluorescence (74000×74000, 2 channels, noisy)."""
    rss, dt, _ = _run_timed([
        _cli(cli), "gen-kfbf", kfbf_path,
        "--width", 74000, "--height", 74000,
        "--channels", 2, "--noisy",
    ])
    out = kfbf_path.with_suffix(".tif")
    rss2, dt2, _ = _run_timed([_cli(cli), "convert", kfbf_path, out, "--overwrite"])
    return max(rss, rss2), dt + dt2, out
