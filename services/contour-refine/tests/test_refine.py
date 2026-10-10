# -*- coding: utf-8 -*-
"""contour-refine 单元测试（seg-venv 运行；不加载真实权重）。

覆盖 refine.py 纯逻辑（accept / low_score / area_ratio 大小两侧 / empty_mask /
泄漏被膨胀裁剪 / >500 点简化收敛 / too_few_points）与 app.py HTTP 契约
（healthz / 校验 400 / token 401 / 假 predictor 注入的成功路径）。

运行：cd services/contour-refine && <seg-venv>/bin/python -m pytest -q tests
opt-in 真实模型冒烟：SAM2_CHECKPOINT=<weights> 同上（见文件末尾）。
"""
import base64
import io
import json
import os
import time

import cv2
import numpy as np
import pytest
from PIL import Image

import refine
import app as service_app


# --------------------------------------------------------------------------- #
# 基建：合成图 + 假模型回调
# --------------------------------------------------------------------------- #
def _disk_mask(shape, center, radius):
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.circle(mask, (int(center[0]), int(center[1])), int(radius), 1, -1)
    return mask


def _rect_mask(shape, x0, y0, x1, y1):
    mask = np.zeros(shape[:2], dtype=np.uint8)
    mask[int(y0):int(y1), int(x0):int(x1)] = 1
    return mask


