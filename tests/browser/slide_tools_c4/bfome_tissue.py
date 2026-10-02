# -*- coding: utf-8 -*-
"""bf-ome 浏览器回归的组织定位预步骤。

从 classic 参照（openslide 缩略图）找组织质心（level-0 坐标），并按
DeepZoomGenerator(tile_size=512, overlap=1, limit_bounds=True) 的几何给出
若干层级上覆盖该质心的瓦片坐标（供驱动端拼 URL 独立抓取比对）。
"""
import argparse
import json

import numpy as np
import openslide
from openslide.deepzoom import DeepZoomGenerator


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classic", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    osr = openslide.OpenSlide(args.classic)
    dz = DeepZoomGenerator(osr, tile_size=512, overlap=1, limit_bounds=True)
    max_level = dz.level_count - 1

    thumb = osr.get_thumbnail((512, 512)).convert("RGB")
    a = np.asarray(thumb, dtype=int)
    luma = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    mask = (luma > 60) & (luma < 240)
    if mask.sum() < 50:
        raise SystemExit("classic 参照缩略图未见组织")
    ys, xs = np.nonzero(mask)
    cx = float(xs.mean()) / thumb.width * osr.level_dimensions[0][0]
    cy = float(ys.mean()) / thumb.height * osr.level_dimensions[0][1]

    stride = 512 - 1
    levels = sorted({max(0, max_level - d) for d in (0, 4, 8)})
    out_levels = []
    for lv in levels:
        scale = 2 ** (max_level - lv)
        xl = cx / scale
        yl = cy / scale
        cols, rows = dz.level_tiles[lv]
        col = min(int(xl // stride), cols - 1)
        row = min(int(yl // stride), rows - 1)
        tiles = [[col, row]]
        if col + 1 < cols:
            tiles.append([col + 1, row])
        out_levels.append({"level": lv, "tiles": tiles})

    payload = {
        "maxLevel": max_level,
        "centroidLevel0": [round(cx, 1), round(cy, 1)],
        "levels": out_levels,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"tissue plan: maxLevel={max_level} centroid=({cx:.0f},{cy:.0f}) "
          + " ".join(f"L{l['level']}x{l['tiles']}" for l in out_levels))


if __name__ == "__main__":
    main()
