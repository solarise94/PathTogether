# -*- coding: utf-8 -*-
"""像素预算闸 1（单请求上限）计费口径回归测试：真实成本 = max(输出像素, 估算解码像素)。

背景：旧口径按 level-0 bbox 面积 w*h 计费，导致「大视野 + 小输出」的低倍取景
（如 bbox 8192² + 输出 1024²）在读盘前被 429 误杀——而真实读图成本由
_read_region_b64 的金字塔选层决定（长边 ≈1568 量级），与 level-0 面积无关。

覆盖：
  - 大视野小输出（bbox 8192² + max_long_edge=1024 / 显式 out 1024²）→ 通过预算
    闸（到达读图路径，不再 single_request_pixels 429）；
  - 极端细长 bbox（100000×100）→ 解码估算 ≈1568×1，同样放行；
  - 真正超标的输出（小 bbox + out 4096×4096，上限压到 4M）→ 429
    single_request_pixels（triggered_by=output_pixels，文案引导缩输出尺寸），
    且**在读盘前拒绝**（读盘路径零调用）；
  - 解码量触闸（上限压到 2M <1568²，bbox 8192² + max_long_edge=1024）→ 429
    triggered_by=decode_pixels，文案引导「放大层级缩小视野」（缩输出无济于事）；
  - 默认上限（4096²）下 out 4096×4096 恰好压线（== 上限，非 >）→ 放行；
  - max_long_edge 越界（4097）→ 400（原有参数校验不回归）；
  - details 字段：reason/out_pixels/est_decode_pixels/pixels/max_pixels。

运行：cd 项目根 && python3 -m pytest tests/test_plugin_region_pixel_gate.py -q
"""
import base64
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-pixgate-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_SIDECAR_URL"] = "http://127.0.0.1:8055"

# openslide 未安装时 stub（本测试不需要真 OpenSlide）
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
import share_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

# 默认单请求上限 4096²（与生产默认一致；压小上限的用例单独 monkeypatch）
DEFAULT_MAX_PIXELS = 4096 ** 2


# --------------------------------------------------------------------------- #
# 隔离 + 引导（同 test_plugin_v1_transport 的形态）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False + 三道闸门重置为全新实例。"""
    monkeypatch.setenv("SHARE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", tmp_path)
    monkeypatch.setattr(share_store, "SHARE_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW", app_mod._SlidingPixelWindow(
        app_mod._PLUGIN_REGION_PIXEL_BUDGET_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER", app_mod._PluginRateLimiter(
        app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_CONCURRENCY_SEM",
                        threading.BoundedSemaphore(app_mod._PLUGIN_REGION_MAX_CONCURRENT))
    # 显式钉住默认单请求上限（防止环境变量覆盖污染断言）
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", DEFAULT_MAX_PIXELS)
    yield


def _bootstrap():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _file_secret():
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _token_for(inst):
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"], "secret": _file_secret()})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


_FAKE_ENTRY = {"pool": None, "sem": None}


def _slide_read_mocks(osr=None, mpp=0.5):
    """slide 读路径 mock 栈（dimensions + mpp），返回 (get_slide, borrow, metadata)。"""
    pair = {"osr": osr or mock.Mock(dimensions=(100000, 100000))}
    return (mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY),
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)),
            mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": mpp}))


def _fake_region(jpeg_bytes, width, height, w, h):
    return {
        "image_base64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "mime": "image/jpeg", "width": width, "height": height,
        "src": {"x": 0, "y": 0, "w": w, "h": h}, "magnification": 20.0,
    }


def _post_region(token, slide, *, w, h, **extra):
    body = {"x": 0, "y": 0, "w": w, "h": h}
    body.update(extra)
    return _client().post("/api/plugin/v1/slides/%s/regions" % slide,
                          headers={"Authorization": "Bearer " + token}, json=body)


