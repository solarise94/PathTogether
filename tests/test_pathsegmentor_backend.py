# -*- coding: utf-8 -*-
"""PathSegmentor Measure 插件后端单元测试（机制级；推理服务用 stub）。

覆盖（docs/measurement-models-homepc-implementation.md §3 计量契约）：
  - 入口守卫：缺 X-Dispatch-Principal / 委托凭证 → 401；参数校验 → 400；
  - mask → level-0 映射（src 缩放）、面积占比、MPP µm² 换算（缺失只给 px）、
    连通域计数与小目标过滤、尺寸分布、top 连通域 bbox/质心；
  - 输出长边选层（~0.5 µm/px 目标 + ≤1024 钳制）；
  - 推理服务错误映射：busy / not_ready / 不可达 / 400 / 契约不符（mask 尺寸
    不一致）；
  - 响应卫生：恒 ≪64KiB、无 mask/base64、不回显委托凭证。

运行：cd 项目根 && python3 -m pytest tests/test_pathsegmentor_backend.py -q
"""
import base64
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import requests as real_requests  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "plugins" / "pathsegmentor-measure" / "backend" / "app.py"

PLATFORM = "http://127.0.0.1:8000"
INFER = "http://127.0.0.1:8971"
DELEGATED = "hdr-delegated-token"


def _load_backend(monkeypatch, **env):
    """按用例 env 加载后端模块（目录名含连字符，不可常规 import）。"""
    base = {
        "PT_PLATFORM_URL": PLATFORM,
        "PATHSEGMENTOR_URL": INFER,
        "PT_INSTALLATION_ID": "pi_test",
        "PT_INSTALLATION_SECRET": "sec_test",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pathsegmentor_measure_backend_%s" % abs(hash(tuple(sorted(env)))),
        BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _InferStubRequests:
    """推理服务 stub：按 script 应答 / 抛连接错误；平台地址一律拒绝。"""

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json})
        assert url.startswith(INFER), "后端不得向非推理地址发 POST：%s" % url
        spec = self.script.get("POST /v1/segment")
        if spec is None:
            return _Resp(200, {"model_id": "pathsegmentor", "prompt_used": "nuclei"})
        if isinstance(spec, Exception):
            raise spec
        return _Resp(*spec)


def _png_grid(grid):
    """二值 grid → PNG bytes（推理 mask 契约：255=前景）。"""
    from PIL import Image
    h, w = len(grid), len(grid[0])
    img = Image.new("L", (w, h))
    img.putdata([255 if v else 0 for row in grid for v in row])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _region_payload(out_w, out_h, src=(0, 0, 16, 16)):
    return {
        "image_base64": base64.b64encode(b"fake-jpeg").decode("ascii"),
        "mime": "image/jpeg", "width": out_w, "height": out_h,
        "src": {"x": src[0], "y": src[1], "w": src[2], "h": src[3]},
        "magnification": 20.0, "read_level": 0, "upsampled": False,
        "asset_revision": "sha256:abcd1234",
    }


def _stub_platform(monkeypatch, mod, *, mpp=0.5, region=None, image=b"img"):
    """钉住平台回调（JWT/元数据/region 读取），返回 region payload。"""
    region = region or _region_payload(16, 16)
    monkeypatch.setattr(mod, "_installation_jwt", lambda: "jwt-stub")
    monkeypatch.setattr(mod, "_platform_slide_info",
                        lambda slide, jwt: {"width": 100000, "height": 100000,
                                            "mpp": mpp,
                                            "asset_revision": "sha256:abcd1234"})

    def _read(slide, args, edge, delegated, jwt):
        assert delegated == DELEGATED, "region 读取必须携带委托凭证"
        assert edge <= mod._MAX_LONG_EDGE
        return image, region
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    return region


_PRINCIPAL = {"type": "agent", "user_id": "u1", "role": "user",
              "session_id": "s1"}


