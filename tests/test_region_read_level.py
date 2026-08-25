# -*- coding: utf-8 -*-
"""W0 跨仓契约：region 响应携带 read_level（实际解码金字塔层）。

HistoPilot whole-slide-snapshot-fix-plan W0：PathTogether region 响应增加
``read_level``（``osr.get_best_level_for_downsample`` 实际选出的层，非语义
state_level），向后兼容新增字段。本文件覆盖两条**不经 mock 替身**的真实选层
计算路径（fake osr 逼真到足以驱动选层公式）：

  - GET /api/slide/<name>/region（内联实现）：JSON 含 int read_level；
  - POST /internal/ai/region（真实 _read_region_b64）：JSON 含 int read_level。

plugin v1 两条 transport（JSON body + X-Region-Read-Level 头）的断言在
test_plugin_v1_transport.py 现有 region 用例旁（不在此重复）。

运行：cd 项目根 && python3 -m pytest tests/test_region_read_level.py -q
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-rlvl-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token-rlvl"

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

from PIL import Image  # noqa: E402

import app as app_mod  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

_TOKEN = "test-internal-token-rlvl"


class _FakeOsr:
    """逼真 fake：dimensions / level_downsamples / 选层公式 / read_region。

    w=2000, h=1000 → longest=2000 > 1568 → ds=2000/1568≈1.276 → 选层公式
    落在 level 1（get_best_level_for_downsample(1.276)=1），read_level 应为 1
    （≠0 才能证明真的过了选层计算，不是缺省回填）。
    """

    dimensions = (2000, 1000)
    level_downsamples = (1.0, 2.0, 4.0)

    def get_best_level_for_downsample(self, ds):
        if ds <= 1.0:
            return 0
        return 2 if ds > 3.0 else 1

    def read_region(self, loc, level, size):
        return Image.new("RGB", size)


@contextlib.contextmanager
def _borrow_pair_ctx(pair):
    yield pair


_FAKE_ENTRY = {"pool": None, "sem": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
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


def _slide_mocks():
    pair = {"osr": _FakeOsr()}
    return (mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY),
            mock.patch.object(app_mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)),
            mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}))


def test_main_region_json_has_int_read_level():
    """GET /api/slide/<name>/region：JSON 含 read_level 且为 int（真实选层）。"""
    slide = _touch_slide()
    m1, m2, m3 = _slide_mocks()
    with m1, m2, m3:
        r = _client().get("/api/slide/%s/region?x=0&y=0&w=2000&h=1000" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert isinstance(j["read_level"], int)
    assert j["read_level"] == 1  # 2000 长边 → ds≈1.276 → level 1


def test_internal_region_json_has_int_read_level():
    """POST /internal/ai/region（真实 _read_region_b64）：read_level 透传。"""
    slide = _touch_slide()
    m1, m2, m3 = _slide_mocks()
    with m1, m2, m3:
        r = _client().post(
            "/internal/ai/region",
            headers={"X-AI-Internal-Token": _TOKEN, "Content-Type": "application/json"},
            json={"slide": slide, "x": 0, "y": 0, "w": 2000, "h": 1000,
                  "max_long_edge": 1024})
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert isinstance(j["read_level"], int)
    assert j["read_level"] == 1
    # 顺手验证保比例输出（同一 fake 下的既有契约不回退）
    assert j["width"] == 1024 and j["height"] == 512


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
