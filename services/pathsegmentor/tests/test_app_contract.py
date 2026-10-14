# -*- coding: utf-8 -*-
"""HTTP 契约测试（无 GPU/权重）：注入假 segmenter 验证路由、校验、并发。"""
import base64
import io
import threading

import numpy as np
import pytest
from PIL import Image

import app


@pytest.fixture()
def client():
    """注入恒定假掩码（左上 1/4 亮）的 segmenter，返回 flask 测试客户端。"""
    app._MODEL.reset()

    def fake_segment(img_rgb, prompt, threshold):
        h, w = img_rgb.shape[:2]
        mask = np.zeros((h, w), bool)
        mask[: h // 2, : w // 2] = True
        assert prompt.startswith("nuclei-level")
        assert 0.0 <= threshold <= 1.0
        return mask

    app._MODEL.inject(fake_segment)
    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        yield test_client
    app._MODEL.reset()


def _png_b64(width, height, mode="RGB"):
    color = 128 if mode == "L" else (128, 128, 128)
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _segment_body(**overrides):
    body = {"image_b64": _png_b64(256, 128), "object": "nuclei",
            "region": "liver"}
    body.update(overrides)
    for key in [k for k, v in body.items() if v is Ellipsis]:
        del body[key]
    return body


# ---------------------------------------------------------------- 就绪状态

def test_healthz_always_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_readyz_ready_with_injected_model(client):
    resp = client.get("/readyz")
    payload = resp.get_json()
    assert resp.status_code == 200
    assert payload["ready"] is True
    assert payload["model_id"] == "pathsegmentor"
    assert len(payload["model_sha256"]) == 64
    assert payload["code_rev"] == app.CODE_REV


def test_readyz_not_ready_without_model():
    app._MODEL.reset()
    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        resp = test_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "not_ready"


def test_segment_not_ready_503():
    app._MODEL.reset()
    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        resp = test_client.post("/v1/segment", json=_segment_body())
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "not_ready"


# ---------------------------------------------------------------- 400 校验

@pytest.mark.parametrize("overrides,code", [
    ({"image_b64": Ellipsis}, "missing_image"),
    ({"image_b64": ""}, "missing_image"),
    ({"image_b64": "not!base64!"}, "invalid_base64"),
    ({"image_b64": base64.b64encode(b"not an image").decode()}, "image_not_decodable"),
    ({"object": Ellipsis}, "missing_object"),
    ({"object": "   "}, "invalid_object"),
    ({"object": 42}, "invalid_object"),
    ({"region": ""}, "invalid_region"),
    ({"threshold": "high"}, "invalid_threshold"),
    ({"threshold": 1.5}, "invalid_threshold"),
    ({"threshold": -0.1}, "invalid_threshold"),
])
def test_segment_400(client, overrides, code):
    resp = client.post("/v1/segment", json=_segment_body(**overrides))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == code


def test_segment_non_json_400(client):
    resp = client.post("/v1/segment", data="not json",
                       content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


def test_segment_image_too_large_400(client):
    resp = client.post("/v1/segment",
                       json=_segment_body(image_b64=_png_b64(2049, 10)))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "image_too_large"


def test_segment_oversized_header_rejected_before_pixel_decode(monkeypatch,
                                                               client):
    """超大图必须只读图片头即拒绝，不得先解码像素（R2 预解码闸）。"""
    calls = []

    class _HeaderOnly:
        size = (4096, 4096)

        def load(self):
            calls.append("decoded_pixels")

    monkeypatch.setattr(app.Image, "open", lambda _f: _HeaderOnly())
    resp = client.post("/v1/segment", json=_segment_body(
        image_b64=base64.b64encode(b"header-only").decode()))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "image_too_large"
    assert calls == [], "oversized input must never reach pixel decoding"


def test_segment_body_too_large_400(client):
    app.app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    big = "A" * (8 * 1024 * 1024 + 2)  # 超 8MB 的 body
    resp = client.post("/v1/segment", json=_segment_body(image_b64=big))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "body_too_large"


# ---------------------------------------------------------------- 成功路径

def test_segment_success_contract(client):
    resp = client.post("/v1/segment", json=_segment_body(threshold=0.7))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["model_id"] == "pathsegmentor"
    assert len(payload["model_sha256"]) == 64
    assert payload["code_rev"] == app.CODE_REV
    assert payload["prompt_used"] == "nuclei-level nuclei in liver pathology"
    assert (payload["input_width"], payload["input_height"]) == (256, 128)
    mask = np.asarray(
        Image.open(io.BytesIO(base64.b64decode(payload["mask_png_b64"]))))
    assert mask.shape == (128, 256)  # 与输入同尺寸
    assert set(np.unique(mask)) <= {0, 255}
    assert payload["positive_pixels"] == int(np.count_nonzero(mask == 255))
    assert payload["threshold"] == 0.7
    assert set(payload["timings_ms"]) == {"decode", "infer", "total"}
    assert payload["warnings"] == []


def test_segment_rgba_converted_to_rgb(client):
    recorded = {}

    def segment_spy(img_rgb, prompt, threshold):
        recorded["shape"] = img_rgb.shape
        return np.zeros(img_rgb.shape[:2], bool)

    app._MODEL.inject(segment_spy)
    resp = app.app.test_client().post(
        "/v1/segment", json=_segment_body(image_b64=_png_b64(64, 48, "RGBA")))
    assert resp.status_code == 200
    assert recorded["shape"] == (48, 64, 3)


def test_segment_greyscale_converted_to_rgb(client):
    recorded = {}

    def segment_spy(img_rgb, prompt, threshold):
        recorded["shape"] = img_rgb.shape
        return np.zeros(img_rgb.shape[:2], bool)

    app._MODEL.inject(segment_spy)
    resp = app.app.test_client().post(
        "/v1/segment", json=_segment_body(image_b64=_png_b64(64, 48, "L")))
    assert resp.status_code == 200
    assert recorded["shape"] == (48, 64, 3)


# ---------------------------------------------------------------- 并发 busy

def test_segment_second_request_busy(client):
    release = threading.Event()
    started = threading.Event()

    def blocking_segment(img_rgb, prompt, threshold):
        started.set()
        release.wait(timeout=10)
        return np.zeros(img_rgb.shape[:2], bool)

    app._MODEL.inject(blocking_segment)
    results = {}

    def run():
        client2 = app.app.test_client()
        results["first"] = client2.post("/v1/segment", json=_segment_body())

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=10)

    second = client.post("/v1/segment", json=_segment_body())
    assert second.status_code == 503
    assert second.get_json() == {"error": "busy"}

    release.set()
    worker.join(timeout=10)
    assert results["first"].status_code == 200


def test_segment_internal_error_500(client):
    def broken_segment(img_rgb, prompt, threshold):
        raise RuntimeError("boom")

    app._MODEL.inject(broken_segment)
    resp = client.post("/v1/segment", json=_segment_body())
    assert resp.status_code == 500
    assert resp.get_json()["error"] == "internal_error"
