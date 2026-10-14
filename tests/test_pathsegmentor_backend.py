# -*- coding: utf-8 -*-
"""PathSegmentor Measure 插件后端单元测试（机制级；推理服务用 stub）。

覆盖（docs/measurement-models-homepc-implementation.md §3 计量契约）：
  - 入口守卫：缺 X-Dispatch-Principal / 委托凭证 → 401；参数校验 → 400；
  - mask → level-0 映射（src 缩放）、面积占比、MPP µm² 换算（缺失只给 px）、
    连通域计数与小目标过滤、尺寸分布、top 连通域 bbox/质心；
  - 输出长边选层（segment_region ~0.25 µm/px 目标 + ≤1024 钳制）；
  - count_cells：固定分辨率枚举（512/768/1024）、推理输入 MPP = 原生 MPP ×
    实际渲染缩放（R3）、缩放预算资源准入（R2）、MPP 缺失发 null（R4）；
  - 推理服务错误映射：busy / not_ready / 不可达 / 400 / 契约不符（mask 尺寸
    不一致）；
  - 响应卫生：恒 ≪64KiB、无 mask/base64、不回显委托凭证。

运行：cd 项目根 && python3 -m pytest tests/test_pathsegmentor_backend.py -q
"""
import base64
import io
import json
import math
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
                        lambda slide, jwt, slide_id=None, delegated=None:
                        {"width": 100000, "height": 100000,
                         "mpp": mpp,
                         "asset_revision": "sha256:abcd1234"})

    def _read(slide, args, edge, delegated, jwt, slide_id=None):
        assert delegated == DELEGATED, "region 读取必须携带委托凭证"
        assert edge <= mod._MAX_LONG_EDGE
        return image, region
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    return region


_PRINCIPAL = {"type": "agent", "user_id": "u1", "role": "user",
              "session_id": "s1"}


def _call(mod, arguments=None, *, headers=None, slide="demo.svs",
          capability="segment_region"):
    base = {"X-Dispatch-Principal": json.dumps(_PRINCIPAL),
            "X-Delegated-Read-Token": DELEGATED}
    if headers is not None:
        base = headers
    return mod.app.test_client().post(
        "/capabilities/%s" % capability,
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
    assert out["approx_object_count"]["connected_components"] == 1
    # µm 尺度可判定 → 拆分 guard 字段齐备
    assert out["approx_object_count"]["objects_resolved"] is not None
    assert "estimated_count_split" in out["approx_object_count"]


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
                           None)  # mpp 缺失：本用例只看 CC 数学（拆分见下节）
    assert out["approx_object_count"]["connected_components"] == 1
    assert out["approx_object_count"]["estimated_count_split"] is None
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
    assert out["approx_object_count"]["connected_components"] == 5
    assert len(out["components_top"]) == 3


def test_mask_size_mismatch_rejected(monkeypatch):
    mod = _load_backend(monkeypatch)
    grid = [[1, 0], [0, 0]]
    with pytest.raises(mod.PlatformError) as ei:
        mod.analyze_mask(grid, 2, 2, _region_payload(4, 4), 0.5)
    assert ei.value.code == "inference_contract"


def test_target_long_edge_math(monkeypatch):
    mod = _load_backend(monkeypatch)
    assert mod._TARGET_MPP == pytest.approx(0.25)  # 默认目标 0.25 µm/px
    # 1000px @0.25mpp → 1000 输出（恰 0.25µm/px；≤1024 合同上限，≤源长边）
    edge, coarse = mod._target_long_edge(1000, 800, 0.25)
    assert edge == 1000 and coarse is False
    # 512px @0.5mpp → 想要 1024，但源长边只有 512：钳到原生（不上采样）
    edge, coarse = mod._target_long_edge(512, 512, 0.5)
    assert edge == 512 and coarse is True
    # 4096px @0.5mpp → 想要 8192，钳到 1024 合同上限（变粗，警告）
    edge, coarse = mod._target_long_edge(4096, 4096, 0.5)
    assert edge == 1024 and coarse is True
    # MPP 未知 → 钳到源长边（不上采样）而非 1024 上限
    edge, coarse = mod._target_long_edge(512, 512, None)
    assert edge == 512 and coarse is True


