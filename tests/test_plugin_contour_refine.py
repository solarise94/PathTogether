# -*- coding: utf-8 -*-
"""插件通道 contours/refine（LLM 轮廓 SAM2 精修代理）测试。

覆盖（对照 test_plugin_api.py 的 region 用例形态）：
  - 鉴权/收录复核与 regions 同链：无 token 401、scope 不足 403、
    无效 X-Run-Grant 403、by-id 未知 404；
  - points 校验 400（缺字段/非数组/点数越界/非有限坐标/字符串坐标/
    完全越出切片范围）；
  - CONTOUR_REFINE_URL 未配置 → 200 {refined:false, reason:"unavailable"}；
  - 成功路径（服务 HTTP 调用 monkeypatch）：crop 长边 ≤1024、polygon 映射到
    crop 像素、金字塔层选择（level 1 降采样）、返回点映射回 level-0；
  - 服务拒绝（accepted:false）→ 透传 reason/score/area_ratio；
  - 服务超时/错误/坏 JSON/非 200 → {refined:false, reason:"service_error"}；
  - expected_fingerprint 不符 → 409 slide_revision_conflict（与 regions 同语义）；
  - CONTOUR_REFINE_TOKEN 设置时以 X-Refine-Token 头转发。

运行：cd 项目根 && TMPDIR=/home/solarise/pytest-tmp .venv/bin/python -m pytest
tests/test_plugin_contour_refine.py -q
"""
import base64
import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import (
    FakeRequests,
    FakeResponse,
    isolate_app,
    make_client as _client,
    register_slide_row,
)  # noqa: E402
import requests as _real_requests  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具与辅助（形态对齐 test_plugin_api.py）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + 数据目录；本地免认证态（与 region 用例同基线）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # 缺省不配服务地址（unavailable 分支）；需要服务路径的用例自行 setattr
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "")
    yield


def _bootstrap_installation():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None, "引导应成功"
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _token_for(inst):
    secret_file = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = secret_file.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            secret = str(obj.get("secret") or "")
    except (ValueError, TypeError):
        secret = raw
    return _client().post(
        "/api/plugin/v1/auth/token",
        json={"installation_id": inst["installation_id"], "secret": secret}
    ).get_json()["access_token"]


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _assert_envelope(r, status, code):
    assert r.status_code == status, "got %s body=%r" % (r.status_code, r.get_json())
    body = r.get_json() or {}
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert err["code"] == code, "code=%r full=%r" % (err.get("code"), err)
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["retryable"], bool)


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    register_slide_row(name)
    return name


class _FakeOsr:
    """可编程假 openslide 句柄：真实 PIL 返回（供 PNG 编码/resize 路径）。"""

    def __init__(self, dimensions=(20000, 12000), level_downsamples=(1.0, 4.0)):
        self.dimensions = dimensions
        self.level_downsamples = list(level_downsamples)
        self.read_region_calls = []

    def read_region(self, location, level, size):
        self.read_region_calls.append((location, level, size))
        w, h = size
        yy, xx = np.mgrid[0:h, 0:w]
        rr = ((xx - w / 2) / (w * 0.42)) ** 2 + ((yy - h / 2) / (h * 0.42)) ** 2
        arr = np.where(rr[..., None] <= 1.0,
                       np.array([90, 60, 50], dtype=np.uint8),
                       np.array([190, 175, 160], dtype=np.uint8))
        return Image.fromarray(arr.astype(np.uint8), mode="RGB")


_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _slide_read_mocks(osr):
    """slide 读路径 mock（与 test_plugin_api 同形态）。"""
    pair = {"osr": osr}
    return mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY), \
        mock.patch.object(app_mod.slide_cache, "borrow_pair",
                          side_effect=lambda _e: _borrow_pair_ctx(pair))


def _fake_service(status=200, svc_body=None, raw=None):
    """替换 app.requests 的假 contour-refine 服务；返回 (fake, responses)。

    FakeRequests 按 URL path 分发（见 _pt_helpers._url_to_path），注册
    "/v1/refine" 即匹配任意 CONTOUR_REFINE_URL 前缀。
    """
    fake = FakeRequests()
    responses = []

    def handler(body, query, headers, kwargs):
        responses.append({"body": body, "headers": headers,
                          "kwargs": kwargs})
        if raw is not None:
            return FakeResponse(status, raw)
        return FakeResponse(status, svc_body if svc_body is not None else {})
    fake.register("POST", "/v1/refine", handler)
    app_mod.requests = fake
    return fake, responses


_SVC_ACCEPT = {
    "accepted": True, "reason": None, "score": 0.93, "area_ratio": 0.87,
    "points": [[10, 20], [100, 20], [100, 80], [10, 80]],
    "model": "sam2.1-hiera-b+", "seconds": 0.42,
}


def _refine_path(slide):
    return "/api/plugin/v1/slides/%s/contours/refine" % slide