def _synth_img(shape=(256, 256)):
    """合成 RGB crop（中心一个深色椭圆「腺体」，浅色背景）。"""
    img = np.full((*shape, 3), (200, 190, 180), dtype=np.uint8)
    cv2.ellipse(img, (shape[1] // 2, shape[0] // 2),
                (shape[1] // 4, shape[0] // 4), 0, 0, 360, (90, 60, 50), -1)
    return img


def _rect_polygon(x0, y0, x1, y1):
    return [[float(x0), float(y0)], [float(x1), float(y0)],
            [float(x1), float(y1)], [float(x0), float(y1)]]


class _FakePredict:
    """可编程模型回调：mask/score 由用例设定，记录 (img_shape, box) 调用。"""

    def __init__(self, mask, score=1.0):
        self.mask = mask
        self.score = score
        self.calls = []

    def __call__(self, img, box):
        self.calls.append((tuple(img.shape), tuple(float(b) for b in box)))
        return self.mask, self.score


@pytest.fixture(autouse=True)
def _reset_model():
    service_app._MODEL.reset()
    yield
    service_app._MODEL.reset()


@pytest.fixture()
def client():
    service_app.app.config["TESTING"] = True
    return service_app.app.test_client()


def _png_b64(img_arr):
    buf = io.BytesIO()
    Image.fromarray(img_arr).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# refine.py 纯逻辑
# --------------------------------------------------------------------------- #
def test_accept_returns_simplified_points():
    img = _synth_img((256, 256))
    poly = _rect_polygon(60, 60, 196, 196)  # 紧贴椭圆
    mask = _disk_mask(img.shape, (128, 128), 68)
    result = refine.refine_crop(img, poly, _FakePredict(mask, score=0.95))
    assert result["accepted"] is True
    assert result["reason"] is None
    assert 0.3 <= result["area_ratio"] <= 1.3
    assert result["score"] == 0.95
    pts = result["points"]
    assert pts is not None and len(pts) >= 3 and len(pts) <= refine.MAX_POINTS
    assert all(isinstance(v, int) for p in pts for v in p)
    # 点应大致落在盘内（半径 68 + 容忍）
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert 128 - 75 <= min(xs) and max(xs) <= 128 + 75
    assert 128 - 75 <= min(ys) and max(ys) <= 128 + 75


def test_low_score_rejected():
    img = _synth_img()
    poly = _rect_polygon(60, 60, 196, 196)
    mask = _disk_mask(img.shape, (128, 128), 68)
    result = refine.refine_crop(img, poly, _FakePredict(mask, score=0.5))
    assert result["accepted"] is False
    assert result["reason"] == "low_score"
    assert result["points"] is None


def test_area_ratio_too_big_rejected():
    # 预测 mask 覆盖整图：与膨胀多边形相交后仍远大于多边形 → ratio > 1.3
    img = _synth_img((256, 256))
    poly = _rect_polygon(98, 98, 158, 158)  # 60x60，面积 3600
    full = np.ones(img.shape[:2], dtype=bool)
    result = refine.refine_crop(img, poly, _FakePredict(full, score=0.99))
    assert result["accepted"] is False
    assert result["reason"] == "area_ratio"
    assert result["area_ratio"] > 1.3
    assert result["points"] is None


def test_area_ratio_too_small_rejected():
    # 预测 mask 是多边形内一个小块（能扛过 5x5 开运算）→ ratio < 0.3
    img = _synth_img((256, 256))
    poly = _rect_polygon(60, 60, 196, 196)  # 136x136
    blob = _rect_mask(img.shape, 118, 118, 138, 138)  # 20x20
    result = refine.refine_crop(img, poly, _FakePredict(blob, score=0.99))
    assert result["accepted"] is False
    assert result["reason"] == "area_ratio"
    assert result["area_ratio"] < 0.3


def test_empty_mask_rejected():
    img = _synth_img()
    poly = _rect_polygon(60, 60, 196, 196)
    empty = np.zeros(img.shape[:2], dtype=bool)
    result = refine.refine_crop(img, poly, _FakePredict(empty, score=0.99))
    assert result["accepted"] is False
    assert result["reason"] == "empty_mask"


def test_leakage_clipped_by_dilation_but_accepted():
    # 预测 mask 越过多边形边界、但未越过膨胀半径：被裁剪且接受。
    # 多边形 100x100（面积 10000，r = max(3, int(0.1*100)) = 10 → 膨胀 120x120）；
    # 预测 130 宽 × 100 高的块（每侧越界 15px）→ 裁到 120x100 → ratio 1.2 ≤ 1.3。
    img = _synth_img((256, 256))
    poly = _rect_polygon(78, 78, 178, 178)
    leak = _rect_mask(img.shape, 63, 78, 193, 178)  # 130x100
    fake = _FakePredict(leak, score=0.9)
    result = refine.refine_crop(img, poly, fake)
    assert result["accepted"] is True, result
    assert result["reason"] is None
    assert 1.0 < result["area_ratio"] <= 1.3
    # 泄漏被膨胀边界裁掉：轮廓 x 范围不超过多边形 + 膨胀半径（10px）+ 简化容忍
    xs = [p[0] for p in result["points"]]
    assert max(xs) <= 178 + 10 + 2
    assert min(xs) >= 78 - 10 - 2
    # 传给模型的 box = 多边形 bbox
    assert fake.calls[0][1] == (78.0, 78.0, 178.0, 178.0)


def _star_mask(shape, n_spikes, r_out, r_in):
    """放射状星形 mask：边界高频摆动（能扛过 5x5 开运算），用于逼近点数超限。"""
    h, w = shape[:2]
    cx, cy = w // 2, h // 2
    angles = np.linspace(0.0, 2.0 * np.pi, n_spikes, endpoint=False)
    radii = np.where(np.arange(n_spikes) % 2 == 0, r_out, r_in)
    pts = np.stack([cx + radii * np.cos(angles), cy + radii * np.sin(angles)],
                   axis=1).astype(np.int32)
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 1)
    return mask


def test_over_500_points_epsilon_escalation(monkeypatch):
    # 把简化 epsilon 压到 0 → 星形边界初始逼近 > 500 点 → 逐级放大 epsilon 收敛
    monkeypatch.setattr(refine, "SIMPLIFY_EPS_MIN", 0.0)
    monkeypatch.setattr(refine, "SIMPLIFY_EPS_FRAC", 0.0)
    img = _synth_img((1400, 1400))
    poly = _rect_polygon(100, 100, 1300, 1300)
    mask = _star_mask(img.shape[:2], 1200, 560, 520)
    # 前置事实：epsilon=0 的初始逼近确实超过 500（走升级路径）
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    assert len(cv2.approxPolyDP(max(cs, key=cv2.contourArea), 0.0, True)) > 500
    result = refine.refine_crop(img, poly, _FakePredict(mask, score=0.9))
    assert result["accepted"] is True, result
    assert 3 <= len(result["points"]) <= 500


def test_too_few_points_after_collapse(monkeypatch):
    # epsilon 下限抬到形状尺度以上 → approxPolyDP 塌缩 < 3 点 → too_few_points
    monkeypatch.setattr(refine, "SIMPLIFY_EPS_MIN", 200.0)
    img = _synth_img((256, 256))
    poly = _rect_polygon(60, 60, 196, 196)
    mask = _disk_mask(img.shape, (128, 128), 68)
    result = refine.refine_crop(img, poly, _FakePredict(mask, score=0.95))
    assert result["accepted"] is False
    assert result["reason"] == "too_few_points"
    assert result["points"] is None


def test_predict_mask_shape_mismatch_raises():
    img = _synth_img((128, 128))
    poly = _rect_polygon(20, 20, 100, 100)
    bad = np.zeros((64, 64), dtype=bool)
    with pytest.raises(ValueError):
        refine.refine_crop(img, poly, _FakePredict(bad))


# --------------------------------------------------------------------------- #
# app.py HTTP 契约（假 predictor 注入）
# --------------------------------------------------------------------------- #
def _post_refine(client, image_b64, polygon, **headers):
    return client.post("/v1/refine",
                       json={"image_png_b64": image_b64, "polygon": polygon},
                       headers=headers)


def test_healthz_with_injected_predictor(client):
    service_app._MODEL.inject(lambda img, box: (np.zeros(img.shape[:2], bool), 0.0))
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"ok": True, "device": "injected", "model": "sam2.1-hiera-b+"}


def test_endpoint_accept_path(client):
    img = _synth_img((256, 256))
    poly = _rect_polygon(60, 60, 196, 196)
    mask = _disk_mask(img.shape, (128, 128), 68)

    def fake_predict(im, box):
        return mask, 0.95

    service_app._MODEL.inject(fake_predict)
    r = _post_refine(client, _png_b64(img), poly)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["accepted"] is True
    assert body["model"] == "sam2.1-hiera-b+"
    assert isinstance(body["seconds"], float) and body["seconds"] >= 0.0
    assert 3 <= len(body["points"]) <= 500


def test_endpoint_rejection_passes_service_reason(client):
    img = _synth_img()
    poly = _rect_polygon(60, 60, 196, 196)
    service_app._MODEL.inject(lambda im, box: (np.zeros(im.shape[:2], bool), 0.99))
    r = _post_refine(client, _png_b64(img), poly)
    assert r.status_code == 200
    body = r.get_json()
    assert body["accepted"] is False
    assert body["reason"] == "empty_mask"
    assert body["points"] is None


def test_endpoint_validation_400s(client):
    service_app._MODEL.inject(lambda im, box: (np.zeros(im.shape[:2], bool), 0.0))
    img_b64 = _png_b64(_synth_img((64, 64)))
    ok_poly = _rect_polygon(10, 10, 50, 50)
    # 非 JSON / 非对象
    assert client.post("/v1/refine", data="not-json",
                       content_type="application/json").status_code == 400
    assert _post_refine(client, img_b64, ok_poly).status_code == 200  # 基线可用
    # 坏 base64
    r = _post_refine(client, "!!!not-base64!!!", ok_poly)
    assert r.status_code == 400 and "error" in r.get_json()
    # 非图像字节
    r = _post_refine(client, base64.b64encode(b"garbage").decode(), ok_poly)
    assert r.status_code == 400 and "error" in r.get_json()
    # 图像超长边
    big = _png_b64(_synth_img((2049, 2049)))
    r = _post_refine(client, big, ok_poly)
    assert r.status_code == 400 and "error" in r.get_json()
    # polygon 点数不足 / 过多
    r = _post_refine(client, img_b64, [[0, 0], [10, 10]])
    assert r.status_code == 400 and "error" in r.get_json()
    r = _post_refine(client, img_b64, [[0, 0], [1, 1], [2, 2]] * 200)
    assert r.status_code == 400 and "error" in r.get_json()
    # 非有限坐标（NaN / 字符串）
    r = client.post("/v1/refine",
                    data=json.dumps({"image_png_b64": img_b64,
                                     "polygon": [[0, 0], [float("nan"), 5], [9, 9]]}),
                    content_type="application/json")
    assert r.status_code == 400 and "error" in r.get_json()
    r = _post_refine(client, img_b64, [[0, 0], ["x", 5], [9, 9]])
    assert r.status_code == 400 and "error" in r.get_json()
    # 缺字段
    r = client.post("/v1/refine", json={"polygon": ok_poly})
    assert r.status_code == 400


def test_endpoint_body_size_limit(client, monkeypatch):
    monkeypatch.setattr(service_app, "MAX_BODY_BYTES", 1024 * 1024)
    service_app.app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    big = "A" * (2 * 1024 * 1024)
    r = client.post("/v1/refine", data=big, content_type="application/json")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_endpoint_token_auth(client, monkeypatch):
    monkeypatch.setenv("CONTOUR_REFINE_TOKEN", "sekrit-token-1")
    img = _synth_img((64, 64))
    poly = _rect_polygon(10, 10, 50, 50)
    service_app._MODEL.inject(lambda im, box: (np.zeros(im.shape[:2], bool), 0.0))
    b64 = _png_b64(img)
    # 未带 / 错 token → 401
    r = _post_refine(client, b64, poly)
    assert r.status_code == 401 and "error" in r.get_json()
    r = _post_refine(client, b64, poly, **{"X-Refine-Token": "wrong"})
    assert r.status_code == 401
    # 正确 token → 通过（业务上 empty_mask 拒绝，但 HTTP 200）
    r = _post_refine(client, b64, poly, **{"X-Refine-Token": "sekrit-token-1"})
    assert r.status_code == 200
    # healthz 不需要 token
    assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# opt-in 真实模型冒烟（SAM2_CHECKPOINT 设置时才运行；CPU 约几十秒）
# --------------------------------------------------------------------------- #
_REAL_SLIDE = "/home/solarise/pytest-tmp/drawing-eval/uploads/objects/sld_HjhjIvq42hcU/data.tif"
_REAL_CROP = (5890, 5340, 210, 190)  # level-0 bbox（导管）


@pytest.mark.skipif(not os.environ.get("SAM2_CHECKPOINT"),
                    reason="SAM2_CHECKPOINT not set (opt-in real-model smoke)")
def test_real_model_duct_smoke():
    import openslide

    slide = openslide.OpenSlide(_REAL_SLIDE)
    region = slide.read_region((_REAL_CROP[0], _REAL_CROP[1]), 0,
                               (_REAL_CROP[2], _REAL_CROP[3])).convert("RGB")
    img = np.asarray(region, dtype=np.uint8)
    # 松多边形：围住导管主体、留余量（不贴边；比紧轮廓大 ~25%）
    polygon = [[35.0, 30.0], [180.0, 25.0], [185.0, 175.0], [30.0, 170.0]]
    t0 = time.time()
    result = refine.refine_crop(img, polygon, service_app._MODEL.ensure())
    dt = time.time() - t0
    print("\nreal-model smoke: %.2fs accepted=%s score=%.3f ratio=%.3f points=%d"
          % (dt, result["accepted"], result["score"], result["area_ratio"],
             len(result["points"] or [])))
    assert result["accepted"] is True, result
    assert 3 <= len(result["points"]) <= 500
    assert all(isinstance(v, int) for p in result["points"] for v in p)
