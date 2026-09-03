# -*- coding: utf-8 -*-
"""slide_io 逻辑格式判定 + 上传验证异常契约测试（上传修复 A0）。

修复根因（review-2026-09-02 §2.4）：V1/V2 都把文件暂存为 ``.uploading-*.part``
后用该临时路径验证，而 ``slide_io`` 的 OME 优先分支与普通 TIFF fallback 都按
路径后缀判定，``.part`` 不满足 ``.tif/.tiff`` → 两个 TIFF 分支被跳过，只剩
OpenSlide 尝试，合法 TIFF 被误判为无效切片。

必测场景（spec §Batch A0"必测场景"逐条对应）：
  - 同一 TIFF 字节：``.tiff`` 真路径成功、``.part + format_hint`` 成功、
    ``.part`` 无 hint 的失败行为被明确记录（invalid_slide——这正是被修复的
    根因形态，现在以稳定码呈现而非误导性"无效切片"）；
  - 普通 TIFF、OME-TIFF、OpenSlide 原生 SVS（本仓测试自举把 openslide stub
    成 ``object``（tests/_bootstrap.py），SVS 用例只能验证 stub 语义下的
    **稳定失败码**而非真实打开；注释逐处注明）；
  - 中文和空格文件名（``0702-L2-2 鼠奥球.tiff``）；
  - ``_validate_slide_file(path, *, format_hint=None) -> None`` 异常契约
    （成功返回 None / 失败抛 SlideValidationError / 未知异常收敛为
    slide_open_failed）；
  - 无效 TIFF、截断 TIFF、扩展名伪装 → 稳定码；
  - V1 小 TIFF 真验证（不 monkeypatch）；V2 create→chunks→commit 真验证
    （不 monkeypatch）；
  - offset/hash/expiry/ownership 语义不在此文件回归（test_upload_v2.py 的
    既有用例全量保留且必须全绿）。

fixture：tifffile 内存生成合成渐变图（无患者数据），见 ``_tiff_fixtures``。

运行：cd 项目根 && python3 -m pytest tests/test_slide_io.py -q
"""
import hashlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import slide_io  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, clear_upload_dir  # noqa: E402
from _tiff_fixtures import make_ome_tiff_bytes, make_tiff_bytes  # noqa: E402


TIFF_NAME = "0702-L2-2 鼠奥球.tiff"  # 中文 + 空格 + 连字符（spec 必测场景）


# --------------------------------------------------------------------------- #
# 1. logical_format_ext：逻辑格式识别（不接受目录/URL/NUL/MIME）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,ext", [
    ("a.tiff", ".tiff"),
    ("a.tif", ".tif"),
    ("a.ome.tiff", ".ome.tiff"),   # 最长匹配：不是 .tiff
    ("a.ome.tif", ".ome.tif"),     # 最长匹配：不是 .tif
    ("A.OME.TIFF", ".ome.tiff"),   # 大小写不敏感
    ("a.svs", ".svs"),
    ("a.ndpi", ".ndpi"),
    ("a.mrxs", ".mrxs"),
    ("a.vms", ".vms"),
    ("a.vmu", ".vmu"),
    ("a.scn", ".scn"),
    ("a.bif", ".bif"),
    ("a.svslide", ".svslide"),
    (TIFF_NAME, ".tiff"),          # 中文/空格文件名
    ("/tmp/x/y.TIFF", ".tiff"),    # 完整路径取 basename
])
def test_logical_format_ext_matches(name, ext):
    assert slide_io.logical_format_ext(name) == ext


@pytest.mark.parametrize("bad", [
    None,
    "",
    ".uploading-abcd.part",          # 临时后缀不参与判定（根因形态）
    "http://x/a.tiff",               # URL 拒绝
    "file:///data/a.tif",            # URL 拒绝
    "image/tiff",                    # MIME 字符串拒绝
    "a\x00.tif",                     # NUL 拒绝
    "/tmp/somedir/",                 # 目录样式拒绝
    "a.txt",
    "a.part",
])
def test_logical_format_ext_rejects(bad):
    assert slide_io.logical_format_ext(bad) == ""


def test_slide_validation_error_code_vocabulary():
    assert slide_io.VALID_SLIDE_ERROR_CODES == frozenset((
        "invalid_slide", "slide_open_unsupported", "slide_open_failed"))
    # 未知码收敛为 slide_open_failed（码表封闭）
    e = slide_io.SlideValidationError("nonsense", cause_type="X")
    assert e.code == "slide_open_failed" and e.cause_type == "X"
    assert isinstance(e, ValueError)  # import_slides 按 ValueError 捕获仍成立


