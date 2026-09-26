# -*- coding: utf-8 -*-
"""viewer 显示编码协议（image-transport-upgrade §5/§6）：profile/dv/ETag/缓存。

覆盖验收矩阵（任务书 §8.2）：
- info additive ``server_capability.display_encoding_v1`` + ``display`` 对象
  （effective image mode / display asset revision / profiles / dv / 默认档）；
- tile/thumbnail 版本化 URL：``?profile=<id>&dv=<display_version>`` 成对提供；
  未知 profile / 非法格式 / 重复冲突参数 → 400 ``invalid_display_profile``；
  合法但过时的 dv → 409 ``display_version_conflict``（错误响应 no-store）；
- RGB 档传给多通道资源 → 400（不得静默按 4:2:0 输出）；
- HTTP 缓存：tile/thumbnail 统一 ``Cache-Control: private, no-cache`` + 强
  ETag（bytes SHA256）；If-None-Match 命中 → 304 无 body；撤权后同 ETag
  不得 304；旧 URL（无 profile/dv）保持既有编码语义可用；
- render-context POST additive ``display_versions``（自定义 context 的 dv）。

切片用 tests/_tiff_fixtures 的合成 OME（无患者数据）；存储隔离同
test_display_jpeg_encoding。运行：
cd PathTogether && python -m pytest tests/test_viewer_encoding_protocol.py -q
"""
import io
import os
import json
import sys
from pathlib import Path

import pytest

import _bootstrap  # noqa: F401  # session 目录 + openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402

from PIL import Image, JpegImagePlugin  # noqa: E402

import share_server as share_srv  # noqa: E402
import slide_cache  # noqa: E402
import slide_render  # noqa: E402
import app as app_mod  # noqa: E402
from _tiff_fixtures import make_ome_cyx_bytes, make_ome_tiff_bytes  # noqa: E402

FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"
CYX_NAME = "vep_cyx4.ome.tiff"
RGB_NAME = "vep_rgb.ome.tiff"


def _reset_caches():
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()
    if hasattr(app_mod, "_viewer_tile_cache"):
        app_mod._viewer_tile_cache.clear()
    if hasattr(share_srv, "_viewer_tile_cache"):
        share_srv._viewer_tile_cache.clear()
    slide_render.reset_caches()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    app_mod.app.config["TESTING"] = True
    share_srv.app.config["TESTING"] = True
    _reset_caches()
    yield
    _reset_caches()


def _client():
    return csrf_client(app_mod.app.test_client())


def _flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def _write(name, data):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    register_slide_row(name)  # P3：无行兼容分支已删——夹具先注册行
    return name


def _sampling(jpeg_bytes):
    img = Image.open(io.BytesIO(jpeg_bytes))
    img.load()
    return JpegImagePlugin.get_sampling(img)


def _info(client, name):
    r = client.get("/api/slide/%s/info" % name)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _tile_url(name=RGB_NAME, level=0, x=0, y=0):
    return "/api/slide/%s_files/%d/%d_%d.jpeg" % (name, level, x, y)


