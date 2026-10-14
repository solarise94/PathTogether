# -*- coding: utf-8 -*-
"""PathSegmentor Measure 插件后端单元测试（机制级；推理服务用 stub）。

覆盖（docs/measurement-models-homepc-implementation.md §3 计量契约）：
  - 入口守卫：缺 X-Dispatch-Principal / 委托凭证 → 401；参数校验 → 400；
  - mask → level-0 映射（src 缩放）、面积占比、MPP µm² 换算（缺失只给 px）、
    连通域计数与小目标过滤、尺寸分布、top 连通域 bbox/质心；
  - 输出长边选层（segment_region ~0.25 µm/px 目标 + ≤1024 钳制）；
  - count_cells（v2.0.0 方形取景）：模型只提供档位与 Cellpose 参数
    （其他键后端 400），位置只来自平台转发的 context.snapshot；方形规划器
    （快照中心、不越界、处理上限收边用真实取整物理）、推理输入 MPP =
    原生 MPP × 实际渲染缩放（R3）、读后方形一致性硬闸、阈值显式转发、
    MPP 缺失发 null 且直径不参与（R4）；
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
# 4. count_cells（Cellpose 实例计数；方形取景 v2.0.0；stub /v1/count 服务）
# --------------------------------------------------------------------------- #
def _snap_ctx(x, y, w, h, snapshot_id="snap-1", rev="sha256:abcd1234",
              fp=None):
    """wire 合同 v1 §4：平台验签后转发的结构化 context.snapshot。"""
    return {"context": {"snapshot": {
        "snapshot_id": snapshot_id,
        "bbox": {"x": x, "y": y, "w": w, "h": h},
        "slide_revision": rev,
        "render_context_fingerprint": fp,
    }}}


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


def _mirror_max_side(mod, mpp, diameter_um, resolution, geo_cap):
    """真实取整物理下的最大可处理方形边长（测试侧权威：与规划器同一镜像
    函数逐级递减；规划器不得依赖理想公式）。"""
    side = geo_cap
    while side >= 1 and not mod._square_admits(side, mpp, diameter_um,
                                               resolution):
        side -= 1
    return side


def _stub_count_platform(monkeypatch, mod, *, mpp=0.5, slide=(100000, 100000)):
    """count_cells 平台回调 stub：info 带尺寸/MPP；region 按方形请求渲染
    （界内方形 → 恰好请求边² 输出，与真实平台 `_aspect_fit_size` 一致）。
    返回 reads——每次读的 (x, y, w, h, edge) 记录。"""
    monkeypatch.setattr(mod, "_installation_jwt", lambda: "jwt-stub")
    monkeypatch.setattr(
        mod, "_platform_slide_info",
        lambda _slide, _jwt, slide_id=None, delegated=None:
        {"width": slide[0], "height": slide[1], "mpp": mpp,
         "asset_revision": "sha256:abcd1234"})
    reads = []

    def _read(slide, args, edge, delegated, jwt, slide_id=None):
        assert delegated == DELEGATED, "region 读取必须携带委托凭证"
        reads.append((args["x"], args["y"], args["w"], args["h"], edge))
        assert args["w"] == args["h"], "count_cells 只读方形 region"
        out = max(1, min(int(edge), 4096))
        return b"img", _region_payload(out, out, (args["x"], args["y"],
                                                  args["w"], args["h"]))

    monkeypatch.setattr(mod, "_platform_read_region", _read)
    return reads


def _call_count(mod, args=None, *, snap=(0, 0, 500, 500), **snap_kw):
    body = _snap_ctx(*snap, **snap_kw)
    body["slide"] = "demo.svs"
    body["slide_id"] = "sld_demo"
    body["arguments"] = args if args is not None else {}
    return mod.app.test_client().post(
        "/capabilities/count_cells",
        json=body,
        headers={"X-Dispatch-Principal": json.dumps(_PRINCIPAL),
                 "X-Delegated-Read-Token": DELEGATED})


def test_count_cells_only_resolution_works_and_maps_level0(monkeypatch):
    """只传 {resolution:1024} 即可计数：方形自动取框、坐标映射回 level-0。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None,
               flow_threshold=None, cellprob_threshold=None):
        seen.update({"mpp": mpp_arg, "diameter_um": diameter_um,
                     "flow": flow_threshold, "cellprob": cellprob_threshold})
        return _count_response()

    # 快照 500² @原生 0.5（粗于 d/30）→ 原生像素渲染、模型内部上采样；
    # res 1024：真实物理最大方形边长 = 478（int(478×30×0.5/7)=1024 恰在档内）。
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call_count(mod, {"resolution": 1024}, snap=(100, 200, 500, 500))
    assert r.status_code == 200, r.get_json()
    assert reads == [(111, 211, 478, 478, 478)], reads  # 快照中心整框
    # 推理输入 MPP = 原生 × 实际缩放（out==src → 0.5）
    assert seen["mpp"] == pytest.approx(0.5)
    # 解析后的缺省参数显式转发（与 services/cellpose 权威缺省镜像一致）
    assert seen["diameter_um"] == pytest.approx(7.0)
    assert seen["flow"] == pytest.approx(0.4)
    assert seen["cellprob"] == pytest.approx(0.0)
    result = r.get_json()["result"]
    assert result["capability"] == "count_cells"
    assert result["analysis_id"].startswith("cpose-")
    assert result["instance_count"] == 3
    # counted_region：实际方形 + 快照身份 + 披露（478 < 500 → reduced）
    cr = result["counted_region"]
    assert cr["bbox"] == {"x": 111, "y": 211, "w": 478, "h": 478}
    assert cr["snapshot_id"] == "snap-1"
    assert cr["slide_revision"] == "sha256:abcd1234"
    # review 2026-10-14 修复 3：counted_region 携带稳定切片 ID（viewer 高亮
    # 跨切片绑定用；dispatch 转发的 body.slide_id）
    assert cr["slide_id"] == "sld_demo"
    assert cr["reduced"] is True
    assert "不是整张切片" in cr["note"]
    # 密度：ROI = (478·0.5/1000)² mm²；3 / ROI
    roi_mm2 = (478 * 0.5 / 1000.0) ** 2
    assert result["density"]["roi_area_mm2"] == pytest.approx(roi_mm2)
    assert result["density"]["per_mm2"] == pytest.approx(3 / roi_mm2)
    # 处理几何：输入/处理边长如实回显（原生渲染 → 推理 mpp 0.5、内部放大）
    assert result["processing"]["input_edge"] == 478
    assert result["processing"]["processing_edge"] == int(478 * 30 * 0.5 / 7.0)
    assert result["processing"]["processing_edge"] <= 1024
    assert result["cellpose_params"]["resolution"] == 1024
    # 卫生：无 label map / base64 / 凭证；体量小
    text = r.get_data(as_text=True)
    assert DELEGATED not in text
    assert "mask_png_b64" not in text and "label" not in result
    assert len(r.get_data()) < 32 * 1024