_SQUARE_POINTS = [[8000, 4000], [12000, 4000], [12000, 7000], [8000, 7000]]


# --------------------------------------------------------------------------- #
# 1. 鉴权 / 收录复核（与 regions 同链）
# --------------------------------------------------------------------------- #
def test_requires_bearer_token():
    _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(_refine_path(slide), json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 401, "unauthorized")
    r = _client().post(_refine_path(slide), json={"points": _SQUARE_POINTS},
                       headers=_bearer("not.a.jwt"))
    _assert_envelope(r, 401, "unauthorized")


def test_scope_insufficient_403_parity_with_regions():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    token = app_mod._plugin_jwt_encode(
        {"iss": "pathtogether", "aud": "plugin", "sub": inst["installation_id"],
         "plugin_id": "histopilot", "scope": "slide:read"})  # 缺 region:read
    client = _client()
    r = client.post(_refine_path(slide), headers=_bearer(token),
                    json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 403, "forbidden")
    # 同一 token 调 regions 也 403（同权限域）
    r = client.post("/api/plugin/v1/slides/%s/regions" % slide,
                    headers=_bearer(token), json={"x": 0, "y": 0, "w": 9, "h": 9})
    _assert_envelope(r, 403, "forbidden")


def test_invalid_run_grant_403():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    client = _client()
    r = client.post(_refine_path(slide),
                    headers={**_bearer(_token_for(inst)),
                             "X-Run-Grant": "rgr_none"},
                    json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 403, "run_grant_invalid")


def test_by_id_unknown_slide_404():
    inst = _bootstrap_installation()
    r = _client().post("/api/plugin/v1/slides/by-id/sld_unknown00/contours/refine",
                       headers=_bearer(_token_for(inst)),
                       json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 404, "not_found")


def test_name_route_unknown_slide_404():
    inst = _bootstrap_installation()
    r = _client().post(_refine_path("missing.svs"),
                       headers=_bearer(_token_for(inst)),
                       json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 404, "not_found")


# --------------------------------------------------------------------------- #
# 2. points 校验（400 invalid_request）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    {},                                   # 缺 points
    {"points": "nope"},                   # 非数组
    {"points": [[0, 0], [1, 1]]},         # 点数 < 3
    {"points": [[0, 0], [1, 1], [2, 2]] * 200},  # 点数 > 500（600）
    {"points": [[0, 0], [1], [2, 2]]},    # 非二元点
    {"points": [[0, 0], ["x", 1], [2, 2]]},  # 字符串坐标
    {"points": [[0, 0], [True, 1], [2, 2]]},  # bool 坐标
])
def test_points_validation_400(payload):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                       json=payload)
    _assert_envelope(r, 400, "invalid_request")


def test_points_non_finite_400():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(
        _refine_path(slide), headers=_bearer(_token_for(inst)),
        data=json.dumps({"points": [[0, 0], [float("nan"), 5], [9, 9]]}),
        content_type="application/json")
    _assert_envelope(r, 400, "invalid_request")


def test_points_fully_outside_slide_400():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    osr = _FakeOsr(dimensions=(20000, 12000))
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": [[-500, -500], [-100, -500],
                                             [-100, -100], [-500, -100]]})
    _assert_envelope(r, 400, "invalid_request")


# --------------------------------------------------------------------------- #
# 3. 服务未配置 / 指纹冲突
# --------------------------------------------------------------------------- #
def test_unavailable_when_env_unset():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    m1, m2 = _slide_read_mocks(_FakeOsr())  # 越界校验需 dimensions（stub 不可开）
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"refined": False, "points": None, "score": None,
                    "area_ratio": None, "model": None, "reason": "unavailable",
                    "seconds": None}


def test_fingerprint_mismatch_409():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(
        _refine_path(slide), headers=_bearer(_token_for(inst)),
        json={"points": _SQUARE_POINTS, "expected_fingerprint": "mtime:2"})
    _assert_envelope(r, 409, "slide_revision_conflict")
    err = r.get_json()["error"]
    assert err.get("details", {}).get("expected") == "mtime:2"


