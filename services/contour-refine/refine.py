# -*- coding: utf-8 -*-
"""轮廓精修核心算法（纯逻辑，不依赖 torch/sam2）。

输入：RGB crop（HxWx3 uint8）、crop 像素坐标下的 LLM 多边形、模型回调
``predict(img, box) -> (mask_bool, score)``（box = 多边形 bbox，crop 像素）。
输出：``{accepted, reason, score, area_ratio, points}``（points 为 crop 像素
``[[x, y], ...]`` 整数点；拒绝时为 None）。

算法（离线原型 gate_sam2.py / refine_proto.py 的服务化收敛）：
  1. box = LLM 多边形 bbox → 模型 box prompt；
  2. mask = 预测 mask ∩（LLM 多边形 mask 以椭圆核膨胀
     r = max(3, 0.10*sqrt(polygon_area))）——容忍紧邻泄漏、拒绝远端漂移；
  3. 保留最大 8-连通分量；5x5 形态学开运算去毛刺；
  4. ratio = mask_area / polygon_area；接受 iff score >= SCORE_MIN 且
     RATIO_MIN <= ratio <= RATIO_MAX（扩散区域分数/比例不达标 → 保留 LLM 原轮廓）；
  5. 轮廓 = 最大外轮廓 → approxPolyDP(epsilon = max(1.0, 0.006*perimeter))；
     点数 > MAX_POINTS 时逐级放大 epsilon（每轮 ×1.5 +1）直到 ≤ MAX_POINTS；
     最终 < 3 点（过度收缩）→ 拒绝 too_few_points。

阈值均为模块常量，可用 CONTOUR_REFINE_* 环境变量覆盖（import 时读取；
测试可 monkeypatch 模块属性——函数体每次调用时读取全局）。
"""
import os

import cv2
import numpy as np

#: 接受分数下限（SAM2 box prompt 置信度）
SCORE_MIN = float(os.environ.get("CONTOUR_REFINE_SCORE_MIN") or 0.75)
#: 面积比下限（mask_area / polygon_area）
RATIO_MIN = float(os.environ.get("CONTOUR_REFINE_RATIO_MIN") or 0.3)
#: 面积比上限
RATIO_MAX = float(os.environ.get("CONTOUR_REFINE_RATIO_MAX") or 1.3)
#: 泄漏容忍半径系数（× sqrt(polygon_area)）
DILATION_FRAC = float(os.environ.get("CONTOUR_REFINE_DILATION_FRAC") or 0.10)
#: 泄漏容忍最小半径（px）
DILATION_MIN_RADIUS = int(os.environ.get("CONTOUR_REFINE_DILATION_MIN_RADIUS") or 3)
#: 形态学开运算核边长（px）
OPEN_KERNEL_SIZE = int(os.environ.get("CONTOUR_REFINE_OPEN_KERNEL") or 5)
#: 多边形简化 epsilon 下限（px）
SIMPLIFY_EPS_MIN = float(os.environ.get("CONTOUR_REFINE_SIMPLIFY_EPS_MIN") or 1.0)
#: 多边形简化 epsilon 系数（× perimeter）
SIMPLIFY_EPS_FRAC = float(os.environ.get("CONTOUR_REFINE_SIMPLIFY_EPS_FRAC") or 0.006)
#: 输出点数上限（超过则放大 epsilon 收敛）
MAX_POINTS = int(os.environ.get("CONTOUR_REFINE_MAX_POINTS") or 500)
#: epsilon 升级轮数上限（eps ≥ perimeter 时 approxPolyDP 必然塌缩，此为保险）
MAX_EPS_GROWTH_ROUNDS = 64

#: 拒绝原因枚举（对外契约字符串）
REASON_LOW_SCORE = "low_score"
REASON_AREA_RATIO = "area_ratio"
REASON_EMPTY_MASK = "empty_mask"
REASON_TOO_FEW_POINTS = "too_few_points"


