# -*- coding: utf-8 -*-
"""Wave 2B：BMP/JPEG 普通图片在 app 上传/导入/元数据层的接线测试。

与既有测试的分工（不重复造轮子）：
  - tests/test_slide_io.py 已覆盖：is_raster_ext / open_slide raster 分发的
    稳定码、V1 真 BMP / 截断 / PNG 伪装、V2 真 BMP commit / 垃圾 .jpg commit、
    V2 TIFF 配额释放；本文件不重复这些用例。
  - tests/test_slide_format_registry.py 已覆盖注册表白名单同步与目录行存在性；
    本文件从**端点**视角验证 /api/slide-formats 输出与词表不漂移。

本文件覆盖（raster-image-compat 任务书 §4.1/§4.4/§4.5/§6 上传与其它入口行）：
  1. 元数据合同：_read_metadata 对 RasterSlide 恒输出 mpp/objective=null、
     mpp_source="missing"（DPI/EXIF 分辨率不当作 µm/px，不补 0.25/40×）；
     /api/slide/<name>/info 与 /api/slides 对普通图片输出 §4.4 JSON
     （width/height 为 EXIF 校正后尺寸）；
  2. /api/slide-formats 目录含 raster-image 行且字段完整（注册表驱动）；
  3. V2 ``.uploading-*.part`` + format_hint：真 JPEG 过、PNG 伪装 .jpg 失败
     且任务终态清理无残留、配额不泄漏（真实 _validate_slide_file，不打桩）；
  4. ZIP 成员：合法 BMP 成员走真实解包+验证后可提升；垃圾 .jpg 成员整包
     拒绝（400、无残留）；复用既有安全/配额校验（不打桩不绕过）；
  5. 本地导入（scripts/import_slides.py）：普通图片经同一 open_slide 验证
     可导入；无效文件按稳定码记入 failed 且不中断整批；
  6. 缩略图/DZI 端点 smoke（只读验证，不改缓存/渲染逻辑）：真 BMP 的
     info / .dzi / 低层瓦片 / 缩略图可用。

全部数据为 PIL 合成（无患者数据）；真实请求样本（fr_3fea8fde031a903d）
本机不可达，真实样本待验收。

运行：cd PathTogether && .venv/bin/python -m pytest tests/test_raster_wiring.py -q
"""
import hashlib
import io
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_server as share_srv  # noqa: E402
import slide_cache  # noqa: E402
import slide_io  # noqa: E402
import slide_render  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402


# --------------------------------------------------------------------------- #
# 合成数据（PIL；无患者数据）
# --------------------------------------------------------------------------- #
def _raster_bytes(fmt, w=64, h=48, **save_kwargs):
    """合成确定性 RGB 渐变图（BMP/JPEG 通用）。"""
    from PIL import Image as PILImage

    im = PILImage.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 9) % 256, (x + y) % 256)
    b = io.BytesIO()
    im.save(b, format=fmt, **save_kwargs)
    return b.getvalue()


def _bmp_bytes(w=64, h=48, dpi=None):
    kw = {"dpi": dpi} if dpi else {}
    return _raster_bytes("BMP", w, h, **kw)


def _exif_blob(orientation):
    """最小合法 EXIF：IFD0 仅一个 SHORT 方向标签（274）。"""
    ifd = (struct.pack("<H", 1) + struct.pack("<HHI", 274, 3, 1)
           + struct.pack("<H", orientation) + b"\x00\x00"
           + struct.pack("<I", 0))
    return b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + ifd


def _jpeg_bytes(w=120, h=40, orientation=1, dpi=None):
    kw = {"exif": _exif_blob(orientation)}
    if dpi:
        kw["dpi"] = dpi
    return _raster_bytes("JPEG", w, h, **kw)


