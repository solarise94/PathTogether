# -*- coding: utf-8 -*-
"""viewer 显示编码基准 CLI（image-transport-upgrade §7；本轮新增工具）。

对每个 ROI：从**同一原始文件、同一 source level、同一 render context** 取
编码前 RGB（无损对照，PNG 落盘），分别用 baseline/candidate 两份显式编码
配置编码，输出逐 ROI 指标与分组汇总：

  bytes、encode/decode 耗时、PSNR、SSIM（固定参数）、边缘区域 RGB MAE
  （Sobel mask 来自参考图）、弱信号变化（阈值窗来自参考图）、R/G/B 各通道
  MAE、输出 SHA256。

红线（§7.1/§7.2）：
  - 弱信号/边缘 mask 一律来自**参考图**，禁止有损候选拿自己的解码定义 mask；
  - SSIM 参数、颜色空间、阈值、边缘算法固定在 manifest，候选之间不可调；
  - 真实样本与合成夹具分表（manifest 里 sample.kind / synthetic 标记区分，
    汇总不混算）；
  - 参考图只是**当前显示管线**的编码前 RGB，不代表恢复扫描仪原始信息；
    源 SVS 自带有损压缩时在 manifest 里如实标注 source_lossy。

用法（先 --generate-manifest 生成模板，再跑比较）：
  python scripts/benchmark_viewer_encoding.py --generate-manifest \
      --samples bench-samples --out manifest.json
  python scripts/benchmark_viewer_encoding.py --manifest manifest.json \
      --output-dir out/bench-20260906 \
      --baseline-config cfg-baseline.json --candidate-config cfg-candidate.json
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from PIL import Image  # noqa: E402

import numpy as np  # noqa: E402

MANIFEST_VERSION = "bench-manifest-v1"
#: SSIM/边缘/弱信号默认定义（写入 manifest；候选之间不得改）
DEFAULT_DEFS = {
    "ssim": {"window": 8, "sigma": 1.5, "k1": 0.01, "k2": 0.03,
             "colorspace": "luma_bt601"},
    "edge_mask": {"algo": "sobel3x3", "threshold": 24, "dilate": 1},
    "weak_signal": {"low": 8, "high": 48, "tolerance": 10},
}
#: 各倍率档默认 ROI 尺寸（512 对齐显示瓦片）
ROI_SIZE = 512


# --------------------------------------------------------------------------- #
# manifest 生成
# --------------------------------------------------------------------------- #
def _open_slide(path: Path):
    """按仓库既有入口打开切片（SVS → OpenSlide；OME → TiffFileSlide 回退）。"""
    import slide_io

    return slide_io.open_slide(path)


def _sample_probe(path: Path) -> dict:
    """探测样本：kind（rgb/multichannel）、levels、尺寸。"""
    osr = _open_slide(path)
    try:
        levels = list(osr.level_downsamples)
        dims = osr.dimensions
        kind = "rgb"
        # 多通道判定与 slide_render.slide_image_mode 同口径的轻量版：
        # TiffFileSlide 提供 channel_count；OpenSlide 厂商格式恒 rgb
        channel_count = getattr(osr, "channel_count", None)
        if channel_count and channel_count >= 2:
            kind = "multichannel"
        return {"levels": len(levels),
                "downsamples": [round(float(d), 2) for d in levels],
                "width": int(dims[0]), "height": int(dims[1]),
                "kind": kind}
    finally:
        try:
            osr.close()
        except Exception:
            pass


def generate_manifest(samples_dir: Path, out_path: Path,
                      rois_per_class: int = 60,
                      seed: str = "image-transport-upgrade") -> dict:
    """确定性生成 ROI manifest（覆盖低/中/高倍 + 位置多样性，seed 固定）。"""
    import hashlib as _hashlib

    extensions = (".svs", ".tif", ".tiff", ".ome.tif", ".ome.tiff")
    files = sorted(p for p in samples_dir.iterdir()
                   if p.is_file() and p.name.lower().endswith(extensions)
                   and not p.name.startswith("."))
    rgb_samples, mc_samples = [], []
    for p in files:
        probe = _sample_probe(p)
        entry = {
            "id": _hashlib.sha256(p.name.encode("utf-8")).hexdigest()[:12],
            "local_label": p.name,   # 本地标识（ manifest 不含路径外的敏感信息）
            "path": str(p),
            "kind": probe["kind"],
            "levels": probe["levels"],
            "downsamples": probe["downsamples"],
            "width": probe["width"],
            "height": probe["height"],
            # 源格式自带的压缩如实记录（有损 SVS 不冒充无损）
            "synthetic": p.name.startswith("synthetic-"),
            "source_lossy": p.suffix.lower() in (".svs", ".jp2"),
        }
        (mc_samples if probe["kind"] == "multichannel" else rgb_samples).append(
            entry)

    rois = []
    for si, sample in enumerate(rgb_samples + mc_samples):
        kind = sample["kind"]
        # 每样本 ROIs = rois_per_class / 样本数（不足 → 至少 6 个）
        per_slide = max(6, rois_per_class
                        // max(1, len(rgb_samples if kind == "rgb"
                                      else mc_samples)))
        # 三个倍率档：最粗（低倍）、中、最高（高倍）
        level_count = sample["levels"]
        picks = sorted({0, level_count // 2, min(2, level_count - 1),
                        level_count - 1})
        h = int(_hashlib.sha256(
            (seed + sample["id"]).encode("utf-8")).hexdigest()[:8], 16)
        for li, level in enumerate(picks):
            n_here = max(2, per_slide // len(picks))
            ds = sample["downsamples"][level]
            lw = int(sample["width"] / ds)
            lh = int(sample["height"] / ds)
            for i in range(n_here):
                # LCG 确定性位置（避免 pandas/random 状态）
                h = (1103515245 * h + 12345) % (1 << 31)
                x = (h % max(1, lw - ROI_SIZE)) if lw > ROI_SIZE else 0
                h = (1103515245 * h + 12345) % (1 << 31)
                y = (h % max(1, lh - ROI_SIZE)) if lh > ROI_SIZE else 0
                rois.append({
                    "id": "%s-L%d-%03d" % (sample["id"], level, i),
                    "sample": sample["id"],
                    "kind": kind,
                    "level": int(level),
                    "x": int(x), "y": int(y),
                    "w": min(ROI_SIZE, lw), "h": min(ROI_SIZE, lh),
                    # 位置分类辅助（低/中/高倍）
                    "mag_band": ["low", "mid", "high"][min(li, 2)],
                })
    manifest = {
        "version": MANIFEST_VERSION,
        "seed": seed,
        "definitions": DEFAULT_DEFS,
        "samples": rgb_samples + mc_samples,
        "rois": rois,
        "counts": {"rgb_rois": sum(1 for r in rois if r["kind"] == "rgb"),
                   "multichannel_rois": sum(1 for r in rois
                                            if r["kind"] == "multichannel"),
                   "rgb_samples": len(rgb_samples),
                   "multichannel_samples": len(mc_samples)},
    }
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- #
# ROI 解码（编码前 RGB 参考）
# --------------------------------------------------------------------------- #
def decode_reference(manifest: dict, roi: dict) -> Image.Image:
    """从原始文件取编码前 RGB（同一 level / 同一 location）。

    RGB：read_region 原样；多通道：按 default context（前 4 个可显示通道、
    全局强度窗）走 slide_render.composite_region——与显示管线同一语义，
    不自创窗/配色。
    """
    sample = next(s for s in manifest["samples"] if s["id"] == roi["sample"])
    path = Path(sample["path"])
    osr = _open_slide(path)
    try:
        if sample["kind"] == "rgb":
            region = osr.read_region((roi["x"], roi["y"]), roi["level"],
                                     (roi["w"], roi["h"]))
            if region.mode != "RGB":
                region = region.convert("RGB")
            return region
        import slide_render

        asset_revision = "bench-reference"
        manifest_ch = slide_render.build_channel_manifest(
            osr, asset_generation="bench", with_intensity=True)
        ctx = slide_render.build_default_render_context(
            osr, manifest_ch, asset_revision)
        if ctx is None:
            raise RuntimeError("样本 %s 无可用默认通道" % sample["id"])
        canonical, _fp = slide_render.canonicalize_render_context(
            ctx, channel_count=len(manifest_ch.get("channels", [])))
        composite = slide_render.composite_region(
            osr, canonical, (roi["x"], roi["y"]), roi["level"],
            (roi["w"], roi["h"]))
        if composite.mode != "RGB":
            composite = composite.convert("RGB")
        return composite
    finally:
        try:
            osr.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 指标（固定定义；mask 全部来自参考图）
# --------------------------------------------------------------------------- #
def _luma(arr: np.ndarray) -> np.ndarray:
    """BT.601 luma（定义固定在 manifest.definitions.ssim.colorspace）。"""
    a = arr.astype(np.float64)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    if mse == 0:
        return float("inf")
    return 10.0 * float(np.log10(255.0 * 255.0 / mse))


def _gaussian_kernel(sigma: float) -> np.ndarray:
    radius = max(1, int(round(3 * sigma)))
    xs = np.arange(-radius, radius + 1, dtype=np.float64)
    k = np.exp(-(xs ** 2) / (2 * sigma * sigma))
    return k / k.sum()


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    k = _gaussian_kernel(sigma)
    pad = len(k) // 2
    padded = np.pad(img, ((pad, pad), (0, 0)), mode="reflect")
    out = np.zeros_like(padded)
    for i, w in enumerate(k):
        out += w * np.roll(padded, -i, axis=0)
    out = out[: img.shape[0]]
    padded = np.pad(out, ((0, 0), (pad, pad)), mode="reflect")
    out2 = np.zeros_like(padded)
    for i, w in enumerate(k):
        out2 += w * np.roll(padded, -i, axis=1)
    return out2[: img.shape[1]] if False else out2[:, : img.shape[1]]


def _ssim_luma(a: np.ndarray, b: np.ndarray, defs: dict) -> float:
    """固定参数 SSIM（luma，高斯窗 σ/w from manifest；全局均值）。"""
    cfg = defs["ssim"]
    wa = _blur(_luma(a), float(cfg["sigma"]))
    wb = _blur(_luma(b), float(cfg["sigma"]))
    la = _blur(_luma(a) ** 2, float(cfg["sigma"])) - wa ** 2
    lb = _blur(_luma(b) ** 2, float(cfg["sigma"])) - wb ** 2
    lab = _blur(_luma(a) * _luma(b), float(cfg["sigma"])) - wa * wb
    c1 = (float(cfg["k1"]) * 255) ** 2
    c2 = (float(cfg["k2"]) * 255) ** 2
    win = int(cfg["window"])
    h, w = wa.shape
    eh, ew = h - (h % win), w - (w % win)
    num = (2 * wa[:eh, :ew] * wb[:eh, :ew] + c1) * \
        (2 * lab[:eh, :ew] + c2)
    den = (wa[:eh, :ew] ** 2 + wb[:eh, :ew] ** 2 + c1) * \
        (la[:eh, :ew] + lb[:eh, :ew] + c2)
    return float(np.mean(num / den))


def _sobel_mag(luma: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(luma)
    gy = np.zeros_like(luma)
    gx[:, 1:-1] = luma[:, 2:] - luma[:, :-2]
    gy[1:-1, :] = luma[2:, :] - luma[:-2, :]
    return np.hypot(gx, gy)


def _dilate(mask: np.ndarray, iters: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(max(0, int(iters))):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def _edge_mae(ref: np.ndarray, dec: np.ndarray, defs: dict) -> float:
    cfg = defs["edge_mask"]
    mask = _sobel_mag(_luma(ref)) > float(cfg["threshold"])
    mask = _dilate(mask, cfg.get("dilate", 1))
    if not mask.any():
        return 0.0
    diff = np.abs(ref.astype(np.float64) - dec.astype(np.float64))
    return float(diff[mask].mean())


def _weak_signal_change(ref: np.ndarray, dec: np.ndarray,
                        defs: dict) -> dict:
    """弱信号 mask 来自参考图 luma ∈ [low, high)；报告窗内 MAE 与越窗比例。"""
    cfg = defs["weak_signal"]
    l = _luma(ref)
    mask = (l >= float(cfg["low"])) & (l < float(cfg["high"]))
    if not mask.any():
        return {"weak_pixels": 0, "weak_mae": None, "weak_exceed": None}
    diff = np.abs(ref.astype(np.float64) - dec.astype(np.float64)).mean(axis=2)
    tol = float(cfg["tolerance"])
    exceed = float((diff[mask] > tol).mean())
    return {"weak_pixels": int(mask.sum()),
            "weak_mae": float(diff[mask].mean()),
            "weak_exceed": exceed}


def encode_with_config(img: Image.Image, cfg: dict) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(cfg["quality"]),
             subsampling=int(cfg["subsampling"]),
             optimize=bool(cfg.get("optimize", False)),
             progressive=bool(cfg.get("progressive", False)))
    ms = (time.perf_counter() - t0) * 1000.0
    return buf.getvalue(), ms


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def _pctl(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * q))))
    return xs[idx]


def summarize(rows: list[dict], defs: dict) -> dict:
    out = {}
    for kind in ("rgb", "multichannel"):
        for cfg_label in ("baseline", "candidate"):
            sel = [r for r in rows
                   if r.get("kind") == kind and r["config"] == cfg_label
                   and not r.get("synthetic")]
            if not sel:
                continue
            out["%s/%s" % (kind, cfg_label)] = {
                "n": len(sel),
                "bytes_median": statistics.median(r["bytes"] for r in sel),
                "bytes_p95": _pctl([r["bytes"] for r in sel], 0.95),
                "psnr_median": statistics.median(r["psnr"] for r in sel),
                "ssim_median": statistics.median(r["ssim"] for r in sel),
                "edge_mae_median": statistics.median(
                    r["edge_mae"] for r in sel),
                "weak_mae_median": statistics.median(
                    r["weak"]["weak_mae"] for r in sel
                    if r["weak"]["weak_mae"] is not None),
                "encode_ms_p95": _pctl([r["encode_ms"] for r in sel], 0.95),
                "decode_ms_p95": _pctl([r["decode_ms"] for r in sel], 0.95),
            }
        b = [r for r in rows if r.get("kind") == kind
             and r["config"] == "baseline" and not r.get("synthetic")]
        c = [r for r in rows if r.get("kind") == kind
             and r["config"] == "candidate" and not r.get("synthetic")]
        if b and c:
            out["%s/ratio_candidate_vs_baseline" % kind] = {
                "bytes": statistics.median(r["bytes"] for r in c)
                / max(1e-9, statistics.median(r["bytes"] for r in b)),
                "edge_mae": statistics.median(r["edge_mae"] for r in c)
                / max(1e-9, statistics.median(r["edge_mae"] for r in b)),
                "ssim": statistics.median(r["ssim"] for r in c)
                - statistics.median(r["ssim"] for r in b),
            }
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(manifest_path: Path, output_dir: Path, baseline_path: Path,
        candidate_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    defs = manifest.get("definitions", DEFAULT_DEFS)
    output_dir.mkdir(parents=True, exist_ok=True)
    ref_dir = output_dir / "reference"
    ref_dir.mkdir(exist_ok=True)

    rows = []
    rois = manifest["rois"]
    synthetic_ids = {s["id"] for s in manifest["samples"]
                     if s.get("synthetic")}
    for i, roi in enumerate(rois):
        try:
            ref = decode_reference(manifest, roi)
        except Exception as e:  # noqa: BLE001  单 ROI 失败记录后继续
            rows.append({"roi": roi["id"], "config": "error",
                         "error": "%s: %s" % (type(e).__name__, e)})
            continue
        ref_path = ref_dir / ("%s-ref.png" % roi["id"])
        if not ref_path.exists():
            ref.save(ref_path, format="PNG")   # 无损对照（当前显示管线）
        ref_arr = np.asarray(ref)
        for label, cfg in (("baseline", baseline), ("candidate", candidate)):
            rule = cfg.get(roi["kind"]) or {}
            data, enc_ms = encode_with_config(ref, rule)
            t0 = time.perf_counter()
            dec = Image.open(io.BytesIO(data)).convert("RGB")
            decode_ms = (time.perf_counter() - t0) * 1000.0
            dec_arr = np.asarray(dec)
            weak = _weak_signal_change(ref_arr, dec_arr, defs)
            rows.append({
                "roi": roi["id"], "config": label, "kind": roi["kind"],
                "synthetic": roi["sample"] in synthetic_ids,
                "profile_id": rule.get("profile_id"),
                "level": roi["level"], "mag_band": roi.get("mag_band"),
                "in_size": [int(ref.size[0]), int(ref.size[1])],
                "out_size": [int(dec.size[0]), int(dec.size[1])],
                "bytes": len(data), "encode_ms": enc_ms,
                "decode_ms": decode_ms,
                "psnr": _psnr(ref_arr, dec_arr),
                "ssim": _ssim_luma(ref_arr, dec_arr, defs),
                "edge_mae": _edge_mae(ref_arr, dec_arr, defs),
                "channel_mae": [float(np.abs(
                    ref_arr[..., c].astype(np.float64)
                    - dec_arr[..., c].astype(np.float64)).mean())
                    for c in range(3)],
                "weak": weak,
                "output_sha256": hashlib.sha256(data).hexdigest(),
            })
        if (i + 1) % 20 == 0:
            print("[bench] %d/%d rois" % (i + 1, len(rois)), flush=True)

    summary = summarize(rows, defs)
    report = {
        "version": "bench-report-v1",
        "manifest_version": manifest["version"],
        "runtime": {"python": sys.version.split()[0],
                    "pillow": Image.__version__ if hasattr(Image, "__version__") else "?"},
        "baseline_config": baseline, "candidate_config": candidate,
        "definitions": defs,
        "counts": manifest["counts"],
        "summary": summary,
        "rows": rows,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# viewer 编码基准报告", "",
             "- baseline: %s" % json.dumps(baseline, ensure_ascii=False),
             "- candidate: %s" % json.dumps(candidate, ensure_ascii=False),
             "- ROI 计数: %s" % json.dumps(manifest["counts"]), ""]
    for key, val in summary.items():
        lines.append("## %s" % key)
        for k, v in val.items():
            lines.append("- %s: %s" % (k, v))
        lines.append("")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--baseline-config", type=Path)
    ap.add_argument("--candidate-config", type=Path)
    ap.add_argument("--generate-manifest", action="store_true")
    ap.add_argument("--samples", type=Path,
                    help="样本目录（--generate-manifest 用）")
    ap.add_argument("--out", type=Path, help="manifest 输出路径")
    args = ap.parse_args()

    if args.generate_manifest:
        if not args.samples or not args.out:
            ap.error("--generate-manifest 需要 --samples 与 --out")
        m = generate_manifest(args.samples, args.out)
        print(json.dumps(m["counts"], ensure_ascii=False))
        return 0
    for need in (args.manifest, args.output_dir, args.baseline_config,
                 args.candidate_config):
        if need is None:
            ap.error("--manifest --output-dir --baseline-config "
                     "--candidate-config 均必填")
    report = run(args.manifest, args.output_dir, args.baseline_config,
                 args.candidate_config)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