def test_count_cells_square_equal_to_snapshot_not_reduced(monkeypatch):
    """方形恰好覆盖快照短边（未缩）→ reduced=False；note 仍说明非整片总数。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 2048}, snap=(0, 0, 500, 500))
    assert r.status_code == 200, r.get_json()
    assert reads == [(0, 0, 500, 500, 500)]  # 500² 在 2048 档内
    cr = r.get_json()["result"]["counted_region"]
    assert cr["reduced"] is False
    assert "不是整张切片" in cr["note"]


def test_count_cells_reduced_disclosure(monkeypatch):
    """方形小于快照（档位收边）→ reduced=True + 明确「只统计这方区域」。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 512}, snap=(0, 0, 4000, 4000))
    assert r.status_code == 200, r.get_json()
    result = r.get_json()["result"]
    cr = result["counted_region"]
    assert cr["reduced"] is True
    assert "方形" in cr["note"] and "不是整张切片" in cr["note"]
    assert any("整片总数" in w for w in result["warnings"])
    # side = 512 档真实物理最大（int(239×30×0.5/7)=512 恰在档内）
    side = _mirror_max_side(mod, 0.5, 7.0, 512, 4000)
    assert side == 239
    assert cr["bbox"] == {"x": (4000 - 239) // 2, "y": (4000 - 239) // 2,
                          "w": 239, "h": 239}
    assert reads == [(cr["bbox"]["x"], cr["bbox"]["y"], 239, 239, 239)]