# --------------------------------------------------------------------------- #
# 隔离（真验证：不 monkeypatch _validate_slide_file）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """存储隔离 + 上限复位 + 缓存清空（普通图片句柄/瓦片不跨用例泄漏）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 ** 2)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches():
    """切片/元数据/瓦片/渲染缓存清空（isolate_app 换目录后旧句柄不得复用）。"""
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()
    with app_mod._tile_cache_lock:
        app_mod._tile_cache.clear()
    with share_srv._tile_cache_lock:
        share_srv._tile_cache.clear()
    slide_render.reset_caches()


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _residue():
    """V1/V2/ZIP 临时残留（.lock sidecar 为既有设计，不视为残留）。"""
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if (p.name.startswith(".uploading-")
                or p.name.startswith(".extracting-"))
            and not p.name.endswith(".lock")]


def _v1_upload(client, name, data):
    return client.post("/api/upload",
                       data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _v2_create(client, name, size):
    r = client.post("/api/uploads",
                    json={"filename": name, "declared_size": size})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["upload_id"]


def _v2_put(client, upload_id, offset, data):
    return client.put(
        "/api/uploads/%s/chunk?offset=%d&sha256=%s"
        % (upload_id, offset, hashlib.sha256(data).hexdigest()),
        data=data, content_type="application/octet-stream")


# =========================================================================== #
# 1. 元数据合同（§4.4）：缺物理标尺 → null + missing，绝不推断
# =========================================================================== #
def test_read_metadata_raster_contract(tmp_path):
    """RasterSlide：恒输出 mpp/objective=null、mpp_source="missing"。"""
    p = tmp_path / "wire.bmp"
    p.write_bytes(_bmp_bytes(64, 48))
    osr = slide_io.open_slide(p)
    try:
        assert getattr(osr, "is_raster_image", False) is True
        meta = app_mod._read_metadata(osr, p)
    finally:
        osr.close()
    assert meta == {
        "width": 64,
        "height": 48,
        "mpp_x": None,
        "mpp_y": None,
        "objective": None,
        "mpp_source": "missing",
    }


@pytest.mark.parametrize("fmt,maker", [
    ("bmp", lambda: _bmp_bytes(64, 48, dpi=(300, 300))),
    ("jpeg", lambda: _jpeg_bytes(120, 40, dpi=(300, 300))),
])
def test_read_metadata_raster_ignores_dpi(tmp_path, fmt, maker):
    """BMP/JPEG 内嵌 DPI（300dpi → ≈84.67 µm/px）不得变成 mpp，恒 missing。"""
    p = tmp_path / ("dpi." + ("jpg" if fmt == "jpeg" else fmt))
    p.write_bytes(maker())
    osr = slide_io.open_slide(p)
    try:
        meta = app_mod._read_metadata(osr, p)
    finally:
        osr.close()
    assert meta["mpp_x"] is None and meta["mpp_y"] is None
    assert meta["objective"] is None
    assert meta["mpp_source"] == "missing"


def test_slide_info_endpoint_raster_metadata_contract():
    """/api/slide/<name>/info 对普通图片输出 §4.4 JSON（不含物理标尺字段值）。"""
    bmp = _bmp_bytes(64, 48)
    c = _client()
    assert _v1_upload(c, "wire_meta.bmp", bmp).status_code == 200
    r = c.get("/api/slide/wire_meta.bmp/info")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j.get("error") in (None, "")  # 打开成功，非 error 分支
    assert j["name"] == "wire_meta.bmp"
    assert j["width"] == 64 and j["height"] == 48  # EXIF 校正后口径的像素尺寸
    assert j["mpp_x"] is None and j["mpp_y"] is None
    assert j["objective"] is None
    assert j["mpp_source"] == "missing"
    # Batch 3 additive：普通图片同样拿到 DZI 描述（查看链路可用的旁证）
    assert j.get("deepzoom", {}).get("width") >= 64


def test_slide_info_endpoint_exif_corrected_dimensions():
    """EXIF Orientation=6（需旋转 90°）的 JPEG：info 宽高为校正后结果。"""
    jpg = _jpeg_bytes(120, 40, orientation=6)
    assert jpg.startswith(b"\xff\xd8")  # 真 JPEG 字节
    c = _client()
    assert _v1_upload(c, "wire_rot.jpg", jpg).status_code == 200
    r = c.get("/api/slide/wire_rot.jpg/info")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert (j["width"], j["height"]) == (40, 120)  # 宽高交换
    assert j["mpp_source"] == "missing" and j["mpp_x"] is None


def test_slides_list_includes_raster_metadata():
    """/api/slides 列表对普通图片输出同一套缺标尺元数据（无物理倍率）。"""
    c = _client()
    assert _v1_upload(c, "wire_list.bmp", _bmp_bytes(48, 32)).status_code == 200
    r = c.get("/api/slides")
    assert r.status_code == 200
    rows = {it["name"]: it for it in r.get_json()}
    it = rows["wire_list.bmp"]
    assert (it["width"], it["height"]) == (48, 32)
    assert it["mpp_x"] is None and it["mpp_y"] is None
    assert it["objective"] is None and it["mpp_source"] == "missing"


# =========================================================================== #
# 2. /api/slide-formats：注册表驱动的 raster-image 目录行
# =========================================================================== #
def test_slide_formats_endpoint_raster_image_row():
    """/api/slide-formats 含 raster-image 行：direct、单文件、可选上传。"""
    c = _client()
    r = c.get("/api/slide-formats")
    assert r.status_code == 200, r.get_data(as_text=True)
    rows = {it["id"]: it for it in r.get_json()}
    it = rows["raster-image"]
    assert it["display_name"] == "普通图片（BMP / JPEG）"
    assert it["extensions"] == [".bmp", ".jpg", ".jpeg"]
    assert it["capability"] == "native-single-file"
    assert it["canonical_format"] is None
    assert it["bundle_required"] is False
    assert it["import_mode"] == "direct"
    assert it["selectable_for_upload"] is True
    assert any("无物理标尺" in lim for lim in it["limits"])  # 用户向短句


def test_slide_formats_endpoint_extension_vocab_no_drift():
    """目录扩展名词表不漂移：全表扩展名不重叠，普通图片只归 raster-image 行。"""
    c = _client()
    rows = c.get("/api/slide-formats").get_json()
    all_exts = [ext for it in rows for ext in it["extensions"]]
    assert len(all_exts) == len(set(all_exts))  # 无重复登记
    raster = next(it for it in rows if it["id"] == "raster-image")
    for ext in (".bmp", ".jpg", ".jpeg"):
        owners = [it["id"] for it in rows if ext in it["extensions"]]
        assert owners == ["raster-image"], (ext, owners)
    assert not any(ext in all_exts
                   for ext in (".png", ".gif", ".webp"))  # 不自动扩大


# =========================================================================== #
# 3. V2 .part + format_hint（真实 _validate_slide_file，不打桩）
# =========================================================================== #
def test_v2_part_real_jpg_commit_committed():
    """.uploading-*.part 的 .jpg 走真实验证：EXIF JPEG commit 成功，
    提升字节 = 原始上传字节（§4.3：保留原始字节），无临时残留。"""
    jpg = _jpeg_bytes(120, 40, orientation=8)
    c = _client()
    uid = _v2_create(c, "wire_v2.jpg", len(jpg))
    assert _v2_put(c, uid, 0, jpg).status_code == 200
    part = Path(UPLOAD_DIR) / (".uploading-%s.part" % uid)
    assert part.exists()  # 传完未 commit：.part 暂存中
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["state"] == "committed"
    assert (Path(UPLOAD_DIR) / "wire_v2.jpg").read_bytes() == jpg
    assert not part.exists()
    assert _residue() == []


def test_v2_part_png_disguised_as_jpg_fails_clean():
    """PNG 字节伪装 .jpg：commit 409 invalid_slide → failed，无提升无残留。"""
    from PIL import Image as PILImage

    b = io.BytesIO()
    PILImage.new("RGB", (8, 6)).save(b, format="PNG")
    png = b.getvalue()
    c = _client()
    uid = _v2_create(c, "wire_fake.jpg", len(png))
    assert _v2_put(c, uid, 0, png).status_code == 200
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "invalid_slide"  # §4.5：真实格式不符 → invalid_slide
    assert j["state"] == "failed"
    assert not (Path(UPLOAD_DIR) / "wire_fake.jpg").exists()
    # 终态幂等：重复 commit 仍 409，DELETE 清 .part 后无残留
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 409
    assert c.delete("/api/uploads/%s" % uid).status_code == 200
    assert _residue() == []


def test_v2_raster_failure_releases_quota_pg(monkeypatch):
    """PG 后端：垃圾 .jpg commit 确定性失败后配额预占释放、无实占（不泄漏）。"""
    import psycopg
    import user_store

    uid_user = user_store.create_user(
        "rasterq@x.com", "pass1234pass1234", role="user")["user_id"]
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                "SET quota_bytes = EXCLUDED.quota_bytes", (uid_user, 10 ** 7))
    junk = b"\x00junk-not-jpeg" * 64
    c = _client()
    app_mod.AUTH_ENABLED = True
    u = user_store.get_user(uid_user)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid_user
        sess["role"] = "user"
        sess["auth_version"] = (u or {}).get("auth_version", 1)
    r = c.post("/api/uploads", json={"filename": "wire_junk.jpg",
                                     "declared_size": len(junk)})
    uid = r.get_json()["upload_id"]
    assert _v2_put(c, uid, 0, junk).status_code == 200
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 409
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT reserved_bytes, used_bytes "
                        "FROM upload_user_quotas WHERE user_id=%s",
                        (uid_user,))
            reserved, used = cur.fetchone()
    assert reserved == 0 and used == 0  # 预占释放、无实占
    assert not (Path(UPLOAD_DIR) / "wire_junk.jpg").exists()
    c.delete("/api/uploads/%s" % uid)
    assert _residue() == []