# --------------------------------------------------------------------------- #
# 2b. 计数拆分（距离变换 + 分水岭，guard：objects_resolved）
# --------------------------------------------------------------------------- #
def _discs_grid(size, centers, r):
    g = [[0] * size for _ in range(size)]
    for cy, cx in centers:
        for y in range(size):
            for x in range(size):
                if (y - cy) ** 2 + (x - cx) ** 2 <= r * r:
                    g[y][x] = 1
    return g


def _analyze_scaled(mod, grid, mpp=0.25):
    """src==out（scale 1）的 analyze_mask 便捷封装：输出像素=level-0 像素。"""
    h, w = len(grid), len(grid[0])
    return mod.analyze_mask(grid, w, h, _region_payload(w, h, (0, 0, w, h)),
                            mpp)


def test_split_separated_discs_counts_truth(monkeypatch):
    """分离圆盘：已分辨（resolved=1.0）→ 拆分计数 ≈ 真实目标数。"""
    mod = _load_backend(monkeypatch)
    # r=14（直径 28px）；mpp 0.25、scale 1 → 期望直径 28px，期望面积≈615
    grid = _discs_grid(80, [(30, 15), (30, 60)], 14)
    out = _analyze_scaled(mod, grid)
    aoc = out["approx_object_count"]
    assert aoc["connected_components"] == 2
    assert aoc["objects_resolved"] == pytest.approx(1.0)
    assert aoc["estimated_count_split"] == 2
    assert aoc["split_method"]


def test_split_touching_two_discs(monkeypatch):
    """两枚相切/重叠圆盘（一个连通域）→ 连通域=1、拆分=2。"""
    mod = _load_backend(monkeypatch)
    grid = _discs_grid(80, [(30, 22), (30, 42)], 14)  # 圆心距 20 < 2r，粘连
    out = _analyze_scaled(mod, grid)
    aoc = out["approx_object_count"]
    assert aoc["connected_components"] == 1
    assert aoc["estimated_count_split"] == 2


def test_split_touching_three_discs(monkeypatch):
    """三枚链状粘连圆盘 → 连通域=1、拆分=3。"""
    mod = _load_backend(monkeypatch)
    grid = _discs_grid(100, [(30, 20), (30, 40), (30, 60)], 14)
    out = _analyze_scaled(mod, grid)
    aoc = out["approx_object_count"]
    assert aoc["connected_components"] == 1
    assert aoc["estimated_count_split"] == 3


def test_band_blob_guard_null(monkeypatch):
    """整片细胞带（单个大区域 ≫ 期望单目标面积）→ resolved 低，拆分为 null
    并附 mask_is_region_not_instances 警告；connected_components 照常给出。"""
    mod = _load_backend(monkeypatch)
    grid = [[1] * 100 for _ in range(30)]  # 3000px ≫ 3×期望面积(~1846)
    out = _analyze_scaled(mod, grid)
    aoc = out["approx_object_count"]
    assert aoc["connected_components"] == 1
    assert aoc["estimated_count_split"] is None
    assert aoc["objects_resolved"] == pytest.approx(0.0)
    assert any("mask_is_region_not_instances" in w for w in out["warnings"])


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
    assert result["approx_object_count"]["connected_components"] == 1
    assert result["components_top"][0]["bbox_l0"] == [10, 20, 8, 8]
    assert result["components_top"][0]["centroid_l0"] == [12, 22]
    # 响应卫生：无 mask/base64 图像、不回显委托凭证、体量 ≪64KiB
    text = r.get_data(as_text=True)
    assert DELEGATED not in text
    assert "mask_png_b64" not in text and "image_base64" not in text
    assert len(r.get_data()) < 32 * 1024


