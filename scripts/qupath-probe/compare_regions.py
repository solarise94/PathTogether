#!/usr/bin/env python3
"""Compare QuPath default-reader region PNGs with a tifffile decode of a
reference TIFF (the classic output) at the same level coordinates.

usage: compare_regions.py probe.json regions_dir reference.tif out.json
                          [--max-abs-diff N] [--expect-regions N]
                          [--min-textured K] [--textured-std T]

Exit status:
  0   every region compared pixel-identically within the tolerance
  1   comparison failure: any region's max_abs_diff > --max-abs-diff, a PNG
      missing, a PNG whose shape differs from its level box, a box beyond the
      reference level bounds (no silent cropping), 0 compared regions or
      fewer than --expect-regions, reference level count != probe
      resolutions, or fewer than --min-textured textured regions
  2   invalid inputs (probe.json missing/unparseable, reference unreadable)

A region is "textured" when the reference crop's pixel std exceeds
--textured-std (default 5.0); --min-textured K fails the run when fewer than
K regions are textured (default center/tissue boxes can land on background).
The JSON report (rows + summary + ok boolean) is written even on failure.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import tifffile
import zarr
from PIL import Image


def fail_input(msg):
    print(f"compare_regions: FAIL: {msg}", file=sys.stderr)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Strictly compare probe region PNGs with a reference TIFF.")
    ap.add_argument("probe_json")
    ap.add_argument("regions_dir")
    ap.add_argument("reference_tif")
    ap.add_argument("out_json")
    ap.add_argument("--max-abs-diff", type=int, default=0,
                    help="tolerated per-channel abs diff (default 0)")
    ap.add_argument("--expect-regions", type=int, default=None,
                    help="fail when fewer than N regions were compared")
    ap.add_argument("--min-textured", type=int, default=0,
                    help="fail when fewer than K regions are textured")
    ap.add_argument("--textured-std", type=float, default=5.0,
                    help="reference std above which a region is textured")
    args = ap.parse_args(argv)

    probe_path = Path(args.probe_json)
    try:
        probe = json.loads(probe_path.read_text())
    except FileNotFoundError:
        return fail_input(f"probe.json not found: {probe_path}")
    except (OSError, json.JSONDecodeError) as exc:
        return fail_input(f"probe.json unparseable: {probe_path}: {exc}")
    if not isinstance(probe, dict):
        return fail_input(f"probe.json is not a JSON object: {probe_path}")

    resolutions = probe.get("resolutions")
    reads = probe.get("region_reads")
    if not isinstance(resolutions, int) or resolutions < 1:
        return fail_input(f"probe resolutions invalid: {resolutions!r}")
    if not isinstance(reads, list):
        return fail_input("probe region_reads missing/not a list")

    rdir = Path(args.regions_dir)
    failures = []
    rows = []

    try:
        tf = tifffile.TiffFile(args.reference_tif)
    except OSError as exc:
        return fail_input(f"reference unreadable: {args.reference_tif}: {exc}")
    with tf:
        n_ref_levels = len(tf.series[0].levels)
        if n_ref_levels != resolutions:
            return fail_input(
                f"reference has {n_ref_levels} levels but probe reports "
                f"resolutions={resolutions}")
        grp = zarr.open(tf.series[0].aszarr(), mode="r")
        arrs = [grp[str(i)] for i in range(n_ref_levels)]

        for r in reads:
            level, name = r.get("level"), r.get("name")
            where = f"level {level}/{name}"
            box = r.get("level_box")
            row = {"level": level, "name": name, "level_box": box}
            rows.append(row)
            if not (isinstance(box, list) and len(box) == 4
                    and all(isinstance(v, int) for v in box)):
                failures.append(f"{where}: level_box must be 4 ints, got {box!r}")
                continue
            x, y, w, h = box
            if level is None or not (0 <= level < n_ref_levels):
                failures.append(f"{where}: level out of range (reference has "
                                f"{n_ref_levels} levels)")
                continue
            if level >= len(arrs):
                failures.append(f"{where}: reference level missing")
                continue
            ref_arr = arrs[level]
            rh, rw = int(ref_arr.shape[0]), int(ref_arr.shape[1])
            row["ref_level_shape"] = [rw, rh]
            if x < 0 or y < 0 or w < 1 or h < 1 or x + w > rw or y + h > rh:
                failures.append(
                    f"{where}: level_box {box} exceeds reference level bounds "
                    f"{rw}x{rh} (invalid probe — boxes must be clamped, not cropped)")
                continue
            png = r.get("png")
            png_path = rdir / str(png)
            if not png_path.is_file():
                failures.append(f"{where}: PNG missing: {png_path}")
                continue
            try:
                got = np.asarray(Image.open(png_path).convert("RGB"))
            except OSError as exc:
                failures.append(f"{where}: PNG unreadable: {png_path}: {exc}")
                continue
            row["got"] = [int(got.shape[1]), int(got.shape[0])]
            if got.shape != (h, w, 3):
                failures.append(
                    f"{where}: PNG shape (h={got.shape[0]}, w={got.shape[1]}) "
                    f"differs from expected box shape (h={h}, w={w})")
                continue
            ref = np.asarray(ref_arr[y:y + h, x:x + w])
            if ref.ndim == 2:
                ref = np.stack([ref] * 3, axis=-1)
            elif ref.shape != (h, w, 3):
                failures.append(
                    f"{where}: reference crop shape {ref.shape} unexpected")
                continue
            d = np.abs(got.astype(int) - ref.astype(int))
            row.update({
                "ref": [w, h],
                "max_abs_diff": int(d.max()),
                "mean_abs_diff": round(float(d.mean()), 6),
                "pixels_over": int((d > args.max_abs_diff).any(axis=-1).sum()),
                "ref_mean_rgb": [round(float(v), 2)
                                 for v in ref.reshape(-1, 3).mean(0)],
                "ref_std": round(float(ref.std()), 2),
                "textured": bool(ref.std() > args.textured_std),
            })
            if row["max_abs_diff"] > args.max_abs_diff:
                failures.append(
                    f"{where}: max_abs_diff {row['max_abs_diff']} > "
                    f"{args.max_abs_diff} ({row['pixels_over']} pixels over)")

    compared = [r for r in rows if "max_abs_diff" in r]
    textured = sum(1 for r in compared if r.get("textured"))
    worst = max((r["max_abs_diff"] for r in compared), default=None)
    if not compared:
        failures.append("0 regions were compared")
    uncovered = sorted(set(range(resolutions)) - {r["level"] for r in compared})
    if compared and uncovered:
        failures.append(f"levels without a compared region: {uncovered}")
    if args.expect_regions is not None and len(compared) < args.expect_regions:
        failures.append(f"compared {len(compared)} regions < expected "
                        f"{args.expect_regions}")
    if textured < args.min_textured:
        failures.append(f"textured regions {textured} < required "
                        f"{args.min_textured}")
    ok = not failures
    report = {
        "rows": rows,
        "summary": {
            "regions": len(compared),
            "textured": textured,
            "textured_std_threshold": args.textured_std,
            "max_abs_diff": worst,
            "max_abs_diff_threshold": args.max_abs_diff,
            "failures": failures,
        },
        "ok": ok,
    }
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_json, "w") as fh:
        json.dump(report, fh, indent=1)
    print(f"regions {len(compared)} textured {textured} worst_max_abs_diff "
          f"{worst} ok {ok}")
    for f in failures:
        print(f"compare_regions: FAIL: {f}", file=sys.stderr)
    for r in rows:
        print(r.get("level"), r.get("name"), r.get("got"), "ref",
              r.get("ref", r.get("ref_level_shape")),
              "maxdiff", r.get("max_abs_diff"), "std", r.get("ref_std"),
              "textured", r.get("textured"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
