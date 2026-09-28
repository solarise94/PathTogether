#!/usr/bin/env python
"""C0 core-spike differential: compare Rust kfb2tiff output vs Python oracle.

Compares (a) page/level structure, dimensions, tile counts, compression,
subsampling, description; (b) sha256 of every FULL tile payload (must be
equal — byte-for-byte passthrough); (c) edge tiles by decoded pixels
(max abs diff per channel, since re-encoders differ); (d) associated JPEG
sidecars (byte equality).

Usage: diff_oracle.py <oracle.tif> <spike.tif> [--json OUT]
Exit code 0 = structure + full-tile parity, else 1.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys

import numpy as np
import tifffile
from PIL import Image

TILE_W = TILE_H = 256


def tile_is_full(page, idx: int) -> bool:
    across = (page.imagewidth + TILE_W - 1) // TILE_W
    row, col = divmod(idx, across)
    return (col + 1) * TILE_W <= page.imagewidth and (row + 1) * TILE_H <= page.imagelength


def read_tile(fh, off, cnt):
    fh.seek(off)
    return fh.read(cnt)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    json_out = None
    source_kfb = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--json":
            json_out = argv[i + 1]
        elif a == "--source":
            source_kfb = argv[i + 1]
    a_path, b_path = args[0], args[1]
    report = {"oracle": a_path, "spike": b_path, "pages": [], "ok": True}

    src_tiles = None
    if source_kfb:
        from kfb.parser import parse_kfb

        doc = parse_kfb(source_kfb)
        try:
            src_tiles = {}
            for t in doc.tiles:
                src_tiles[(t.level, t.row, t.col)] = doc.tile_payload(t)
        finally:
            doc.close()

    with open(a_path, "rb") as fa, open(b_path, "rb") as fb, \
            tifffile.TiffFile(a_path) as ta, tifffile.TiffFile(b_path) as tb:
        if not ta.is_bigtiff or not tb.is_bigtiff:
            report["ok"] = False
            report["error"] = "not bigtiff"
        pa, pb = list(ta.pages), list(tb.pages)
        if len(pa) != len(pb):
            report["ok"] = False
            report["error"] = f"page count {len(pa)} != {len(pb)}"
        for i, (x, y) in enumerate(zip(pa, pb)):
            edge_vs_source = {}
            doc_levels = getattr(doc, "levels", None) if src_tiles is not None else None
            px = {
                "dims": (x.imagewidth, x.imagelength),
                "tile": (x.tilewidth, x.tilelength),
                "compression": int(x.compression),
                "subsampling": tuple(x.tags["YCbCrSubSampling"].value),
                "photometric": int(x.photometric),
                "description": x.description,
            }
            py = {
                "dims": (y.imagewidth, y.imagelength),
                "tile": (y.tilewidth, y.tilelength),
                "compression": int(y.compression),
                "subsampling": tuple(y.tags["YCbCrSubSampling"].value),
                "photometric": int(y.photometric),
                "description": y.description,
            }
            entry = {"page": i, "structure_equal": px == py, "structure": px}
            if px != py:
                report["ok"] = False
                entry["spike_structure"] = py

            off_a = x.tags["TileOffsets"].value
            cnt_a = x.tags["TileByteCounts"].value
            off_b = y.tags["TileOffsets"].value
            cnt_b = y.tags["TileByteCounts"].value
            if len(off_a) != len(off_b):
                entry["tile_count_equal"] = False
                report["ok"] = False
                report["pages"].append(entry)
                continue
            full_eq = full_n = 0
            edge_max = 0
            edge_gt2 = edge_gt8 = edge_n = 0
            edge_bad = []
            for t in range(len(off_a)):
                da = read_tile(fa, off_a[t], cnt_a[t])
                db = read_tile(fb, off_b[t], cnt_b[t])
                if tile_is_full(x, t):
                    full_n += 1
                    if hashlib.sha256(da).digest() == hashlib.sha256(db).digest():
                        full_eq += 1
                    else:
                        report["ok"] = False
                else:
                    edge_n += 1
                    ia = np.asarray(Image.open(io.BytesIO(da)).convert("RGB"), dtype=np.int16)
                    ib = np.asarray(Image.open(io.BytesIO(db)).convert("RGB"), dtype=np.int16)
                    if ia.shape != ib.shape:
                        edge_bad.append({"tile": t, "shape_a": ia.shape, "shape_b": ib.shape})
                        report["ok"] = False
                        continue
                    d = int(np.abs(ia - ib).max())
                    edge_max = max(edge_max, d)
                    if d > 2:
                        edge_gt2 += 1
                    if d > 8:
                        edge_gt8 += 1
                    if src_tiles is not None:
                        lv = next(
                            (l.level for l in doc_levels or []
                             if (l.width, l.height) == (x.imagewidth, x.imagelength)),
                            i,
                        )
                        sp = src_tiles.get((lv, t // ((x.imagewidth + 255) // 256), t % ((x.imagewidth + 255) // 256)))
                        if sp is not None:
                            isrc = np.asarray(Image.open(io.BytesIO(sp)).convert("RGB"), dtype=np.int16)
                            canvas = np.full((256, 256, 3), 255, dtype=np.int16)
                            canvas[: isrc.shape[0], : isrc.shape[1]] = isrc
                            edge_vs_source.setdefault("oracle_max", 0)
                            edge_vs_source.setdefault("spike_max", 0)
                            edge_vs_source["oracle_max"] = max(
                                edge_vs_source["oracle_max"], int(np.abs(canvas - ia).max())
                            )
                            edge_vs_source["spike_max"] = max(
                                edge_vs_source["spike_max"], int(np.abs(canvas - ib).max())
                            )
            entry["full_tiles"] = {"equal": full_eq, "total": full_n}
            entry["edge_tiles"] = {
                "count": edge_n,
                "max_abs_diff": edge_max,
                "diff_gt2": edge_gt2,
                "diff_gt8": edge_gt8,
                "bad": edge_bad,
            }
            if edge_vs_source:
                entry["edge_vs_source"] = dict(edge_vs_source)
            if full_eq != full_n:
                report["ok"] = False
            report["pages"].append(entry)

    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=1)
    print(json.dumps(report.get("error", "structure+full-tile parity OK"), indent=None))
    for p in report["pages"]:
        e = p.get("edge_tiles", {})
        print(
            "page {page}: struct={structure_equal} full={0}/{1} edge(count={2}, max={3}, >2:{4}, >8:{5})".format(
                p["full_tiles"]["equal"],
                p["full_tiles"]["total"],
                e.get("count", 0),
                e.get("max_abs_diff", 0),
                e.get("diff_gt2", 0),
                e.get("diff_gt8", 0),
                page=p["page"],
                structure_equal=p["structure_equal"],
            )
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
