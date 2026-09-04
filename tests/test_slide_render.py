# -*- coding: utf-8 -*-
"""slide_render 纯函数与像素合成测试（Batch 2：OME manifest / 全局统计 / 伪彩）。

规格：docs/agent-fix-sse-summary-and-multichannel-pseudocolor-2026-09-04.md
§5（默认颜色与渲染算法）、§6.1/§6.2 纯函数部分、§7.1 共享模块边界、§13.1
合成图像矩阵中无 Flask 路由即可测的项。

原则：
  - 全部 fixture 由真实 tifffile 2024.5.22 写出再读回（见 ``_tiff_fixtures``），
    不 mock axes、不使用患者数据；
  - 本批不做 HMAC render_token / Flask 路由（Batch 3）；
  - 断言「只读所选通道 / 不读 level-0 全图 / 统计只算一次」用包装 zarr
    ``__getitem__`` 的记录代理实现。

运行：cd PathTogether && python -m pytest tests/test_slide_render.py -q
"""
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

import slide_io
import slide_render
from _tiff_fixtures import (
    make_ome_cyx_bytes,
    make_ome_cyx_pyramid_bytes,
    make_ome_explicit_color_bytes,
    make_ome_float_cyx_bytes,
    make_ome_gray_yx_bytes,
    make_ome_multifile_bytes,
    make_ome_multiseries_bytes,
    make_ome_tczyx_bytes,
    make_ome_tiff_bytes,
)


def _open(data):
    """从字节打开 TiffFileSlide（测试内直接持有，不跨 borrow 缓存）。"""
    return slide_io.TiffFileSlide(io.BytesIO(data))


# --------------------------------------------------------------------------- #
# 0. 记录代理：包装 zarr 数组，记录每次 __getitem__ 的索引（不改变行为）
# --------------------------------------------------------------------------- #
class _RecordingArray:
    """zarr 数组包装：记录索引调用并委托真实数组（验证「不读未选 plane」）。"""

    def __init__(self, arr):
        self._arr = arr
        self.records = []

    def __getitem__(self, idx):
        self.records.append(idx)
        return self._arr[idx]

    @property
    def shape(self):
        return self._arr.shape

    @property
    def dtype(self):
        return self._arr.dtype


@pytest.fixture
def recording_slide():
    """打开 slide 并把 _zarrays 换成记录代理（同一数组对象，仅包装）。"""
    def _make(data):
        slide = _open(data)
        slide._zarrays[:] = [_RecordingArray(a) for a in slide._zarrays]
        return slide
    yield _make


@pytest.fixture(autouse=True)
def _reset_render_caches():
    slide_render.reset_caches()
    yield
    slide_render.reset_caches()


# --------------------------------------------------------------------------- #
# 1. OME RGBA 解析与确定性色卡（§5.1 / §5.2）
# --------------------------------------------------------------------------- #
def test_parse_ome_channel_color_signed_int_forms():
    assert slide_render.parse_ome_channel_color("-1") == (255, 255, 255, 255)
    # 显式 -1 是白色且来源 OME——与「属性不存在」必须可区分（返回值非 None）
    assert slide_render.parse_ome_channel_color("0") == (0, 0, 0, 0)
    assert slide_render.parse_ome_channel_color("255") == (0, 0, 0, 255)
    # -256 → u32=0xFFFFFF00 → 白色、alpha=0（默认关闭语义的输入形态）
    assert slide_render.parse_ome_channel_color("-256") == (255, 255, 255, 0)
    # 16777215 → u32=0x00FFFFFF → 青、alpha=255
    assert slide_render.parse_ome_channel_color("16777215") == (0, 255, 255, 255)
    assert slide_render.parse_ome_channel_color("-16711681") == (255, 0, 255, 255)


def test_parse_ome_channel_color_hex_and_invalid():
    assert slide_render.parse_ome_channel_color("#00FFFF") == (0, 255, 255, 255)
    assert slide_render.parse_ome_channel_color("#FF00FF00") == (255, 0, 255, 0)
    assert slide_render.parse_ome_channel_color("abc") is None
    assert slide_render.parse_ome_channel_color("") is None
    assert slide_render.parse_ome_channel_color(None) is None
    assert slide_render.parse_ome_channel_color("99999999999") is None  # 越界


def test_default_pseudocolor_palette_deterministic():
    spec = ["#00FFFF", "#FF00FF", "#FFD166", "#00E676",
            "#FF5C5C", "#4D7CFE", "#FF8C42", "#B388FF"]
    for i, expect in enumerate(spec):
        assert slide_render.default_pseudocolor(i) == expect
    # 循环使用；同索引跨调用稳定（禁止随机/进程哈希）
    assert slide_render.default_pseudocolor(8) == "#00FFFF"
    assert slide_render.default_pseudocolor(11) == spec[3]
    assert all(slide_render.default_pseudocolor(i) == spec[i % 8]
               for i in range(24))