def polygon_bbox(polygon):
    """多边形 bbox → ``[x0, y0, x1, y1]``（float，crop 像素）。"""
    pts = np.asarray(polygon, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 1 or pts.shape[1] != 2:
        raise ValueError("polygon must be a non-empty list of [x, y] points")
    mn, mx = pts.min(axis=0), pts.max(axis=0)
    return [float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1])]


def polygon_mask(shape, polygon):
    """LLM 多边形 → 二值 mask（uint8 0/1，与 img 同高宽）。"""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    pts = np.round(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
    cv2.fillPoly(mask, [pts], 1)
    return mask


def _reject(reason, score, ratio):
    return {"accepted": False, "reason": reason, "score": float(score),
            "area_ratio": float(ratio), "points": None}


def simplify_contour(mask):
    """最大外轮廓 → approxPolyDP 简化点列表（crop 像素整数点）。

    epsilon = max(SIMPLIFY_EPS_MIN, SIMPLIFY_EPS_FRAC * perimeter)；点数超过
    MAX_POINTS 时逐级放大 epsilon 重试（收敛到 ≤ MAX_POINTS；塌缩到 < 3 点
    返回 None）。
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    eps = max(SIMPLIFY_EPS_MIN, SIMPLIFY_EPS_FRAC * perimeter)
    approx = cv2.approxPolyDP(contour, eps, True)
    rounds = 0
    while len(approx) > MAX_POINTS and rounds < MAX_EPS_GROWTH_ROUNDS:
        eps = eps * 1.5 + 1.0
        approx = cv2.approxPolyDP(contour, eps, True)
        rounds += 1
    if len(approx) < 3 or len(approx) > MAX_POINTS:
        return None
    return [[int(x), int(y)] for x, y in approx[:, 0, :]]


def refine_crop(img, polygon, predict):
    """精修主体（纯逻辑；predict 为模型回调，见模块 docstring）。

    img: HxWx3 uint8 RGB；polygon: crop 像素 [[x, y], ...]；
    predict(img, box) -> (mask_bool HxW, score float)。
    异常（形状不符）向上抛 ValueError；业务拒绝走返回值。
    """
    if not isinstance(img, np.ndarray) or img.ndim != 3 or img.shape[2] != 3 \
            or img.dtype != np.uint8:
        raise ValueError("img must be an HxWx3 uint8 array")
    height, width = img.shape[:2]
    box = polygon_bbox(polygon)

    mask_pred, score = predict(img, box)
    mask_pred = np.asarray(mask_pred).astype(bool)
    if mask_pred.shape != (height, width):
        raise ValueError("predict returned mask of shape %r, expected %r"
                         % (mask_pred.shape, (height, width)))
    score = float(score)

    llm = polygon_mask(img.shape, polygon)
    poly_area = int(llm.sum())
    radius = max(DILATION_MIN_RADIUS,
                 int(DILATION_FRAC * np.sqrt(poly_area)))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    allow = cv2.dilate(llm, kernel)
    mask = (mask_pred & (allow > 0)).astype(np.uint8)
    if mask.sum() == 0:
        return _reject(REASON_EMPTY_MASK, score, 0.0)

    # 最大 8-连通分量（远端碎块丢弃）
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = (labels == largest).astype(np.uint8)
    # 5x5 开运算去毛刺（可能清空细碎结构 → empty_mask）
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        np.ones((OPEN_KERNEL_SIZE, OPEN_KERNEL_SIZE), np.uint8))
    if mask.sum() == 0:
        return _reject(REASON_EMPTY_MASK, score, 0.0)

    mask_area = int(mask.sum())
    ratio = mask_area / max(1.0, float(poly_area))
    if score < SCORE_MIN:
        return _reject(REASON_LOW_SCORE, score, ratio)
    if not (RATIO_MIN <= ratio <= RATIO_MAX):
        return _reject(REASON_AREA_RATIO, score, ratio)

    points = simplify_contour(mask)
    if points is None:
        return _reject(REASON_TOO_FEW_POINTS, score, ratio)
    return {"accepted": True, "reason": None, "score": score,
            "area_ratio": float(ratio), "points": points}
