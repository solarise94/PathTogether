#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MoNuSeg H&E 核实例分割评测：count error / F1@IoU0.5 / AJI。

独立脚本（不依赖服务），在含 cellpose 3.x 或 4.x 的环境里运行以对比候选
模型；数据（公开 MoNuSeg 2018 训练子集，仅供研究使用）保存在运行目录，
**绝不入库**。

用法（family=4 需 cellpose>=4，family=3 需 cellpose<4，二者不可共存）：
  python3 scripts/eval_monuseg.py --family 4 --preset cpsam \
      --weights ~/models/cellpose/cpsam-sha256-<12h>/cpsam \
      --data <dir-with-{Tissue_Images,Binary_masks_instance}> \
      --out-dir <run-dir>/eval --limit 10 [--native] [--diameter-um 7] [--mpp 0.25]

cellpose 3 家族在 H&E 上按上游惯例：灰度化 + invert（核=暗→亮）。
指标：逐图 instance count 对比（MAE）、F1@IoU>=0.5（匈牙利一对一）、AJI。
"""
import argparse
import glob
import json
import os
import sys
import time

import numpy as np
from PIL import Image
from scipy.optimize import linear_sum_assignment


def pair_intersections(gt, pred):
    """实例标签图 → (inter 矩阵, gt_ids, pred_ids, gt_area, pred_area)。

    inter[i, j] = GT 实例 i 与预测实例 j 的交集像素数（联合直方图一次算完）。
    """
    gt_ids = np.unique(gt)
    gt_ids = gt_ids[gt_ids > 0]
    pred_ids = np.unique(pred)
    pred_ids = pred_ids[pred_ids > 0]
    if gt_ids.size == 0 or pred_ids.size == 0:
        return None, gt_ids, pred_ids, None, None
    maxg, maxp = int(gt_ids.max()), int(pred_ids.max())
    joint = np.bincount(
        gt.ravel().astype(np.int64) * (maxp + 1) + pred.ravel(),
        minlength=(maxg + 1) * (maxp + 1)).reshape(maxg + 1, maxp + 1)
    inter = joint[np.ix_(gt_ids, pred_ids)]
    gt_area = np.bincount(gt.ravel(), minlength=maxg + 1)[gt_ids]
    pred_area = np.bincount(pred.ravel(), minlength=maxp + 1)[pred_ids]
    return inter, gt_ids, pred_ids, gt_area, pred_area


def iou_from_inter(inter, gt_area, pred_area):
    with np.errstate(divide="ignore", invalid="ignore"):
        union = gt_area[:, None] + pred_area[None, :] - inter
        return np.where(union > 0, inter / np.maximum(union, 1), 0.0)


def iou_matrix(gt, pred):
    """实例标签图 → IoU 矩阵 (n_gt, n_pred)。"""
    inter, gt_ids, pred_ids, gt_area, pred_area = pair_intersections(gt, pred)
    if inter is None:
        return np.zeros((gt_ids.size, pred_ids.size))
    return iou_from_inter(inter, gt_area, pred_area)


def match_at(gt, pred, thr=0.5):
    """匈牙利一对一匹配 → (tp, n_gt, n_pred)。"""
    iou = iou_matrix(gt, pred)
    if iou.size == 0:
        return 0, iou.shape[0], iou.shape[1]
    rows, cols = linear_sum_assignment(-iou)
    return int(sum(iou[r, c] >= thr for r, c in zip(rows, cols))), \
        iou.shape[0], iou.shape[1]


def aji(gt, pred):
    """Aggregated Jaccard Index（MoNuSeg 官方评测的贪心定义，Kumar et al.）。

    每个 GT 取交叠最大的 pred：未占用且交叠>0 → 累计 I/U 并占用之；否则该
    GT 记未匹配，分母累计 GT 面积 + 全部交叠 pred 面积（惩罚）。
    """
    inter, gt_ids, pred_ids, gt_area, pred_area = pair_intersections(gt, pred)
    if gt_ids.size == 0:
        return 1.0 if pred_ids.size == 0 else 0.0
    if pred_ids.size == 0:
        return 0.0
    used = np.zeros(pred_ids.size, dtype=bool)
    numerator = 0
    denominator = 0
    for r in range(gt_ids.size):
        row = inter[r]
        c = int(np.argmax(row))
        if row[c] == 0:
            denominator += int(gt_area[r])
            continue
        if used[c]:
            overlapping = np.nonzero(row)[0]
            denominator += int(gt_area[r]) + int(pred_area[overlapping].sum())
            continue
        used[c] = True
        numerator += int(row[c])
        denominator += int(gt_area[r]) + int(pred_area[c]) - int(row[c])
    return numerator / denominator if denominator else 0.0


def segment_family(args, img_rgb):
    """按 family/preset 配置推理 → 实例标签图。"""
    if args.family == 4:
        from cellpose import models as cp_models
        model = cp_models.CellposeModel(
            gpu=True, pretrained_model=args.weights, use_bfloat16=False)
        diameter = None if args.native else (
            args.diameter_um / args.mpp if args.mpp else None)
        masks, _, _ = model.eval(img_rgb, diameter=diameter,
                                 flow_threshold=args.flow_threshold,
                                 cellprob_threshold=args.cellprob_threshold,
                                 normalize=True, resample=True, batch_size=8)
        return np.asarray(masks)
    # family 3：H&E → 灰度 + invert（上游 brightfield 惯例）
    from cellpose import models as cp_models
    model = cp_models.CellposeModel(gpu=True, pretrained_model=args.weights)
    grey = img_rgb.mean(axis=2)
    diameter = 0 if args.native or not args.mpp else (
        args.diameter_um / args.mpp)
    masks, _, _ = model.eval(grey, channels=[0, 0], invert=True,
                             diameter=diameter,
                             flow_threshold=args.flow_threshold,
                             cellprob_threshold=args.cellprob_threshold,
                             normalize=True, resample=True)
    return np.asarray(masks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", type=int, choices=(3, 4), required=True)
    parser.add_argument("--preset", required=True, help="如 cpsam / cyto3 / nuclei")
    parser.add_argument("--weights", required=True)
    parser.add_argument("--data", required=True, help="含 Tissue_Images/ 与 Binary_masks_instance/")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mpp", type=float, default=0.25, help="MoNuSeg 40×: 0.25")
    parser.add_argument("--diameter-um", type=float, default=7.0)
    parser.add_argument("--native", action="store_true",
                        help="不重缩放（cpsam: diameter=None；cp3: diameter=0）")
    parser.add_argument("--flow-threshold", type=float, default=0.4)
    parser.add_argument("--cellprob-threshold", type=float, default=0.0)
    parser.add_argument("--save-overlays", action="store_true")
    args = parser.parse_args()

    images_dir = os.path.join(args.data, "Tissue_Images")
    masks_dir = os.path.join(args.data, "Binary_masks_instance")
    images = sorted(glob.glob(os.path.join(images_dir, "*.tif")))[:args.limit]
    if not images:
        print("no images under %s" % images_dir, file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)

    rows = []
    for path in images:
        stem = os.path.splitext(os.path.basename(path))[0]
        gt = np.load(os.path.join(masks_dir, stem + ".npy"))
        img = np.asarray(Image.open(path).convert("RGB"))
        started = time.monotonic()
        pred = segment_family(args, img)
        elapsed = time.monotonic() - started
        tp, n_gt, n_pred = match_at(gt, pred, 0.5)
        precision = tp / n_pred if n_pred else 0.0
        recall = tp / n_gt if n_gt else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        score = aji(gt, pred)
        rows.append({"image": stem, "gt_count": n_gt, "pred_count": n_pred,
                     "abs_count_err": abs(n_pred - n_gt), "tp": tp,
                     "precision": round(precision, 4), "recall": round(recall, 4),
                     "f1_iou50": round(f1, 4), "aji": round(score, 4),
                     "infer_s": round(elapsed, 2)})
        print(json.dumps(rows[-1]))
        if args.save_overlays:
            overlay = img.copy()
            from cv2 import (drawContours, findContours, putText,
                             RETR_EXTERNAL, CHAIN_APPROX_SIMPLE, imwrite)
            for lab in np.unique(pred):
                if lab == 0:
                    continue
                contours, _ = findContours(
                    (pred == lab).astype(np.uint8), RETR_EXTERNAL,
                    CHAIN_APPROX_SIMPLE)
                drawContours(overlay, contours, -1, (255, 80, 80), 2)
            putText(overlay, "%s pred=%d gt=%d" % (args.preset, n_pred, n_gt),
                    (10, 40), 0, 1.2, (255, 255, 255), 4)
            putText(overlay, "%s pred=%d gt=%d" % (args.preset, n_pred, n_gt),
                    (10, 40), 0, 1.2, (0, 0, 200), 2)
            imwrite(os.path.join(args.out_dir, "monuseg_%s_%s.png"
                                 % (args.preset, stem)), overlay)

    summary = {
        "preset": args.preset, "family": args.family,
        "diameter_um": None if args.native else args.diameter_um,
        "mpp": args.mpp, "n_images": len(rows),
        "mean_abs_count_err": round(float(np.mean(
            [r["abs_count_err"] for r in rows])), 2),
        "mean_f1_iou50": round(float(np.mean(
            [r["f1_iou50"] for r in rows])), 4),
        "mean_aji": round(float(np.mean([r["aji"] for r in rows])), 4),
        "mean_infer_s": round(float(np.mean(
            [r["infer_s"] for r in rows])), 2),
        "total_gt": sum(r["gt_count"] for r in rows),
        "total_pred": sum(r["pred_count"] for r in rows),
    }
    with open(os.path.join(args.out_dir, "summary_%s%s.json"
                           % (args.preset, "_native" if args.native else "")),
              "w") as fh:
        json.dump({"summary": summary, "per_image": rows}, fh, indent=1)
    print("\n== %s (family %d, %s) ==" % (args.preset, args.family,
                                          "native" if args.native
                                          else "diameter %.1fum" % args.diameter_um))
    for key in ("n_images", "total_gt", "total_pred", "mean_abs_count_err",
                "mean_f1_iou50", "mean_aji", "mean_infer_s"):
        print("  %s: %s" % (key, summary[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