# --------------------------------------------------------------------------- #
# 2. channel manifest（§6.1 / §7.2）
# --------------------------------------------------------------------------- #
def test_manifest_native_rgb_mode():
    slide = _open(make_ome_tiff_bytes())
    m = slide_render.build_channel_manifest(slide)
    assert m["image_mode"] == "native_rgb"
    assert m["channels"] == []
    assert m["plane"]["policy"] == "first-plane-v1"
    assert m["plane"]["size_t"] == 1 and m["plane"]["size_z"] == 1


def test_manifest_cyx_channels_and_defaults():
    slide = _open(make_ome_cyx_bytes(c=4, h=64, w=96))
    m = slide_render.build_channel_manifest(slide)
    assert m["image_mode"] == "multichannel"
    assert m["axes"] == "CYX"
    assert m["series_index"] == 0
    assert len(m["channels"]) == 4
    names = [c["name"] for c in m["channels"]]
    assert names == ["通道 1", "通道 2", "通道 3", "通道 4"]
    colors = [c["color"] for c in m["channels"]]
    assert colors == ["#00FFFF", "#FF00FF", "#FFD166", "#00E676"]
    assert all(c["color_source"] == "default" for c in m["channels"])
    assert all(c["alpha"] == 1.0 for c in m["channels"])
    assert all(c["dtype"] == "uint16" for c in m["channels"])
    assert all(c["default_active"] for c in m["channels"])
    assert m["warnings"] == []


def test_manifest_gray_yx_single_channel():
    slide = _open(make_ome_gray_yx_bytes())
    m = slide_render.build_channel_manifest(slide)
    assert m["image_mode"] == "multichannel"
    assert m["axes"] == "YX"
    assert len(m["channels"]) == 1
    assert m["channels"][0]["color"] == "#00FFFF"
    assert m["channels"][0]["color_source"] == "default"
    assert m["channels"][0]["name"] == "通道 1"


def test_manifest_explicit_ome_colors_signed_int():
    slide = _open(make_ome_explicit_color_bytes())
    m = slide_render.build_channel_manifest(slide)
    ch = m["channels"]
    # 0: Color="-1" → 显式白，来源 OME，alpha 255 → 默认启用
    assert ch[0]["color"] == "#FFFFFF"
    assert ch[0]["color_source"] == "ome"
    assert ch[0]["alpha"] == 1.0
    assert ch[0]["name"] == "DAPI"
    assert ch[0]["default_active"] is True
    # 1: Color=0x00FFFFFF → alpha 0 → 默认关闭并提示
    assert ch[1]["color"] == "#FFFFFF"
    assert ch[1]["color_source"] == "ome"
    assert ch[1]["alpha"] == 0.0
    assert ch[1]["default_active"] is False
    # 2: 无 Color 无 Name → 默认色卡 + 「通道 N」
    assert ch[2]["color"] == "#FFD166"
    assert ch[2]["color_source"] == "default"
    assert ch[2]["name"] == "通道 3"
    # 3: Color="abc" 非整数 → 视为缺失 + warning + 默认色卡
    assert ch[3]["color"] == "#00E676"
    assert ch[3]["color_source"] == "default"
    codes = [w.get("code") for w in m["warnings"]]
    assert "ome_color_invalid" in codes
    assert "ome_channel_alpha_zero" in codes


def test_manifest_ome_name_and_id_passthrough():
    slide = _open(make_ome_cyx_bytes(
        c=2, h=32, w=40, names=["DAPI", "FITC"],
        colors=["#123456", "#654321"]))
    m = slide_render.build_channel_manifest(slide)
    assert m["channels"][0]["name"] == "DAPI"
    assert m["channels"][0]["color"] == "#123456"
    assert m["channels"][0]["color_source"] == "ome"
    assert m["channels"][1]["id"] == "Channel:0:1"


def test_manifest_12_channels_default_active_first_4_only():
    slide = _open(make_ome_cyx_bytes(c=12, h=32, w=32))
    m = slide_render.build_channel_manifest(slide)
    assert len(m["channels"]) == 12  # 全部列出
    active = [c["index"] for c in m["channels"] if c["default_active"]]
    assert active == [0, 1, 2, 3]  # 默认仅前 4
    # 一次最多 8：第 9 个通道被拒绝
    def chan(i):
        return {"index": i, "color": "#00FFFF", "alpha": 1.0,
                "black": 0.0, "white": 100.0, "gamma": 1.0}
    ctx9 = {"version": "multichannel-additive-v1", "asset_revision": "g1",
            "plane": {"t": 0, "z": 0},
            "active_channels": [chan(i) for i in range(9)]}
    with pytest.raises(slide_io.SlideRenderError) as ei:
        slide_render.canonicalize_render_context(ctx9, channel_count=12)
    assert ei.value.code == "render_channel_limit"
    # 8 通道允许
    ctx8 = dict(ctx9, active_channels=[chan(i) for i in range(8)])
    canonical, _ = slide_render.canonicalize_render_context(ctx8,
                                                            channel_count=12)
    assert len(canonical["active_channels"]) == 8