def test_happy_path_with_slide_id_uses_result_and_Echoes_id(monkeypatch):
    """dispatch 转发的 slide_id（P2 双字段）透传到平台回调与结果。"""
    mod = _happy_setup(monkeypatch)
    seen = {}

    def _read(slide, args, edge, delegated, jwt, slide_id=None):
        seen["slide_id"] = slide_id
        return b"img", _region_payload(4, 4, src=(10, 20, 16, 16))
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    r = mod.app.test_client().post(
        "/capabilities/segment_region",
        json={"slide": "26-005817.tif", "slide_id": "sld_abc",
              "arguments": GOOD_ARGS},
        headers={"X-Dispatch-Principal": json.dumps(_PRINCIPAL),
                 "X-Delegated-Read-Token": DELEGATED})
    assert r.status_code == 200, r.get_json()
    assert seen["slide_id"] == "sld_abc"
    assert r.get_json()["result"]["slide_id"] == "sld_abc"


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
                        lambda s, a, e, d, j, slide_id=None:
                        (b"img", _region_payload(200, 200)))
    r = _call(mod, GOOD_ARGS)
    assert r.status_code == 200, r.get_json()
    assert len(r.get_data()) < 64 * 1024


# --------------------------------------------------------------------------- #
# 4. count_cells（Cellpose 实例计数；stub /v1/count 服务）
# --------------------------------------------------------------------------- #
COUNT_ARGS = {"x": 10, "y": 20, "w": 64, "h": 64}


def _count_response():
    """Cellpose /v1/count stub 响应：输出图 32×32，实例坐标为输出像素。"""
    return {
        "model_id": "cellpose", "model_name": "nuclei",
        "model_sha256": "cd" * 32, "code_version": "v1.2",
        "input_width": 32, "input_height": 32,
        "instance_count": 3,
        "diameter_px_used": 14.0,
        "areas_px": {"min": 10.0, "p25": 12.0, "median": 14.0,
                     "p75": 16.0, "max": 18.0},
        "instances_top": [
            {"bbox": [2, 4, 6, 6], "centroid": [5.0, 7.0], "area_px": 28.0},
            {"bbox": [20, 10, 4, 4], "centroid": [22.0, 12.0], "area_px": 14.0},
        ],
        "timings_ms": {"decode": 1, "infer": 4, "total": 5},
        "warnings": [],
    }


class _CellposeStubRequests(_InferStubRequests):
    """`requests` 替身：/v1/count 按 script 应答（默认成功响应）。"""

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        assert url.startswith(INFER) or "8972" in url, url
        spec = self.script.get("POST /v1/count")
        if spec is None:
            return _Resp(200, _count_response())
        if isinstance(spec, Exception):
            raise spec
        return _Resp(*spec)


def test_count_cells_happy_path_mapping_density_um2(monkeypatch):
    """实例坐标映射回 level-0、µm² 换算、密度（每 mm²）数学。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen.update({"mpp": mpp_arg, "diameter_um": diameter_um,
                     "model": model})
        return _count_response()
    _stub_platform(monkeypatch, mod,
                   region=_region_payload(32, 32, (0, 0, 64, 64)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, dict(COUNT_ARGS, diameter_um=9), capability="count_cells")
    assert r.status_code == 200, r.get_json()
    # 服务端收到的 mpp / diameter。R3 修正旧断言的物理意义：64 level-0 px
    # @原生 0.5 渲染成 32 输出 px（scale 2）→ 推理输入 MPP = 0.5×2 = 1.0，
    # 不是原生的 0.5（密度/坐标映射仍用原生 MPP，见下方数学断言）。
    assert seen["mpp"] == pytest.approx(1.0) and seen["diameter_um"] == 9
    result = r.get_json()["result"]
    assert result["capability"] == "count_cells"
    assert result["analysis_id"].startswith("cpose-")
    assert result["instance_count"] == 3
    assert result["model"] == {"model_id": "cellpose",
                               "model_name": "nuclei",
                               "model_sha256": "cd" * 32,
                               "code_version": "v1.2"}
    # 映射：scale 2（64/32）；src (0,0) → bbox [2*2, 4*2, 6*2, 6*2]
    inst0 = result["instances_top"][0]
    assert inst0["bbox_l0"] == [4, 8, 12, 12]
    assert inst0["centroid_l0"] == [10, 14]
    # µm²：area_px(输出) × scale² × mpp² = 28 × 4 × 0.25 = 28
    assert inst0["area_um2"] == pytest.approx(28.0)
    assert inst0["area_px_l0"] == pytest.approx(112.0)
    # 面积分布：area_px × 1.0（scale²·mpp² = 4×0.25 = 1 µm²/输出px）
    assert result["area_stats_um2"]["median"] == pytest.approx(14.0)
    assert result["area_stats_um2"]["max"] == pytest.approx(18.0)
    # 密度：ROI = (64·0.5/1000)² mm² = 1.024e-3；3 / 1.024e-3 ≈ 2929.7
    assert result["density"]["roi_area_mm2"] == pytest.approx(1.024e-3)
    assert result["density"]["per_mm2"] == pytest.approx(
        3 / 1.024e-3, rel=1e-3)
    assert "实例" in result["instance_count_note"]
    # 卫生：无 label map / base64 / 凭证；体量小
    text = r.get_data(as_text=True)
    assert DELEGATED not in text
    assert "mask_png_b64" not in text and "label" not in result
    assert len(r.get_data()) < 32 * 1024


def test_count_cells_default_diameter_forwarded(monkeypatch):
    """缺省 diameter_um=7 传给服务；model 省略不传。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen.update({"diameter_um": diameter_um, "model": model})
        return _count_response()
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["diameter_um"] is None  # 端点不传，服务端用自身缺省；
    assert seen["model"] is None
    # 结果层 diameter_um 如实回显缺省值
    assert r.get_json()["result"]["diameter_um"] == mod._COUNT_DIAMETER_DEFAULT_UM


