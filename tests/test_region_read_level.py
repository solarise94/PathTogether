# -*- coding: utf-8 -*-
"""F1 选层与尺寸诚实：choose_read_level 纯函数 + 两条 region 路由真实选层。

规格（review-2026-09-05 F1）：
  - 先有合法 out 尺寸，再选层：max_ds = min(src_w/out_w, src_h/out_h)；<1 则
    level 0 + upsampled=True；否则在 downsample<=max_ds 的层里选**最粗**层，
    绝不选需要再放大的层（不把 OpenSlide get_best_level_for_downsample 当
    权威——fake osr 干脆不实现它，实现若调用即 AttributeError）。
  - resize 条件 region.size != (out_w,out_h)；响应 width/height == 编码后
    JPEG 实际像素；JSON 增加 upsampled。
  - 旧期望「2000 长边默认 → level 1」是 bug：默认最长边 1568 时 ds=1 的层
    才够 1568px，应读 level 0（本文件改写该期望）。

两条真实路由：GET /api/slide/<name>/region（内联实现）与
POST /internal/ai/region（真实 _read_region_b64）。plugin v1 两条 transport
的断言在 test_plugin_v1_transport.py 现有 region 用例旁（不在此重复）。

运行：cd 项目根 && python3 -m pytest tests/test_region_read_level.py -q
"""
import base64
import contextlib
import io
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token-rlvl"
from PIL import Image  # noqa: E402

import app as app_mod  # noqa: E402
import slide_render  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

_TOKEN = "test-internal-token-rlvl"


class _FakeOsr:
    """金字塔 [1,2,4,8]、dims 8192² 的 fake；**不实现**选层方法。"""

    dimensions = (8192, 8192)
    level_downsamples = (1.0, 2.0, 4.0, 8.0)

    def read_region(self, loc, level, size):
        self.last_read = (loc, level, tuple(size))
        return Image.new("RGB", size)


class _FakeOsr2000:
    """dims 2000×1000、金字塔 [1,2,4] 的 fake（旧用例形态）。"""

    dimensions = (2000, 1000)
    level_downsamples = (1.0, 2.0, 4.0)

    def read_region(self, loc, level, size):
        self.last_read = (loc, level, tuple(size))
        return Image.new("RGB", size)


@contextlib.contextmanager
def _borrow_pair_ctx(pair):
    yield pair


_FAKE_ENTRY = {"pool": None, "sem": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", _TOKEN)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


def _slide_mocks(osr):
    pair = {"osr": osr}
    return (mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY),
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)),
            mock.patch.object(app_mod, "_read_metadata",
                              return_value={"mpp_x": 0.5}))


def _decode_jpeg(payload):
    """响应 JSON → (PIL.Image, json)；断言 JPEG 实际像素 == width/height。"""
    img = Image.open(io.BytesIO(base64.b64decode(payload["image_base64"])))
    img.load()
    assert img.size == (payload["width"], payload["height"]), \
        "JSON width/height 必须等于编码后 JPEG 实际像素"
    return img, payload


# --------------------------------------------------------------------------- #
# choose_read_level 纯函数
# --------------------------------------------------------------------------- #
def test_choose_read_level_picks_coarsest_within_budget():
    """bbox 4096 请求 4096：max_ds=1 → level 0；bbox 8192 请求 4096 → level 1。"""
    ds = (1.0, 2.0, 4.0, 8.0)
    assert slide_render.choose_read_level(ds, 4096, 4096, 4096, 4096) \
        == (0, 1.0, False)
    assert slide_render.choose_read_level(ds, 8192, 8192, 4096, 4096) \
        == (1, 2.0, False)
    # 绝不选需要再放大的层：8192² bbox 请求 2048 → max_ds=4 → level 2 不是 3
    assert slide_render.choose_read_level(ds, 8192, 8192, 2048, 2048)[0] == 2


def test_choose_read_level_upsampled_when_out_exceeds_src():
    """out 任一边大于 src（max_ds<1）→ level 0 + upsampled=True。"""
    ds = (1.0, 2.0)
    assert slide_render.choose_read_level(ds, 1000, 500, 2000, 1000) \
        == (0, 1.0, True)
    assert slide_render.choose_read_level(ds, 2000, 1000, 1568, 784) \
        == (0, 1.0, False)
    # 单层文件
    assert slide_render.choose_read_level((1.0,), 640, 480, 1280, 960) \
        == (0, 1.0, True)