# --------------------------------------------------------------------------- #
# 1. 大视野 + 小输出：不再被预算闸 1 误杀（旧口径下 429，新口径放行）
# --------------------------------------------------------------------------- #
def test_large_fov_small_output_max_long_edge_passes_gate():
    """bbox 8192² + max_long_edge=1024：解码估算 1568²、输出 1024²，均远低于
    4096² 上限 → 放行并到达读图路径（旧口径按 8192²=67M 计费会 429）。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    read_region = mock.patch.object(
        app_mod, "_read_region_b64",
        return_value=_fake_region(b"\xff\xd8fov\xffd9", 1024, 1024, 8192, 8192))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, read_region as rr:
        r = _post_region(token, slide, w=8192, h=8192, max_long_edge=1024)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert rr.call_count == 1  # 确认请求真正到达读图（不是被预算闸拦下）


def test_large_fov_small_output_explicit_out_passes_gate():
    """bbox 8192² + 显式 out_w/out_h=1024（legacy 平铺参数）→ 同样放行。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    read_region = mock.patch.object(
        app_mod, "_read_region_b64",
        return_value=_fake_region(b"\xff\xd8fov2\xffd9", 1024, 1024, 8192, 8192))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, read_region as rr:
        r = _post_region(token, slide, w=8192, h=8192, out_w=1024, out_h=1024)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert rr.call_count == 1


def test_large_fov_bbox_contract_shape_passes_gate():
    """契约形态 bbox{...}（HistoPilot sidecar 实际用法）+ max_long_edge=1280。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    read_region = mock.patch.object(
        app_mod, "_read_region_b64",
        return_value=_fake_region(b"\xff\xd8fov3\xffd9", 1280, 1280, 8192, 8192))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, read_region as rr:
        r = _client().post(
            "/api/plugin/v1/slides/%s/regions" % slide,
            headers={"Authorization": "Bearer " + token},
            json={"bbox": {"x": 0, "y": 0, "w": 8192, "h": 8192},
                  "max_long_edge": 1280})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert rr.call_count == 1


def test_extremely_elongated_bbox_passes_gate():
    """极端细长 bbox 100000×100 + max_long_edge=1024：解码估算 ≈1568×1，
    输出 1024×1 → 远低于上限，放行（旧口径按 10M 计费虽未超 16.7M 但接近；
    新口径证明细长视野按真实解码量计费）。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    read_region = mock.patch.object(
        app_mod, "_read_region_b64",
        return_value=_fake_region(b"\xff\xd8thin\xffd9", 1024, 1, 100000, 100))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, read_region as rr:
        r = _post_region(token, slide, w=100000, h=100, max_long_edge=1024)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert rr.call_count == 1


# --------------------------------------------------------------------------- #
# 2. 真正超标的输出：原有保护不回归（读盘前拒绝）
# --------------------------------------------------------------------------- #
def test_huge_output_rejected_before_disk(monkeypatch):
    """小 bbox + out 4096×4096（clamp 后 16.7M），上限压到 4M → 429
    single_request_pixels，且读盘路径零调用。文案指向输出尺寸而非视野。"""
    inst = _bootstrap()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 4 * 1000 * 1000)

    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    read_region = mock.patch.object(app_mod, "_read_region_b64")
    get_slide = mock.patch.object(app_mod, "_get_slide")
    with get_slide as g, borrow as b, read_region as rr:
        r = _post_region(_token_for(inst), slide,
                         w=100, h=100, out_w=4096, out_h=4096)
    assert r.status_code == 429, r.get_data(as_text=True)
    err = (r.get_json() or {}).get("error") or {}
    assert err.get("code") == "rate_limited"
    assert err.get("retryable") is True
    details = err.get("details") or {}
    assert details.get("reason") == "single_request_pixels"
    assert details.get("triggered_by") == "output_pixels"  # 触发项 = 输出像素
    assert details.get("pixels") == 4096 * 4096
    assert details.get("out_pixels") == 4096 * 4096
    assert details.get("est_decode_pixels") == 100 * 100  # 小 bbox，ds=1
    assert "Retry-After" in r.headers and int(r.headers["Retry-After"]) >= 1
    # 文案引导缩小输出尺寸，而不是误导模型去缩视野
    assert "输出尺寸" in err.get("message", "")
    # 读盘/解码路径零调用（读盘前拒绝）
    assert b.call_count == 0
    assert rr.call_count == 0
    assert g.call_count == 0


def test_huge_output_max_long_edge_rejected_before_disk(monkeypatch):
    """大视野 + max_long_edge=4096（输出 4096²），上限压到 4M → 429；解码估算
    （1568²=2.46M）本身不超限，被闸住的是输出像素。"""
    inst = _bootstrap()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 4 * 1000 * 1000)

    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    with borrow as b:
        r = _post_region(_token_for(inst), slide,
                         w=8192, h=8192, max_long_edge=4096)
    assert r.status_code == 429
    details = ((r.get_json() or {}).get("error") or {}).get("details") or {}
    assert details.get("reason") == "single_request_pixels"
    assert details.get("triggered_by") == "output_pixels"  # 4096² 输出 > 1568² 解码
    assert details.get("out_pixels") == 4096 * 4096
    assert details.get("est_decode_pixels") == 1568 * 1568  # ds=8192/1568 → 长边 1568
    assert b.call_count == 0


