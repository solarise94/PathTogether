# -*- coding: utf-8 -*-
"""HTTP 契约测试（无 GPU/权重）：注入假 counter 验证路由、校验、并发、busy。"""
import base64
import io
import threading

import numpy as np
import pytest
from PIL import Image

import app


@pytest.fixture()
def client():
    """注入恒定假标签图（两个实例）的 counter，返回 flask 测试客户端。"""
    app._MODEL.reset()

    def fake_count(img_rgb, diameter_px_=None, flow_threshold=0.4,
                   cellprob_threshold=0.0):
        h, w = img_rgb.shape[:2]
        masks = np.zeros((h, w), np.int32)
        masks[10:20, 10:20] = 1    # 100 px
        masks[30:34, 40:44] = 2    # 16 px
        return masks

    app._MODEL.inject(fake_count)
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


def _count_body(**overrides):
    body = {"image_b64": _png_b64(256, 128), "mpp": 0.25}
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
    assert payload["model_id"] == "cellpose"
    assert payload["model_name"] == "cpsam"
    assert len(payload["model_sha256"]) == 64
    assert payload["code_version"] == app.CODE_VERSION
    assert payload["device"] == "injected"


def test_readyz_not_ready_without_model():
    app._MODEL.reset()
    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        resp = test_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "not_ready"


# ---------------------------------------------------------------- 400 校验

@pytest.mark.parametrize("overrides,code", [
    ({"image_b64": Ellipsis}, "missing_image"),
    ({"image_b64": ""}, "missing_image"),
    ({"image_b64": "not!base64!"}, "invalid_base64"),
    ({"image_b64": base64.b64encode(b"not an image").decode()}, "image_not_decodable"),
    ({"mpp": 0}, "invalid_mpp"),
    ({"mpp": -0.5}, "invalid_mpp"),
    ({"mpp": "fine"}, "invalid_mpp"),
    ({"diameter_um": 0}, "invalid_diameter_um"),
    ({"diameter_um": -3}, "invalid_diameter_um"),
    ({"diameter_um": "big"}, "invalid_diameter_um"),
    ({"flow_threshold": -0.1}, "invalid_flow_threshold"),
    ({"flow_threshold": 1.5}, "invalid_flow_threshold"),
    ({"cellprob_threshold": -7}, "invalid_cellprob_threshold"),
    ({"cellprob_threshold": 7}, "invalid_cellprob_threshold"),
    ({"model": "cyto"}, "invalid_model"),
    ({"model": 42}, "invalid_model"),
    ({"return_labels": "yes"}, "invalid_return_labels"),
])
def test_count_400(client, overrides, code):
    resp = client.post("/v1/count", json=_count_body(**overrides))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == code