# =========================================================================== #
# 4. ZIP 成员（真实解包+验证，复用既有安全/配额校验）
# =========================================================================== #
def _make_zip(members):
    import zipfile

    p = Path(UPLOAD_DIR) / "wire-input.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return p


def test_zip_valid_bmp_member_promoted_with_real_validation():
    """zip 内合法 BMP 成员：真实解包 + 逐成员真实验证后提升，字节一致。"""
    bmp = _bmp_bytes(64, 48)
    z = _make_zip([("wire_zip.bmp", bmp)])
    result = app_mod._prepare_zip_bundle(z)
    assert not isinstance(result, tuple), result
    assert result["slides"] == ["wire_zip.bmp"]
    assert result["main"] == "wire_zip.bmp"
    app_mod._promote_zip_bundle(result)
    assert (Path(UPLOAD_DIR) / "wire_zip.bmp").read_bytes() == bmp
    assert _residue() == []


def test_zip_only_garbage_jpg_member_whole_rejected():
    """zip 内只有垃圾 .jpg 成员：整包 400 拒绝，无任何提升、无残留。"""
    junk = b"\x00not-jpeg-at-all" * 16
    z = _make_zip([("wire_bad.jpg", junk)])
    result = app_mod._prepare_zip_bundle(z)
    assert isinstance(result, tuple)  # (error_message, http_status)
    msg, status = result
    assert status == 400
    assert "未找到可打开的有效切片" in msg
    assert not (Path(UPLOAD_DIR) / "wire_bad.jpg").exists()
    assert _residue() == []