def test_logical_exts_stay_in_sync_with_app_supported_exts():
    """防漂移：slide_io.LOGICAL_EXTS 与 app.SUPPORTED_EXTS 必须互相覆盖。

    slide_io 是底层模块不反向 import app，两处各自维护常量（A0 实现取舍，
    见 slide_io.LOGICAL_EXTS docstring）。对应关系：SUPPORTED_EXTS 每个扩展名
    都有 ``.<ext>`` 形态出现在 LOGICAL_EXTS；LOGICAL_EXTS 每项的末段扩展名
    （``.ome.tif`` → ``tif``）都必须属于 SUPPORTED_EXTS（双后缀是更长匹配，
    不是新格式）。
    """
    logical = set(slide_io.LOGICAL_EXTS)
    assert all("." + ext in logical for ext in app_mod.SUPPORTED_EXTS)
    assert all(e.rsplit(".", 1)[-1] in app_mod.SUPPORTED_EXTS for e in logical)


# --------------------------------------------------------------------------- #
# 2. open_slide：真实 TIFF 字节 + .part 临时名场景（根因回归）
# --------------------------------------------------------------------------- #
def test_open_slide_real_tiff_by_real_path(tmp_path):
    """同一 TIFF 字节，``.tiff`` 真路径成功。"""
    p = tmp_path / "plain.tiff"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p)
    try:
        assert osr.dimensions == (96, 64)
        assert osr.properties.get("openslide.vendor") == "generic-tiff"
    finally:
        osr.close()


def test_open_slide_part_with_hint_succeeds(tmp_path):
    """同一 TIFF 字节落在 ``.part``：带 format_hint（净化名）成功。"""
    p = tmp_path / ".uploading-deadbeef.part"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p, format_hint=TIFF_NAME)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()
    # V2 形态：hint 为 task 的 safe_name（.ome 双后缀也要能识别）
    p2 = tmp_path / ".uploading-5f1c9a02.part"
    p2.write_bytes(make_ome_tiff_bytes())
    osr2 = slide_io.open_slide(p2, format_hint="slide.ome.tiff")
    try:
        assert osr2.dimensions == (96, 64)
    finally:
        osr2.close()


def test_open_slide_part_without_hint_fails_documented(tmp_path):
    """``.part`` 无 hint：明确失败（invalid_slide）——被修复的根因形态。

    记录该行为：无格式提示时按 ``.part`` 逻辑名判定 → 不在允许列表 →
    稳定码 invalid_slide，而不是旧行为里含义含混的 OpenSlide 裸异常。
    """
    p = tmp_path / ".uploading-cafebabe.part"
    p.write_bytes(make_tiff_bytes())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"


def test_open_slide_ome_tiff_preferred_branch(tmp_path):
    """OME-TIFF 走 OME 优先分支（.ome.tiff 真路径 + .part+hint 两形态）。"""
    ome = make_ome_tiff_bytes()
    p = tmp_path / "slide.ome.tiff"
    p.write_bytes(ome)
    osr = slide_io.open_slide(p)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()
    p2 = tmp_path / ".uploading-1234.part"
    p2.write_bytes(ome)
    osr2 = slide_io.open_slide(p2, format_hint="0702-L2-2 鼠奥球.ome.tif")
    try:
        assert osr2.dimensions == (96, 64)
    finally:
        osr2.close()


def test_open_slide_chinese_space_name_hint(tmp_path):
    """中文和空格文件名作为 format_hint：``0702-L2-2 鼠奥球.tiff``。"""
    p = tmp_path / ".uploading-cafe0001.part"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p, format_hint=TIFF_NAME)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()