def test_manifest_tczyx_first_plane_warning():
    slide = _open(make_ome_tczyx_bytes(t=2, c=3, z=2, h=32, w=40))
    m = slide_render.build_channel_manifest(slide)
    assert m["plane"]["size_t"] == 2
    assert m["plane"]["size_z"] == 2
    assert m["plane"]["t"] == 0 and m["plane"]["z"] == 0
    codes = [w.get("code") for w in m["warnings"]]
    assert "first-plane-v1" in codes


# --------------------------------------------------------------------------- #
# 3. 全局强度窗 global-percentile-v1（§5.3）
# --------------------------------------------------------------------------- #
def test_global_window_uint16_matches_percentile():
    slide = _open(make_ome_cyx_bytes(c=2, h=64, w=96))
    st = slide_render.compute_global_window(slide, 1)
    # 采样步长为 1（像素数 ≤ 262144）→ 统计等于对该层全量的百分位
    raw = np.asarray(slide.level_arrays[0][1, :, :])
    expect_black = float(np.percentile(raw, 0.1))
    expect_white = float(np.percentile(raw, 99.9))
    assert st["status"] == "ok"
    assert st["black"] == pytest.approx(expect_black)
    assert st["white"] == pytest.approx(expect_white)
    assert st["gamma"] == 1.0
    assert st["source"] == "global-percentile-v1"


def test_global_window_uint8_dtype_range_fallback():
    # uint8 常量数据：white<=black → 回退 dtype 有效范围 [0, 255]
    import tifffile
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.full((2, 32, 40), 42, dtype=np.uint8),
                     photometric="minisblack", ome=True,
                     metadata={"axes": "CYX"})
    slide = _open(buf.getvalue())
    st = slide_render.compute_global_window(slide, 0)
    assert st["status"] == "ok"
    assert (st["black"], st["white"]) == (0.0, 255.0)


def test_global_window_float_nan_inf_counted_and_ok():
    slide = _open(make_ome_float_cyx_bytes(c=2, h=32, w=40,
                                           nan=True, inf=True))
    st = slide_render.compute_global_window(slide, 0)
    assert st["status"] == "ok"
    assert st["nonfinite"] == 2
    raw = np.asarray(slide.level_arrays[0][0, :, :], dtype=np.float64)
    finite = raw[np.isfinite(raw)]
    assert st["black"] == pytest.approx(float(np.percentile(finite, 0.1)))
    assert st["white"] == pytest.approx(float(np.percentile(finite, 99.9)))
    assert slide_render.metrics_snapshot()["nonfinite_pixels"] >= 2


def test_global_window_float_constant_is_empty_or_constant():
    slide = _open(make_ome_float_cyx_bytes(c=2, h=32, w=40, const=True))
    st = slide_render.compute_global_window(slide, 0)
    assert st["status"] == "empty_or_constant"
    m = slide_render.build_channel_manifest(slide, with_intensity=True)
    assert m["channels"][0]["default_active"] is False
    codes = [w.get("code") for w in m["warnings"]]
    assert "channel_not_displayable" in codes
    # NaN 全场也是 empty_or_constant，不崩溃、像素确定
    import tifffile
    buf = io.BytesIO()
    tifffile.imwrite(
        buf, np.full((1, 8, 8), np.nan, dtype=np.float32),
        photometric="minisblack", ome=True, metadata={"axes": "CYX"})
    s2 = _open(buf.getvalue())
    st2 = slide_render.compute_global_window(s2, 0)
    assert st2["status"] == "empty_or_constant"
    assert slide_render.compute_global_window(s2, 0) == st2


def test_global_window_uses_lowest_pyramid_level_not_level0():
    slide = _open(make_ome_cyx_pyramid_bytes(c=2, h=128, w=128, levels=2))
    wrapped = [_RecordingArray(a) for a in slide._zarrays]
    slide._zarrays[:] = wrapped
    st = slide_render.compute_global_window(slide, 0)
    assert st["level"] == len(slide.level_arrays) - 1  # 最低分辨率层
    assert wrapped[0].records == []  # level-0 全图未被读取
    assert wrapped[-1].records  # 采样发生在最低层
    assert st["samples"] <= 262144  # 采样上限


def test_global_window_cache_single_compute_under_concurrency():
    slide = _open(make_ome_cyx_bytes(c=4, h=64, w=96))
    n = 20
    barrier = threading.Barrier(n)

    def run(_):
        barrier.wait()
        return slide_render.compute_global_window(slide, 2)

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(run, range(n)))
    snap = slide_render.metrics_snapshot()
    assert snap["stats_computed"] == 1  # 同一 generation 只算一次
    assert snap["stats_cache_hit"] == n - 1
    first = results[0]
    for r in results[1:]:
        assert r == first  # 所有等待者复用同一结果