def test_count_cells_validates_arguments(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    for bad in ({"x": 0, "y": 0, "w": 8, "h": 8, "diameter_um": 0},
                {"x": 0, "y": 0, "w": 8, "h": 8, "diameter_um": 500},
                {"x": 0, "y": 0, "w": 8, "h": 8, "model": "  "},
                {"x": 0, "y": 0, "w": 8, "h": 8, "diameter_um": "big"}):
        r = _call(mod, bad, capability="count_cells")
        assert r.status_code == 400, bad
        assert r.get_json()["error"]["code"] == "invalid_request"


def test_count_cells_mpp_missing_no_density(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod, mpp=None)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 200
    result = r.get_json()["result"]
    assert result["density"]["per_mm2"] is None
    assert result["area_stats_um2"] is None
    assert result["instances_top"][0]["area_um2"] is None
    assert any("MPP" in w for w in result["warnings"])


def test_count_cells_instances_top_capped(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    count = _count_response()
    count["instances_top"] = [
        {"bbox": [i, i, 2, 2], "centroid": [i + 1.0, i + 1.0],
         "area_px": 4.0} for i in range(30)]
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: count)
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 200
    assert len(r.get_json()["result"]["instances_top"]) == \
        mod._MAX_COMPONENTS


def test_count_cells_error_mapping(monkeypatch):
    """busy/not_ready/unreachable/400 与 PSM 同款错误映射。"""
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (503, {"error": "busy"})}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_busy"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (503, {"error": "not_ready"})}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.get_json()["error"]["code"] == "inference_not_ready"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": real_requests.ConnectionError("down")}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.get_json()["error"]["code"] == "inference_unavailable"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (400, {"error": "bad_input",
                                 "message": "image too large"})}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_argument"


def test_count_cells_contract_violation(monkeypatch):
    """200 但缺 instance_count → inference_contract（可重试）。"""
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    bad = _count_response()
    del bad["instance_count"]
    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (200, bad)}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_contract"


def _stub_read(monkeypatch, mod, region):
    """替换平台 region 读取（无 1024 长边断言；count 可按计划读到 2048）。"""
    seen = {}

    def _read(slide, args, edge, delegated, jwt, slide_id=None):
        seen["edge"] = edge
        return b"img", region

    monkeypatch.setattr(mod, "_platform_read_region", _read)
    return seen