def _call(mod, arguments=None, *, headers=None, slide="demo.svs"):
    base = {"X-Dispatch-Principal": json.dumps(_PRINCIPAL),
            "X-Delegated-Read-Token": DELEGATED}
    if headers is not None:
        base = headers
    return mod.app.test_client().post(
        "/capabilities/segment_region",
        json={"slide": slide, "arguments": arguments or {}},
        headers=base)


GOOD_ARGS = {"x": 10, "y": 20, "w": 16, "h": 16, "object": "nuclei"}


# --------------------------------------------------------------------------- #
# 1. 入口守卫 / 参数校验
# --------------------------------------------------------------------------- #
def test_healthz(monkeypatch):
    mod = _load_backend(monkeypatch)
    r = mod.app.test_client().get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["plugin"] == "dev.pathsegmentor.measure"


def test_rejects_missing_principal(monkeypatch):
    mod = _load_backend(monkeypatch)
    r = mod.app.test_client().post(
        "/capabilities/segment_region",
        json={"slide": "s", "arguments": GOOD_ARGS},
        headers={"X-Delegated-Read-Token": DELEGATED})
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "unauthorized"


def test_rejects_missing_delegated_token(monkeypatch):
    """无平台签发的委托凭证 → 401（直连调用不受理）。"""
    mod = _load_backend(monkeypatch)
    r = mod.app.test_client().post(
        "/capabilities/segment_region",
        json={"slide": "s", "arguments": GOOD_ARGS},
        headers={"X-Dispatch-Principal": json.dumps(_PRINCIPAL)})
    assert r.status_code == 401
    body = r.get_json()["error"]
    assert body["code"] == "unauthorized"
    assert "X-Delegated-Read-Token" in body["message"]


@pytest.mark.parametrize("bad,args_key", [
    ("missing_object", {"x": 0, "y": 0, "w": 8, "h": 8}),
    ("float_x", {"x": 1.5, "y": 0, "w": 8, "h": 8, "object": "nuclei"}),
    ("negative_w", {"x": 0, "y": 0, "w": -8, "h": 8, "object": "nuclei"}),
    ("bad_threshold", {"x": 0, "y": 0, "w": 8, "h": 8, "object": "nuclei",
                       "threshold": 5}),
    ("empty_object", {"x": 0, "y": 0, "w": 8, "h": 8, "object": "  "}),
])
def test_validates_arguments(monkeypatch, bad, args_key):
    mod = _load_backend(monkeypatch)
    r = _call(mod, args_key)
    assert r.status_code == 400, (bad, r.get_json())
    assert r.get_json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# 2. mask 后处理数学
# --------------------------------------------------------------------------- #
def test_mask_to_level0_mapping_and_um2(monkeypatch):
    mod = _load_backend(monkeypatch)
    # 4x4 输出图，左上 2x2 前景；src 为平台夹取后 level-0 bbox (100,200,16,16)
    grid = [[1, 1, 0, 0],
            [1, 1, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0]]
    region = _region_payload(4, 4, src=(100, 200, 16, 16))
    out = mod.analyze_mask(grid, 4, 4, region, 0.5)
    # 缩放：level-0 每输出像素 = 16/4 = 4 px
    assert out["foreground"]["positive_pixels_out"] == 4
    assert out["foreground"]["area_px_l0"] == pytest.approx(4 * 16)
    assert out["foreground"]["area_fraction_of_roi"] == pytest.approx(0.25)
    # µm²：64 level-0 px × 0.5 × 0.5 = 16
    assert out["foreground"]["area_um2"] == pytest.approx(16.0)
    assert out["mpp"]["x"] == 0.5 and out["mpp"]["y"] == 0.5
    comp = out["components_top"][0]
    assert comp["bbox_l0"] == [100, 200, 8, 8]
    assert comp["centroid_l0"] == [102, 202]  # (0.5,0.5)*4 + (100,200)
    assert comp["area_um2"] == pytest.approx(16.0)
    assert out["approx_object_count"]["value"] == 1