def test_global_window_cache_key_separated_by_generation():
    slide = _open(make_ome_cyx_bytes(c=2, h=32, w=40))
    a = slide_render.compute_global_window(slide, 0, asset_generation="gen-a")
    slide_render.compute_global_window(slide, 0, asset_generation="gen-a")
    b = slide_render.compute_global_window(slide, 0, asset_generation="gen-b")
    snap = slide_render.metrics_snapshot()
    assert snap["stats_computed"] == 2  # 不同 generation 各算一次
    assert a == b  # 同文件结果一致


# --------------------------------------------------------------------------- #
# 4. canonical render context 与 fingerprint（§6.2 纯函数部分）
# --------------------------------------------------------------------------- #
def _ctx(**over):
    base = {
        "version": "multichannel-additive-v1",
        "asset_revision": "rev-1",
        "plane": {"t": 0, "z": 0},
        "active_channels": [
            {"index": 0, "color": "#00FFFF", "alpha": 1.0,
             "black": 10.0, "white": 200.0, "gamma": 1.0},
            {"index": 1, "color": "#FF00FF", "alpha": 0.5,
             "black": 0.0, "white": 100.0, "gamma": 2.0},
        ],
    }
    base.update(over)
    return base


def _mutated(**over):
    """返回 active_channels[0] 被覆写的 context。"""
    ctx = _ctx()
    ctx["active_channels"][0].update(over)
    return ctx


def test_canonicalize_sorts_and_hashes_deterministically():
    c1, fp1 = slide_render.canonicalize_render_context(_ctx())
    c2, fp2 = slide_render.canonicalize_render_context(_ctx())
    assert fp1 == fp2
    assert re.fullmatch(r"[0-9a-f]{64}", fp1)
    # active_channels 按 index 排序（canonical）
    assert [ch["index"] for ch in c1["active_channels"]] == [0, 1]
    # 字段顺序变化但语义相同 → fingerprint 不变
    reordered = _ctx()
    reordered["active_channels"] = list(
        reordered["active_channels"])[::-1]
    reordered["plane"] = {"z": 0, "t": 0}
    _, fp3 = slide_render.canonicalize_render_context(reordered)
    assert fp3 == fp1
    assert c1 == c2


def test_fingerprint_changes_on_any_semantic_change():
    _, fp0 = slide_render.canonicalize_render_context(_ctx())
    added = _ctx()
    added["active_channels"] = added["active_channels"] + [
        {"index": 2, "color": "#FFD166", "alpha": 1.0,
         "black": 0.0, "white": 50.0, "gamma": 1.0}]
    mutations = [
        _mutated(color="#FFFFFF"),        # 颜色
        _mutated(black=11.0),             # black
        _mutated(white=201.0),            # white
        _mutated(gamma=1.5),              # gamma
        _mutated(alpha=0.9),              # alpha
        dict(_ctx(), active_channels=_ctx()["active_channels"][:1]),  # 开关
        added,                            # 通道组合变化
    ]
    for ctx in mutations:
        _, fp = slide_render.canonicalize_render_context(ctx)
        assert fp != fp0, ctx


def test_fingerprint_independent_of_asset_revision():
    _, fp1 = slide_render.canonicalize_render_context(_ctx())
    _, fp2 = slide_render.canonicalize_render_context(
        _ctx(asset_revision="rev-2"))
    assert fp1 == fp2  # asset_revision 与 fingerprint 是两个值
    c1, _ = slide_render.canonicalize_render_context(_ctx())
    c2, _ = slide_render.canonicalize_render_context(
        _ctx(asset_revision="rev-2"))
    assert c1["asset_revision"] == "rev-1"
    assert c2["asset_revision"] == "rev-2"