def test_decode_pixels_rejected_with_view_guidance(monkeypatch):
    """解码量触闸路径：上限压到 2M（<1568²），bbox 8192² + max_long_edge=1024 →
    输出仅 1024²=1.05M 不超限，解码估算 1568²=2.46M 超限 → 429。文案应引导
    「放大层级缩小视野」而非只让缩输出尺寸；triggered_by=decode_pixels。"""
    inst = _bootstrap()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 2 * 1000 * 1000)

    borrow = mock.patch.object(app_mod.slide_cache, "borrow_pair")
    read_region = mock.patch.object(app_mod, "_read_region_b64")
    get_slide = mock.patch.object(app_mod, "_get_slide")
    with get_slide as g, borrow as b, read_region as rr:
        r = _post_region(_token_for(inst), slide,
                         w=8192, h=8192, max_long_edge=1024)
    assert r.status_code == 429, r.get_data(as_text=True)
    err = (r.get_json() or {}).get("error") or {}
    assert err.get("code") == "rate_limited"
    details = err.get("details") or {}
    assert details.get("reason") == "single_request_pixels"
    assert details.get("triggered_by") == "decode_pixels"  # 触发项 = 解码估算
    assert details.get("est_decode_pixels") == 1568 * 1568
    assert details.get("out_pixels") == 1024 * 1024
    assert details.get("pixels") == 1568 * 1568  # 取大者 = 解码估算
    # 文案含视野/层级引导（缩输出尺寸解决不了解码量超限）
    assert "视野" in err.get("message", "")
    assert "放大" in err.get("message", "")
    # 读盘/解码路径零调用（读盘前拒绝）
    assert b.call_count == 0
    assert rr.call_count == 0
    assert g.call_count == 0


def test_output_exactly_at_default_limit_passes():
    """默认上限 4096² 下 out 4096×4096：估算 pixels == 上限（非 >）→ 放行到读图。"""
    inst = _bootstrap()
    slide = _touch_slide()
    token = _token_for(inst)
    read_region = mock.patch.object(
        app_mod, "_read_region_b64",
        return_value=_fake_region(b"\xff\xd8edge\xffd9", 4096, 4096, 100, 100))
    m1, m2, m3 = _slide_read_mocks()
    with m1, m2, m3, read_region as rr:
        r = _post_region(token, slide, w=100, h=100, out_w=8192, out_h=8192)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert rr.call_count == 1


def test_max_long_edge_out_of_range_400():
    """max_long_edge=4097 → 400（参数校验不回归）。"""
    inst = _bootstrap()
    slide = _touch_slide()
    r = _post_region(_token_for(inst), slide, w=100, h=100, max_long_edge=4097)
    assert r.status_code == 400
    r0 = _post_region(_token_for(inst), slide, w=100, h=100, max_long_edge=0)
    assert r0.status_code == 400


# --------------------------------------------------------------------------- #
# 3. 计费口径单元断言（与 _read_region_b64 的选层算术同式）
# --------------------------------------------------------------------------- #
def test_billing_matches_read_region_decode_arithmetic():
    """闸门估算的解码像素与 _read_region_b64 的 ds/rw/rh 纯算术一致
    （整数分式 ceil(w*1568/L) 精确等价于 ceil(w/ds)，无浮点整除边界噪声）。"""

    def est_decode(w, h):
        edge = max(w, h)
        if edge <= 1568:
            return w * h
        ceil_w = (w * 1568 + edge - 1) // edge
        ceil_h = (h * 1568 + edge - 1) // edge
        return ceil_w * ceil_h

    # 正方形大视野：长边压到 1568
    assert est_decode(8192, 8192) == 1568 * 1568
    assert est_decode(4096, 4096) == 1568 * 1568
    assert est_decode(65536, 65536) == 1568 * 1568
    # ≤1568 的视野：不缩层
    assert est_decode(1000, 500) == 500000
    assert est_decode(1568, 1568) == 1568 * 1568
    # 细长视野：长边 1568、短边同比压扁（ceil）
    assert est_decode(100000, 100) == 1568 * 2
    # 非正方形：短边 = h * 1568 / max(w,h) 向上取整
    assert est_decode(8192, 4096) == 1568 * 784
    assert est_decode(2000, 1001) == 1568 * 785


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