def test_mpp_missing_px_only(monkeypatch):
    mod = _load_backend(monkeypatch)
    grid = [[1, 1], [0, 0]]
    out = mod.analyze_mask(grid, 2, 2, _region_payload(2, 2), None)
    assert out["foreground"]["area_um2"] is None
    assert out["size_distribution"]["area_um2"] is None
    assert out["mpp"]["x"] is None
    assert any("MPP" in w for w in out["warnings"])


def test_component_counting_filtering_and_distribution(monkeypatch):
    mod = _load_backend(monkeypatch)
    # 10x10 输出图：3x3 块（9px）+ 单像素（1px）；min = max(4, 100*1e-4)=4px
    grid = [[0] * 10 for _ in range(10)]
    for y in range(3):
        for x in range(3):
            grid[y][x] = 1
    grid[9][9] = 1
    out = mod.analyze_mask(grid, 10, 10, _region_payload(10, 10, (0, 0, 40, 40)),
                           0.25)
    assert out["approx_object_count"]["value"] == 1  # 单像素被小目标过滤
    assert any("小目标过滤" in w for w in out["warnings"])
    dist = out["size_distribution"]
    assert dist["components"] == 1
    assert dist["area_px_l0"]["min"] == pytest.approx(9 * 4 * 4)
    # 分布与 top 组件同源
    assert out["components_top"][0]["area_px_l0"] == pytest.approx(144)


def test_components_top_limit(monkeypatch):
    mod = _load_backend(monkeypatch)
    monkeypatch.setattr(mod, "_MAX_COMPONENTS", 3)
    grid = [[0] * 20 for _ in range(20)]
    for i in range(5):  # 5 个 2x2 块（各 4px，均 ≥ min 4px）
        for y in range(2):
            for x in range(2):
                grid[i * 4 + y][i * 4 + x] = 1
    out = mod.analyze_mask(grid, 20, 20, _region_payload(20, 20), None)
    assert out["approx_object_count"]["value"] == 5
    assert len(out["components_top"]) == 3


def test_mask_size_mismatch_rejected(monkeypatch):
    mod = _load_backend(monkeypatch)
    grid = [[1, 0], [0, 0]]
    with pytest.raises(mod.PlatformError) as ei:
        mod.analyze_mask(grid, 2, 2, _region_payload(4, 4), 0.5)
    assert ei.value.code == "inference_contract"


def test_target_long_edge_math(monkeypatch):
    mod = _load_backend(monkeypatch)
    # 2048px @0.25mpp → 1024 输出（恰 0.5µm/px）
    edge, coarse = mod._target_long_edge(2048, 1024, 0.25)
    assert edge == 1024 and coarse is False
    # 4096px @0.5mpp → 想要 4096，钳到 1024（变粗，警告）
    edge, coarse = mod._target_long_edge(4096, 4096, 0.5)
    assert edge == 1024 and coarse is True
    # MPP 未知 → 上限 + 调用方附警告
    edge, coarse = mod._target_long_edge(512, 512, None)
    assert edge == mod._MAX_LONG_EDGE and coarse is True


# --------------------------------------------------------------------------- #
# 3. 端到端机制（平台回调与推理均 stub）
# --------------------------------------------------------------------------- #
def _happy_setup(monkeypatch, mpp=0.5):
    mod = _load_backend(monkeypatch)
    # region：bbox(16,16) 渲染为 4x4 输出（scale 4），与 mask 尺寸一致
    _stub_platform(monkeypatch, mod, mpp=mpp,
                   region=_region_payload(4, 4, src=(10, 20, 16, 16)))
    mask = _png_grid([[1, 1, 0, 0],
                      [1, 1, 0, 0],
                      [0, 0, 0, 0],
                      [0, 0, 0, 0]])
    infer_body = {"model_id": "psm", "model_sha256": "aa"*32,
                  "code_rev": "rev1", "prompt_used": "nuclei",
                  "input_width": 4, "input_height": 4,
                  "mask_png_b64": base64.b64encode(mask).decode("ascii"),
                  "positive_pixels": 4, "threshold": None,
                  "timings_ms": {"decode": 1, "infer": 2, "total": 3},
                  "warnings": []}

    def _infer(image_png_bytes, obj, region, threshold):
        assert obj == "nuclei"
        return infer_body, mask
    monkeypatch.setattr(mod, "_infer", _infer)
    return mod