def test_canonicalize_validation_errors():
    def dup_index():
        ctx = _ctx()
        ctx["active_channels"][1]["index"] = 0
        return ctx

    def neg_index():
        ctx = _ctx()
        ctx["active_channels"][0]["index"] = -1
        return ctx

    def oob_index():
        ctx = _ctx()
        ctx["active_channels"][0]["index"] = 5
        return ctx

    def bad_color():
        return _mutated(color="red")

    def bad_alpha():
        return _mutated(alpha=1.5)

    def white_le_black():
        ctx = _mutated(white=10.0)
        ctx["active_channels"][0]["black"] = 20.0
        return ctx

    def gamma_low():
        return _mutated(gamma=0.05)

    def gamma_high():
        return _mutated(gamma=6.0)

    def nan_black():
        return _mutated(black=float("nan"))

    cases = [
        # (ctx, channel_count, 期望稳定码)
        (dict(_ctx(), version="native-rgb-v1"), None,
         "invalid_render_context"),  # native 带通道
        (_ctx(active_channels=[]), None, "invalid_render_context"),
        (_ctx(asset_revision=""), None, "invalid_render_context"),
        (_ctx(asset_revision=None), None, "invalid_render_context"),
        (_ctx(plane={"t": 1, "z": 0}), None,
         "unsupported_plane_selection"),
        (_ctx(plane={"t": 0, "z": 2}), None,
         "unsupported_plane_selection"),
        (_ctx(plane={"t": -1, "z": 0}), None, "invalid_render_context"),
        (_ctx(plane={"t": "x", "z": 0}), None, "invalid_render_context"),
        (neg_index(), None, "invalid_render_context"),
        (dup_index(), None, "invalid_render_context"),  # 重复索引
        (oob_index(), 2, "render_channel_out_of_range"),
        (bad_color(), None, "invalid_render_context"),
        (bad_alpha(), None, "invalid_render_context"),
        (white_le_black(), None, "invalid_render_context"),
        (gamma_low(), None, "invalid_render_context"),
        (gamma_high(), None, "invalid_render_context"),
        (nan_black(), None, "invalid_render_context"),
    ]
    for ctx, cc, code in cases:
        with pytest.raises(slide_io.SlideRenderError) as ei:
            slide_render.canonicalize_render_context(ctx, channel_count=cc)
        assert ei.value.code == code, (ctx, code, ei.value.code)


def test_canonicalize_native_rgb_empty_channels_ok():
    ctx = {"version": "native-rgb-v1", "asset_revision": "rev-1",
           "plane": {"t": 0, "z": 0}, "active_channels": []}
    canonical, fp = slide_render.canonicalize_render_context(ctx)
    assert canonical["version"] == "native-rgb-v1"
    assert canonical["active_channels"] == []
    assert re.fullmatch(r"[0-9a-f]{64}", fp)


def test_canonicalize_rounds_fixed_decimals():
    ctx = _mutated(alpha=0.123456, black=1.23456, white=99.99999,
                   gamma=1.111111)
    canonical, fp1 = slide_render.canonicalize_render_context(ctx)
    ch = canonical["active_channels"][0]
    assert ch["alpha"] == round(0.123456, 4)
    assert ch["black"] == round(1.23456, 4)
    assert ch["white"] == round(99.99999, 4)
    assert ch["gamma"] == round(1.111111, 4)
    # 同值不同小数位输入 → 同 fingerprint（固定小数序列化）
    ctx2 = _mutated(alpha=0.1235, black=1.2346, white=100.0, gamma=1.1111)
    _, fp2 = slide_render.canonicalize_render_context(ctx2)
    assert fp2 == fp1


# --------------------------------------------------------------------------- #
# 5. 合成公式与区域读取（§5.3 / §7.2）
# --------------------------------------------------------------------------- #
def _composite_plane_expected(raw, black, white, gamma, color_rgb, alpha=1.0):
    """测试侧独立实现合成公式（§5.3），用于交叉验证实现。"""
    n = np.clip((np.asarray(raw, dtype=np.float64) - black)
                / (white - black), 0.0, 1.0) ** (1.0 / gamma)
    return n[..., None] * (alpha * np.asarray(color_rgb, dtype=np.float64))


def _single_channel_ctx(st, color="#00FFFF", channel_count=1):
    return slide_render.canonicalize_render_context(
        {"version": "multichannel-additive-v1", "asset_revision": "rev-1",
         "plane": {"t": 0, "z": 0},
         "active_channels": [{"index": 0, "color": color, "alpha": 1.0,
                              "black": st["black"], "white": st["white"],
                              "gamma": 1.0}]}, channel_count=channel_count)[0]


def test_composite_region_formula_cyan_channel():
    slide = _open(make_ome_cyx_bytes(c=2, h=64, w=96))
    st = slide_render.compute_global_window(slide, 0)
    ctx = _single_channel_ctx(st, channel_count=2)
    img = slide_render.composite_region(slide, ctx, (0, 0), 0, (96, 64))
    assert img.mode == "RGBA"
    assert img.size == (96, 64)
    arr = np.asarray(img)
    raw = np.asarray(slide.level_arrays[0][0, :, :], dtype=np.float64)
    expect = _composite_plane_expected(raw, st["black"], st["white"], 1.0,
                                       (0.0, 1.0, 1.0))
    expect_u8 = np.clip(np.rint(expect * 255.0), 0, 255).astype(np.uint8)
    assert np.array_equal(arr[..., :3], expect_u8)
    assert (arr[..., 3] == 255).all()  # 有效区域内不透明