# --------------------------------------------------------------------------- #
# 4. 成功路径（坐标映射）
# --------------------------------------------------------------------------- #
def test_success_maps_coordinates_both_ways(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr(dimensions=(20000, 12000), level_downsamples=(1.0, 4.0))
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["refined"] is True
    assert body["score"] == 0.93 and body["model"] == "sam2.1-hiera-b+"
    assert body["reason"] is None and body["seconds"] == 0.42

    assert len(responses) == 1
    svc_req = responses[0]["body"]
    # crop 几何：bbox (8000,4000)-(12000,7000) → pad=1040 → crop (6960,2960,6080,5080)
    # 长边 6080 → 1024；金字塔层选择 ds=4（level 1）
    assert osr.read_region_calls, "应实际读取 crop"
    (loc, level, size) = osr.read_region_calls[-1]
    assert loc == (6960, 2960) and level == 1
    assert size == (1520, 1270)  # round(6080/4) x round(5080/4)
    # 发给服务的 PNG：解码后长边 ≤ 1024（aspect-fit 后 1024x856：
    # oh = round(5080*1024/6080) = round(855.58) = 856）
    png = base64.b64decode(svc_req["image_png_b64"])
    img = Image.open(io.BytesIO(png))
    assert max(img.size) <= 1024
    assert img.size == (1024, 856)
    # polygon 映射到 crop 像素（与端点同式重算）
    sx = 1024 / 6080.0
    sy = 856 / 5080.0
    expect0 = ((8000 - 6960) * sx, (4000 - 2960) * sy)
    got0 = svc_req["polygon"][0]
    assert abs(got0[0] - expect0[0]) < 1e-6 and abs(got0[1] - expect0[1]) < 1e-6
    assert all(0 <= p[0] <= 1024 and 0 <= p[1] <= 856 for p in svc_req["polygon"])
    # 返回点映射回 level-0（整数）
    expect_pts = [[int(round(px / sx + 6960)), int(round(py / sy + 2960))]
                  for px, py in _SVC_ACCEPT["points"]]
    assert body["points"] == expect_pts
    assert all(isinstance(v, int) for p in body["points"] for v in p)


def test_success_small_crop_no_downscale(monkeypatch):
    """小 polygon（crop 长边 ≤1024）不降采样：read_level 0、原尺寸读出。"""
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr(dimensions=(20000, 12000), level_downsamples=(1.0, 4.0))
    m1, m2 = _slide_read_mocks(osr)
    points = [[100, 100], [400, 100], [400, 300], [100, 300]]  # 300x200 + pad
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": points})
    assert r.status_code == 200 and r.get_json()["refined"] is True
    (loc, level, size) = osr.read_region_calls[-1]
    # bbox 300x200 → pad=115 → crop 530x430（level 0 原尺寸，无 resize）
    assert level == 0 and size == (530, 430)
    assert loc == (max(0, 100 - 115), max(0, 100 - 115))


def test_rejection_passthrough_reason(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _fake_service(svc_body={"accepted": False, "reason": "low_score",
                            "score": 0.4, "area_ratio": 0.9, "points": None,
                            "model": "sam2.1-hiera-b+", "seconds": 0.3})
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["points"] is None
    assert body["reason"] == "low_score"
    assert body["score"] == 0.4 and body["area_ratio"] == 0.9
    assert body["model"] == "sam2.1-hiera-b+"


def test_token_forwarded_to_service(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "shared-secret-1")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    assert responses[0]["headers"].get("X-Refine-Token") == "shared-secret-1"
    assert responses[0]["kwargs"]["timeout"] == app_mod._CONTOUR_REFINE_TIMEOUT_S


# --------------------------------------------------------------------------- #
# 5. 服务故障面（timeout / 错误 / 坏 JSON / 非 200 / 坏点列）
# --------------------------------------------------------------------------- #
def _post_with_service(monkeypatch, svc_body=None, raw=None, status=200,
                       raise_exc=None):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    fake = FakeRequests()

    def handler(body, query, headers, kwargs):
        if raise_exc is not None:
            raise raise_exc
        if raw is not None:
            return FakeResponse(status, raw)
        return FakeResponse(status, svc_body if svc_body is not None else {})
    fake.register("POST", "/v1/refine", handler)
    app_mod.requests = fake
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        return _client().post(_refine_path(slide),
                              headers=_bearer(_token_for(inst)),
                              json={"points": _SQUARE_POINTS})


def _assert_service_error(r):
    assert r.status_code == 200
    assert r.get_json() == {"refined": False, "points": None, "score": None,
                            "area_ratio": None, "model": None,
                            "reason": "service_error", "seconds": None}


def test_service_timeout_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           raise_exc=_real_requests.Timeout("t"))
    _assert_service_error(r)


def test_service_connection_error_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           raise_exc=_real_requests.ConnectionError("down"))
    _assert_service_error(r)


def test_service_bad_json_service_error(monkeypatch):
    r = _post_with_service(monkeypatch, raw="not-json")
    _assert_service_error(r)


def test_service_http_500_service_error(monkeypatch):
    r = _post_with_service(monkeypatch, status=500,
                           svc_body={"error": "internal"})
    _assert_service_error(r)


def test_service_bad_shape_service_error(monkeypatch):
    # 200 但缺 accepted 布尔 → 形状异常按 service_error
    r = _post_with_service(monkeypatch, svc_body={"unexpected": True})
    _assert_service_error(r)


def test_service_accepted_but_bad_points_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           svc_body={"accepted": True, "points": "bogus",
                                     "score": 0.9, "area_ratio": 1.0,
                                     "model": "sam2.1-hiera-b+", "seconds": 0.1,
                                     "reason": None})
    _assert_service_error(r)