def test_open_slide_svs_stub_semantics(tmp_path):
    """SVS：无真实 openslide 时验证 stub 语义下的**稳定失败码**（注明）。

    tests/_bootstrap.py 把 openslide stub 成 ``object``，真实 SVS 无法在
    单测内打开；装了真实 openslide 的环境（CI 之外的本机/镜像）则应能打开
    generic-tiff 字节。两种环境都断言"要么成功、要么稳定码"，不允许裸异常
    逃逸。
    """
    try:
        import openslide  # noqa: F401
        real = hasattr(openslide, "OpenSlide") and openslide.OpenSlide is not object
    except ImportError:
        real = False
    p = tmp_path / "sample.svs"
    p.write_bytes(make_tiff_bytes())
    if real:  # pragma: no cover  # 仅真实 openslide 环境走该分支
        osr = slide_io.open_slide(p)
        try:
            assert osr.dimensions == (96, 64)
        finally:
            osr.close()
    else:
        # stub 语义：object(path) 抛 TypeError → 解析器/IO 异常 → 稳定码
        with pytest.raises(slide_io.SlideValidationError) as ei:
            slide_io.open_slide(p)
        assert ei.value.code in ("slide_open_failed", "slide_open_unsupported")


# --------------------------------------------------------------------------- #
# 3. 无效 / 截断 / 伪装 → 稳定码
# --------------------------------------------------------------------------- #
def test_open_slide_invalid_tiff_garbage_bytes(tmp_path):
    p = tmp_path / "junk.tif"
    p.write_bytes(b"\x00not-a-tiff-at-all" * 8)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"  # 字节明显无效（tifffile 拒解析）


def test_open_slide_truncated_tiff(tmp_path):
    """截断到只剩文件头（IFD 被截掉）→ tifffile 拒解析 → 稳定码。"""
    data = make_tiff_bytes()
    p = tmp_path / "cut.tif"
    p.write_bytes(data[:24])
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code in ("invalid_slide", "slide_open_failed")


def test_open_slide_extension_disguise(tmp_path):
    """扩展名伪装：合法 TIFF 字节挂不受支持的 ``.png`` 名 → 名字门拒绝。"""
    p = tmp_path / "fake.png"
    p.write_bytes(make_tiff_bytes())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"
    # 反向伪装：垃圾字节挂受支持的切片名 → 字节门拒绝（稳定码）
    p2 = tmp_path / "junk.svs"
    p2.write_bytes(b"garbage")
    with pytest.raises(slide_io.SlideValidationError) as ei2:
        slide_io.open_slide(p2)
    assert ei2.value.code in ("invalid_slide", "slide_open_unsupported",
                              "slide_open_failed")


def test_open_slide_rejects_directory(tmp_path):
    d = tmp_path / "dir.tiff"
    d.mkdir()
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(d)
    assert ei.value.code == "invalid_slide"


def test_open_slide_missing_file_stable_code(tmp_path):
    """IO 类失败（文件不存在）也是稳定码，不透出裸 OSError。"""
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(tmp_path / "missing.tiff")
    assert ei.value.code == "slide_open_failed"


# --------------------------------------------------------------------------- #
# 4. _validate_slide_file 异常契约（app 层）
# --------------------------------------------------------------------------- #
def test_validate_slide_file_returns_none_on_success(tmp_path):
    ok = tmp_path / "ok.tiff"
    ok.write_bytes(make_tiff_bytes())
    assert app_mod._validate_slide_file(ok) is None
    part = tmp_path / ".uploading-x.part"
    part.write_bytes(make_tiff_bytes())
    assert app_mod._validate_slide_file(part, format_hint=TIFF_NAME) is None


def test_validate_slide_file_raises_typed_error(tmp_path):
    p = tmp_path / "bad.tif"
    p.write_bytes(b"junk" * 16)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        app_mod._validate_slide_file(p, format_hint="bad.tif")
    assert ei.value.code == "invalid_slide"
    assert ei.value.cause_type  # 底层异常类型名进日志（不透出前端）


def test_validate_slide_file_unknown_exception_becomes_slide_open_failed(
        tmp_path, monkeypatch):
    """未知异常收敛为 slide_open_failed（含 cause_type），不泄露路径/堆栈。"""
    def boom(path, **_):
        raise RuntimeError("simulated crash with /secret/path")

    monkeypatch.setattr(slide_io, "open_slide", boom)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        app_mod._validate_slide_file(tmp_path / "x.tiff", format_hint="x.tiff")
    assert ei.value.code == "slide_open_failed"
    assert ei.value.cause_type == "RuntimeError"


# --------------------------------------------------------------------------- #
# 5. 路由级真验证（不 monkeypatch）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """存储隔离（真验证用例不得 monkeypatch _validate_slide_file）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, login_limits=True)
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES", 64 * 1024 ** 2)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def _residue():
    """V1/V2 临时残留（V2 的 .lock sidecar 为既有设计，不视为残留）。"""
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if (p.name.startswith(".uploading-")
                or p.name.startswith(".extracting-"))
            and not p.name.endswith(".lock")]


