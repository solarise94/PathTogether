# -*- coding: utf-8 -*-
"""R2/R3/R4 修复的跨端合同回归：插件真实序列化 × 真实服务校验。

R4 要求不能把推理客户端整个 stub 掉：这里只在 HTTP 边界 stub
（``requests.post``），插件 ``_infer_cellpose`` 的 JSON 序列化与 cellpose
服务的 ``_validate_request`` / 路由校验都走**真实实现**；cellpose 服务注入
假模型，不需要 GPU/权重。只 import cellpose 服务（不 import pathsegmentor
服务——后者依赖 skimage，平台套件 venv 不含）。
"""
import base64
import importlib.util
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def _load_module(path, name):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cellpose_app(monkeypatch):
    """EAGER_LOAD=0 下加载真实 cellpose 服务模块（不加载权重）。"""
    monkeypatch.setenv("CELLPOSE_EAGER_LOAD", "0")
    mod = _load_module(ROOT / "services" / "cellpose" / "app.py",
                       "r2r4_cellpose")
    mod._MODEL.reset()
    yield mod
    mod._MODEL.reset()


def _png_b64(width, height):
    img = Image.new("RGB", (width, height), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_plugin_null_mpp_serialization_meets_real_service(cellpose_app,
                                                          monkeypatch):
    """插件把缺失 MPP 序列化为 JSON null；真实服务接受并走缺省尺度+warning。

    旧实现发 0.0 → 真实服务 400 invalid_mpp，无 MPP 降级分支不可达（R4）。
    """
    backend = _load_module(
        ROOT / "plugins" / "pathsegmentor-measure" / "backend" / "app.py",
        "r2r4_backend")
    captured = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"instance_count": 0}

    def _post(url, json=None, **kwargs):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(backend.requests, "post", _post)
    body = backend._infer_cellpose(b"image-bytes", None)
    assert body == {"instance_count": 0}
    assert captured.get("mpp") is None, "missing mpp must be JSON null"

    # 真实服务侧：同一 null payload 过 _validate_request → 缺省尺度 + warning
    cp = cellpose_app

    def fake_count(img_rgb, diameter_px_=None, flow_threshold=0.4,
                   cellprob_threshold=0.0):
        captured["diameter_px"] = diameter_px_
        return np.zeros(img_rgb.shape[:2], np.int32)

    cp._MODEL.inject(fake_count)
    cp.app.config["TESTING"] = True
    with cp.app.test_client() as client:
        resp = client.post("/v1/count", json={
            "image_b64": _png_b64(64, 64), "mpp": None, "diameter_um": 7})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert captured["diameter_px"] is None  # 模型缺省尺度（cpsam 原生）
    assert payload["diameter_px_used"] is None
    assert payload["warnings"]
    assert "mpp" in payload["warnings"][0]


def test_service_rejects_zero_mpp_that_old_plugin_sent(cellpose_app):
    """旧插件把缺失 MPP 发成 0.0：真实服务 400 invalid_mpp（R4 修复动机）。"""
    cp = cellpose_app

    def fake_count(img_rgb, **kwargs):
        return np.zeros(img_rgb.shape[:2], np.int32)

    cp._MODEL.inject(fake_count)
    cp.app.config["TESTING"] = True
    with cp.app.test_client() as client:
        resp = client.post("/v1/count", json={
            "image_b64": _png_b64(64, 64), "mpp": 0.0})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_mpp"


def test_service_rescale_budget_before_model_with_real_route(cellpose_app,
                                                             monkeypatch):
    """真实路由（解码 stub 除外）下爆炸缩放在进模型前 400 resource_limit。"""
    cp = cellpose_app
    calls = []
    monkeypatch.setattr(cp, "_decode_image",
                        lambda _b: (None, Image.new("RGB", (1024, 1024))))

    def spy(img, **kwargs):
        calls.append(img.shape)
        return np.zeros(img.shape[:2], np.int32)

    cp._MODEL.inject(spy)
    cp.app.config["TESTING"] = True
    with cp.app.test_client() as client:
        resp = client.post("/v1/count", json={
            "image_b64": _png_b64(64, 64), "mpp": 0.5, "diameter_um": 1.0})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "resource_limit"
    assert calls == []
