# -*- coding: utf-8 -*-
"""internal_ai_region expected_fingerprint 校验。

运行：cd 项目根 && python3 -m pytest tests/test_region_fingerprint.py -q
"""
import contextlib
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="svs-fp-")
os.environ["SHARE_DATA_DIR"] = os.path.join(TMP, "share-data")
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ["AI_INTERNAL_TOKEN"] = "test-internal-token-fp"

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

# 若其他测试已先 import app，强制指向本次 TMP，避免污染 ~/svs-viewer/uploads。
from pathlib import Path as _Path  # noqa: E402

app_mod.UPLOAD_DIR = _Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _headers():
    return {"X-AI-Internal-Token": "test-internal-token-fp", "Content-Type": "application/json"}


def _touch_slide(name: str = "demo.svs") -> str:
    """_safe_name 要求 UPLOAD_DIR 下存在文件。"""
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    return name


@contextlib.contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def test_region_fingerprint_mismatch_returns_409():
    slide = _touch_slide()
    with mock.patch.object(app_mod, "_slide_fingerprint", return_value="mtime:1"):
        with mock.patch.object(app_mod, "_get_slide") as get_slide:
            get_slide.side_effect = AssertionError("should not open slide on fingerprint mismatch")
            r = _client().post(
                "/internal/ai/region",
                headers=_headers(),
                json={
                    "slide": slide,
                    "x": 0,
                    "y": 0,
                    "w": 100,
                    "h": 100,
                    "expected_fingerprint": "mtime:2",
                },
            )
    assert r.status_code == 409
    assert "指纹" in (r.get_json() or {}).get("error", "")
    get_slide.assert_not_called()


def test_region_fingerprint_match_proceeds_to_read():
    slide = _touch_slide()
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(1000, 1000))}
    with mock.patch.object(app_mod, "_slide_fingerprint", return_value="mtime:1"):
        with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
            with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
                with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                    with mock.patch.object(
                        app_mod,
                        "_read_region_b64",
                        return_value={
                            "image_base64": "QQ==",
                            "mime": "image/jpeg",
                            "width": 64,
                            "height": 64,
                            "src": {"x": 0, "y": 0, "w": 100, "h": 100},
                            "magnification": 20,
                        },
                    ):
                        r = _client().post(
                            "/internal/ai/region",
                            headers=_headers(),
                            json={
                                "slide": slide,
                                "x": 0,
                                "y": 0,
                                "w": 100,
                                "h": 100,
                                "expected_fingerprint": "mtime:1",
                            },
                        )
    assert r.status_code == 200
    assert (r.get_json() or {}).get("image_base64") == "QQ=="


def test_region_omits_fingerprint_still_works():
    """未传 expected_fingerprint 时行为与旧契约一致（不拦）。"""
    slide = _touch_slide()
    fake_entry = {"pool": None, "sem": None}
    pair = {"osr": mock.Mock(dimensions=(1000, 1000))}
    with mock.patch.object(app_mod, "_get_slide", return_value=fake_entry):
        with mock.patch.object(app_mod.slide_cache, "borrow_pair", side_effect=lambda _e: _borrow_pair_ctx(pair)):
            with mock.patch.object(app_mod, "_read_metadata", return_value={"mpp_x": 0.5}):
                with mock.patch.object(
                    app_mod,
                    "_read_region_b64",
                    return_value={
                        "image_base64": "QQ==",
                        "mime": "image/jpeg",
                        "width": 64,
                        "height": 64,
                        "src": {"x": 0, "y": 0, "w": 100, "h": 100},
                        "magnification": 20,
                    },
                ):
                    r = _client().post(
                        "/internal/ai/region",
                        headers=_headers(),
                        json={"slide": slide, "x": 0, "y": 0, "w": 100, "h": 100},
                    )
    assert r.status_code == 200