def test_v1_small_real_tiff_no_monkeypatch():
    """V1 小 TIFF 真验证：.part 临时名 + 净化名 hint → 200 提升。"""
    tiff = make_tiff_bytes()
    c = _client()
    r = c.post("/api/upload",
               data={"file": (io.BytesIO(tiff), TIFF_NAME)},
               content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["name"] == TIFF_NAME
    assert (Path(UPLOAD_DIR) / TIFF_NAME).read_bytes() == tiff
    assert _residue() == []


def test_v1_real_truncated_tiff_stable_code_no_residue():
    """V1 截断 TIFF：稳定码 + 临时文件清理（无残留）。"""
    data = make_tiff_bytes()[:24]
    c = _client()
    r = c.post("/api/upload",
               data={"file": (io.BytesIO(data), "cut.tif")},
               content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_slide"
    assert not (Path(UPLOAD_DIR) / "cut.tif").exists()
    assert _residue() == []


def _put(client, upload_id, offset, data):
    return client.put(
        "/api/uploads/%s/chunk?offset=%d&sha256=%s"
        % (upload_id, offset, hashlib.sha256(data).hexdigest()),
        data=data, content_type="application/octet-stream")


def test_v2_create_chunks_commit_real_tiff_no_monkeypatch():
    """V2 create→多 chunk→commit 真验证（无 monkeypatch），中文空格名。"""
    tiff = make_tiff_bytes()
    c = _client()
    r = c.post("/api/uploads", json={"filename": TIFF_NAME,
                                     "declared_size": len(tiff)})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["upload_id"]
    for off in range(0, len(tiff), 4096):
        assert _put(c, uid, off, tiff[off:off + 4096]).status_code == 200
    part = Path(UPLOAD_DIR) / (".uploading-%s.part" % uid)
    assert part.exists()  # 传完但未 commit：part 在
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["state"] == "committed"
    assert (Path(UPLOAD_DIR) / TIFF_NAME).read_bytes() == tiff
    assert not part.exists()
    assert _residue() == []


def test_v2_real_invalid_tiff_commit_stable_code():
    """V2 垃圾 .tif commit：稳定码 failed；DELETE 清 part；终态幂等可查。"""
    junk = b"\x00junk-not-tiff" * 8
    c = _client()
    r = c.post("/api/uploads", json={"filename": "junk.tif",
                                     "declared_size": len(junk)})
    uid = r.get_json()["upload_id"]
    assert _put(c, uid, 0, junk).status_code == 200
    r = c.post("/api/uploads/%s/commit" % uid)
    assert r.status_code == 409
    j = r.get_json()
    assert j["code"] == "invalid_slide"
    assert j["state"] == "failed"
    # 终态幂等：重复 commit 仍 409 failed，GET 可查
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 409
    assert c.get("/api/uploads/%s" % uid).get_json()["state"] == "failed"
    assert not (Path(UPLOAD_DIR) / "junk.tif").exists()
    assert c.delete("/api/uploads/%s" % uid).status_code == 200
    assert _residue() == []


def test_v2_quota_cleanup_on_real_invalid_commit_pg(monkeypatch):
    """PG 后端：真验证失败的确定性失败仍释放预占（无 reservation 泄漏）。"""
    import psycopg
    import user_store

    uid_user = user_store.create_user("si@x.com", "pass1234pass1234",
                                      role="user")["user_id"]
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                "SET quota_bytes = EXCLUDED.quota_bytes", (uid_user, 10 ** 7))
    junk = b"\x00junk" * 64
    c = _client()
    app_mod.AUTH_ENABLED = True

    u = user_store.get_user(uid_user)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid_user
        sess["role"] = "user"
        sess["auth_version"] = (u or {}).get("auth_version", 1)
    r = c.post("/api/uploads", json={"filename": "jq.tif",
                                     "declared_size": len(junk)})
    uid = r.get_json()["upload_id"]
    assert _put(c, uid, 0, junk).status_code == 200
    assert c.post("/api/uploads/%s/commit" % uid).status_code == 409
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT reserved_bytes, used_bytes "
                        "FROM upload_user_quotas WHERE user_id=%s", (uid_user,))
            reserved, used = cur.fetchone()
    assert reserved == 0 and used == 0  # 预占释放、无实占