def test_count_cells_default_diameter_and_thresholds_forwarded(monkeypatch):
    """缺省参数解析后显式转发；resolution 缺省 2048。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None,
               flow_threshold=None, cellprob_threshold=None):
        seen.update({"diameter_um": diameter_um, "flow": flow_threshold,
                     "cellprob": cellprob_threshold})
        return _count_response()

    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call_count(mod, {}, snap=(0, 0, 500, 500))
    assert r.status_code == 200, r.get_json()
    assert seen["diameter_um"] == pytest.approx(
        mod._COUNT_DIAMETER_DEFAULT_UM)
    assert seen["flow"] == pytest.approx(
        mod._CELLPOSE_FLOW_THRESHOLD_DEFAULT)
    assert seen["cellprob"] == pytest.approx(
        mod._CELLPOSE_CELLPROB_THRESHOLD_DEFAULT)
    result = r.get_json()["result"]
    assert result["resolution"] == mod._COUNT_RESOLUTION_DEFAULT
    assert result["cellpose_params"]["diameter_um"] == pytest.approx(7.0)


def test_count_cells_explicit_thresholds_forwarded(monkeypatch):
    """显式阈值原样转发（服务端 _validate_request 同域复核）。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None,
               flow_threshold=None, cellprob_threshold=None):
        seen.update({"flow": flow_threshold, "cellprob": cellprob_threshold,
                     "diameter_um": diameter_um})
        return _count_response()

    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call_count(mod, {"diameter_um": 15, "flow_threshold": 0.6,
                          "cellprob_threshold": -1.5},
                    snap=(0, 0, 500, 500))
    assert r.status_code == 200, r.get_json()
    assert seen["flow"] == pytest.approx(0.6)
    assert seen["cellprob"] == pytest.approx(-1.5)
    assert seen["diameter_um"] == pytest.approx(15.0)


def test_count_cells_rejects_non_whitelisted_keys(monkeypatch):
    """additionalProperties:false 之外后端同样拒绝：几何/身份/内部键全 400。"""
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    for bad in ({"x": 1}, {"y": 1}, {"w": 8}, {"h": 8},
                {"center": [1, 2]}, {"side": 100}, {"bbox": {}},
                {"mpp": 0.5}, {"snapshot_id": "s"}, {"slide_id": "s"},
                {"render_context": {}}, {"return_labels": True},
                {"model": "cpsam"}, {"resolution": 512, "extra": 1}):
        r = _call_count(mod, bad, snap=(0, 0, 500, 500))
        assert r.status_code == 400, (bad, r.get_json())
        assert r.get_json()["error"]["code"] == "invalid_request"


def test_count_cells_validates_argument_values(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    for bad in ({"resolution": 768}, {"resolution": "1024"},
                {"resolution": True}, {"resolution": 1024.0},
                {"diameter_um": 0}, {"diameter_um": 500},
                {"diameter_um": "big"}, {"flow_threshold": 1.5},
                {"cellprob_threshold": 7}, {"cellprob_threshold": "x"}):
        r = _call_count(mod, bad, snap=(0, 0, 500, 500))
        assert r.status_code == 400, (bad, r.get_json())
        assert r.get_json()["error"]["code"] == "invalid_request"


def test_count_cells_requires_snapshot_context(monkeypatch):
    """无 context.snapshot → 400 snapshot_required（先 goto 再调用，读图前
    拒绝）；context 形状不完整 → invalid_request。"""
    mod = _load_backend(monkeypatch)
    reads = []
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "_platform_read_region",
                        lambda *a, **kw: reads.append(a) or (b"img", {}))
    client = mod.app.test_client()
    headers = {"X-Dispatch-Principal": json.dumps(_PRINCIPAL),
               "X-Delegated-Read-Token": DELEGATED}
    r = client.post("/capabilities/count_cells",
                    json={"slide": "demo.svs", "slide_id": "sld_demo",
                          "arguments": {}},
                    headers=headers)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "snapshot_required"
    r = client.post("/capabilities/count_cells",
                    json={"slide": "demo.svs", "slide_id": "sld_demo",
                          "arguments": {},
                          "context": {"snapshot": {"snapshot_id": "s"}}},
                    headers=headers)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    r = client.post("/capabilities/count_cells",
                    json={"slide": "demo.svs", "slide_id": "sld_demo",
                          "arguments": {},
                          "context": {"snapshot": {
                              "snapshot_id": "s",
                              "bbox": {"x": 0, "y": 0, "w": 100, "h": 0}}}},
                    headers=headers)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    assert reads == [], "快照缺失必须在读图前拒绝"


