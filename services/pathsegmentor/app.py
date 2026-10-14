# -*- coding: utf-8 -*-
"""pathsegmentor HTTP 服务：PathSegmentor（上游 e67763a）自然语言提示分割。

端点（127.0.0.1:8971，--network host，不对外）：
  GET  /healthz   → 200 ``{ok: true, pid, ready}``（进程存活即 200，
                    **不反映模型就绪**——就绪看 /readyz）；
  GET  /readyz    → 200 ``{ready, model_id, model_sha256, code_rev, device}``
                    模型已加载且 warm-up 推理完成；否则 503 ``{ready:false}``；
  POST /v1/segment``{image_b64, object, region?, threshold?}`` →
     200 ``{model_id, model_sha256, code_rev, prompt_used, input_width,
     input_height, mask_png_b64, positive_pixels, threshold, timings_ms,
     warnings}``；
     错误：400 ``{error, message}``（坏输入）/ 503 ``{error:"busy"}``（已有
     一路推理在跑，不排队）/ 503 ``{error:"not_ready"}``（模型未就绪）。

要点（风格对齐 services/contour-refine）：
  - 模型在 **worker 进程内**加载（gunicorn 不用 --preload——CUDA 上下文
    绝不能在 master fork 后继承；post_worker_init 幂等 warm-up：加载 +
    一路 1024² 假图推理，PATHSEG_EAGER_LOAD=0 可关 import 期加载供测试）；
  - ``_INFERENCE_LOCK`` 单飞 GPU 推理：非阻塞获取，占用即 503 busy，
    不排队（在锁上等待会占住 gunicorn 线程直到调用方超时）；
  - 入参校验：image ≤ 2048×2048（**先读图片头拒超大图，再解码像素**——
    8 MiB 压缩体不约束解压后像素，R2；模型内部固定 resize 到 1024²，无
    第二次放大，故除入口闸外无额外资源准入）、请求体 ≤ 8MB、object 必填、
    threshold 0..1；RGBA/灰度统一转 RGB；
  - 请求体与图像**不落盘、不进日志**（日志只有状态码与耗时）；
  - 权重 PATHSEG_WEIGHTS（缺省 /models/model_state_dict.pt，只读挂载），
    启动时流式校验 SHA-256（PATHSEG_EXPECTED_SHA256，不符拒绝加载）。
"""
import base64
import binascii
import io
import os
import threading
import time

import numpy as np
from flask import Flask, jsonify, request
from PIL import Image

import pathseg_infer

MODEL_ID = "pathsegmentor"
CODE_REV = pathseg_infer.UPSTREAM_COMMIT
PATHSEG_WEIGHTS = os.environ.get("PATHSEG_WEIGHTS") or "/models/model_state_dict.pt"
PATHSEG_EXPECTED_SHA256 = (os.environ.get("PATHSEG_EXPECTED_SHA256") or "").strip().lower()
MAX_IMAGE_EDGE = int(os.environ.get("PATHSEG_MAX_IMAGE_EDGE") or 2048)
MAX_BODY_BYTES = int(os.environ.get("PATHSEG_MAX_BODY_BYTES") or 8 * 1024 * 1024)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

# 推理互斥锁：单模型实例串行推理；非阻塞获取，占用即 503 busy。
_INFERENCE_LOCK = threading.Lock()