def test_zip_mixed_valid_bmp_and_garbage_jpg():
    """合法 + 无效混合：验证通过的成员进入 slides，无效成员不进可用清单。

    提升语义沿用既有 bundle 行为（entries 全量提升——与既有「损坏 TIFF 伴侣
    成员」一致，不改 MRXS 伴侣目录语义）；可用切片清单 slides 只含验证通过者。
    """
    bmp = _bmp_bytes(32, 24)
    z = _make_zip([("wire_ok.bmp", bmp), ("wire_mixed_bad.jpg",
                                          b"\x00junk" * 16)])
    result = app_mod._prepare_zip_bundle(z)
    assert not isinstance(result, tuple), result
    assert result["slides"] == ["wire_ok.bmp"]  # 垃圾 .jpg 不进可用清单
    app_mod._promote_zip_bundle(result)
    assert (Path(UPLOAD_DIR) / "wire_ok.bmp").read_bytes() == bmp
    assert _residue() == []


# =========================================================================== #
# 5. 本地导入（scripts/import_slides.py）：同一 open_slide 验证
# =========================================================================== #
def test_import_slides_raster_batch_ok_and_stable_failure(tmp_path, monkeypatch):
    """普通图片经 _validate_slide_file（真实 open_slide）导入；垃圾 .jpg 按
    稳定码进 failed 且不中断整批；非切片后缀维持既有拒绝。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import import_slides as imp

    upload = tmp_path / "uploads"
    upload.mkdir(parents=True, exist_ok=True)  # 水位检查需已存在目录
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    src = tmp_path / "incoming"
    src.mkdir()
    bmp = _bmp_bytes(64, 48)
    jpg = _jpeg_bytes(120, 40)
    (src / "wire_imp.bmp").write_bytes(bmp)
    (src / "wire_imp.jpg").write_bytes(jpg)
    (src / "wire_imp_bad.jpg").write_bytes(b"\x00garbage-jpg" * 16)
    (src / "notes.txt").write_bytes(b"x")
    (src / "bundle.zip").write_bytes(b"PK")

    r = imp.run(src, upload_dir=upload)
    assert sorted(r["ok"]) == ["wire_imp.bmp", "wire_imp.jpg"]  # 整批未中断
    errs = {f["file"]: f["error"] for f in r["failed"]}
    assert "code=invalid_slide" in errs["wire_imp_bad.jpg"]  # §4.5 稳定码
    assert "不支持的扩展名" in errs["notes.txt"]
    assert "ZIP" in errs["bundle.zip"]
    assert (upload / "wire_imp.bmp").read_bytes() == bmp  # 原始字节保留
    assert (upload / "wire_imp.jpg").read_bytes() == jpg


def test_import_slides_raster_truncated_rejected_not_promoted(tmp_path,
                                                              monkeypatch):
    """截断 BMP：导入校验失败按稳定码拒绝，目标目录不留下无效文件。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import import_slides as imp

    upload = tmp_path / "uploads"
    upload.mkdir(parents=True, exist_ok=True)  # 水位检查需已存在目录
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    src_dir = tmp_path / "incoming"
    src_dir.mkdir()
    full = _bmp_bytes(64, 48)
    (src_dir / "wire_cut.bmp").write_bytes(full[:int(len(full) * 0.6)])

    r = imp.run(src_dir, upload_dir=upload)
    assert r["ok"] == []
    assert len(r["failed"]) == 1
    assert "code=" in r["failed"][0]["error"]
    assert not (upload / "wire_cut.bmp").exists()