def test_count_cells_mpp_missing_pixel_only_and_diameter_ignored(monkeypatch):
    """R4/方形：MPP 缺失 → 原生像素、发 null、直径不发且附提示、仅像素口径。"""
    mod = _load_backend(monkeypatch)
    seen = {}

    def _infer(image_png_bytes, mpp_arg, diameter_um=None,
               flow_threshold=None, cellprob_threshold=None):
        seen.update({"mpp": mpp_arg, "diameter_um": diameter_um})
        return _count_response()

    reads = _stub_count_platform(monkeypatch, mod, mpp=None)
    monkeypatch.setattr(mod, "_infer_cellpose", _infer)
    r = _call_count(mod, {"resolution": 1024}, snap=(0, 0, 3000, 3000))
    assert r.status_code == 200, r.get_json()
    assert seen["mpp"] is None
    assert seen["diameter_um"] is None, "MPP 缺失时直径无物理意义，不发送"
    # 边长 ≤ 档位（1024）、方形、原生像素渲染（edge == side）、快照中心
    assert reads == [((3000 - 1024) // 2, (3000 - 1024) // 2, 1024, 1024,
                      1024)]
    result = r.get_json()["result"]
    assert result["density"]["per_mm2"] is None
    assert result["area_stats_um2"] is None
    assert result["instances_top"][0]["area_um2"] is None
    assert result["inference_input"]["mpp"] is None
    assert any("MPP" in w and "直径" in w for w in result["warnings"])


def test_count_cells_square_planner_geometry_matrix(monkeypatch):
    """中心/四角/四边/小切片/窄快照：方形不越界、居中平移、完整落在快照内。"""
    slide = (2000, 2000)
    snaps = {
        "centre": (500, 500, 400, 400),
        "top-left corner": (0, 0, 400, 400),
        "top-right corner": (1600, 0, 400, 400),
        "bottom-left corner": (0, 1600, 400, 400),
        "bottom-right corner": (1600, 1600, 400, 400),
        "left edge strip": (0, 800, 200, 400),
        "right edge strip": (1800, 800, 200, 400),
        "top edge strip": (800, 0, 400, 200),
        "bottom edge strip": (800, 1800, 400, 200),
        "narrow snapshot": (900, 900, 30, 1600),
        "tiny snapshot": (1000, 1000, 10, 12),
        "snapshot equals slide": (0, 0, 2000, 2000),
    }
    for name, snap in snaps.items():
        mod = _load_backend(monkeypatch)
        reads = _stub_count_platform(monkeypatch, mod, mpp=0.12, slide=slide)
        monkeypatch.setattr(mod, "_infer_cellpose",
                            lambda *a, **kw: _count_response())
        r = _call_count(mod, {"resolution": 512}, snap=snap)
        assert r.status_code == 200, (name, r.get_json())
        sx, sy, sw, sh = snap
        bx, by, bw, bh, edge = reads[-1]
        assert bw == bh, name  # 永远方形
        # 快照短边封顶；快照大于档位最大方形时由处理上限收边（真实物理）
        assert bw == min(sw, sh, _mirror_max_side(mod, 0.12, 7.0, 512,
                                                  4096)), name
        assert 0 <= bx and bx + bw <= slide[0], name
        assert 0 <= by and by + bh <= slide[1], name
        assert sx <= bx and bx + bw <= sx + sw, name  # 完整落在快照内
        assert sy <= by and by + bh <= sy + sh, name
        # 档位是**模型处理边长**上限（细原生下渲染边可略高于档位——模型
        # int 截断后回到档内）；处理边长按真实物理复核
        proc = mod._square_processing_dims(bw, edge, 0.12, 7.0)
        assert max(proc) <= 512, (name, edge, proc)
        # 中心定位：左上角 = 快照中心 - side/2（整数）
        assert bx == sx + (sw - bw) // 2, (name, reads)
        assert by == sy + (sh - bh) // 2, (name, reads)