class _ModelHolder:
    """单模型持有者：worker 内加载一次 + 测试注入口（对齐 contour-refine）。

    持有 ``segment(img_rgb, prompt, threshold) -> mask_bool`` 回调与加载信息
    （sha256/weights_only/device）。``ready`` 仅在本 pid 内已加载并完成
    warm-up 时为 True；加载失败记忆错误（稳定 503，不反复重试）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._segment = None
        self._info = None
        self._error = None
        self._pid = None
        self._warmed = False

    def inject(self, segment, info=None):
        """测试注入口：替换真实模型（不加载权重），直接视为已 warm。"""
        with self._lock:
            self._segment = segment
            self._info = info or {"sha256": "0" * 64, "weights_only": True,
                                  "device": "injected"}
            self._error = None
            self._pid = os.getpid()
            self._warmed = True

    def reset(self):
        with self._lock:
            self._segment = None
            self._info = None
            self._error = None
            self._pid = None
            self._warmed = False

    def ensure(self):
        """返回 segment 回调；首次调用时加载模型（进程内只加载一次）。"""
        with self._lock:
            if self._segment is not None:
                return self._segment
            if self._error is not None:
                raise RuntimeError("model load previously failed")
            try:
                import torch  # noqa: F401 — 确认 torch 可用后再走上游链路

                model, info = pathseg_infer.load_model(
                    PATHSEG_WEIGHTS, PATHSEG_EXPECTED_SHA256 or None)
                device = next(model.parameters()).device

                def _segment(img_rgb, prompt, threshold):
                    return pathseg_infer.segment(model, img_rgb, prompt,
                                                 threshold)

                self._segment = _segment
                self._info = info
                self._pid = os.getpid()
                app.logger.info(
                    "PathSegmentor loaded: pid=%d device=%s weights=%s "
                    "sha256=%s weights_only=%s code_rev=%s", os.getpid(),
                    info["device"], PATHSEG_WEIGHTS, info["sha256"][:12],
                    info["weights_only"], CODE_REV)
                return _segment
            except Exception:  # noqa: BLE001 — 记忆失败，稳定 503
                self._error = "PathSegmentor load failed (weights=%s)" % PATHSEG_WEIGHTS
                app.logger.exception("PathSegmentor 加载失败")
                raise

    def warm_up(self):
        """幂等 warm-up：1024² 假图跑通一路推理（只做一次；持推理锁）。"""
        with self._lock:
            if self._warmed or self._segment is None:
                return
            img = np.zeros((1024, 1024, 3), dtype=np.uint8)
            prompt, _, _, _ = pathseg_infer.build_prompt("nuclei")
            self._segment(img, prompt, 0.5)
            self._warmed = True
            app.logger.info("PathSegmentor warm-up done: pid=%d", os.getpid())

    @property
    def info(self):
        return self._info

    @property
    def ready(self):
        """本 pid 内已加载且 warm-up 完成（不触发加载）。"""
        return (self._segment is not None and self._warmed
                and self._pid == os.getpid())


_MODEL = _ModelHolder()


def _bad_request(code, message):
    return jsonify({"error": code, "message": message}), 400


@app.errorhandler(413)
def _request_too_large(_exc):
    # 契约统一 400 {error, message}（spec：请求体 ≤ 8MB）
    return _bad_request("body_too_large",
                        "request body exceeds %d bytes" % MAX_BODY_BYTES)


@app.errorhandler(500)
def _internal_error(_exc):  # pragma: no cover — Flask 兜底
    return jsonify({"error": "internal_error", "message": "internal error"}), 500


@app.route("/healthz", methods=["GET"])
def healthz():
    """进程存活即 200（模型就绪状态另见 /readyz）。"""
    return jsonify({"ok": True, "pid": os.getpid(), "ready": _MODEL.ready})


@app.route("/readyz", methods=["GET"])
def readyz():
    """模型已加载且 warm-up 完成 → 200；否则 503。"""
    if _MODEL.ready:
        info = _MODEL.info or {}
        return jsonify({"ready": True, "model_id": MODEL_ID,
                        "model_sha256": info.get("sha256"),
                        "code_rev": CODE_REV,
                        "device": info.get("device")})
    return jsonify({"ready": False, "error": "not_ready",
                    "model_id": MODEL_ID, "code_rev": CODE_REV}), 503


def _decode_image(image_b64):
    """base64 → PIL RGB 图像；返回 (错误元组或 None, 图像或 None)。

    尺寸闸在**像素解码前**（R2）：``Image.open`` 只读文件头，先按头部的
    两轴与像素总量拒绝超大图，再 ``load()`` 解码像素——否则低压缩体积的
    大图会在被拒绝前白白完成整幅解码。
    """
    if not isinstance(image_b64, str) or not image_b64:
        return ("missing_image",
                "image_b64 must be a non-empty base64 string"), None
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return ("invalid_base64", "image_b64 is not valid base64"), None
    if not raw:
        return ("invalid_base64", "image_b64 decodes to zero bytes"), None
    try:
        image = Image.open(io.BytesIO(raw))
    except Exception:  # noqa: BLE001 — PIL 对坏数据的各类异常统一 400
        return ("image_not_decodable",
                "image_b64 is not a decodable image"), None
    width, height = image.size  # 仅头部；像素尚未解码
    if (width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE
            or width * height > MAX_IMAGE_EDGE * MAX_IMAGE_EDGE):
        return ("image_too_large",
                "image %dx%d exceeds %dx%d limit"
                % (width, height, MAX_IMAGE_EDGE, MAX_IMAGE_EDGE)), None
    try:
        image.load()
    except Exception:  # noqa: BLE001 — 头部合法但像素数据坏（如截断 JPEG）
        return ("image_not_decodable",
                "image_b64 is not a decodable image"), None
    return None, image.convert("RGB")


def _validate_request(body):
    """JSON 字段校验 → (error_tuple, object, region, threshold)。"""
    object_phrase = body.get("object")
    if object_phrase is None:
        return (("missing_object", "object is required"), None, None, None)
    if not isinstance(object_phrase, str) or not object_phrase.strip():
        return (("invalid_object", "object must be a non-empty string"),
                None, None, None)
    region = body.get("region")
    if region is not None and (not isinstance(region, str)
                               or not region.strip()):
        return (("invalid_region",
                 "region must be a non-empty string when provided"),
                None, None, None)
    threshold = body.get("threshold")
    if threshold is None:
        threshold = 0.5
    else:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not np.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            return (("invalid_threshold",
                     "threshold must be a number in [0, 1]"), None, None, None)
    return (None, object_phrase, region, float(threshold))


@app.route("/v1/segment", methods=["POST"])
def v1_segment():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad_request("invalid_json", "request body must be a JSON object")

    err, image = _decode_image(body.get("image_b64"))
    if err is not None:
        return _bad_request(err[0], err[1])
    err, object_phrase, region, threshold = _validate_request(body)
    if err is not None:
        return _bad_request(err[0], err[1])

    try:
        prompt, _, _, _ = pathseg_infer.build_prompt(object_phrase, region)
    except ValueError as exc:
        return _bad_request("invalid_object", str(exc))

    try:
        segment = _MODEL.ensure()
    except Exception:  # noqa: BLE001
        return jsonify({"error": "not_ready",
                        "message": "model not ready (see server logs)"}), 503
    if not _MODEL.ready:
        return jsonify({"error": "not_ready",
                        "message": "model still warming up"}), 503
    if not _INFERENCE_LOCK.acquire(blocking=False):
        return jsonify({"error": "busy"}), 503

    started = time.monotonic()
    try:
        img = np.asarray(image, dtype=np.uint8)
        decoded_s = time.monotonic()
        mask = segment(img, prompt, threshold)
        inferred_s = time.monotonic()
        mask_png = pathseg_infer.encode_mask_png(mask)
    except Exception:  # noqa: BLE001
        app.logger.exception("segment failed unexpectedly")
        return jsonify({"error": "internal_error",
                        "message": "inference failed (see server logs)"}), 500
    finally:
        _INFERENCE_LOCK.release()

    total_ms = (time.monotonic() - started) * 1000.0
    info = _MODEL.info or {}
    warnings = []
    if info.get("weights_only") is False:
        warnings.append(
            "weights loaded with torch.load(weights_only=False); the "
            "checkpoint is not a plain tensor state_dict")
    return jsonify({
        "model_id": MODEL_ID,
        "model_sha256": info.get("sha256"),
        "code_rev": CODE_REV,
        "prompt_used": prompt,
        "input_width": int(image.size[0]),
        "input_height": int(image.size[1]),
        "mask_png_b64": base64.b64encode(mask_png).decode("ascii"),
        "positive_pixels": int(np.count_nonzero(mask)),
        "threshold": threshold,
        "timings_ms": {
            "decode": round((decoded_s - started) * 1000.0, 1),
            "infer": round((inferred_s - decoded_s) * 1000.0, 1),
            "total": round(total_ms, 1),
        },
        "warnings": warnings,
    })


# 启动即加载 + warm-up（python app.py 直跑在 import 期执行；gunicorn 无
# --preload，import 发生在 worker 进程内；gunicorn.conf.py 的
# post_worker_init 会再做幂等 warm-up。测试用 PATHSEG_EAGER_LOAD=0 关闭。）
if os.environ.get("PATHSEG_EAGER_LOAD", "1") != "0":
    try:
        _MODEL.ensure()
        _MODEL.warm_up()
    except Exception:  # noqa: BLE001 — 不阻断进程启动，/readyz 报告 503
        app.logger.error("startup load failed; /readyz will report not ready")


if __name__ == "__main__":  # 本地开发直跑（生产走 gunicorn，见 Containerfile）
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT") or 8971),
            debug=False, threaded=True)
