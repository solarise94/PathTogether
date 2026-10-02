# -*- coding: utf-8 -*-
"""bf-ome 浏览器回归的瓦片色彩对照助手。

输入 manifest（run_bfome_viewer.js 生成）：
  [{"file": <served jpg path>, "level": L, "col": c, "row": r}, ...]

classic 参照：openslide DeepZoomGenerator(tile_size=512, overlap=1,
limit_bounds=True) —— 只取坐标几何（两产物层级/尺寸一致，瓦片载荷同源），
served 瓦片是平台 JPEG 再编码（q82 4:2:0），只允许小再编码差异、不允许
通道互换。
"""
import argparse
import itertools
import json

import numpy as np
import openslide
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image

CHAN_MEAN_TOL = 2.5     # 每通道均值差（再编码偏色上限）
MAD_TOL = 8.0           # 全像素平均绝对差（q82 4:2:0 噪声上限）
PERM_RATIO = 0.8        # 恒等映射的均值差和 ≤ 0.8 × 任何互换映射
PERM_ABS = 4.0          # 且自身足够小
COLORFUL_MIN = 6.0      # 参照瓦片色彩度下限：低于它的（近白/灰）瓦片无通道
                        # 判别力，跳过互换负向；但整批至少 2 张须过负向
LUMA_TISSUE_MAX = 240.0  # 组织瓦片判据：参照 luma 均值须低于此值（非空白）


def _rgb(img):
    return np.asarray(img.convert("RGB"), dtype=np.float64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classic", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = json.loads(open(args.manifest, encoding="utf-8").read())
    if isinstance(raw, dict):
        tiles = raw.get("tiles") or []
        th = raw.get("tolerances") or {}
    else:  # 兼容纯数组形态
        tiles = raw
        th = {}
    chan_tol = float(th.get("chanMean", CHAN_MEAN_TOL))
    mad_tol = float(th.get("mad", MAD_TOL))
    luma_tol = float(th.get("lumaMad", mad_tol))
    perm_on = bool(th.get("perm", True))
    osr = openslide.OpenSlide(args.classic)
    dz = DeepZoomGenerator(osr, tile_size=512, overlap=1, limit_bounds=True)

    out_tiles, failures = [], []
    worst = {"chan": 0.0, "mad": 0.0, "luma": 0.0}
    perms = list(itertools.permutations(range(3)))
    perm_tested = 0
    for t in tiles:
        served = _rgb(Image.open(t["file"]))
        ref = _rgb(dz.get_tile(t["level"], (t["col"], t["row"])))
        where = f"L{t['level']}/{t['col']}_{t['row']}"
        if served.shape != ref.shape:
            failures.append(f"{where}: shape {served.shape} != {ref.shape}")
            continue
        g, r = served, ref
        mean_g = g.reshape(-1, 3).mean(axis=0)
        mean_r = r.reshape(-1, 3).mean(axis=0)
        chan_diff = np.abs(mean_g - mean_r)
        mad = float(np.abs(g - r).mean())
        luma_g = 0.299 * g[..., 0] + 0.587 * g[..., 1] + 0.114 * g[..., 2]
        luma_r = 0.299 * r[..., 0] + 0.587 * r[..., 1] + 0.114 * r[..., 2]
        luma_mad = float(np.abs(luma_g - luma_r).mean())
        # 参照瓦片色彩度（通道间差异）：近白/灰瓦片没有通道判别力
        spread = np.abs(r[..., 0] - r[..., 1]) + np.abs(r[..., 1] - r[..., 2])
        colorful = float(spread.mean())
        ref_luma_mean = float(luma_r.mean())
        rec = {
            "tile": where,
            "level": t["level"],
            "shape": list(g.shape[:2]),
            "servedMeanRGB": [round(v, 2) for v in mean_g],
            "referenceMeanRGB": [round(v, 2) for v in mean_r],
            "chanMeanDiff": [round(v, 3) for v in chan_diff],
            "mad": round(mad, 3),
            "lumaMad": round(luma_mad, 3),
            "referenceColorfulness": round(colorful, 2),
            "referenceLumaMean": round(ref_luma_mean, 1),
        }
        if float(chan_diff.max()) > chan_tol:
            failures.append(f"{where}: chan mean diff {chan_diff.tolist()}")
        if mad > mad_tol:
            failures.append(f"{where}: MAD {mad:.2f}")
        if luma_mad > luma_tol:
            failures.append(f"{where}: luma MAD {luma_mad:.2f}")
        # 通道互换负向：仅对有色彩判别力（且非空白）的瓦片执行；
        # 逐像素彩色噪声样张的通道均值对称，无均值级判别力 → 整批关闭
        if perm_on and colorful >= COLORFUL_MIN and ref_luma_mean < LUMA_TISSUE_MAX:
            perm_sums = [float(np.abs(mean_g[list(p)] - mean_r).sum())
                         for p in perms]
            identity = perm_sums[0]
            min_cross = min(perm_sums[1:])
            rec["identityPermSum"] = round(identity, 3)
            rec["minCrossPermSum"] = round(min_cross, 3)
            perm_tested += 1
            if not (identity <= PERM_ABS and identity <= PERM_RATIO * min_cross):
                failures.append(f"{where}: channel perm ambiguous "
                                f"(identity={identity:.2f} cross={min_cross:.2f})")
        else:
            rec["permSkipped"] = True
        out_tiles.append(rec)
        worst["chan"] = max(worst["chan"], float(chan_diff.max()))
        worst["mad"] = max(worst["mad"], mad)
        worst["luma"] = max(worst["luma"], luma_mad)

    if perm_on and perm_tested < 2:
        failures.append(f"通道互换负向覆盖不足（colorful 瓦片 {perm_tested} < 2）")
    osr.close()
    payload = {
        "ok": not failures,
        "tolerances": {"chanMean": chan_tol, "mad": mad_tol, "perm": perm_on},
        "worstChanMeanDiff": round(worst["chan"], 3),
        "worstMad": round(worst["mad"], 3),
        "worstLumaMad": round(worst["luma"], 3),
        "permTestedTiles": perm_tested,
        "tiles": out_tiles,
        "failures": failures,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"tilecmp ok={payload['ok']} tiles={len(out_tiles)} "
          f"worstChan={payload['worstChanMeanDiff']} worstMad={payload['worstMad']}")
    raise SystemExit(0 if payload["ok"] else 1)


if __name__ == "__main__":
    main()
