#!/usr/bin/env python3
"""
Phase 4 A/B framework — SYNTHETIC de-identified pyramidal-TIFF fixture generator.

Generates 3 small synthetic whole-slide-like TIFFs (tissue-like colored blobs +
noise, NO real patient data) plus, when run with ``--pin`` and a reachable Flask,
a versioned ``manifest.json`` recording per-fixture: fixture_id, file, byte size,
sha256, slide fingerprint (from Flask ``/internal/ai/slide_info``), ground-truth
region bboxes (where the generator drew the synthetic clusters) and task tags.

The output TIFFs are openslide-readable pyramids built as a page series with
``NewSubfileType=1`` reduced-resolution pages (verified openslide 1.4.6 reads
3 levels with downsamples 1.0/2.0/4.0). Filenames pass app.py ``_safe_name``
(ASCII, no leading underscore, no path separators).

==============================================================================
EXACT COMMAND (the working interpreter is the repo-root .venv):

    .venv/bin/python sidecar/experiments/fixtures/generate.py --out-dir sidecar/experiments/fixtures/slides

Pinning a manifest (requires a running Flask that can see the slides — copy the
generated files into the Flask UPLOAD_DIR first, then):

    .venv/bin/python sidecar/experiments/fixtures/generate.py --pin \\
        --flask-url http://127.0.0.1:5000 \\
        --out-dir sidecar/experiments/fixtures/slides \\
        --manifest sidecar/experiments/fixtures/manifest.json

``--pin`` without ``--flask-url`` fails loudly. If Flask is unreachable, it fails
loudly too. Until pinning succeeds the repo ships only ``manifest.example.json``
(placeholder fingerprints) + ``manifest.schema.json``.

Wave 1 does NOT commit generated slides (``experiments/fixtures/slides/`` is
gitignored). Wave 2's smoke run regenerates + pins.
==============================================================================

Exit codes: 0 on success, 1 on usage/generation/pin error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Defer heavy imports until main() so ``--help`` works without the venv.
# --------------------------------------------------------------------------- #
# Fixture specs — single source of truth for coordinates + region labels.
# tasksets/reading-v1.json bbox_revisit assertions reference these labels.
# Coordinates are level-0 pixels in a 4000x3000 frame.
# --------------------------------------------------------------------------- #
FIXTURE_SPECS: Dict[str, Dict] = {
    "synth-dense": {
        "width": 4000,
        "height": 3000,
        "levels": 3,
        "regions": [
            {"label": "high_density_cluster_A", "x": 800, "y": 600, "w": 600, "h": 600, "density": "high"},
            {"label": "high_density_cluster_B", "x": 2400, "y": 1800, "w": 500, "h": 500, "density": "high"},
        ],
        "tags": ["dense", "multi-cluster", "localization", "comparison"],
    },
    "synth-heterogeneous": {
        "width": 4000,
        "height": 3000,
        "levels": 3,
        "regions": [
            {"label": "focal_atypical_A", "x": 1200, "y": 1000, "w": 400, "h": 400, "density": "medium"},
            {"label": "focal_atypical_B", "x": 2800, "y": 500, "w": 400, "h": 400, "density": "medium"},
        ],
        "tags": ["heterogeneous", "focal", "progressive-zoom", "long-session"],
    },
    "synth-sparse": {
        "width": 4000,
        "height": 3000,
        "levels": 3,
        "regions": [
            {"label": "background_only", "x": 0, "y": 0, "w": 4000, "h": 3000, "density": "low"},
        ],
        "tags": ["sparse", "background", "full-scan", "no-annotation"],
    },
}

# Filenames must pass app.py _safe_name (ASCII, secure_filename keeps these).
# .tiff is openslide-readable; the team's smoke tests use pyramidal TIFF.
FIXTURE_EXT = ".tiff"


def _safe_filename(name: str) -> str:
    """Mirror app.py _safe_name acceptance for ASCII names (no path chars / leading _)."""
    if not name or any(ord(c) < 32 for c in name):
        raise ValueError(f"unsafe fixture name: {name!r}")
    if any(ch in name for ch in "/\\:"):
        raise ValueError(f"unsafe fixture name (path char): {name!r}")
    if name.startswith(".") or name.startswith("_"):
        raise ValueError(f"unsafe fixture name (leading dot/underscore): {name!r}")
    return name


def _draw_fixture(spec: Dict) -> "object":  # returns np.ndarray
    """Build the level-0 RGB image for one fixture spec (synthetic tissue-like)."""
    import numpy as np  # local import (venv-only)

    w = int(spec["width"])
    h = int(spec["height"])
    # Stable per-fixture seed so re-generation is byte-similar (TIFF bytes still
    # depend on encoder, but the pixel content is deterministic).
    seed = int(hashlib.sha256(spec["regions"][0]["label"].encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    # Tissue-like background: low-saturation pink/purple noise.
    base = rng.integers(60, 150, size=(h, w, 3), dtype=np.uint8)
    base[:, :, 0] += np.array(rng.integers(20, 40, size=w), dtype=np.uint16).astype(np.uint8)  # reddish
    yy, xx = np.ogrid[:h, :w]
    for region in spec["regions"]:
        if region["density"] == "low":
            continue  # sparse: no focal blob
        cx = region["x"] + region["w"] // 2
        cy = region["y"] + region["h"] // 2
        rx = region["w"] / 2
        ry = region["h"] / 2
        # Elliptical mask for the cluster.
        mask = ((xx - cx) ** 2 / max(rx, 1) ** 2 + (yy - cy) ** 2 / max(ry, 1) ** 2) <= 1
        color = rng.integers(120, 230, size=3, dtype=np.uint8)
        # Dense texture inside the cluster: stronger noise on the mask.
        noise = rng.integers(0, 40, size=(h, w, 3), dtype=np.uint8)
        cluster = np.clip(color.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        base = np.where(mask[:, :, None], cluster, base)
    return base


def _build_pyramid(base, n_levels: int) -> List:
    """Return [level0, level1, ...] by successive 2x downsamples (nearest)."""
    import numpy as np  # local import

    levels = [base]
    cur = base
    for _ in range(n_levels - 1):
        cur = cur[::2, ::2]
        levels.append(cur)
    return levels


def _write_pyramidal_tiff(path: Path, levels) -> None:
    """Write an openslide-readable page-series pyramid (NewSubfileType markers, tiled, LZW)."""
    import tifffile  # local import (venv-only)

    opts = dict(tile=(256, 256), compression="lzw", photometric="rgb")
    with tifffile.TiffWriter(path) as t:
        # Main full-resolution page: NewSubfileType 0.
        t.write(levels[0], subfiletype=0, **opts)
        for lv in levels[1:]:
            # Reduced-resolution pages: NewSubfileType 1.
            t.write(lv, subfiletype=1, **opts)


def generate_slides(out_dir: Path) -> Dict[str, Path]:
    """Generate all synthetic fixtures into ``out_dir``. Returns {fixture_id: path}."""
    import numpy as np  # noqa: F401  (ensures import works; errors surface clearly)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}
    for fixture_id, spec in FIXTURE_SPECS.items():
        fname = _safe_filename(fixture_id + FIXTURE_EXT)
        path = out_dir / fname
        base = _draw_fixture(spec)
        levels = _build_pyramid(base, int(spec["levels"]))
        _write_pyramidal_tiff(path, levels)
        written[fixture_id] = path
        print(f"[generate] wrote {path} ({path.stat().st_size} bytes, {len(levels)} levels)")
    return written


# --------------------------------------------------------------------------- #
# Pinning (manifest.json) — requires a running Flask
# --------------------------------------------------------------------------- #
def _fetch_slide_info(flask_url: str, slide_name: str, timeout: float = 5.0) -> Dict:
    """Query Flask /internal/ai/slide_info?slide=<name>. Fails loudly on any error.

    Wave 2: sends the ``X-AI-Internal-Token`` header from env ``AI_INTERNAL_TOKEN``
    when present, so the runner's pinned manifest step authenticates against a
    token-secured Flask (app.py ``_require_internal``). Without the header the
    internal endpoint returns 401.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{flask_url.rstrip('/')}/internal/ai/slide_info?{urllib.parse.urlencode({'slide': slide_name})}"
    headers = {}
    token = os.environ.get("AI_INTERNAL_TOKEN", "")
    if token:
        headers["X-AI-Internal-Token"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Flask slide_info for {slide_name!r} returned HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"Flask slide_info for {slide_name!r} returned HTTP {e.code}: {body}. "
            f"If 401, set AI_INTERNAL_TOKEN to the same value the Flask was spawned with."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"无法连接 Flask ({url}) 以固定 slide fingerprint：{e}. "
            f"manifest pinning 需要一个可访问且已部署生成切片的 Flask 实例。"
        ) from e


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pin_manifest(out_dir: Path, manifest_path: Path, flask_url: str) -> Dict:
    """Generate the versioned manifest by querying Flask for each fixture's fingerprint."""
    written = generate_slides(out_dir)
    fixtures = []
    for fixture_id, path in written.items():
        spec = FIXTURE_SPECS[fixture_id]
        slide_name = path.name
        info = _fetch_slide_info(flask_url, slide_name)
        fp = info.get("fingerprint") or ""
        if not fp:
            raise RuntimeError(f"Flask returned empty fingerprint for {slide_name!r}")
        fixtures.append({
            "fixture_id": fixture_id,
            "file": slide_name,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_of_file(path),
            "fingerprint": fp,
            "width": int(info.get("width") or spec["width"]),
            "height": int(info.get("height") or spec["height"]),
            "level_downsamples": list(info.get("level_downsamples") or [1.0]),
            "mpp": info.get("mpp"),
            "regions": [
                {"label": r["label"], "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"], "density": r["density"]}
                for r in spec["regions"]
            ],
            "tags": list(spec["tags"]),
        })
    manifest = {
        "manifest_version": 1,
        "generated_at": _utc_now_iso(),
        "fixtures": fixtures,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"[pin] wrote {manifest_path} ({len(fixtures)} fixtures)")
    return manifest


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Optional[List[str]] = None) -> int:
    here = Path(__file__).resolve()
    default_out = here.parent / "slides"
    default_manifest = here.parent / "manifest.json"

    p = argparse.ArgumentParser(description="Generate synthetic Phase 4 fixture slides (+ optional manifest pin).")
    p.add_argument("--out-dir", type=Path, default=default_out, help=f"output dir for slides (default: {default_out})")
    p.add_argument("--pin", action="store_true", help="also write a versioned manifest.json (requires --flask-url)")
    p.add_argument("--flask-url", type=str, default=None, help="Flask base URL (required for --pin)")
    p.add_argument("--manifest", type=Path, default=default_manifest, help=f"manifest.json output path (default: {default_manifest})")
    args = p.parse_args(argv)

    if args.pin and not args.flask_url:
        p.error("--pin requires --flask-url (slide fingerprint must come from a running Flask /internal/ai/slide_info)")

    try:
        if args.pin:
            pin_manifest(args.out_dir, args.manifest, args.flask_url)
        else:
            generate_slides(args.out_dir)
    except Exception as e:  # noqa: BLE001 — surface a clear message, non-zero exit
        print(f"[generate] ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
