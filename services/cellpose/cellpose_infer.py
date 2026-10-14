# -*- coding: utf-8 -*-
"""Cellpose 推理核心：实例计数 / 面积统计 / 16-bit 标签图 / 模型加载。

纯逻辑部分（实例几何统计、面积分位数、16-bit PNG 标签图编码、直径换算）
不依赖 torch，单元测试可在无 GPU/权重的环境运行；torch 与 cellpose 仅在
``load_model`` / ``count`` 内部使用。

推理路径（Cellpose-SAM，cellpose==4.2.1.1，上游固定权重文件）：

  1. RGB uint8 HxWx3（调用方完成 RGBA/灰度统一）→ 上游
     ``CellposeModel.eval``：percentile 1-99 归一化 → 256² tile 推理
     （bsize=256 固定，tile_overlap=0.1）→ flow 动力学求实例掩码；
  2. ``diameter``（像素）非空时上游按 ``rescale=30/diameter`` 先整体缩放
     （原生尺度 = None → rescale 1.0），``resample=True`` 把流场插值回原尺寸；
  3. 掩码 → 实例列表（面积 / bbox / 质心）→ 面积分位数 + top 实例。

权重许可说明（README 详述）：cellpose 代码 BSD-3-Clause（HHMI）；上游
README 明示 **所有 Cellpose 模型的训练数据为 CC-BY-NC**（cpsam/cpsam_v2
亦然），权重文件未附独立许可——商用授权不明确，当前仅作研究/内部评估。
"""
import hashlib
import io
import math

import numpy as np
from PIL import Image

#: cellpose 包固定版本（Containerfile 的 wheels/ 与之一致）
CELLPOSE_VERSION = "4.2.1.1"

#: 服务预设 → 权重文件名（目录布局见 README；sha256 校验在加载期完成）
PRESET_WEIGHT_NAMES = {
    "cpsam": "cpsam",
    "cpsam_v2": "cpsam_v2",
}

#: 合同缺省值（与 PathSegmentor 上游习惯一致：flow 0.4 / cellprob 0.0）
DEFAULT_FLOW_THRESHOLD = 0.4
DEFAULT_CELLPROB_THRESHOLD = 0.0
DEFAULT_DIAMETER_UM = 7.0

#: 16-bit 标签图上限：超过则只保留面积前 N 的实例标签并附 warning
LABEL_MAP_MAX = 65535

#: 请求图最长边上限（合同：≤2048²）
MAX_IMAGE_EDGE = 2048

#: cpsam diam_mean（cellpose 4.2.1.1）：``CellposeModel.eval`` 在给出 diameter
#: （像素）时按 ``rescale = DIAM_MEAN_PX / diameter_px`` 先整体缩放输入。
DIAM_MEAN_PX = 30.0

#: 模型内部缩放后的像素量预算（R2 资源准入）：合法参数组合可以把入口内的
#: 2048² 图放大许多倍（极端例：diameter_um=1 @ mpp=0.5 → diameter_px=2 →
#: 15×，1024² → 15360²≈2.36 亿 px）。8 Mi px（≈2896²）：
#:   - 缺省插件流（1024² 输入 + 7µm @ 0.5 µm/px → diameter_px=14 →
#:     rescale≈2.14 → 2195²≈4.82M px）有 1.7× 余量；
#:   - 单路推理墙钟按缩放后像素近似线性（实测 1024²@rescale1.07 ≈ 3.6s），
#:     预算内 ≈≤25s，低于插件侧 32s 推理超时；
#:   - fp32 平面 67 MiB/张，流场等临时结构在 22 GB 卡、~5 GB 基线上安全。
MAX_SCALED_PIXELS = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# 直径换算与参数校验（纯逻辑）
# --------------------------------------------------------------------------

def diameter_px(diameter_um, mpp):
    """直径换算：µm → 像素；mpp 缺失时返回 None（模型走缺省尺度）。"""
    if mpp is None:
        return None
    if mpp <= 0:
        raise ValueError("mpp must be > 0 when provided, got %r" % (mpp,))
    if diameter_um <= 0:
        raise ValueError("diameter_um must be > 0, got %r" % (diameter_um,))
    return float(diameter_um) / float(mpp)


def scaled_pixel_count(width, height, diameter_px_):
    """cpsam 内部缩放后的像素量估计（资源准入用，纯逻辑）。

    ``CellposeModel.eval`` 按每轴 ``round-ish(size × rescale)`` 缩放输入
    （resample=True 再插值回原尺寸，但流场/中间激活都在缩放后尺寸上分配）。
    这里按逐轴 ceil 保守估计：

    - ``diameter_px_`` 为 None（mpp 缺省尺度）→ rescale=1.0，工作集即输入；
    - rescale 非有限（diameter_px 异常小导致溢出）→ 返回 None（调用方拒绝）；
    - 其余返回 ``ceil(w×r) × ceil(h×r)``（Python 大整数，不截断溢出）。
    """
    width = int(width)
    height = int(height)
    if diameter_px_ is None:
        return width * height
    rescale = DIAM_MEAN_PX / float(diameter_px_)
    if not math.isfinite(rescale) or rescale <= 0:
        return None
    scaled_w = width * rescale
    scaled_h = height * rescale
    if not (math.isfinite(scaled_w) and math.isfinite(scaled_h)):
        return None
    return math.ceil(scaled_w) * math.ceil(scaled_h)


