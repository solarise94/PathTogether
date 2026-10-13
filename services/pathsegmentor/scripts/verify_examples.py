#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对上游 examples/ 四类示例跑 /v1/segment，与 GT 掩码算 Dice/IoU 并存叠加图。

用法（workstation，SSD2 运行目录内）：
  python3 scripts/verify_examples.py \
      --examples-dir <upstream>/examples --out-dir <run-dir>/overlays \
      [--host 127.0.0.1] [--port 8971]

提示词按上游 README 从 mask 文件名派生（与 notebook 一致）：
  <id>_pathology_<region>_<level+object>.png
  → "<level>-level <object 去加号> in <region 去加号> pathology"
  （unspecified → multiple，与上游 format_string 一致）。
"""
import argparse
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image


def parse_pair(mask_path):
    """mask 文件名 → (image 文件名, prompt)。规则见模块 docstring。"""
    stem = mask_path.stem
    left, right = stem.split("_pathology_", 1)
    parts = right.split("_")
    if len(parts) < 2 or "+" not in parts[-1]:
        raise ValueError("not a mask filename: %s" % mask_path.name)
    target = parts[-1]
    region = "_".join(parts[:-1]).replace("+", " ")
    level, object_phrase = target.split("+", 1)
    prompt = "%s-level %s in %s pathology" % (
        level, object_phrase.replace("+", " "), region.replace("+", " "))
    prompt = prompt.replace("unspecified", "multiple")
    image_name = "%s_pathology_%s.png" % (left, "_".join(parts[:-1]))
    return image_name, prompt, level, object_phrase.replace("+", " "), region


def call_service(base_url, image_b64, object_phrase, region, timeout=180):
    body = {"image_b64": image_b64, "object": object_phrase,
            "region": region or "unspecified"}
    request = urllib.request.Request(
        base_url + "/v1/segment",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def overlay(image_rgb, mask_bool, color=(255, 0, 0), alpha=0.6):
    out = image_rgb.copy()
    out[mask_bool] = ((1 - alpha) * out[mask_bool]
                      + alpha * np.array(color, dtype=float)).astype(np.uint8)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8971)
    args = parser.parse_args()

    base_url = "http://%s:%d" % (args.host, args.port)
    examples = Path(args.examples_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    masks = sorted(examples.rglob("*_pathology_*.png"))
    masks = [m for m in masks if "+" in m.stem]
    if not masks:
        print("no example masks found under %s" % examples, file=sys.stderr)
        return 2

    rows = []
    for mask_path in masks:
        image_name, prompt, level, object_phrase, region = \
            parse_pair(mask_path)
        image_path = mask_path.with_name(image_name)
        if not image_path.exists():
            print("SKIP (no image): %s" % mask_path, file=sys.stderr)
            continue
        image = Image.open(image_path).convert("RGB")
        image_rgb = np.asarray(image, dtype=np.uint8)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        started = time.monotonic()
        try:
            result = call_service(base_url, image_b64, "%s %s" % (level, object_phrase),
                                  region)
        except Exception as exc:  # noqa: BLE001
            print("FAIL %s: %s" % (image_path.name, exc), file=sys.stderr)
            rows.append({"image": str(image_path.relative_to(examples)),
                         "prompt": prompt, "error": str(exc)})
            continue
        wall_ms = (time.monotonic() - started) * 1000.0

        pred = np.asarray(Image.open(io.BytesIO(
            base64.b64decode(result["mask_png_b64"])))) > 127
        gt_arr = np.asarray(Image.open(mask_path).convert("RGB"))
        gt = gt_arr[:, :, 0] > 127
        if pred.shape != gt.shape:
            print("SKIP (size mismatch %r vs %r): %s"
                  % (pred.shape, gt.shape, image_path.name), file=sys.stderr)
            continue

        intersection = int(np.count_nonzero(pred & gt))
        dice = 2.0 * intersection / (pred.sum() + gt.sum()) \
            if (pred.sum() + gt.sum()) else 1.0
        union = int(np.count_nonzero(pred | gt))
        iou = intersection / union if union else 1.0

        strip = np.concatenate([
            image_rgb,
            overlay(image_rgb, pred),
            overlay(image_rgb, gt, color=(0, 160, 255)),
        ], axis=1)
        rel = image_path.relative_to(examples).with_suffix(".overlay.png")
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(strip).save(dest)

        row = {
            "image": str(image_path.relative_to(examples)),
            "prompt": prompt,
            "dice": round(dice, 4), "iou": round(iou, 4),
            "pred_px": int(pred.sum()), "gt_px": int(gt.sum()),
            "positive_pixels": result["positive_pixels"],
            "threshold": result["threshold"],
            "server_ms": result["timings_ms"], "roundtrip_ms": round(wall_ms, 1),
        }
        rows.append(row)
        print("%-72s dice=%.3f iou=%.3f  [%s]"
              % (row["image"], dice, iou, prompt))

    summary = {"base_url": base_url, "count": len(rows), "results": rows}
    (out_dir / "metrics.json").write_text(json.dumps(summary, indent=2))
    ok = [r for r in rows if "dice" in r]
    if ok:
        dices = [r["dice"] for r in ok]
        ious = [r["iou"] for r in ok]
        print("\n%d/%d evaluated  mean dice=%.4f  mean iou=%.4f"
              % (len(ok), len(rows), float(np.mean(dices)), float(np.mean(ious))))
    print("overlays + metrics.json → %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