def test_count_cells_planner_shrinks_to_slide_short_side(monkeypatch):
    """小切片（< 档位最大方形）：边长钳到快照/切片短边，仍方形、不出界。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.12, slide=(600, 800))
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 2048}, snap=(0, 0, 600, 800))
    assert r.status_code == 200, r.get_json()
    assert reads == [(0, 100, 600, 600, mod._square_render_edge(600, 0.12,
                                                                7.0))]
    cr = r.get_json()["result"]["counted_region"]
    assert cr["reduced"] is False  # 600 == 快照短边（矩形快照的另一轴更大）
    assert cr["bbox"]["w"] == 600 and cr["bbox"]["h"] == 600


def test_count_cells_processing_cap_shrinks_square(monkeypatch):
    """档位处理上限收边（round-2 场景的方形化）：快照 500² @0.5、res 512 →
    500² 需 1071 px 处理边 > 512 —— 规划器**缩边**到真实物理最大 239 后
    读图，而不是拒绝（设计 §自动生成不越界的方形：资源限制自动缩小边长；
    缩小经 counted_region.reduced + warning 披露）。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.5,
                                 slide=(100000, 100000))
    seen = {}
    monkeypatch.setattr(
        mod, "_infer_cellpose",
        lambda image, mpp_arg, **kw: seen.update({"mpp": mpp_arg})
        or _count_response())
    r = _call_count(mod, {"resolution": 512, "diameter_um": 7},
                    snap=(0, 0, 500, 500))
    assert r.status_code == 200, r.get_json()
    bx, by, bw, bh, edge = reads[-1]
    assert (bw, bh) == (239, 239), "方形边长缩到 512 档真实可处理上限"
    # 整数边界：239 的实际处理边长恰为 512（int(239×30×0.5/7)=512）
    assert int(239 * 30 * 0.5 / 7.0) == 512
    assert max(mod._square_processing_dims(239, edge, 0.5, 7.0)) <= 512
    assert (bx, by) == ((500 - 239) // 2, (500 - 239) // 2)
    assert seen["mpp"] == pytest.approx(0.5)
    result = r.get_json()["result"]
    assert result["counted_region"]["reduced"] is True
    assert result["processing"]["processing_edge"] <= 512


def test_count_cells_exact_integer_boundary_max_side(monkeypatch):
    """整数边界：规划器取到的边长 = 真实取整物理下满足档位的最大值；
    多 1 个 level-0 px 处理边长就会越档（不用理想公式近似）。"""
    mod = _load_backend(monkeypatch)
    mpp, d, res = 0.5, 7.0, 512
    max_side = _mirror_max_side(mod, mpp, d, res, 4096)
    assert mod._square_admits(max_side, mpp, d, res)
    assert not mod._square_admits(max_side + 1, mpp, d, res)
    # 规划器在大快照里取到的就是该最大值
    reads = _stub_count_platform(monkeypatch, mod, mpp=mpp,
                                 slide=(100000, 100000))
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": res}, snap=(0, 0, 20000, 20000))
    assert r.status_code == 200, r.get_json()
    assert reads[-1][2] == max_side == reads[-1][3]
    edge = reads[-1][4]
    # 读后镜像复核：处理边长 ≤ res（int 截断物理，与硬闸同一函数）
    infer_mpp = mod._axis_mean_mpp(mpp, max_side, max_side, edge, edge)
    proc = mod._cellpose_processing_dims(edge, edge, infer_mpp, d)
    assert max(proc) <= res


def test_count_cells_orientation_symmetry_in_snapshot(monkeypatch):
    """方向对称（round-3 G1 快照化）：3982×30 与 30×3982 在同一物理下给出
    相同边长的不越界方形、相同读图行为（都成功、读图前完成全部准入）。"""
    for snap in ((0, 0, 3982, 30), (0, 0, 30, 3982)):
        mod = _load_backend(monkeypatch)
        reads = _stub_count_platform(monkeypatch, mod, mpp=0.12,
                                     slide=(20000, 20000))
        monkeypatch.setattr(mod, "_infer_cellpose",
                            lambda *a, **kw: _count_response())
        r = _call_count(mod, {"resolution": 2048, "diameter_um": 7},
                        snap=snap)
        assert r.status_code == 200, (snap, r.get_json())
        bx, by, bw, bh, edge = reads[-1]
        assert bw == bh == 30  # 快照短边封顶，方形
        assert edge == 16  # ceil(30×0.12×30/7)
        if snap[2] > snap[3]:
            assert (bx, by) == ((3982 - 30) // 2, 0)
        else:
            assert (bx, by) == (0, (3982 - 30) // 2)
        result = r.get_json()["result"]
        assert result["counted_region"]["reduced"] is False


def test_count_cells_non_square_platform_response_rejected(monkeypatch):
    """读后硬闸：平台返回非方形几何（违背合同）→ 400 platform_contract，
    不进入推理。"""
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)

    def _bad_read(slide, args, edge, delegated, jwt, slide_id=None):
        return b"img", _region_payload(edge, max(1, edge - 3),
                                       (args["x"], args["y"],
                                        args["w"], args["h"]))
    monkeypatch.setattr(mod, "_platform_read_region", _bad_read)
    calls = []
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: calls.append(1) or _count_response())
    r = _call_count(mod, {}, snap=(0, 0, 500, 500))
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "platform_contract"
    assert calls == [], "几何合同被违背的数据不得进入推理"


def test_count_cells_offsquare_snapshot_takes_short_side(monkeypatch):
    """长条快照：方形取短边、中心定位（不拉伸、不补边）。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.25)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 2048}, snap=(10, 20, 997, 503))
    assert r.status_code == 200, r.get_json()
    bx, by, bw, bh, edge = reads[-1]
    assert (bw, bh) == (503, 503)  # 短边方形
    assert (bx, by) == (10 + (997 - 503) // 2, 20)  # 快照中心


def test_count_cells_instances_top_capped(monkeypatch):
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    count = _count_response()
    count["instances_top"] = [
        {"bbox": [i, i, 2, 2], "centroid": [i + 1.0, i + 1.0],
         "area_px": 4.0} for i in range(30)]
    monkeypatch.setattr(mod, "_infer_cellpose", lambda *a, **kw: count)
    r = _call_count(mod, {}, snap=(0, 0, 500, 500))
    assert r.status_code == 200
    assert len(r.get_json()["result"]["instances_top"]) == mod._MAX_COMPONENTS


def test_count_cells_error_mapping(monkeypatch):
    """busy/not_ready/unreachable/400 与 PSM 同款错误映射。"""
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (503, {"error": "busy"})}))
    r = _call_count(mod, {})
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_busy"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (503, {"error": "not_ready"})}))
    r = _call_count(mod, {})
    assert r.get_json()["error"]["code"] == "inference_not_ready"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": real_requests.ConnectionError("down")}))
    r = _call_count(mod, {})
    assert r.get_json()["error"]["code"] == "inference_unavailable"

    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (400, {"error": "bad_input",
                                 "message": "image too large"})}))
    r = _call_count(mod, {})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_argument"


def test_count_cells_contract_violation(monkeypatch):
    """200 但缺 instance_count → inference_contract（可重试）。"""
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    bad = _count_response()
    del bad["instance_count"]
    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (200, bad)}))
    r = _call_count(mod, {})
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_contract"


def test_count_cells_service_resource_limit_mapping(monkeypatch):
    """服务端双保险 400 resource_limit → 透传同码（不是 invalid_argument）。"""
    mod = _load_backend(monkeypatch)
    _stub_count_platform(monkeypatch, mod, mpp=0.5)
    monkeypatch.setattr(mod, "requests", _CellposeStubRequests({
        "POST /v1/count": (400, {"error": "resource_limit",
                                 "message": "upscaled beyond budget"})}))
    r = _call_count(mod, {})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "resource_limit"


def test_count_cells_inference_mpp_from_actual_scale(monkeypatch):
    """R3（方形化保真）：推理 mpp = 原生 × **实际**渲染缩放——细于处理尺度
    的切片按 d/30 渲染（ceil 取整），推理 MPP 是取整后的真实值
    （0.12×1991/1024），不是原生、也不是理想处理尺度 7/30。"""
    mod = _load_backend(monkeypatch)
    seen = {}
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.12,
                                 slide=(100000, 100000))
    monkeypatch.setattr(
        mod, "_infer_cellpose",
        lambda image, mpp_arg, **kw: seen.update({"mpp": mpp_arg})
        or _count_response())
    r = _call_count(mod, {"resolution": 1024}, snap=(0, 0, 20000, 20000))
    assert r.status_code == 200, r.get_json()
    side, edge = reads[-1][2], reads[-1][4]
    # 规划器取真实物理最大（镜像权威；理想式 floor(1024×7/(30×0.12))=1991
    # 因 ceil 渲染取整与截断的相互作用少 1~2 px——不能靠理想公式）
    assert side == _mirror_max_side(mod, 0.12, 7.0, 1024, 4096)
    assert edge == mod._square_render_edge(side, 0.12, 7.0)
    # 实际几何推导（两轴均值；同式同浮点序）
    assert seen["mpp"] == pytest.approx(0.12 * side / edge, rel=1e-12)
    # 且与两个「偷懒替代值」都可区分
    assert abs(seen["mpp"] - 0.12) > 0.01
    # 理想式替代值（7/30）与实际值不同（精确断言 above 已区分到 1e-12）
    assert abs(seen["mpp"] - 7.0 / 30.0) > 1e-9
    result = r.get_json()["result"]
    assert result["inference_input"]["mpp"] == pytest.approx(
        0.12 * side / edge, rel=1e-12)
    assert result["inference_input"]["mpp_render"] == pytest.approx(7.0 / 30.0)
    assert result["inference_input"]["scale"] == \
        {"x": pytest.approx(side / edge), "y": pytest.approx(side / edge)}
    assert result["processing"]["input_edge"] == edge


def test_count_cells_native_coarser_renders_at_native(monkeypatch):
    """原生粗于处理尺度：按原生渲染（不上采样超过原生），模型内部放大并在
    结果中给出 warning；档位收边到真实物理最大（int(956×15/7)=2048 恰在档内）。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=0.5,
                                 slide=(100000, 100000))
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 2048}, snap=(0, 0, 4000, 4000))
    assert r.status_code == 200, r.get_json()
    assert reads == [(1522, 1522, 956, 956, 956)]
    assert int(956 * 30 * 0.5 / 7.0) == 2048  # 恰好卡在档位上
    result = r.get_json()["result"]
    assert any("上采样" in w for w in result["warnings"])