# =========================================================================== #
# 6. 缩略图 / DZI 端点 smoke（只读验证，不改缓存/渲染逻辑）
# =========================================================================== #
def test_bmp_info_dzi_tile_thumbnail_smoke():
    """真 BMP 全链路 smoke：V1 上传 → info → .dzi → 低层瓦片 → 缩略图。"""
    from PIL import Image as PILImage

    bmp = _bmp_bytes(96, 64)
    c = _client()
    assert _v1_upload(c, "wire_view.bmp", bmp).status_code == 200

    # info：渲染 additive 字段可用（deepzoom 描述）
    info = c.get("/api/slide/wire_view.bmp/info").get_json()
    assert info.get("error") in (None, "")
    assert info["width"] == 96 and info["height"] == 64
    assert info["mpp_source"] == "missing"

    # DZI XML
    r = c.get("/api/slide/wire_view.bmp.dzi")
    assert r.status_code == 200
    xml = r.get_data(as_text=True)
    assert 'xmlns="http://schemas.microsoft.com/deepzoom/2008"' in xml
    assert '<Size Width="' in xml

    # 低层瓦片（level 0 = DZI 最小层，单瓦片）
    r = c.get("/api/slide/wire_view.bmp_files/0/0_0.jpeg")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"].startswith("image/jpeg")
    tile = PILImage.open(io.BytesIO(r.get_data()))
    tile.load()
    assert tile.size[0] > 0 and tile.size[1] > 0

    # 缩略图：JPEG 可解码，保持宽高比（96x64 → 3:2）
    r = c.get("/api/slide/wire_view.bmp/thumbnail")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"].startswith("image/jpeg")
    thumb = PILImage.open(io.BytesIO(r.get_data()))
    thumb.load()
    w, h = thumb.size
    assert abs(w / h - 96 / 64) < 0.05


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
