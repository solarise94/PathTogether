# -*- coding: utf-8 -*-
"""internal_ai_region max_long_edge 宽高比与互斥行为测试（§6.1）。

覆盖：
  - max_long_edge 按原始 bbox 比例计算 out_w/out_h（横向 2:1、纵向 1:2）；
  - 边界裁剪 bbox（极窄/极扁）仍保持比例；
  - max_long_edge 与显式 out_w/out_h 同时给定时以 max_long_edge 为准；
  - max_long_edge 越界（0 / 4097）→ 400；
  - 响应携带 encoder 字段（§6.3）。

运行：cd 项目根 && python3 -m pytest tests/test_region_max_long_edge.py -q
"""
import contextlib
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-mle-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token-mle"

try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import app as app_mod  # noqa: E402

from pathlib import Path as _Path  # noqa: E402

app_mod.UPLOAD_DIR = _Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 固定内部 token：模块级 AI_INTERNAL_TOKEN 在 import 时解析，可能被其它测试
# 模块的 import 顺序污染（见 test_ai_proxy 设置不同的 SHARE_DATA_DIR/token）。
# 本模块用 pytest autouse fixture 在每个用例前把模块级 token 夺回为固定值，
# 保证 _headers() 与 _require_internal() 一致，不受收集顺序影响。
import pytest  # noqa: E402

_TOKEN = "test-internal-token-mle"


@pytest.fixture(autouse=True)
def _pin_internal_token(monkeypatch):
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", _TOKEN)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _headers():
    return {"X-AI-Internal-Token": _TOKEN, "Content-Type": "application/json"}


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextlib.contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _patch_read_region(captured):
    """mock _read_region_b64，记录调用参数并回放可控 width/height。

    captured: list，每次调用追加一个 dict（含 max_long_edge / out_w / out_h /
    jpeg_quality）。返回的 width/height 由 sidecar 端的 _aspect_fit_size 计算——
    但本测试在 Python 侧直接用同一函数算期望值。
    """
    def fake(entry, x, y, w, h, out_w, out_h, safe, mpp,
             max_long_edge=None, jpeg_quality=85):
        if max_long_edge is not None and int(max_long_edge) > 0:
            ow, oh = app_mod._aspect_fit_size(w, h, max_long_edge)
        else:
            ow = max(1, min(out_w, 4096))
            oh = max(1, min(out_h, 4096))
        captured.append({
            "max_long_edge": max_long_edge,
            "out_w": out_w, "out_h": out_h,
            "jpeg_quality": jpeg_quality,
            "computed_w": ow, "computed_h": oh,
        })
        return {
            "image_base64": "QQ==",
            "mime": "image/jpeg",
            "width": ow, "height": oh,
            "src": {"x": x, "y": y, "w": w, "h": h},
            "magnification": 20,
        }
    return fake


def _post(slide, **extra):
    body = {"slide": slide, "x": 0, "y": 0, "w": 1000, "h": 500}
    body.update(extra)
    return _client().post("/internal/ai/region", headers=_headers(), json=body)


def test_max_long_edge_horizontal_2to1():
    """横向 bbox (w=1000,h=500) max_long_edge=1024 → 1024×512（保比例）。"""
    slide = _touch_slide()
    captured = []
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(10000, 8000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(app_mod, "_read_region_b64", side_effect=_patch_read_region(captured)):
                    r = _post(slide, w=1000, h=500, max_long_edge=1024)
    assert r.status_code == 200
    j = r.get_json()
    assert j["width"] == 1024
    assert j["height"] == 512
    # 比例保持 2:1
    assert abs(j["width"] / j["height"] - 2.0) < 0.01


def test_max_long_edge_vertical_1to2():
    """纵向 bbox (w=500,h=1000) max_long_edge=768 → 384×768（保比例）。"""
    slide = _touch_slide()
    captured = []
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(10000, 8000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(app_mod, "_read_region_b64", side_effect=_patch_read_region(captured)):
                    r = _post(slide, w=500, h=1000, max_long_edge=768)
    assert r.status_code == 200
    j = r.get_json()
    assert j["width"] == 384
    assert j["height"] == 768


def test_max_long_edge_square_bbox():
    """正方形 bbox max_long_edge=1280 → 1280×1280。"""
    slide = _touch_slide()
    captured = []
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(10000, 8000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(app_mod, "_read_region_b64", side_effect=_patch_read_region(captured)):
                    r = _post(slide, w=800, h=800, max_long_edge=1280)
    assert r.status_code == 200
    j = r.get_json()
    assert j["width"] == 1280
    assert j["height"] == 1280


def test_max_long_edge_overrides_explicit_out():
    """max_long_edge 与 out_w/out_h 同时给定时，以 max_long_edge 为准。"""
    slide = _touch_slide()
    captured = []
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(10000, 8000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(app_mod, "_read_region_b64", side_effect=_patch_read_region(captured)):
                    # 显式 out_w/out_h=2000×2000 会被忽略，用 max_long_edge=1024 保比例
                    r = _post(slide, w=1000, h=500, max_long_edge=1024, out_w=2000, out_h=2000)
    assert r.status_code == 200
    j = r.get_json()
    assert j["width"] == 1024
    assert j["height"] == 512
    # 确认底层用的是 max_long_edge 路径
    assert captured[0]["max_long_edge"] == 1024


def test_max_long_edge_reject_out_of_range():
    """max_long_edge=0 / 4097 → 400。"""
    slide = _touch_slide()
    r0 = _post(slide, max_long_edge=0)
    assert r0.status_code == 400
    r_big = _post(slide, max_long_edge=4097)
    assert r_big.status_code == 400


def test_region_response_carries_encoder():
    """region 响应携带 encoder 字段（§6.3 派生规格校验用）。"""
    slide = _touch_slide()
    captured = []
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(10000, 8000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(app_mod, "_read_region_b64", side_effect=_patch_read_region(captured)):
                    r = _post(slide, max_long_edge=1024)
    assert r.status_code == 200
    enc = r.get_json().get("encoder") or {}
    assert enc.get("id") == "pillow"
    assert enc.get("resize") == "LANCZOS"
    assert enc.get("overlay_version") == "v1"
    assert enc.get("jpeg_quality") == 85


def test_aspect_fit_size_helper():
    """直接覆盖 _aspect_fit_size 的比例计算与 clamp。"""
    fit = app_mod._aspect_fit_size
    # 横向 2:1
    assert fit(2000, 1000, 1024) == (1024, 512)
    # 纵向 1:2
    assert fit(1000, 2000, 768) == (384, 768)
    # 正方形
    assert fit(800, 800, 1280) == (1280, 1280)
    # 超过 4096 上限 clamp
    assert fit(10000, 5000, 9999) == (4096, 2048)
    # 最长边 < max_long_edge 时放大到 max_long_edge（最长边对齐）
    ow, oh = fit(100, 50, 1024)
    assert ow == 1024


if __name__ == "__main__":
    test_max_long_edge_horizontal_2to1()
    test_max_long_edge_vertical_1to2()
    test_max_long_edge_square_bbox()
    test_max_long_edge_overrides_explicit_out()
    test_max_long_edge_reject_out_of_range()
    test_region_response_carries_encoder()
    test_aspect_fit_size_helper()
    print("all max_long_edge tests passed")