def test_count_cells_extreme_coarse_calibration_rejected_before_read(
        monkeypatch):
    """极端粗「标定」（荒谬值）下连 1px 方形都超档位 → 读图前 400
    resource_limit（规划器递减耗尽；不浪费平台读图预算）。"""
    mod = _load_backend(monkeypatch)
    reads = []
    _stub_count_platform(monkeypatch, mod, mpp=100.0)

    def _read(*a, **kw):
        reads.append(a)
        return b"img", _region_payload(1, 1, (0, 0, 1, 1))
    monkeypatch.setattr(mod, "_platform_read_region", _read)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 512, "diameter_um": 4},
                    snap=(0, 0, 3000, 3000))
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "resource_limit"
    assert reads == [], "无可行方形必须在读图前拒绝"


def test_count_cells_extreme_coarse_small_side_still_counts(monkeypatch):
    """极端粗原生但有物理意义的最小直径：方形缩到处理档内的极小边长仍可
    计数（处理边 int(17×30×4/4)=510 ≤ 512）。"""
    mod = _load_backend(monkeypatch)
    reads = _stub_count_platform(monkeypatch, mod, mpp=4.0)
    monkeypatch.setattr(mod, "_infer_cellpose",
                        lambda *a, **kw: _count_response())
    r = _call_count(mod, {"resolution": 512, "diameter_um": 4},
                    snap=(0, 0, 3000, 3000))
    assert r.status_code == 200, r.get_json()
    bx, by, bw, bh, edge = reads[-1]
    assert bw == bh == 17 and edge == 17
    assert int(17 * 30 * 4.0 / 4.0) == 510 <= 512