def test_composite_region_multichannel_order_independent():
    slide = _open(make_ome_cyx_bytes(c=2, h=32, w=40))
    st0 = slide_render.compute_global_window(slide, 0)
    st1 = slide_render.compute_global_window(slide, 1)

    def mk(ch0_first):
        chans = [
            {"index": 0, "color": "#00FFFF", "alpha": 1.0,
             "black": st0["black"], "white": st0["white"], "gamma": 1.0},
            {"index": 1, "color": "#FF00FF", "alpha": 1.0,
             "black": st1["black"], "white": st1["white"], "gamma": 1.0},
        ]
        if not ch0_first:
            chans.reverse()
        return slide_render.canonicalize_render_context(
            {"version": "multichannel-additive-v1",
             "asset_revision": "rev-1", "plane": {"t": 0, "z": 0},
             "active_channels": chans}, channel_count=2)[0]

    a = np.asarray(slide_render.composite_region(slide, mk(True), (0, 0), 0,
                                                 (40, 32)))
    b = np.asarray(slide_render.composite_region(slide, mk(False), (0, 0), 0,
                                                 (40, 32)))
    assert np.array_equal(a, b)  # 线性加色顺序无关


def test_adjacent_tiles_share_global_window_no_seam():
    # 左右两半亮度差约 300 倍：逐瓦片 min/max 会有接缝；全局窗没有
    import tifffile
    h, w = 64, 96
    yy, xx = np.indices((h, w))
    plane = np.where(xx < 48, 100.0 + yy, 30000.0 + yy).astype(np.uint16)
    buf = io.BytesIO()
    tifffile.imwrite(buf, plane[None], photometric="minisblack", ome=True,
                     metadata={"axes": "CYX"}, tile=(32, 32))
    slide = _open(buf.getvalue())
    st = slide_render.compute_global_window(slide, 0)
    ctx = _single_channel_ctx(st)
    left = np.asarray(slide_render.composite_region(slide, ctx, (0, 0), 0,
                                                    (32, 64)))
    right = np.asarray(slide_render.composite_region(slide, ctx, (32, 0), 0,
                                                     (32, 64)))
    # 全局最小 → 0；全局最大 → 满强度（青色 g/b=255）
    assert left[0, 0, 1] == 0 and left[0, 0, 2] == 0
    assert right[-1, -1, 1] == 255 and right[-1, -1, 2] == 255
    # 左半最亮像素不再被该瓦片拉满（旧 per-tile min/max 会到 255）
    assert left[:, -1, 1].max() <= 2
    # 两块瓦片用同一 black/white：拼接处无跳变
    seam_l = np.asarray(slide_render.composite_region(
        slide, ctx, (40, 0), 0, (16, 64)))[:, -1, 1].astype(int)
    seam_r = np.asarray(slide_render.composite_region(
        slide, ctx, (56, 0), 0, (16, 64)))[:, 0, 1].astype(int)
    assert np.abs(seam_l - seam_r).max() <= 1


def test_composite_region_partial_and_full_out_of_bounds():
    slide = _open(make_ome_cyx_bytes(c=1, h=32, w=40))
    ctx = _single_channel_ctx({"black": 0.0, "white": 65025.0})
    # 完全越界 → 全透明
    img = slide_render.composite_region(slide, ctx, (1000, 1000), 0, (16, 16))
    assert np.asarray(img)[..., 3].max() == 0
    # 部分越界（右/下越界）：有效区在原点处，其余透明（alpha padding 语义不变）
    img2 = slide_render.composite_region(slide, ctx, (32, 24), 0, (16, 16))
    a2 = np.asarray(img2)
    assert (a2[:8, :8, 3] == 255).all()  # 40x32 图：x<40、y<32 有效
    assert (a2[8:, :, 3] == 0).all()
    assert (a2[:, 8:, 3] == 0).all()


def test_composite_rejects_out_of_range_channel():
    slide = _open(make_ome_cyx_bytes(c=2, h=32, w=40))
    ctx = _single_channel_ctx({"black": 0.0, "white": 65025.0},
                              channel_count=2)
    tampered = dict(ctx, active_channels=[
        dict(ctx["active_channels"][0], index=9)])
    with pytest.raises(slide_io.SlideRenderError) as ei:
        slide_render.composite_region(slide, tampered, (0, 0), 0, (8, 8))
    assert ei.value.code == "render_channel_out_of_range"


def test_composite_two_contexts_differ_but_stable():
    slide = _open(make_ome_cyx_bytes(c=2, h=32, w=40))
    st0 = slide_render.compute_global_window(slide, 0)
    st1 = slide_render.compute_global_window(slide, 1)
    mk = lambda st, color: _single_channel_ctx(st, color=color,
                                               channel_count=2)
    pa1 = np.asarray(slide_render.composite_region(
        slide, mk(st0, "#00FFFF"), (0, 0), 0, (40, 32)))
    pa2 = np.asarray(slide_render.composite_region(
        slide, mk(st0, "#00FFFF"), (0, 0), 0, (40, 32)))
    pb = np.asarray(slide_render.composite_region(
        slide, mk(st1, "#FF00FF"), (0, 0), 0, (40, 32)))
    assert np.array_equal(pa1, pa2)  # 各自稳定
    assert not np.array_equal(pa1, pb)  # 不同 context 结果不同


