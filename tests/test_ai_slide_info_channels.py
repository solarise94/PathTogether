# -*- coding: utf-8 -*-
"""G1：AI slide info 两条路径补 channels（真实 OME fixture，无患者数据）。

规格（review-2026-09-05 G1）：
  - ``internal_ai_slide_info``（/internal/ai/slide_info）与
    ``plugin_v1_slide_info``（/api/plugin/v1/slides/<slide>）在**同一次
    borrow_pair** 内读几何 + ``build_render_info``；返回至少
    width/height/level_downsamples/mpp/fingerprint/asset_revision/image_mode/
    channels/warnings/plane/default_render_context/server_capability；
  - internal 可不下发 default_render_token（token 不是 AI 授权，§6.2）；
  - flag 关保持 build_render_info 探测字段行为（channels 等不出现）；
  - OME 名称（DAPI / CD68(480)）两条 info 都返回真名 + color_source=ome；
    RGB 切片 channels=[] 且 image_mode=native_rgb。

运行：cd PathTogether && python3 -m pytest tests/test_ai_slide_info_channels.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

import _bootstrap  # noqa: F401  # session 目录 + openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

import app as app_mod  # noqa: E402
from _tiff_fixtures import make_ome_cyx_bytes, make_ome_tiff_bytes  # noqa: E402

FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"
CYX_NAME = "info_cyx.ome.tiff"
RGB_NAME = "info_rgb.ome.tiff"
INTERNAL_TOKEN = "test-internal-token-aich"

REQUIRED_FIELDS = (
    "width", "height", "level_downsamples", "mpp", "fingerprint",
    "asset_revision", "image_mode", "channels", "warnings", "plane",
    "default_render_context", "server_capability",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "AI_INTERNAL_TOKEN", INTERNAL_TOKEN)
    app_mod.app.config["TESTING"] = True
    yield


def _client():
    return csrf_client(app_mod.app.test_client())


def _flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)


def _write(name, data):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return name


def _write_cyx(name=CYX_NAME):
    return _write(name, make_ome_cyx_bytes(
        c=2, h=64, w=96, names=["DAPI", "CD68(480)"],
        colors=["#0000FF", "#FF00FF"]))


def _write_rgb(name=RGB_NAME):
    return _write(name, make_ome_tiff_bytes())


def _internal_info(slide):
    r = _client().get("/internal/ai/slide_info",
                      headers={"X-AI-Internal-Token": INTERNAL_TOKEN},
                      query_string={"slide": slide})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _plugin_token():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            raw = str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": raw})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _plugin_info(slide, token):
    r = _client().get("/api/plugin/v1/slides/%s" % slide,
                      headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


# --------------------------------------------------------------------------- #
# 1. OME 多通道：两条 info 都返回真实通道名
# --------------------------------------------------------------------------- #
def _assert_ome_channels(body):
    assert body["width"] == 96 and body["height"] == 64
    assert isinstance(body["level_downsamples"], list)
    assert "mpp" in body and "fingerprint" in body
    assert body["asset_revision"]
    assert body["image_mode"] == "multichannel"
    names = [c["name"] for c in body["channels"]]
    assert names == ["DAPI", "CD68(480)"], names
    assert all(c["color_source"] == "ome" for c in body["channels"])
    assert [c["color"] for c in body["channels"]] == ["#0000FF", "#FF00FF"]
    assert body["plane"]["policy"] == "first-plane-v1"
    assert isinstance(body["warnings"], list)
    drc = body["default_render_context"]
    assert drc["version"] == "multichannel-additive-v1"
    assert len(drc["active_channels"]) == 2
    assert body["server_capability"]["multichannel"] is True


def test_internal_slide_info_has_ome_channel_names(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    body = _internal_info(CYX_NAME)
    _assert_ome_channels(body)
    for key in REQUIRED_FIELDS:
        assert key in body, "internal info 缺字段 %s" % key
    # token 不是 AI 授权：internal 通道不下发
    assert "default_render_token" not in body


def test_plugin_slide_info_has_ome_channel_names(monkeypatch):
    _flag_on(monkeypatch)
    _write_cyx()
    body = _plugin_info(CYX_NAME, _plugin_token())
    _assert_ome_channels(body)
    for key in REQUIRED_FIELDS:
        assert key in body, "plugin info 缺字段 %s" % key
    assert "default_render_token" not in body


def test_internal_and_plugin_channels_same_generation(monkeypatch):
    """两条 info 的 channels/fingerprint 对同一切片一致（同一实现取数）。"""
    _flag_on(monkeypatch)
    _write_cyx()
    a = _internal_info(CYX_NAME)
    b = _plugin_info(CYX_NAME, _plugin_token())
    assert a["channels"] == b["channels"]
    assert a["default_render_context"] == b["default_render_context"]
    assert a["asset_revision"] == b["asset_revision"]


# --------------------------------------------------------------------------- #
# 2. RGB 切片：channels=[]、image_mode=native_rgb
# --------------------------------------------------------------------------- #
def test_rgb_slide_info_channels_empty(monkeypatch):
    _flag_on(monkeypatch)
    _write_rgb()
    internal = _internal_info(RGB_NAME)
    assert internal["image_mode"] == "native_rgb"
    assert internal["channels"] == []
    assert internal["default_render_context"]["version"] == "native-rgb-v1"
    plugin = _plugin_info(RGB_NAME, _plugin_token())
    assert plugin["image_mode"] == "native_rgb"
    assert plugin["channels"] == []


# --------------------------------------------------------------------------- #
# 3. flag 关：保持 build_render_info 探测字段行为
# --------------------------------------------------------------------------- #
def test_flag_off_keeps_probe_only_fields(monkeypatch):
    _flag_off(monkeypatch)
    _write_cyx()
    body = _internal_info(CYX_NAME)
    assert body["image_mode"] == "multichannel"
    assert body["server_capability"]["multichannel"] is False
    for key in ("channels", "warnings", "plane", "default_render_context"):
        assert key not in body, "flag 关不得下发 %s" % key


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
