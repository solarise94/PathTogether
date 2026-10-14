#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户切片 ROI（A/B/C，fine/native）实例计数 + 叠加图 + 与 PathSegmentor 并排。

用途：真实 H&E 上目检各候选模型（单独运行）或已部署服务（--service）的
计数合理性；输出叠加图（实例轮廓 + 计数）与 PathSegmentor 掩码并排图。
ROI 数据保留在运行目录，**绝不入库**。

用法：
  python3 scripts/run_rois.py --service http://127.0.0.1:8972 \
      --rois-dir <dir-with-{A,B,C}.{fine,native}.roi.png> \
      --psm-dir <dir-with-{A,B,C}.{fine,native}.mask.png> \
      --out-dir <run-dir>/overlays [--model cpsam]

  # 或独立运行候选（不经服务）：
  python3 scripts/run_rois.py --family 4 --preset cpsam \
      --weights <path> --rois-dir ... --out-dir ... [--native]

mpp 约定：fine=0.25 µm/px，native=0.484 µm/px（与 ROI 文件名后缀一致）。
"""
import argparse
import base64
import glob
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

import cv2
import numpy as np
from PIL import Image

MPP_BY_SUFFIX = {"fine": 0.25, "native": 0.484}


def draw_overlay(img, masks_or_labels, title):
    """RGB 图 + 标签图 → 叠加轮廓与计数标注的 RGB 图（原分辨率）。"""
    out = img.copy()
    labels = np.asarray(masks_or_labels)
    for lab in np.unique(labels):
        if lab == 0:
            continue
        contours, _ = cv2.findContours((labels == lab).astype(np.uint8),
                                       cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (255, 80, 80), 2)
    banner = (240, 240, 240)
    cv2.rectangle(out, (0, 0), (out.shape[1], 56), (30, 30, 30), -1)
    cv2.putText(out, title, (12, 40), 0, 1.3, banner, 5)
    cv2.putText(out, title, (12, 40), 0, 1.3, (255, 80, 80), 2)
    return out


def count_standalone(args, img_rgb, mpp):
    """独立模型推理 → (labels, diameter_px_used)。"""
    dpx = None if mpp is None else args.diameter_um / mpp
    if args.family == 4:
        from cellpose import models as cp_models
        model = cp_models.CellposeModel(
            gpu=True, pretrained_model=args.weights, use_bfloat16=False)
        masks, _, _ = model.eval(img_rgb, diameter=dpx,
                                 flow_threshold=args.flow_threshold,
                                 cellprob_threshold=args.cellprob_threshold,
                                 normalize=True, resample=True, batch_size=8)
        return np.asarray(masks), dpx
    from cellpose import models as cp_models
    model = cp_models.CellposeModel(gpu=True, pretrained_model=args.weights)
    grey = img_rgb.mean(axis=2)
    diameter = 0 if dpx is None else dpx
    masks, _, _ = model.eval(grey, channels=[0, 0], invert=True,
                             diameter=diameter,
                             flow_threshold=args.flow_threshold,
                             cellprob_threshold=args.cellprob_threshold,
                             normalize=True, resample=True)
    return np.asarray(masks), dpx


def count_service(url, img_rgb, mpp, model=None):
    """经服务契约推理 → (labels, diameter_px_used, warnings, infer_ms)。"""
    buf = io.BytesIO()
    Image.fromarray(img_rgb).save(buf, format="PNG")
    body = {"image_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "mpp": mpp, "return_labels": True}
    if model:
        body["model"] = model
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/count",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = json.loads(response.read().decode("utf-8"))
    wall_ms = (time.monotonic() - started) * 1000.0
    labels = np.asarray(Image.open(io.BytesIO(
        base64.b64decode(payload["labels_png_b64"]))).convert("I"), np.int32)
    return (labels, payload["diameter_px_used"], payload["warnings"],
            {"infer": payload["timings_ms"]["infer"], "roundtrip": round(wall_ms, 1)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default=None, help="http://127.0.0.1:8972")
    parser.add_argument("--model", default=None, help="服务预设（可选）")
    parser.add_argument("--family", type=int, choices=(3, 4), default=4)
    parser.add_argument("--preset", default="cpsam")
    parser.add_argument("--weights", default=None)
    parser.add_argument("--rois-dir", required=True)
    parser.add_argument("--psm-dir", default=None,
                        help="PathSegmentor {roi}.mask.png 所在目录（并排图）")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--diameter-um", type=float, default=7.0)
    parser.add_argument("--flow-threshold", type=float, default=0.4)
    parser.add_argument("--cellprob-threshold", type=float, default=0.0)
    parser.add_argument("--native", action="store_true", help="不重缩放")
    args = parser.parse_args()

    rois = sorted(glob.glob(os.path.join(args.rois_dir, "*.roi.png")))
    if not rois:
        print("no *.roi.png under %s" % args.rois_dir, file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    report = []
    for path in rois:
        stem = os.path.basename(path).replace(".roi.png", "")
        suffix = stem.split(".")[-1]          # fine / native
        mpp = None if args.native else MPP_BY_SUFFIX.get(suffix)
        img = np.asarray(Image.open(path).convert("RGB"))
        if args.service:
            labels, dpx, warnings, infer_ms = count_service(
                args.service, img, mpp, model=args.model)
            preset = (args.model or "default")
        else:
            labels, dpx = count_standalone(args, img, mpp)
            warnings, infer_ms = [], None
            preset = args.preset
        count = int(labels.max())
        title = "%s [%s] %s count=%d%s" % (
            preset, stem, "mpp=%.3f" % mpp if mpp else "native-scale",
            count, " dpx=%.1f" % dpx if dpx else "")
        overlay = draw_overlay(img, labels, title)
        overlay_path = os.path.join(args.out_dir, "%s.%s.overlay.png"
                                    % (stem, preset))
        cv2.imwrite(overlay_path, cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))

        panel = [img, overlay]
        psm_mask = None
        if args.psm_dir:
            mask_path = os.path.join(args.psm_dir, "%s.mask.png" % stem)
            if os.path.exists(mask_path):
                psm_mask = np.asarray(Image.open(mask_path).convert("L"))
                panel.append(draw_overlay(img, psm_mask,
                                          "PathSegmentor [%s]" % stem))
        if len(panel) > 1:
            height = min(p.shape[0] for p in panel)
            resized = []
            for p in panel:
                scale = height / p.shape[0]
                resized.append(cv2.resize(
                    p, (int(p.shape[1] * scale), height)))
            side = np.concatenate(resized, axis=1)
            side_path = os.path.join(args.out_dir, "%s.sidebyside.png" % stem)
            cv2.imwrite(side_path, cv2.cvtColor(side, cv2.COLOR_RGB2BGR))

        report.append({"roi": stem, "preset": preset, "mpp": mpp,
                       "count": count, "diameter_px_used": dpx,
                       "warnings": warnings, "infer_ms": infer_ms,
                       "overlay": overlay_path})
        print(json.dumps(report[-1]))
    with open(os.path.join(args.out_dir, "rois_report.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