def test_composite_treats_nonfinite_as_zero_deterministically():
    # 合成层：NaN/Inf 输入按 0 处理并计指标（§5.3.4），像素确定
    slide = _open(make_ome_float_cyx_bytes(c=1, h=32, w=40,
                                           nan=True, inf=True))
    st = slide_render.compute_global_window(slide, 0)
    ctx = _single_channel_ctx(st)
    img1 = np.asarray(slide_render.composite_region(slide, ctx, (0, 0), 0,
                                                    (40, 32)))
    img2 = np.asarray(slide_render.composite_region(slide, ctx, (0, 0), 0,
                                                    (40, 32)))
    assert np.array_equal(img1, img2)  # 两次合成逐字节一致
    # (0,0)=NaN、(0,1)=+Inf → 按 raw=0 归一化（0 不参与统计窗，但输出按 0）
    raw0 = 0.0
    expect_n = max(0.0, min(1.0, (raw0 - st["black"])
                            / (st["white"] - st["black"])))
    expect_b = int(np.clip(round(expect_n * 255.0), 0, 255))
    assert img1[0, 0, 1] == expect_b and img1[0, 0, 2] == expect_b
    assert slide_render.metrics_snapshot()["nonfinite_pixels"] >= 2


# --------------------------------------------------------------------------- #
# 6. RenderedSlideView（§7.1/§7.3：只包装当前借出的 slide）
# --------------------------------------------------------------------------- #
def test_rendered_slide_view_native_rgb_passthrough():
    slide = _open(make_ome_tiff_bytes())
    ctx, fp = slide_render.canonicalize_render_context(
        {"version": "native-rgb-v1", "asset_revision": "rev-1",
         "plane": {"t": 0, "z": 0}, "active_channels": []})
    view = slide_render.RenderedSlideView(slide, ctx, fingerprint=fp)
    assert view.dimensions == slide.dimensions
    assert view.level_dimensions == slide.level_dimensions
    assert view.level_downsamples == slide.level_downsamples
    assert view.level_count == slide.level_count
    assert view.get_best_level_for_downsample(4.0) == \
        slide.get_best_level_for_downsample(4.0)
    assert view.properties == slide.properties
    r1 = view.read_region((0, 0), 0, (32, 32))
    r2 = slide.read_region((0, 0), 0, (32, 32))
    assert np.array_equal(np.asarray(r1), np.asarray(r2))
    view.close()
    # close 不关闭借出的 slide
    assert slide.read_region((0, 0), 0, (8, 8)) is not None


def test_rendered_slide_view_multichannel_composite():
    slide = _open(make_ome_cyx_bytes(c=2, h=64, w=96))
    m = slide_render.build_channel_manifest(slide)
    dctx = slide_render.build_default_render_context(
        slide, m, asset_revision="rev-1")
    ctx, fp = slide_render.canonicalize_render_context(dctx, channel_count=2)
    view = slide_render.RenderedSlideView(slide, ctx, fingerprint=fp)
    img = view.read_region((0, 0), 0, (96, 64))
    expect = slide_render.composite_region(slide, ctx, (0, 0), 0, (96, 64))
    assert np.array_equal(np.asarray(img), np.asarray(expect))
    # OpenSlide duck-type：缩略图可用
    thumb = view.get_thumbnail((48, 32))
    assert thumb.size == (48, 32)
    assert view.fingerprint == fp


def test_rendered_slide_view_tczyx_equivalent_cyx():
    # TCZYX(T=1,Z=1) 与 CYX 同数据 → 合成逐字节一致
    s_t = _open(make_ome_tczyx_bytes(t=1, c=3, z=1, h=32, w=40))
    s_c = _open(make_ome_cyx_bytes(c=3, h=32, w=40))
    outs = []
    for s in (s_t, s_c):
        m = slide_render.build_channel_manifest(s)
        dctx = slide_render.build_default_render_context(
            s, m, asset_revision="rev-1")
        ctx, _ = slide_render.canonicalize_render_context(dctx,
                                                          channel_count=3)
        outs.append(np.asarray(slide_render.composite_region(
            s, ctx, (0, 0), 0, (40, 32))))
    assert np.array_equal(outs[0], outs[1])


def test_rendered_slide_view_tczyx_reads_first_plane():
    s2 = _open(make_ome_tczyx_bytes(t=2, c=2, z=2, h=32, w=40))
    s1 = _open(make_ome_tczyx_bytes(t=1, c=2, z=1, h=32, w=40))
    outs = []
    for s in (s2, s1):
        m = slide_render.build_channel_manifest(s)
        dctx = slide_render.build_default_render_context(
            s, m, asset_revision="rev-1")
        ctx, _ = slide_render.canonicalize_render_context(dctx,
                                                          channel_count=2)
        outs.append(np.asarray(slide_render.composite_region(
            s, ctx, (0, 0), 0, (40, 32))))
    # T/Z>1 固定 (0,0)：与 T/Z=1 文件同数据合成一致
    assert np.array_equal(outs[0], outs[1])