def test_count_render_plan_math(monkeypatch):
    """R2 重设计：渲染尺度 = max(原生, d/30)，resolution 只是处理长边上限。"""
    mod = _load_backend(monkeypatch)
    assert mod._COUNT_RESOLUTIONS == (512, 1024, 2048)
    assert mod._COUNT_RESOLUTION_DEFAULT == 2048
    # 细于处理尺度（native 0.2 < 7/30≈0.2333）：按 d/30 渲染、rescale=1
    plan = mod._count_render_plan(1000, 800, 0.2, 7.0, 1024)
    assert plan["mpp_render"] == pytest.approx(7.0 / 30.0)
    assert plan["edge_long"] == 858   # ceil(1000×0.2×30/7)
    assert plan["edge_short_req"] == 686
    assert plan["rescale"] == pytest.approx(1.0)
    assert plan["native_clamped"] is False
    # 原生粗于处理尺度（native 0.5）：按原生渲染、模型内部上采样 30×0.5/7
    plan = mod._count_render_plan(600, 400, 0.5, 7.0, 1024)
    assert plan["mpp_render"] == pytest.approx(0.5)
    assert plan["edge_long"] == 600
    assert plan["rescale"] == pytest.approx(30.0 * 0.5 / 7.0)
    assert plan["native_clamped"] is True
    # null mpp：按所选选项长边渲染（钳到源长边），rescale 1.0
    plan = mod._count_render_plan(4096, 2000, None, 7.0, 1024)
    assert plan["edge_long"] == 1024 and plan["rescale"] == 1.0
    plan = mod._count_render_plan(500, 300, None, 7.0, 2048)
    assert plan["edge_long"] == 500


def test_count_max_roi_table(monkeypatch):
    """d=7、原生细于处理尺度（0.2 < 7/30）时各选项最大视场：
    119.5/238.9/477.9 µm；原生更粗（0.25）时按原生渲染、上限随之增大。"""
    mod = _load_backend(monkeypatch)
    for res, um in ((512, 119.4667), (1024, 238.9333), (2048, 477.8667)):
        max_um, max_l0 = mod._count_max_roi(0.2, 7.0, res)
        assert max_um == pytest.approx(um, abs=1e-3)
        assert max_l0 == math.floor(max_um / 0.2)
    # 原生 0.25 > 7/30：mpp_render = 原生 → 上限 = resolution×0.25
    max_um, max_l0 = mod._count_max_roi(0.25, 7.0, 1024)
    assert max_um == pytest.approx(256.0)
    assert max_l0 == 1024


def test_count_cells_validates_resolution(monkeypatch):
    """resolution 只接受固定枚举（512/1024/2048），其余 400。"""
    mod = _load_backend(monkeypatch)
    for bad in (768, "1024", True, 1024.0, 0):
        r = _call(mod, dict(COUNT_ARGS, resolution=bad),
                  capability="count_cells")
        assert r.status_code == 400, bad
        assert r.get_json()["error"]["code"] == "invalid_request"


def test_count_cells_exceeds_option_rejected_before_region_read(monkeypatch):
    """所需处理长边超过选项：读 ROI **之前** 400 resource_limit（R2 重设计）。

    2048 level-0 px @0.25、d=7 → 处理尺度 0.2333 粗于原生 → 按原生渲染需要
    2048 px 长边 > 显式选择的 1024 选项；不做「渲染到 1024 再让模型放大
    30/14 倍」（缺省 2048 时同一 ROI 会放行，见 reviewer 回归）。
    """
    mod = _load_backend(monkeypatch)
    reads = []

    def _read(*a, **kw):
        reads.append(a)
        return b"img", _region_payload(1024, 1024, (0, 0, 2048, 2048))

    _stub_platform(monkeypatch, mod, mpp=0.25)
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 2048, "resolution": 1024},
              capability="count_cells")
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "resource_limit"
    assert err["extra"]["resolution"] == 1024
    assert err["extra"]["diameter_um"] == 7.0
    # 原生 0.25 > d/30 → mpp_render = 原生：上限 = resolution×native
    assert err["extra"]["max_roi_um"] == [256.0, 256.0]
    assert err["extra"]["max_roi_level0_px"] == [1024, 1024]
    assert reads == [], "must reject before any region read"


def test_count_cells_resolution_2048_fits_big_roi(monkeypatch):
    """同一 ROI 选 2048 选项即可处理：渲染长边 2048、R3 推理 mpp 不变。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.25)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(1024, 1024, (0, 0, 2048, 2048)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 2048, "resolution": 2048},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 2048
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["resolution"] == 2048
    assert result["rendered"]["requested_long_edge"] == 2048


def test_count_cells_inference_mpp_from_actual_scale(monkeypatch):
    """R3：推理 mpp = 原生 × 实际渲染缩放；原生 MPP 仍用于密度映射。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.25)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(1024, 1024, (0, 0, 2048, 2048)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 2048, "resolution": 2048},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 2048
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["inference_input"]["mpp"] == pytest.approx(0.5)
    assert result["inference_input"]["mpp_render"] == pytest.approx(0.25)
    assert result["inference_input"]["scale"] == {"x": 2.0, "y": 2.0}
    # 原生 MPP 映射密度：ROI = (2048·0.25/1000)² mm²
    assert result["mpp"]["x"] == pytest.approx(0.25)
    assert result["density"]["roi_area_mm2"] == pytest.approx(0.262144)


