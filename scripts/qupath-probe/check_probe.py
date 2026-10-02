#!/usr/bin/env python3
"""Validate the evidence written by a QuPath default-reader probe run.

usage: check_probe.py <probe.json> [--regions-dir DIR] [--expect-resolutions N]
                      [--expect-server-substring SUB] [--expect-rgb]

Exit status:
  0   probe.json parses and every structural check below passes
  1   one or more checks failed (each failure is printed on stderr)
  2   probe.json is missing or unparseable / bad usage

Checks (stdlib + json only; PNG sizes are read straight from the IHDR chunk):
  * probe.json exists and is valid JSON
  * `default_server` present (non-empty string)
  * `resolutions` >= 1 and equals len(`levels`)
  * every level 0..resolutions-1 has the 4 region reads
    (center/corner/edge/tissue), each exactly once
  * each region PNG exists in the regions dir, and its IHDR width/height
    equals both the recorded width/height and the level box w/h
  * each level box lies within the recorded level bounds
  * `bioformats` present with `series` and `resolutions` == probe resolutions
  * `project_reopen` present with `resolutions` == probe resolutions
  * optional: --expect-resolutions N, --expect-server-substring SUB,
    --expect-rgb (is_rgb true)
"""
import argparse
import json
import struct
import sys
from pathlib import Path

EXPECTED_REGION_NAMES = ("center", "corner", "edge", "tissue")


def png_size(path):
    """Return (width, height) from a PNG IHDR chunk, or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def load_probe(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"probe.json not found: {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"probe.json unreadable/unparseable: {path}: {exc}"


def check(probe, regions_dir, expect_resolutions, expect_server_substring,
          expect_rgb):
    errors = []

    def err(msg):
        errors.append(msg)

    server = probe.get("default_server")
    if not isinstance(server, str) or not server:
        err("default_server missing/empty")

    resolutions = probe.get("resolutions")
    levels = probe.get("levels")
    if not isinstance(resolutions, int) or resolutions < 1:
        err(f"resolutions must be an int >= 1, got {resolutions!r}")
        resolutions = None
    if not isinstance(levels, list):
        err(f"levels must be a list, got {type(levels).__name__}")
        levels = None
    if resolutions is not None and levels is not None and resolutions != len(levels):
        err(f"resolutions ({resolutions}) != len(levels) ({len(levels)})")
    if expect_resolutions is not None and resolutions is not None \
            and resolutions != expect_resolutions:
        err(f"resolutions {resolutions} != expected {expect_resolutions}")
    if expect_server_substring and isinstance(server, str) \
            and expect_server_substring not in server:
        err(f"default_server {server!r} does not contain "
            f"{expect_server_substring!r}")
    if expect_rgb and probe.get("is_rgb") is not True:
        err(f"--expect-rgb but is_rgb is {probe.get('is_rgb')!r}")

    # region reads: 4 per level, PNGs present with the exact box geometry
    n_levels = resolutions if resolutions is not None else (
        len(levels) if isinstance(levels, list) else 0)
    reads = probe.get("region_reads")
    if not isinstance(reads, list):
        err(f"region_reads must be a list, got {type(reads).__name__}")
        reads = []
    per_level = {}
    for r in reads:
        per_level.setdefault(r.get("level"), []).append(r)
    for i in range(n_levels):
        got = per_level.get(i, [])
        names = [r.get("name") for r in got]
        for name in EXPECTED_REGION_NAMES:
            if names.count(name) != 1:
                err(f"level {i}: expected exactly one {name!r} region read, "
                    f"got {names}")
        if i not in per_level:
            continue
        lw = lh = None
        if isinstance(levels, list) and i < len(levels) and isinstance(levels[i], dict):
            lw = levels[i].get("width")
            lh = levels[i].get("height")
        for r in got:
            name, where = r.get("name"), f"level {i}/{r.get('name')!r}"
            box = r.get("level_box")
            if not (isinstance(box, list) and len(box) == 4
                    and all(isinstance(v, int) for v in box)):
                err(f"{where}: level_box must be 4 ints, got {box!r}")
                continue
            x, y, w, h = box
            if min(x, y, w, h) < 0:
                err(f"{where}: negative level_box {box}")
            if lw is not None and (x + w > lw or y + h > lh):
                err(f"{where}: level_box {box} exceeds level bounds "
                    f"{lw}x{lh}")
            rw, rh = r.get("width"), r.get("height")
            if (rw, rh) != (w, h):
                err(f"{where}: recorded PNG size {rw}x{rh} != box w/h "
                    f"{w}x{h}")
            png = r.get("png")
            if not isinstance(png, str) or not png:
                err(f"{where}: missing png name")
                continue
            png_path = regions_dir / png
            size = png_size(png_path)
            if size is None:
                err(f"{where}: PNG missing or not a PNG: {png_path}")
            elif size != (w, h):
                err(f"{where}: PNG {png} is {size[0]}x{size[1]}, expected "
                    f"{w}x{h}")

    # Bio-Formats grouping and project reopen must agree with the server
    bf = probe.get("bioformats")
    if not isinstance(bf, dict):
        err(f"bioformats must be an object, got {type(bf).__name__}")
    else:
        if "series" not in bf:
            err("bioformats.series missing")
        bf_res = bf.get("resolutions")
        if not isinstance(bf_res, int):
            err(f"bioformats.resolutions must be an int, got {bf_res!r}")
        elif resolutions is not None and bf_res != resolutions:
            err(f"bioformats.resolutions ({bf_res}) != resolutions "
                f"({resolutions})")
    pr = probe.get("project_reopen")
    if not isinstance(pr, dict):
        err(f"project_reopen must be an object, got {type(pr).__name__}")
    else:
        pr_res = pr.get("resolutions")
        if not isinstance(pr_res, int):
            err(f"project_reopen.resolutions must be an int, got {pr_res!r}")
        elif resolutions is not None and pr_res != resolutions:
            err(f"project_reopen.resolutions ({pr_res}) != resolutions "
                f"({resolutions})")

    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Validate QuPath default-reader probe evidence.")
    ap.add_argument("probe_json", help="path to probe.json")
    ap.add_argument("--regions-dir", default=None,
                    help="regions dir (default: <probe.json dir>/regions)")
    ap.add_argument("--expect-resolutions", type=int, default=None,
                    help="fail unless resolutions equals N")
    ap.add_argument("--expect-server-substring", default=None,
                    help="fail unless default_server contains SUB")
    ap.add_argument("--expect-rgb", action="store_true",
                    help="fail unless is_rgb is true")
    args = ap.parse_args(argv)

    probe, load_err = load_probe(args.probe_json)
    if load_err:
        print(f"check_probe: FAIL: {load_err}", file=sys.stderr)
        return 2
    if not isinstance(probe, dict):
        print(f"check_probe: FAIL: probe.json is not a JSON object",
              file=sys.stderr)
        return 2
    regions_dir = (Path(args.regions_dir) if args.regions_dir
                   else Path(args.probe_json).resolve().parent / "regions")
    errors = check(probe, regions_dir, args.expect_resolutions,
                   args.expect_server_substring, args.expect_rgb)
    if errors:
        for e in errors:
            print(f"check_probe: FAIL: {e}", file=sys.stderr)
        print(f"check_probe: {len(errors)} error(s) in "
              f"{args.probe_json}", file=sys.stderr)
        return 1
    print(f"check_probe: OK: {args.probe_json}: resolutions="
          f"{probe.get('resolutions')} levels={len(probe.get('levels') or [])} "
          f"region_reads={len(probe.get('region_reads') or [])} "
          f"server={probe.get('default_server')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