def test_count_non_json_400(client):
    resp = client.post("/v1/count", data="not json",
                       content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


def test_count_image_too_large_400(client):
    resp = client.post("/v1/count",
                       json=_count_body(image_b64=_png_b64(2049, 10)))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "image_too_large"


def test_count_body_too_large_400(client):
    app.app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    big = "A" * (8 * 1024 * 1024 + 2)  # 超 8MB 的 body
    resp = client.post("/v1/count", json=_count_body(image_b64=big))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "body_too_large"


# ---------------------------------------------------------------- 成功路径

def test_count_success_contract(client):
    resp = client.post("/v1/count", json=_count_body())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["model_id"] == "cellpose"
    assert payload["model_name"] == "cpsam"
    assert len(payload["model_sha256"]) == 64
    assert payload["code_version"] == app.CODE_VERSION
    assert (payload["input_width"], payload["input_height"]) == (256, 128)
    assert payload["instance_count"] == 2
    assert payload["diameter_px_used"] == pytest.approx(28.0)  # 7µm / 0.25mpp
    areas = payload["areas_px"]
    assert areas["min"] == 16.0 and areas["max"] == 100.0
    assert areas["median"] == 58.0
    top = payload["instances_top"]
    assert len(top) == 2
    assert top[0]["area_px"] == 100                # 面积降序
    assert top[0]["bbox"] == [10, 10, 10, 10]      # [x, y, w, h]
    assert top[0]["centroid"] == [14.5, 14.5]
    assert set(payload["timings_ms"]) == {"decode", "infer", "total"}
    assert payload["warnings"] == []
    assert "labels_png_b64" not in payload         # return_labels 缺省 false


def test_count_return_labels_16bit_png(client):
    resp = client.post("/v1/count", json=_count_body(return_labels=True))
    assert resp.status_code == 200
    payload = resp.get_json()
    png = base64.b64decode(payload["labels_png_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[24] == 16  # IHDR bit depth：16-bit 标签图
    image = Image.open(io.BytesIO(png))
    labels = np.asarray(image).astype(np.int64)
    assert labels.shape == (128, 256)
    assert set(np.unique(labels)) == {0, 1, 2}
    assert payload["instance_count"] == 2


def test_count_mpp_null_uses_model_default_with_warning(client):
    recorded = {}

    def count_spy(img_rgb, diameter_px_=None, flow_threshold=0.4,
                  cellprob_threshold=0.0):
        recorded["diameter_px"] = diameter_px_
        return np.zeros(img_rgb.shape[:2], np.int32)

    app._MODEL.inject(count_spy)
    resp = client.post("/v1/count", json=_count_body(mpp=None, diameter_um=9))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert recorded["diameter_px"] is None         # 模型缺省尺度（cpsam: 原生）
    assert payload["diameter_px_used"] is None
    assert payload["warnings"]
    assert "mpp" in payload["warnings"][0]


def test_count_mpp_null_diameter_default_used(client):
    resp = client.post("/v1/count", json=_count_body(mpp=Ellipsis))
    assert resp.status_code == 200
    assert resp.get_json()["diameter_px_used"] is None


def test_count_optional_thresholds_forwarded(client):
    recorded = {}

    def count_spy(img_rgb, diameter_px_=None, flow_threshold=0.4,
                  cellprob_threshold=0.0):
        recorded["flow"] = flow_threshold
        recorded["cellprob"] = cellprob_threshold
        recorded["dpx"] = diameter_px_
        return np.zeros(img_rgb.shape[:2], np.int32)

    app._MODEL.inject(count_spy)
    resp = client.post("/v1/count", json=_count_body(
        flow_threshold=0.6, cellprob_threshold=-1.5, mpp=0.484,
        diameter_um=7))
    assert resp.status_code == 200
    assert recorded["flow"] == 0.6
    assert recorded["cellprob"] == -1.5
    assert recorded["dpx"] == pytest.approx(14.4628, rel=1e-3)


def test_count_instances_top_capped_at_50(client):
    def many_count(img_rgb, diameter_px_=None, flow_threshold=0.4,
                   cellprob_threshold=0.0):
        h, w = img_rgb.shape[:2]
        masks = np.zeros((h, w), np.int32)
        for i in range(60):  # 60 个 2x2 实例，间隔 4px
            y, x = 2 + i * 2, 2
            masks[y:y + 2, x:x + 2] = i + 1
        return masks

    app._MODEL.inject(many_count)
    resp = client.post("/v1/count", json=_count_body(image_b64=_png_b64(16, 256)))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["instance_count"] == 60
    assert len(payload["instances_top"]) == 50     # ≤50 上限


def test_count_rgba_and_grey_converted_to_rgb(client):
    recorded = {}

    def count_spy(img_rgb, diameter_px_=None, flow_threshold=0.4,
                  cellprob_threshold=0.0):
        recorded["shape"] = img_rgb.shape
        return np.zeros(img_rgb.shape[:2], np.int32)

    app._MODEL.inject(count_spy)
    for mode in ("RGBA", "L"):
        resp = app.app.test_client().post(
            "/v1/count", json=_count_body(image_b64=_png_b64(64, 48, mode)))
        assert resp.status_code == 200
    assert recorded["shape"] == (48, 64, 3)


def test_count_unknown_preset_400_before_inference(client):
    resp = client.post("/v1/count", json=_count_body(model="nuclei"))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_model"


# ---------------------------------------------------------------- 并发 busy

def test_count_second_request_busy(client):
    release = threading.Event()
    started = threading.Event()

    def blocking_count(img_rgb, diameter_px_=None, flow_threshold=0.4,
                       cellprob_threshold=0.0):
        started.set()
        release.wait(timeout=10)
        return np.zeros(img_rgb.shape[:2], np.int32)

    app._MODEL.inject(blocking_count)
    results = {}

    def run():
        client2 = app.app.test_client()
        results["first"] = client2.post("/v1/count", json=_count_body())

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=10)
    try:
        second = client.post("/v1/count", json=_count_body())
        assert second.status_code == 503
        assert second.get_json() == {"error": "busy"}
    finally:
        release.set()
        worker.join(timeout=10)
    assert results["first"].status_code == 200