# --------------------------------------------------------------------------
# 实例几何与统计（纯逻辑）
# --------------------------------------------------------------------------

def instance_stats(masks):
    """标签图 (H,W) int → 实例几何信息列表 + 面积分位数。

    返回 ``(instances, areas)``：instances 按**面积降序**，元素
    ``{"bbox": [x, y, w, h], "centroid": [x, y], "area_px": int}``；
    areas 为全部实例面积（升序排列的 np.ndarray，可为空）。
    """
    masks = np.asarray(masks)
    labels = np.unique(masks)
    labels = labels[labels > 0]
    instances = []
    areas = []
    for lab in labels:
        ys, xs = np.nonzero(masks == lab)
        if ys.size == 0:
            continue
        area = int(ys.size)
        areas.append(area)
        instances.append({
            "bbox": [int(xs.min()), int(ys.min()),
                     int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)],
            "centroid": [float(xs.mean()), float(ys.mean())],
            "area_px": area,
        })
    instances.sort(key=lambda d: d["area_px"], reverse=True)
    return instances, np.sort(np.asarray(areas, dtype=np.float64))


def area_summary(areas):
    """面积数组 → {min,p25,median,p75,max}（无实例时全 None）。"""
    if areas.size == 0:
        return {"min": None, "p25": None, "median": None,
                "p75": None, "max": None}
    return {
        "min": round(float(areas.min()), 1),
        "p25": round(float(np.percentile(areas, 25)), 1),
        "median": round(float(np.median(areas)), 1),
        "p75": round(float(np.percentile(areas, 75)), 1),
        "max": round(float(areas.max()), 1),
    }


def encode_labels_png(masks):
    """标签图 → (16-bit PNG 字节, truncated)。

    标签 > 65535 时保留面积前 65535 的实例（其余置 0），truncated=True
    （调用方据此加 warning）。PNG mode "I;16"，0=背景。
    """
    masks = np.asarray(masks)
    truncated = False
    out = masks.astype(np.int64, copy=True)
    labels = np.unique(out)
    labels = labels[labels > 0]
    if labels.size > LABEL_MAP_MAX:
        truncated = True
        areas = np.bincount(out.ravel())[1:]  # label l → areas[l-1]
        keep = np.argsort(areas)[::-1][:LABEL_MAP_MAX] + 1
        drop = np.setdiff1d(labels, keep)
        out[np.isin(out, drop)] = 0
    if truncated:
        # 重编号为 1..N 连续（按剩余标签升序；实例信息以 instances_top 为准）
        remap = np.zeros(int(out.max()) + 1, dtype=np.int64)
        remaining = np.unique(out)
        remaining = remaining[remaining > 0]
        for new_id, lab in enumerate(remaining, start=1):
            remap[lab] = new_id
        out = remap[out]
    image = Image.fromarray(out.astype(np.uint16), mode="I;16")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), truncated


# --------------------------------------------------------------------------
# 权重校验与模型加载（torch / cellpose 仅在此使用）
# --------------------------------------------------------------------------

def file_sha256(path, chunk_bytes=8 * 1024 * 1024):
    """流式 SHA-256（大权重不整读内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_model(weights_path, expected_sha256=None):
    """加载 Cellpose-SAM 权重 → (CellposeModel, info dict)。

    - 加载前流式校验 SHA-256（``expected_sha256`` 给定且不符 → ValueError，
      拒绝加载；校验先于 torch 导入，坏权重 fail-fast）；
    - **use_bfloat16=False**：RTX 2080 Ti（sm_75, Turing）无原生 bf16，
      cellpose 4.2 缺省 ``use_bfloat16=True`` 在该卡上不可用，必须 fp32；
    - info: sha256 / device / cellpose 版本等公共字段。
    """
    actual = file_sha256(weights_path)
    if expected_sha256 and actual != expected_sha256:
        raise ValueError(
            "weights sha256 mismatch for %s: expected %s, got %s"
            % (weights_path, expected_sha256, actual))
    import torch
    from cellpose import models as cp_models

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = cp_models.CellposeModel(
        gpu=device.type == "cuda", pretrained_model=str(weights_path),
        use_bfloat16=False)
    info = {
        "sha256": actual,
        "device": str(device) if device.type == "cuda" else "cpu",
        "cellpose_version": CELLPOSE_VERSION,
        "weights_path": str(weights_path),
    }
    return model, info


def count(model, img_rgb, diameter_px_=None,
          flow_threshold=DEFAULT_FLOW_THRESHOLD,
          cellprob_threshold=DEFAULT_CELLPROB_THRESHOLD,
          batch_size=8):
    """RGB uint8 HxWx3 → 实例标签图 (H,W) int32。

    直通上游 ``CellposeModel.eval``：percentile 1-99 归一化、256² tile、
    flow_threshold / cellprob_threshold 与合同对齐；``diameter_px_`` 为
    None 时走模型缺省尺度（cpsam：原生分辨率，rescale=1.0）。
    """
    masks, _, _ = model.eval(
        img_rgb, diameter=diameter_px_, flow_threshold=flow_threshold,
        cellprob_threshold=cellprob_threshold, normalize=True,
        resample=True, batch_size=batch_size)
    return np.asarray(masks).astype(np.int32, copy=False)