# --------------------------------------------------------------------------- #
# 5. F2 延续（方形化）：处理边长上限与整数边界（预读镜像 = 读后硬闸）
# --------------------------------------------------------------------------- #
def test_square_planner_unit_math():
    """方形规划纯函数：渲染边、处理尺寸镜像与 admits 判定（不跑 HTTP）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "psm_unit", REPO_ROOT / "plugins/pathsegmentor-measure/backend/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._COUNT_RESOLUTIONS == (512, 1024, 2048)
    assert mod._COUNT_RESOLUTION_DEFAULT == 2048
    # 原生粗于处理尺度：渲染 = 原生像素
    assert mod._square_render_edge(600, 0.5, 7.0) == 600
    # 细于处理尺度：渲染 = ceil(side×native×30/d)
    assert mod._square_render_edge(1991, 0.12, 7.0) == 1024
    assert mod._square_render_edge(30, 0.12, 7.0) == 16
    # null mpp：原生像素
    assert mod._square_render_edge(1024, None, 7.0) == 1024
    # 方形处理尺寸镜像：平台 aspect-fit 恰好 edge² + cellpose 逐轴截断
    assert mod._square_processing_dims(600, 600, 0.5, 7.0) == \
        (int(600 * 30 * 0.5 / 7.0),) * 2
    # null mpp：处理尺寸 = 输入尺寸
    assert mod._square_processing_dims(512, 512, None, 7.0) == (512, 512)
    # 整数边界（物理镜像 vs 理想式的 ±1 偏差——正是不能靠理想式的原因）：
    # @0.5、512 档：int(238×15/7)=510、int(239×15/7)=512（恰在档内）、
    # int(240×15/7)=514（越档）→ 真实最大 239，理想式 floor=238 少 1。
    assert int(238 * 15 / 7.0) == 510
    assert int(239 * 15 / 7.0) == 512
    assert int(240 * 15 / 7.0) == 514
    assert mod._square_admits(239, 0.5, 7.0, 512)
    assert not mod._square_admits(240, 0.5, 7.0, 512)
    # ROI 像素上限常量与平台镜像一致
    assert mod._PLATFORM_ROI_MAX_PIXELS == 4096 * 4096


def test_count_max_side_table():
    """每档最大方形视场（MPP 无关口径）：max side = resolution×d/30/native。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "psm_unit2", REPO_ROOT / "plugins/pathsegmentor-measure/backend/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for res, um in ((512, 119.4667), (1024, 238.9333), (2048, 477.8667)):
        for native in (0.12, 0.2, 0.25, 0.5):
            side = mod._count_max_side(native, 7.0, res)
            assert side == int(math.floor(res * 7.0 / 30.0 / native)), \
                (res, native)
    assert mod._count_max_side(None, 7.0, 512) is None


