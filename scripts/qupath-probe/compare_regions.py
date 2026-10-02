"""Compare QuPath default-reader region PNGs with a tifffile decode of a
reference TIFF (the classic output) at the same level coordinates.

usage: compare_regions.py probe.json regions_dir reference.tif out.json
"""
import json
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

probe = json.load(open(sys.argv[1]))
rdir = Path(sys.argv[2])
ref_path = sys.argv[3]
rows = []
with tifffile.TiffFile(ref_path) as tf:
    import zarr
    grp = zarr.open(tf.series[0].aszarr(), mode="r")
    arrs = [grp[str(i)] for i in range(len(tf.series[0].levels))]
    for r in probe["region_reads"]:
        x, y, w, h = r["level_box"]
        got = np.asarray(Image.open(rdir / r["png"]).convert("RGB"))
        ref = np.asarray(arrs[r["level"]][y:y + h, x:x + w])
        hh = min(got.shape[0], ref.shape[0])
        ww = min(got.shape[1], ref.shape[1])
        d = np.abs(got[:hh, :ww].astype(int) - ref[:hh, :ww].astype(int))
        rows.append({
            "level": r["level"], "name": r["name"],
            "got": list(got.shape[:2]), "ref": list(ref.shape[:2]),
            "max_abs_diff": int(d.max()), "mean_abs_diff": float(d.mean()),
            "ref_mean_rgb": [round(float(v), 2) for v in ref[:hh, :ww].reshape(-1, 3).mean(0)],
            "ref_std": round(float(ref.std()), 2),
        })
json.dump(rows, open(sys.argv[4], "w"), indent=1)
worst = max(rows, key=lambda r: r["max_abs_diff"])
print("regions", len(rows), "worst", worst)
print("size mismatches", [r for r in rows if r["got"] != r["ref"]])
for r in rows:
    print(r["level"], r["name"], r["got"], "maxdiff", r["max_abs_diff"], "mean", round(r["mean_abs_diff"], 4), "refmean", r["ref_mean_rgb"], "std", r["ref_std"])