# --------------------------------------------------------------------------- #
# 1. info additive：display 能力对象
# --------------------------------------------------------------------------- #
def test_info_advertises_display_encoding_capability(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    data = _info(_client(), RGB_NAME)
    assert data["server_capability"].get("display_encoding_v1") is True
    disp = data.get("display")
    assert isinstance(disp, dict), "info 缺 display 对象"
    assert disp["image_mode"] == "native_rgb"
    assert disp["display_asset_revision"], "缺 display_asset_revision"
    profiles = {p["profile_id"]: p for p in disp["profiles"]}
    assert set(profiles) == {"native-standard-v1", "native-detail-v1"}
    assert disp["default_profile"] == "native-standard-v1"
    for p in profiles.values():
        assert p.get("display_version"), "每档必须带 display_version"
    # 旧字段不被覆盖（additive）
    assert data["server_capability"]["render_context_endpoint"] is True
    assert "default_render_token" in data


def test_info_display_multichannel_profiles(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    data = _info(_client(), CYX_NAME)
    disp = data["display"]
    assert disp["image_mode"] == "multichannel"
    profiles = {p["profile_id"] for p in disp["profiles"]}
    assert profiles == {"fluorescence-preserve-v1"}, \
        "荧光只提供 preserve 档，不得出现会降 4:2:0 的省流档"


def test_info_display_no_capability_leak_when_flag_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    _write(RGB_NAME, make_ome_tiff_bytes())
    data = _info(_client(), RGB_NAME)
    # flag 关：无多通道能力；display 仍可提供 RGB 两档（legacy 语义不变）
    if data["server_capability"].get("display_encoding_v1"):
        profiles = {p["profile_id"] for p in data["display"]["profiles"]}
        assert profiles <= {"native-standard-v1", "native-detail-v1"}


def test_render_context_response_has_display_versions(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    c = _client()
    r = c.post("/api/slide/%s/render-context" % CYX_NAME,
               json={"active_channels": [{"index": 0}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    dvs = data.get("display_versions")
    assert isinstance(dvs, dict) and dvs, \
        "render-context 响应必须带自定义 context 的 display_versions"
    # 荧光 context：只出现 preserve 词表（tile + thumbnail purpose），
    # 不得出现会降 4:2:0 的 RGB 档
    assert set(dvs) == {"fluorescence-preserve-v1",
                        "fluorescence-thumb-v1"}


# --------------------------------------------------------------------------- #
# 2. 版本化 tile URL：参数校验与 dv 冲突
# --------------------------------------------------------------------------- #
def _dv_from_info(disp, profile_id):
    for p in disp["profiles"]:
        if p["profile_id"] == profile_id:
            return p["display_version"]
    raise AssertionError("profile %s not in display" % profile_id)


def test_versioned_tile_url_ok_and_cache_headers(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    disp = _info(_client(), RGB_NAME)["display"]
    dv = _dv_from_info(disp, "native-standard-v1")
    c = _client()
    url = _tile_url() + "?profile=native-standard-v1&dv=" + dv
    r = c.get(url)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("Cache-Control") == "private, no-cache"
    etag = r.headers.get("ETag")
    assert etag, "tile 响应必须带强 ETag"
    r304 = c.get(url, headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert not r304.data, "304 不得携带图像 body"
    assert r304.headers.get("Cache-Control") == "private, no-cache"


def test_unknown_profile_rejected(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    for q in ("profile=native-bogus-v1&dv=deadbeef",
              "profile=&dv=deadbeef",
              "profile=native-standard-v1",          # 缺 dv
              "dv=deadbeef",                          # 缺 profile
              "dv=%zz",                               # 非法 dv 格式
              ):
        r = c.get(_tile_url() + "?" + q)
        assert r.status_code == 400, "%s → %s" % (q, r.status_code)
        body = r.get_json()
        assert body.get("code") == "invalid_display_profile", body


def test_duplicate_conflicting_params_rejected(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    r = _client().get(_tile_url()
                      + "?profile=native-standard-v1&dv=aa"
                        "&profile=native-detail-v1&dv=bb")
    assert r.status_code == 400
    assert r.get_json().get("code") == "invalid_display_profile"


def test_stale_display_version_conflict_409(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    stale = "0" * 64
    r = c.get(_tile_url() + "?profile=native-standard-v1&dv=" + stale)
    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("code") == "display_version_conflict"
    assert r.headers.get("Cache-Control") == "no-store"


def test_rgb_profile_on_multichannel_rejected(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    c = _client()
    r = c.get(_tile_url(CYX_NAME)
              + "?profile=native-standard-v1&dv=" + "0" * 64)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json().get("code") == "invalid_display_profile"
    assert r.headers.get("Cache-Control") == "no-store"


def test_legacy_tile_url_keeps_working(monkeypatch):
    """无 profile/dv 的旧 URL 继续原有编码语义（native 4:2:0）。"""
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    r1 = c.get(_tile_url())
    assert r1.status_code == 200
    assert _sampling(r1.data) == 2
    # 旧 URL 同样不再 public immutable（§5.4）
    cc = r1.headers.get("Cache-Control") or ""
    assert "immutable" not in cc and "public" not in cc
    assert r1.headers.get("ETag")


# --------------------------------------------------------------------------- #
# 3. thumbnail 版本化与荧光 4:4:4
# --------------------------------------------------------------------------- #
def test_thumbnail_profile_url_and_headers(monkeypatch):
    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    disp = _info(_client(), RGB_NAME)["display"]
    # thumbnail URL 携带 purpose 对应的 profile/dv（§5.2 "同理增加 purpose
    # 对应的 profile/dv"）；缩略图 dv 与 tile dv 不同 purpose → 不同值
    thumb_profiles = {p["profile_id"]: p
                      for p in disp["thumbnail"]["profiles"]}
    assert set(thumb_profiles) == {"native-thumb-v1"}
    thumb_dv = thumb_profiles["native-thumb-v1"]["display_version"]
    tile_profiles = {p["profile_id"]: p for p in disp["profiles"]}
    assert tile_profiles["native-standard-v1"]["display_version"] != thumb_dv
    r = _client().get("/api/slide/%s/thumbnail?profile=native-thumb-v1&dv=%s"
                      % (RGB_NAME, thumb_dv))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("Cache-Control") == "private, no-cache"
    assert r.headers.get("ETag")
    assert _sampling(r.data) == 2, "RGB thumbnail 仍 4:2:0"


def test_thumbnail_rejects_rgb_profile_for_multichannel(monkeypatch):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    r = _client().get("/api/slide/%s/thumbnail?profile=native-standard-v1&dv=%s"
                      % (CYX_NAME, "0" * 64))
    assert r.status_code == 400
    assert r.get_json().get("code") == "invalid_display_profile"


# --------------------------------------------------------------------------- #
# 4. 撤权后同 ETag 不得 304（share 端 revoke 场景）
# --------------------------------------------------------------------------- #
def test_revoked_share_gets_no_304(monkeypatch):
    import share_store

    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data")
    monkeypatch.setattr(share_store, "SHARE_FILE",
                        Path(app_mod.UPLOAD_DIR).parent / "share-data"
                        / "shares.json")
    share_store.set_slide_meta(RGB_NAME)  # P2：分享创建收口——先建行
    share = share_store.create_share([RGB_NAME], 24)
    sc = share_srv.app.test_client()
    url = "/s/%s/api/slide/%s_files/0/0_0.jpeg" % (share["token"], RGB_NAME)
    r1 = sc.get(url)
    assert r1.status_code == 200
    etag = r1.headers.get("ETag")
    assert etag
    assert share_store.revoke_share(share["token"]) is not False
    r2 = sc.get(url, headers={"If-None-Match": etag})
    assert r2.status_code != 304, "撤权后同 ETag 不得返回 304"


# --------------------------------------------------------------------------- #
# 5. demo 入口：能力可用但无 thumbnail 描述
# --------------------------------------------------------------------------- #
def test_demo_info_display_without_thumbnail(monkeypatch):
    import demo_store

    _flag_on(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: True)
    monkeypatch.setattr(demo_store, "is_open", lambda: True, raising=False)
    # capability/开放门直通（本测试只验证 display 的 demo 形态，不验证门本身）
    monkeypatch.setattr(app_mod, "_demo_require_open", lambda: None)
    monkeypatch.setattr(app_mod, "_demo_require_capability",
                        lambda: (None, None))
    # 目录注入（catalog 允许该文件；catalog_get 为 _demo_catalog_slide 实际入口）
    monkeypatch.setattr(demo_store, "catalog_get",
                        lambda sid: {"slide_id": "vep-rgb",
                                     "filename": RGB_NAME,
                                     "display_name": "vep",
                                     "description": "",
                                     "is_default": True}
                        if sid == "vep-rgb" else None,
                        raising=False)
    monkeypatch.setattr(demo_store, "resolve_slide_filename",
                        lambda sid: RGB_NAME if sid == "vep-rgb" else None,
                        raising=False)
    # P2 收口（合同 §3.4）：demo catalog 项必须 resolve_slide_id 命中——
    # 夹具补注册 slides 行（slide_id 固定为 catalog 注入值；断言不变）。
    # P6：行建为 id_bundle（发布 objects/vep-rgb/ 包——legacy 布局运行时不可读）。
    import psycopg as _psy
    _conn = _psy.connect(os.environ["DATABASE_URL"])
    try:
        with _conn.cursor() as _cur:
            # P3：_write 已按夹具注册行（随机 slide_id）——先清掉再落固定 ID 行
            _cur.execute("DELETE FROM slides WHERE legacy_filename=%s",
                         (RGB_NAME,))
            import slide_storage as _ss
            _staging = _ss.staging_dir("vep-demo", "1",
                                       root=Path(app_mod.UPLOAD_DIR))
            _staging.mkdir(parents=True, exist_ok=True)
            _entry = "data.tif"
            _payload = (Path(app_mod.UPLOAD_DIR) / RGB_NAME).read_bytes()
            (_staging / _entry).write_bytes(_payload)
            _ss.publish_bundle_no_clobber(
                _staging, "vep-rgb",
                {"entry": _entry, "files": [{"path": _entry,
                                             "size": len(_payload)}]},
                root=Path(app_mod.UPLOAD_DIR))
            import shutil as _sh
            _sh.rmtree(_staging.parent, ignore_errors=True)
            _cur.execute(
                "INSERT INTO slides (slide_id, legacy_filename, "
                "original_filename, display_name, format_ext, asset_state, "
                "storage_layout, storage_relpath, published_at) VALUES "
                "('vep-rgb', %s, %s, 'vep', 'tif', 'ready', 'id_bundle', %s, "
                "now())",
                (RGB_NAME, RGB_NAME,
                 _ss.entry_relpath("vep-rgb", "tif")))
            _cur.execute(
                "INSERT INTO slide_assets (asset_id, slide_id, "
                "legacy_revision) VALUES ('ast_vep1', 'vep-rgb', %s)",
                ("sha256:%s" % __import__("hashlib").sha256(
                    _payload).hexdigest()[:16],))
            # authorize_read 的 allowlist 查 demo_catalog 表（R-10）：
            # 夹具同时落目录行（catalog_get 的 monkeypatch 保留为直通）。
            _cur.execute(
                "INSERT INTO demo_catalog (slide_id, display_name, "
                "description, is_default) VALUES ('vep-rgb', 'vep', '', TRUE)")
        _conn.commit()
    finally:
        _conn.close()
    c = _client()
    r = c.get("/api/demo/slides/vep-rgb/info")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["server_capability"].get("display_encoding_v1") is True
    assert "thumbnail" not in (data.get("display") or {}), \
        "Demo 无缩略图能力，不得输出 thumbnail 描述"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