# --------------------------------------------------------------------------- #
# GET /api/slide/<name>/region（内联选层实现）
# --------------------------------------------------------------------------- #
def test_main_region_default_long_edge_reads_level0():
    """默认最长边 1568、bbox 2000×1000 → level 0（旧期望 level 1 是 bug）。"""
    slide = _touch_slide()
    osr = _FakeOsr2000()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().get("/api/slide/%s/region?x=0&y=0&w=2000&h=1000" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert j["width"] == 1568 and j["height"] == 784
    assert j["read_level"] == 0
    assert j["upsampled"] is False
    assert osr.last_read[1] == 0  # 实际 read_region 层


def test_main_region_bbox4096_out4096_reads_level0_full_size():
    """金字塔 [1,2,4,8]、bbox 4096 请求 4096 → level 0 且 JPEG 4096。"""
    slide = _touch_slide()
    osr = _FakeOsr()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().get(
            "/api/slide/%s/region?x=0&y=0&w=4096&h=4096"
            "&out_w=4096&out_h=4096" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert (j["width"], j["height"]) == (4096, 4096)
    assert j["read_level"] == 0 and j["upsampled"] is False
    assert osr.last_read[1] == 0


def test_main_region_bbox8192_out4096_reads_level1_not_level2():
    """bbox 8192 请求 4096 → level 1（ds=2 够用），不要 level 2 再放大。"""
    slide = _touch_slide()
    osr = _FakeOsr()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().get(
            "/api/slide/%s/region?x=0&y=0&w=8192&h=8192"
            "&out_w=4096&out_h=4096" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert (j["width"], j["height"]) == (4096, 4096)
    assert j["read_level"] == 1 and j["upsampled"] is False
    assert osr.last_read[1] == 1


def test_main_region_out_over_4096_clamped_then_level0():
    """bbox 4096 请求 8192：clamp 到 4096 后 out==src → level 0、不放大。"""
    slide = _touch_slide()
    osr = _FakeOsr()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().get(
            "/api/slide/%s/region?x=0&y=0&w=4096&h=4096"
            "&out_w=8192&out_h=8192" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert (j["width"], j["height"]) == (4096, 4096)
    assert j["read_level"] == 0 and j["upsampled"] is False


def test_main_region_upsample_true_when_out_exceeds_src():
    """bbox 1000×500 请求 2000×1000 → level 0 且 upsampled=True。"""
    slide = _touch_slide()
    osr = _FakeOsr()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().get(
            "/api/slide/%s/region?x=0&y=0&w=1000&h=500"
            "&out_w=2000&out_h=1000" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert (j["width"], j["height"]) == (2000, 1000)
    assert j["read_level"] == 0 and j["upsampled"] is True


# --------------------------------------------------------------------------- #
# POST /internal/ai/region（真实 _read_region_b64）
# --------------------------------------------------------------------------- #
def test_internal_region_json_has_int_read_level():
    """_read_region_b64：max_long_edge=1024（bbox 2000×1000）→ level 0。"""
    slide = _touch_slide()
    osr = _FakeOsr2000()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().post(
            "/internal/ai/region",
            headers={"X-AI-Internal-Token": _TOKEN, "Content-Type": "application/json"},
            json={"slide": slide, "x": 0, "y": 0, "w": 2000, "h": 1000,
                  "max_long_edge": 1024})
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert j["read_level"] == 0 and j["upsampled"] is False
    # 保比例输出契约不回退
    assert j["width"] == 1024 and j["height"] == 512
    assert osr.last_read[1] == 0


def test_internal_region_reports_upsampled():
    """_read_region_b64：out 超过 src → upsampled=True 透传。"""
    slide = _touch_slide()
    osr = _FakeOsr()
    m1, m2, m3 = _slide_mocks(osr)
    with m1, m2, m3:
        r = _client().post(
            "/internal/ai/region",
            headers={"X-AI-Internal-Token": _TOKEN, "Content-Type": "application/json"},
            json={"slide": slide, "x": 0, "y": 0, "w": 1000, "h": 500,
                  "out_w": 2000, "out_h": 1000})
    assert r.status_code == 200, r.get_data(as_text=True)
    _, j = _decode_jpeg(r.get_json())
    assert (j["width"], j["height"]) == (2000, 1000)
    assert j["upsampled"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