def test_count_cells_option_mpp_diameter_sweep_integer_boundary():
    """验收矩阵（设计 §验收用例）：512/1024/2048 × MPP(0.12/0.25/0.5) ×
    直径(7/15/25) ——规划器取到的边长在**真实取整物理**下恰为最大可处理
    值（side 放行、同一快照下 side+1 越档），渲染边与处理边长均 ≤ 档位。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "psm_sweep", REPO_ROOT / "plugins/pathsegmentor-measure/backend/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cases = 0
    for res in (512, 1024, 2048):
        for mpp in (0.12, 0.25, 0.5):
            for d in (7.0, 15.0, 25.0):
                geo_cap = 4096
                side = geo_cap
                while side >= 1 and not mod._square_admits(side, mpp, d, res):
                    side -= 1
                assert side >= 1, (res, mpp, d)
                edge = mod._square_render_edge(side, mpp, d)
                proc = mod._square_processing_dims(side, edge, mpp, d)
                assert proc is not None and max(proc) <= res, (res, mpp, d)
                assert proc[0] * proc[1] <= mod._CELLPOSE_MAX_SCALED_PIXELS
                # 整数边界（非理想公式）：处理上限先于几何上限生效时，
                # side+1 在同一几何下必须越档；几何上限（4096 ROI/平台钳制）
                # 先生效时 side 恰为几何上限且仍满足处理档位。
                if side < geo_cap:
                    assert not mod._square_admits(side + 1, mpp, d, res), \
                        (res, mpp, d, side)
                else:
                    assert side == geo_cap, (res, mpp, d, side)
                # 规划器在大快照里取到的就是该最大值
                plan, err = mod._plan_count_square(
                    {"x": 0, "y": 0, "w": geo_cap * 2, "h": geo_cap * 2},
                    geo_cap * 4, geo_cap * 4, mpp, d, res)
                assert err is None
                assert plan["side"] == side, (res, mpp, d, plan["side"])
                cases += 1
    assert cases == 27


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
    _stub_count_platform(monkeypatch, mod, mpp=0.0)
    monkeypatch.setattr(
        mod, "_infer_cellpose",
        lambda image, mpp_arg, **kw: seen.update({"mpp": mpp_arg})
        or _count_response())
    r = _call_count(mod, {}, snap=(0, 0, 500, 500))
    assert r.status_code == 200, r.get_json()
    assert seen["mpp"] is None
    result = r.get_json()["result"]
    assert result["density"]["per_mm2"] is None
    assert result["area_stats_um2"] is None
    assert result["inference_input"]["mpp"] is None
    assert any("MPP" in w for w in result["warnings"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