# --------------------------------------------------------------------------- #
# 7. 只读所选通道（zarr 索引保留 Y/X 与所选 C）
# --------------------------------------------------------------------------- #
def test_read_region_channels_only_selected_c_sliced(recording_slide):
    slide = recording_slide(make_ome_cyx_bytes(c=4, h=64, w=96))
    planes, geom = slide.read_region_channels((0, 0), 0, (96, 64), [1, 3])
    assert planes.shape == (2, 64, 96)
    assert geom["valid_w"] == 96 and geom["valid_h"] == 64
    records = slide._zarrays[0].records
    assert len(records) == 2  # 只解码 2 个 plane
    for idx in records:
        assert idx[0] in (1, 3)  # C 轴只切了所选通道
        assert isinstance(idx[1], slice) and isinstance(idx[2], slice)


def test_read_region_channels_tczyx_index_keeps_yx(recording_slide):
    slide = recording_slide(make_ome_tczyx_bytes(t=2, c=3, z=2, h=32, w=40))
    planes, _ = slide.read_region_channels((0, 0), 0, (40, 32), [2],
                                           t=0, z=0)
    assert planes.shape == (1, 32, 40)
    idx = slide._zarrays[0].records[0]
    # level axes 'TCZYX'：T=0, C=2, Z=0，Y/X 为 slice
    assert idx[0] == 0 and idx[1] == 2 and idx[2] == 0
    assert isinstance(idx[3], slice) and isinstance(idx[4], slice)


def test_read_region_channels_plane_selection_guard():
    slide = _open(make_ome_tczyx_bytes(t=2, c=2, z=2, h=32, w=40))
    with pytest.raises(slide_io.SlideRenderError) as ei:
        slide.read_region_channels((0, 0), 0, (40, 32), [0], t=1, z=0)
    assert ei.value.code == "unsupported_plane_selection"
    with pytest.raises(slide_io.SlideRenderError) as ei2:
        slide.read_region_channels((0, 0), 0, (40, 32), [0], t=0, z=5)
    assert ei2.value.code == "unsupported_plane_selection"


# --------------------------------------------------------------------------- #
# 8. 多 series / multi-file / 默认 context / 指标（§13.1 尾部 + §7.4）
# --------------------------------------------------------------------------- #
def test_manifest_multiseries_selects_main_spatial_area():
    slide = _open(make_ome_multiseries_bytes())
    m = slide_render.build_channel_manifest(slide)
    # 辅助 series（CYX 12ch 32x32）全 shape 乘积更大，但主空间面积小
    assert m["series_index"] == 0
    assert m["axes"] == "YX"
    assert slide.dimensions == (96, 64)
    assert len(m["channels"]) == 1


def test_multifile_ome_rejected_stably():
    data = make_ome_multifile_bytes()
    with pytest.raises(slide_io.SlideRenderError) as ei:
        slide_io.TiffFileSlide(io.BytesIO(data))
    assert ei.value.code == "unsupported_multifile_ome"
    # open_slide 不吞掉该错误去 fallback OpenSlide（禁止悄悄显示不完整数据）
    with pytest.raises(slide_io.SlideRenderError) as ei2:
        slide_io.open_slide(io.BytesIO(data), format_hint="multi.ome.tiff")
    assert ei2.value.code == "unsupported_multifile_ome"


def test_manifest_multichannel_needs_window_for_default_context():
    slide = _open(make_ome_cyx_bytes(c=2, h=32, w=40))
    m = slide_render.build_channel_manifest(slide, with_intensity=True)
    assert m["channels"][0]["intensity"]["source"] == "global-percentile-v1"
    dctx = slide_render.build_default_render_context(
        slide, m, asset_revision="rev-1")
    assert dctx is not None
    assert [c["index"] for c in dctx["active_channels"]] == [0, 1]
    assert dctx["plane"] == {"t": 0, "z": 0}
    # 常量 float：无可用通道 → 返回 None（调用方提示，不造伪默认）
    slide_c = _open(make_ome_float_cyx_bytes(c=1, h=32, w=40, const=True))
    m_c = slide_render.build_channel_manifest(slide_c, with_intensity=True)
    assert slide_render.build_default_render_context(
        slide_c, m_c, asset_revision="rev-1") is None


def test_metrics_counters_present():
    slide = _open(make_ome_cyx_bytes(c=2, h=64, w=96))
    slide_render.build_channel_manifest(slide, with_intensity=True)
    snap = slide_render.metrics_snapshot()
    assert snap["manifest_built"] >= 1
    assert snap["stats_computed"] >= 1
    assert "channels_decoded" in snap
    assert "nonfinite_pixels" in snap