def test_count_cells_render_at_processing_scale(monkeypatch):
    """细于处理尺度的切片（native 0.2 < 7/30）：按 d/30 渲染、推理 mpp 即
    处理尺度（rescale≈1，模型不再缩放）。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    # 平台按 max_long_edge=858 保宽高比渲染：out = (858, round(800·858/1000))
    _stub_platform(monkeypatch, mod, mpp=0.2)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(858, 686, (0, 0, 1000, 800)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 1000, "h": 800},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 858
    assert seen["mpp"] == pytest.approx(7.0 / 30.0, rel=0.01)
    result = r.get_json()["result"]
    assert result["inference_input"]["mpp_render"] == pytest.approx(7.0 / 30.0)
    # 预算防御（实际几何）：rescale = 30×infer_mpp/7 ≈ 1
    assert seen["mpp"] == pytest.approx(0.2 * 1000 / 858, rel=1e-3)


def test_count_cells_native_coarser_renders_at_native(monkeypatch):
    """原生粗于处理尺度：按原生渲染（不上采样超过原生），模型内部放大并在
    结果中给出 warning。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.5)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(600, 400, (0, 0, 600, 400)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 600, "h": 400},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 600
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["inference_input"]["mpp_render"] == pytest.approx(0.5)
    assert any("上采样" in w for w in result["warnings"])


