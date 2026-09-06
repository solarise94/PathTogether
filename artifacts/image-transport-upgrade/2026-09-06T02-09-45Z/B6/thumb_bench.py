# -*- coding: utf-8 -*-
"""荧光 thumbnail 编码基准（§7.2）：旧 save(q90, 默认采样=4:2:0) vs 新
fluorescence-thumb-v1（q95, 4:4:4, optimize）。

对编码前缩略 RGB（composite → LANCZOS 缩到 400 内）比较彩色边缘 MAE、
SSIM、bytes、实际色度抽样。运行：
  python thumb_bench.py --manifest manifest.json --sample <id> [--rois 12]
"""
import argparse
import io
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from PIL import Image, JpegImagePlugin  # noqa: E402
import numpy as np  # noqa: E402

from scripts.benchmark_viewer_encoding import (  # noqa: E402
    decode_reference, _edge_mae, _ssim_luma, DEFAULT_DEFS)


def sampling(b):
    img = Image.open(io.BytesIO(b))
    img.load()
    return JpegImagePlugin.get_sampling(img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--rois", type=int, default=12)
    ap.add_argument("--out", default="thumb-report.json")
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rois = [r for r in manifest["rois"] if r["sample"] == args.sample]
    rois = rois[: args.rois]
    rows = []
    for roi in rois:
        ref = decode_reference(manifest, roi)
        ref.thumbnail((400, 400), Image.LANCZOS)
        ref_arr = np.asarray(ref.convert("RGB"))
        encs = {}
        for label, cfg in (
            ("old-q90-420-default-sampling",
             {"quality": 90, "subsampling": 2, "optimize": False,
              "progressive": False}),
            ("fluorescence-thumb-v1-q95-444-opt",
             {"quality": 95, "subsampling": 0, "optimize": True,
              "progressive": False}),
        ):
            buf = io.BytesIO()
            ref.save(buf, format="JPEG", **cfg)
            data = buf.getvalue()
            dec = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
            encs[label] = {
                "bytes": len(data),
                "sampling": sampling(data),
                "edge_mae": _edge_mae(ref_arr, dec, DEFAULT_DEFS),
                "ssim": _ssim_luma(ref_arr, dec, DEFAULT_DEFS),
            }
        rows.append({"roi": roi["id"], **encs})
    summary = {}
    for label in rows[0]:
        if label == "roi":
            continue
        summary[label] = {
            "n": len(rows),
            "bytes_median": statistics.median(r[label]["bytes"] for r in rows),
            "sampling": rows[0][label]["sampling"],
            "edge_mae_median": statistics.median(
                r[label]["edge_mae"] for r in rows),
            "ssim_median": statistics.median(r[label]["ssim"] for r in rows),
        }
    report = {"summary": summary, "rows": rows}
    Path(args.out).write_text(json.dumps(report, indent=1),
                              encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
