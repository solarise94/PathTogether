# -*- coding: utf-8 -*-
"""contour-refine HTTP 服务：SAM2.1（hiera base+）box-prompted 轮廓精修。

端点：
  GET  /healthz   → ``{ok, device, model, pid}``
     （**不触发加载**：本 worker 进程内模型未就绪/加载失败 → 503 ``{ok:false, ...}``）
  POST /v1/refine → ``{image_png_b64, polygon}``（polygon 为 crop 像素坐标）→
     ``{accepted, reason, score, area_ratio, points, model, seconds}``；
     另一推理进行中且排队等待超过 CONTOUR_REFINE_QUEUE_WAIT_S → 503 ``{error:"busy"}``

要点：
  - 模型在 **worker 进程内**加载（gunicorn 不用 --preload；gunicorn.conf.py 的
    post_worker_init 幂等 warm-up，python app.py 直跑时 import 期加载不变；
    CONTOUR_REFINE_EAGER_LOAD=0 可改为惰性，测试用）——CUDA 上下文绝不能在
    master 创建后被 fork 继承（PyTorch poison-fork 约束）；
  - 推理互斥锁串行化（单实例 SAM2ImagePredictor 的 set_image+predict 不并发），
    且排队等待有上界（见上 busy）——调用方 8 s 超时后的计算不应在后台堆积；
  - 权重路径 SAM2_CHECKPOINT（缺省 /models/sam2.1_hiera_base_plus.pt）、配置
    SAM2_CONFIG（缺省 configs/sam2.1/sam2.1_hiera_b+.yaml）；
  - 可选共享密钥：设置 CONTOUR_REFINE_TOKEN 后请求须带 X-Refine-Token 头
    （常数时间比较），否则 401；
  - 入参校验：polygon 3..500 个有限数值点、图像长边 ≤ 2048、请求体 ≤ 8MB
    （超限统一 400 ``{error}``）。
"""
import base64
import binascii
import hmac
import io
import logging
import math
import os
import threading
import time

import numpy as np
from flask import Flask, jsonify, request
from PIL import Image

import refine

MODEL_ID = "sam2.1-hiera-b+"
SAM2_CHECKPOINT = os.environ.get("SAM2_CHECKPOINT") or "/models/sam2.1_hiera_base_plus.pt"
SAM2_CONFIG = os.environ.get("SAM2_CONFIG") or "configs/sam2.1/sam2.1_hiera_b+.yaml"
MAX_IMAGE_EDGE = int(os.environ.get("CONTOUR_REFINE_MAX_IMAGE_EDGE") or 2048)
MAX_BODY_BYTES = int(os.environ.get("CONTOUR_REFINE_MAX_BODY_BYTES") or 8 * 1024 * 1024)
MIN_POLYGON_POINTS = 3
MAX_POLYGON_POINTS = 500
try:
    QUEUE_WAIT_S = float(os.environ.get("CONTOUR_REFINE_QUEUE_WAIT_S") or 2)
except (TypeError, ValueError):
    QUEUE_WAIT_S = 2.0

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

# 推理互斥锁：单模型实例（gunicorn 单 worker 多线程时保护 set_image+predict
# 成对执行）。/v1/refine 以 acquire(timeout=QUEUE_WAIT_S) 获取——排队等待超过
# 上界即 503 busy，不无限排队（调用方 8 s 超时后不应在后台继续堆积计算）。
_INFERENCE_LOCK = threading.Lock()