def test_count_cells_mpp_null_uses_option_edge(monkeypatch):
    """null mpp：按所选 resolution 长边渲染（钳到源长边），算力 = edge²。"""
    mod = _load_backend(monkeypatch)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    _stub_platform(monkeypatch, mod, mpp=None)
    # 显式 1024 选项
    seen = _stub_read(monkeypatch, mod,
                      _region_payload(1024, 500, (0, 0, 4096, 2000)))
    r = _call(mod, {"x": 0, "y": 0, "w": 4096, "h": 2000, "resolution": 1024},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["edge"] == 1024
    # 缺省 2048 选项
    seen = _stub_read(monkeypatch, mod,
                      _region_payload(2048, 1000, (0, 0, 4096, 2000)))
    r = _call(mod, {"x": 0, "y": 0, "w": 4096, "h": 2000},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["edge"] == 2048
    # 源长边钳制
    seen = _stub_read(monkeypatch, mod,
                      _region_payload(500, 300, (0, 0, 500, 300)))
    r = _call(mod, {"x": 0, "y": 0, "w": 500, "h": 300},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["edge"] == 500


def test_count_cells_no_scale_inference_mpp_native(monkeypatch):
    """无缩放（src==out）→ 推理 mpp == 原生 MPP。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.5,
                   region=_region_payload(64, 64, (0, 0, 64, 64)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["mpp"] == pytest.approx(0.5)


def test_count_cells_scale_from_actual_src_after_clamp(monkeypatch):
    """边缘夹取：scale 按实际返回的 src/输出算，不用请求的目标长边。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    # 请求 2048×64 @0.25 → 处理长边 2048；平台夹取后返回 src 512×32 → 256×16
    _stub_platform(monkeypatch, mod, mpp=0.25)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(256, 16, (0, 0, 512, 32)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 64, "resolution": 2048},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 2048
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["inference_input"]["scale"] == {"x": 2.0, "y": 2.0}


def test_count_cells_non_square_scale(monkeypatch):
    """长宽不等 ROI：两轴各自按实际 src/输出取整后的缩放一致。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.25)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(1024, 512, (0, 0, 2048, 1024)))
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 1024, "resolution": 2048},
              capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen_edge["edge"] == 2048
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["inference_input"]["scale"] == {"x": 2.0, "y": 2.0}
    assert result["rendered"]["width"] == 1024
    assert result["rendered"]["height"] == 512


def test_count_cells_anisotropic_scale_rejected(monkeypatch):
    """两轴 MPP 相对差 >1% → 拒绝（单一 MPP 合同不做猜测）。"""
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod, mpp=0.25)
    seen_edge = _stub_read(monkeypatch, mod,
                           _region_payload(1024, 400, (0, 0, 2048, 1024)))
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call(mod, {"x": 0, "y": 0, "w": 2048, "h": 1024, "resolution": 2048},
              capability="count_cells")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "anisotropic_scale"


def test_count_cells_resource_limit_before_inference(monkeypatch):
    """原生粗于处理尺度时处理预算可成为紧约束：超预算在读图前拒绝，引导按
    真实物理（缩小 ROI/增大直径），不建议调 resolution；增大直径后放行。"""
    mod = _load_backend(monkeypatch)
    calls = []
    reads = []

    def _infer(image_png_bytes, mpp_arg, **kw):
        calls.append(mpp_arg)
        return _count_response()

    def _read(*a, **kw):
        reads.append(a)
        return b"img", _region_payload(1024, 1024, (0, 0, 1024, 1024))

    # 原生 2.0 µm/px、1024² level-0（缺省 2048 选项内）：处理尺度 0.2333
    # 粗于原生 → 按原生渲染 1024 长边，但模型内部放大 30×2/7≈8.57×
    # → 8778² ≈ 77M px
    _stub_platform(monkeypatch, mod, mpp=2.0)
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, {"x": 0, "y": 0, "w": 1024, "h": 1024},
              capability="count_cells")
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "resource_limit"
    assert err["extra"]["budget_pixels"] == mod._CELLPOSE_MAX_SCALED_PIXELS
    assert "diameter" in err["message"]
    assert "resolution 无法" in err["message"]
    assert reads == [] and calls == [], "reject before region read/inference"

    # 直径 30 → rescale = 30×2/30 = 2 → 2048² = 4.19M px，预算内放行
    r2 = _call(mod, {"x": 0, "y": 0, "w": 1024, "h": 1024, "diameter_um": 30},
               capability="count_cells")
    assert r2.status_code == 200, r2.get_json()
    assert calls == [2.0]
    assert len(reads) == 1


def test_count_cells_service_resource_limit_mapping(monkeypatch):
    """服务端双保险 400 resource_limit → 透传同码（不是 invalid_argument）。"""
    mod = _load_backend(monkeypatch)
    _stub_platform(monkeypatch, mod)
    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (400, {"error": "resource_limit",
                                 "message": "upscaled beyond budget"})}))
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "resource_limit"


def test_count_cells_mpp_invalid_normalized(monkeypatch):
    """R4：原生 MPP ≤0 / 非有限 → 规范为缺失（null）。"""
    mod = _load_backend(monkeypatch)
    assert mod._normalize_mpp(0.0) is None
    assert mod._normalize_mpp(-0.5) is None
    assert mod._normalize_mpp("abc") is None
    assert mod._normalize_mpp(float("nan")) is None
    assert mod._normalize_mpp(0.25) == pytest.approx(0.25)


def test_count_cells_mpp_invalid_normalized(monkeypatch):
    """R4：原生 MPP ≤0 / 非有限 → 规范为缺失（null）。"""
    mod = _load_backend(monkeypatch)
    assert mod._normalize_mpp(0.0) is None
    assert mod._normalize_mpp(-0.5) is None
    assert mod._normalize_mpp("abc") is None
    assert mod._normalize_mpp(float("nan")) is None
    assert mod._normalize_mpp(0.25) == pytest.approx(0.25)


def test_count_cells_mpp_zero_sends_null_and_pixel_only(monkeypatch):
    """原生 MPP=0（无效）→ 推理收 null、结果仅像素口径。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None, model=None):
        seen["mpp"] = mpp_arg
        return _count_response()

    _stub_platform(monkeypatch, mod, mpp=0.0)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call(mod, COUNT_ARGS, capability="count_cells")
    assert r.status_code == 200, r.get_json()
    assert seen["mpp"] is None
    result = r.get_json()["result"]
    assert result["density"]["per_mm2"] is None
    assert result["area_stats_um2"] is None
    assert result["inference_input"]["mpp"] is None
    assert any("MPP" in w for w in result["warnings"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
