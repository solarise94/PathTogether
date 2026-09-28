#!/usr/bin/env python
"""C0 ② independent-reader validation of spike outputs.

1. tifffile: open, per-level structure, decode sample tiles (via raw bytes +
   PIL — this tifffile build has no per-tile asarray kwarg) and decode a
   whole small level through tifffile's own JPEG pipeline.
2. Optionally emit a command list for bftools showinf (run separately).

Usage: validate_readers.py <a.tif> [<b.tif> ...] [--json OUT]
"""
from __future__ import annotations

import hashlib
import io
import json
import sys

import tifffile
from PIL import Image


def validate(path: str) -> dict:
    out = {"file": path, "levels": [], "ok": True}
    with open(path, "rb") as fh, tifffile.TiffFile(path) as t:
        out["is_bigtiff"] = t.is_bigtiff
        out["pages"] = len(t.pages)
        for i, page in enumerate(t.pages):
            offs = page.tags["TileOffsets"].value
            cnts = page.tags["TileByteCounts"].value
            lv = {
                "page": i,
                "dims": [page.imagewidth, page.imagelength],
                "tiles": len(offs),
                "samples": [],
            }
            # 解码首/中/末三个 tile（覆盖含边缘 tile 的层）
            picks = sorted({0, len(offs) // 2, len(offs) - 1})
            for idx in picks:
                fh.seek(offs[idx])
                raw = fh.read(cnts[idx])
                im = Image.open(io.BytesIO(raw))
                im.load()
                ok = im.size == (256, 256) and im.mode in ("RGB", "L")
                if not ok:
                    out["ok"] = False
                lv["samples"].append(
                    {"tile": idx, "size": list(im.size), "mode": im.mode,
                     "sha256": hashlib.sha256(raw).hexdigest()[:16]}
                )
            out["levels"].append(lv)
        # 通过 tifffile 自身解码管线读最小层（末页）整幅
        last = t.pages[-1]
        arr = last.asarray()
        lv = out["levels"][-1]
        lv["asarray_shape"] = list(arr.shape)
        if arr.shape[:2] != (last.imagelength, last.imagewidth):
            out["ok"] = False
    return out


def main() -> int:
    argv = sys.argv[1:]
    json_out = None
    if "--json" in argv:
        i = argv.index("--json")
        json_out = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    report = [validate(p) for p in argv]
    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=1)
    for r in report:
        print(
            f"{r['file']}: bigtiff={r['is_bigtiff']} pages={r['pages']} "
            f"levels={[(l['dims'], l['tiles']) for l in r['levels']]} ok={r['ok']}"
        )
    return 0 if all(r["ok"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