class _ModelHolder:
    """单 SAM2 predictor 持有者：worker 内加载一次 + 测试注入口。

    持有的是 ``predict(img, box) -> (mask_bool, score)`` 回调（真实路径闭包
    包住 SAM2ImagePredictor；推理串行由 /v1/refine 持 _INFERENCE_LOCK 保证，
    回调本身不再加锁——直接调 refine.refine_crop 的离线路径不受影响）。
    device 随加载确定（cuda 可用即 cuda，否则 cpu）；记录加载进程 pid，
    ``ready`` 只在本进程内已加载时为 True（fork 继承的 holder 不可信）。
    加载失败记忆错误（稳定 503，不反复重试砸日志）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._predict = None
        self._device = None
        self._error = None
        self._pid = None

    def inject(self, predict, device="injected"):
        """测试注入口：以假 predict 替换真实模型（不加载权重）。"""
        with self._lock:
            self._predict = predict
            self._device = device
            self._error = None
            self._pid = os.getpid()

    def reset(self):
        with self._lock:
            self._predict = None
            self._device = None
            self._error = None
            self._pid = None

    def ensure(self):
        """返回 predict 回调；首次调用时加载 SAM2（进程内只加载一次）。"""
        with self._lock:
            if self._predict is not None:
                return self._predict
            if self._error is not None:
                raise RuntimeError("model load previously failed")
            try:
                import torch
                from sam2.build_sam import build_sam2
                from sam2.sam2_image_predictor import SAM2ImagePredictor

                device = "cuda" if torch.cuda.is_available() else "cpu"
                predictor = SAM2ImagePredictor(
                    build_sam2(SAM2_CONFIG, SAM2_CHECKPOINT, device=device))

                def _predict(img, box):
                    with torch.inference_mode():
                        predictor.set_image(img)
                        masks, scores, _ = predictor.predict(
                            box=np.asarray(box, dtype=np.float32),
                            multimask_output=False)
                    return masks[0] > 0, float(scores[0])

                self._predict = _predict
                self._device = device
                self._pid = os.getpid()
                app.logger.info("SAM2 loaded: pid=%d device=%s checkpoint=%s "
                                "config=%s",
                                os.getpid(), device, SAM2_CHECKPOINT, SAM2_CONFIG)
                return _predict
            except Exception:  # noqa: BLE001 — 记忆失败，healthz/refine 稳定 503
                self._error = "SAM2 load failed (checkpoint=%s)" % SAM2_CHECKPOINT
                app.logger.exception("SAM2 加载失败")
                raise

    @property
    def device(self):
        return self._device

    @property
    def ready(self):
        """本进程内模型是否已加载（不触发加载；fork 继承的实例不算就绪）。"""
        return self._predict is not None and self._pid == os.getpid()


_MODEL = _ModelHolder()


def _bad_request(message):
    return jsonify({"error": message}), 400


def _check_token():
    """可选共享密钥校验：CONTOUR_REFINE_TOKEN 设置时要求 X-Refine-Token 头。"""
    expected = (os.environ.get("CONTOUR_REFINE_TOKEN") or "").strip()
    if not expected:
        return None
    submitted = request.headers.get("X-Refine-Token") or ""
    if not hmac.compare_digest(submitted.encode("utf-8"),
                               expected.encode("utf-8")):
        return jsonify({"error": "invalid or missing X-Refine-Token"}), 401
    return None


def _validate_polygon(polygon):
    """polygon 校验（3..500 个 [x, y] 有限数值点）→ 错误消息或 None。"""
    if not isinstance(polygon, (list, tuple)):
        return "polygon must be a list of [x, y] points"
    if not (MIN_POLYGON_POINTS <= len(polygon) <= MAX_POLYGON_POINTS):
        return ("polygon must have %d..%d points, got %d"
                % (MIN_POLYGON_POINTS, MAX_POLYGON_POINTS, len(polygon)))
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return "each polygon point must be a pair [x, y]"
        for value in point:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "polygon coordinates must be numbers"
            if not math.isfinite(value):
                return "polygon coordinates must be finite"
    return None


@app.errorhandler(413)
def _request_too_large(_exc):
    # 契约统一 400 {error}（spec：body size limit ~8MB → 400）
    return _bad_request("request body exceeds %d bytes" % MAX_BODY_BYTES)


@app.route("/healthz", methods=["GET"])
def healthz():
    """只读本 worker 进程的就绪状态（不触发加载）。

    pid + device 让健康检查能区分「本 worker 已就绪」与「fork 继承的旧
    holder」；模型未加载/加载失败均 503（加载由 post_worker_init warm-up
    或首个 /v1/refine 惰性完成）。
    """
    ready = _MODEL.ready
    payload = {"ok": ready, "device": _MODEL.device, "model": MODEL_ID,
               "pid": os.getpid()}
    if not ready:
        payload["error"] = "model not ready in this worker (loading or failed)"
        return jsonify(payload), 503
    return jsonify(payload)


@app.route("/v1/refine", methods=["POST"])
def v1_refine():
    token_err = _check_token()
    if token_err is not None:
        return token_err
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad_request("request body must be a JSON object")

    image_b64 = body.get("image_png_b64")
    polygon = body.get("polygon")
    if not isinstance(image_b64, str) or not image_b64:
        return _bad_request("image_png_b64 must be a non-empty base64 string")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return _bad_request("image_png_b64 is not valid base64")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:  # noqa: BLE001 — PIL 对坏数据的各类异常统一 400
        return _bad_request("image_png_b64 is not a decodable image")
    long_edge = max(image.size)
    if long_edge > MAX_IMAGE_EDGE:
        return _bad_request(
            "image long edge %d px exceeds limit %d px" % (long_edge, MAX_IMAGE_EDGE))
    polygon_err = _validate_polygon(polygon)
    if polygon_err is not None:
        return _bad_request(polygon_err)

    try:
        predict = _MODEL.ensure()
    except Exception:  # noqa: BLE001
        return jsonify({"error": "model not ready (see server logs)"}), 503

    # 排队上界：另一推理进行中时最多等 QUEUE_WAIT_S，超时 503 busy——
    # 调用方（PT，8 s 超时）不应让已放弃的请求在后台继续排队计算。
    if not _INFERENCE_LOCK.acquire(timeout=QUEUE_WAIT_S):
        return jsonify({"error": "busy"}), 503
    img = np.asarray(image.convert("RGB"), dtype=np.uint8)
    started = time.monotonic()
    try:
        result = refine.refine_crop(img, polygon, predict)
    except ValueError as exc:
        return _bad_request(str(exc))
    except Exception:  # noqa: BLE001
        app.logger.exception("refine failed unexpectedly")
        return jsonify({"error": "internal error"}), 500
    finally:
        _INFERENCE_LOCK.release()
    seconds = round(time.monotonic() - started, 4)

    out = dict(result)
    out["model"] = MODEL_ID
    out["seconds"] = seconds
    return jsonify(out)


# 启动即加载一次（python app.py 直跑在 import 期执行；gunicorn 无 --preload，
# import 发生在 worker 进程内 → 即 worker 内加载；测试用 EAGER_LOAD=0 关闭。
# gunicorn.conf.py 的 post_worker_init 会再做一次幂等 warm-up。）
if os.environ.get("CONTOUR_REFINE_EAGER_LOAD", "1") != "0":
    try:
        _MODEL.ensure()
    except Exception:  # noqa: BLE001 — 不阻断进程启动，/healthz 反映 503
        app.logger.error("startup SAM2 load failed; /healthz will report not ok")


if __name__ == "__main__":  # 本地开发直跑（生产走 gunicorn，见 Containerfile）
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or 8970),
            debug=False, threaded=True)