def test_happy_path_result_shape_and_hygiene(monkeypatch):
    mod = _happy_setup(monkeypatch)
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 200, r.get_json()
    result = r.get_json()["result"]
    assert result["analysis_id"].startswith("psm-")
    assert result["model"] == {"model_id": "psm", "model_sha256": "aa"*32,
                               "code_rev": "rev1"}
    assert result["prompt_used"] == "nuclei"
    assert result["slide"] == "demo.svs"
    assert result["slide_revision"] == "sha256:abcd1234"
    assert result["source_bbox"] == {"x": 10, "y": 20, "w": 16, "h": 16}
    assert result["rendered"]["width"] == 4
    # 4 前景像素 × 4 × 4 = 64 level-0 px；×0.5×0.5 = 16 µm²
    assert result["foreground"]["positive_pixels_out"] == 4
    assert result["foreground"]["area_px_l0"] == pytest.approx(64.0)
    assert result["foreground"]["area_um2"] == pytest.approx(16.0)
    assert result["approx_object_count"]["value"] == 1
    assert result["components_top"][0]["bbox_l0"] == [10, 20, 8, 8]
    assert result["components_top"][0]["centroid_l0"] == [12, 22]
    # 响应卫生：无 mask/base64 图像、不回显委托凭证、体量 ≪64KiB
    text = r.get_data(as_text=True)
    assert DELEGATED not in text
    assert "mask_png_b64" not in text and "image_base64" not in text
    assert len(r.get_data()) < 32 * 1024


def test_inference_busy_maps_retryable_503(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "requests", _InferStubRequests({
        "POST /v1/segment": (503, {"error": "busy"})}))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == "inference_busy"
    assert err["retryable"] is True


def test_inference_not_ready_maps_503(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "requests", _InferStubRequests({
        "POST /v1/segment": (503, {"error": "not_ready"})}))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_not_ready"


def test_inference_unreachable_maps_503(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "requests", _InferStubRequests({
        "POST /v1/segment": real_requests.ConnectionError("down")}))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == "inference_unavailable"
    assert err["retryable"] is True


def test_inference_bad_input_maps_400(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "requests", _InferStubRequests({
        "POST /v1/segment": (400, {"error": "bad_input",
                                   "message": "image too large"})}))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "invalid_argument"
    assert "image too large" in err["message"]


def test_result_stays_under_64kib_with_many_components(monkeypatch):
    """密集组件（语义分割碎块）下响应仍远小于 dispatch 64KiB 截断阈值。"""
    mod = _happy_setup(monkeypatch)
    grid = [[0] * 200 for _ in range(200)]
    for i in range(0, 200, 20):
        for j in range(0, 200, 20):
            grid[i][j] = 1  # 100 个单像素（低于 min 过滤线 4px）
    for i in range(0, 200, 40):
        for j in range(0, 200, 40):
            for y in range(4):
                for x in range(4):
                    grid[i + y][j + x] = 1  # 25 个 4x4 块
    mask = _png_grid(grid)
    infer_body = {"model_id": "psm", "prompt_used": "nuclei",
                  "mask_png_b64": base64.b64encode(mask).decode("ascii"),
                  "warnings": []}
    monkeypatch.setattr(mod, "_infer",
                        lambda *a: (infer_body, mask))
    monkeypatch.setattr(mod, "_platform_read_region",
                        lambda s, a, e, d, j: (b"img", _region_payload(200, 200)))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 200, r.get_json()
    assert len(r.get_data()) < 64 * 1024


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
