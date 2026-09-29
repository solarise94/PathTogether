# -*- coding: utf-8 -*-
"""C4 上传接入服务端验收（/api/tools/slides/upload-capability + 可查看格式表）。

对应计划 docs/browser-slide-tools-and-baidu-plugin-agent-plan-20260929.md
§1（「工具能导出」与「平台能查看」是不同能力——不支持查看的输出禁用上传
入口并说明原因）、§9 C4；浏览器侧证据在 tests/browser/slide_tools_c4/。

本文件的**读取器证明**是 viewable_formats 的唯一准入门禁：用原生 CLI
（slide-transform-core）转出合成产物，经平台读取器（slide_io.open_slide——
与查看器同一路径）实际打开、读区域（荧光另读通道）。哪个格式打开失败，
就必须从 app.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS 移除——页面随之对该产物
显示「平台暂不支持查看」并禁用上传，本地保存不受影响。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # noqa: F401
import app as app_mod  # noqa: E402
import cos_config  # noqa: E402
import cos_pool_store  # noqa: E402
import slide_format_registry  # noqa: E402
import slide_io  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "slide-transform-core" / "target" / "release" / "slide-transform"

#: 核心结果 format 字符串（convert_bf.rs / convert_fl.rs 的 `format:` 值）
FORMAT_BF = "classic-bigtiff-jpeg-pyramid"
FORMAT_FL = "ome-bigtiff-subifd-multichannel-jpeg-passthrough"


# --------------------------------------------------------------------------- #
# 合成夹具：原生 CLI 生成 + 转换（真实产物；不使用任何私有样本）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def native_fixtures(tmp_path_factory):
    if not CLI.is_file():
        pytest.skip(
            f"native CLI missing: {CLI}（build: PATH=$HOME/.cargo/bin:$PATH "
            "bash scripts/build_slide_transform.sh）")
    d = tmp_path_factory.mktemp("c4-native")
    bf_kfb = d / "bf.kfb"
    fl_kfbf = d / "fl.kfbf"
    bf_tif = d / "bf.tif"
    fl_ome = d / "fl.ome.tif"
    subprocess.run([str(CLI), "gen-kfb", str(bf_kfb),
                    "--width", "580", "--height", "300"], check=True)
    # 600x400：与 C2/C3 夹具同参（部分小尺寸的合成 KFBF 会触发核心校验
    # 拒绝 IFD 布局错位——不用那些尺寸做读取证明）
    subprocess.run([str(CLI), "gen-kfbf", str(fl_kfbf),
                    "--width", "600", "--height", "400"], check=True)
    reports = {}
    for key, src, out in (("bf", bf_kfb, bf_tif), ("fl", fl_kfbf, fl_ome)):
        done = subprocess.run([str(CLI), "convert", str(src), str(out),
                               "--overwrite"], check=True,
                              capture_output=True, text=True)
        reports[key] = json.loads(done.stdout)
    return {"bf": bf_tif, "fl": fl_ome, "reports": reports}


def _open_and_read_region(path, *, channels=False):
    """平台读取器路径：open_slide → read_region（荧光另 read_region_channels）。

    返回 (reader 类型名, 描述 dict)；打开/读取抛异常即证明失败。"""
    osr = slide_io.open_slide(path)
    try:
        img = osr.read_region((0, 0), 0, (64, 64))
        assert img.size == (64, 64), "read_region 返回尺寸不符"
        out = {"reader": type(osr).__name__,
               "levels": getattr(osr, "level_count", None)}
        if channels:
            n = int(osr.channel_count)
            assert n >= 1, "荧光通道数为 0"
            planes, geometry = osr.read_region_channels(
                (0, 0), 0, (64, 64), list(range(n)))
            assert planes.shape[0] == n, "通道 plane 数不符"
            assert planes.shape[1] <= 64 and planes.shape[2] <= 64
            assert geometry["width"] == 64 and geometry["height"] == 64
            out["channels"] = n
        return out
    finally:
        close = getattr(osr, "close", None)
        if close:
            close()


# --------------------------------------------------------------------------- #
# 读取器证明 → viewable_formats 准入
# --------------------------------------------------------------------------- #
def test_reader_proof_brightfield(native_fixtures):
    """明场 BigTIFF 产物经平台读取器打开 + 读区域（读取失败的格式不得列入
    viewable_formats——本用例与 test_viewable_formats_bound_to_proof 绑定）。"""
    info = _open_and_read_region(native_fixtures["bf"])
    assert info["levels"] and info["levels"] >= 2, "金字塔层级未读到"


def test_reader_proof_fluorescence(native_fixtures):
    """荧光 OME-BigTIFF 产物：TiffFileSlide 打开 + 读区域 + 逐通道读取。"""
    info = _open_and_read_region(native_fixtures["fl"], channels=True)
    assert info["channels"] >= 2, "通道读取未证明（KFBF 合成夹具 ≥2 通道）"


def test_format_constants_match_core_reports(native_fixtures):
    """证明用的 format 常量就是核心转换报告里的值（页面按它判定可查看性）。"""
    def fmt(report):
        return (report.get("result") or report).get("format")
    assert fmt(native_fixtures["reports"]["bf"]) == FORMAT_BF
    assert fmt(native_fixtures["reports"]["fl"]) == FORMAT_FL


def test_viewable_formats_bound_to_proof(native_fixtures):
    """端点下发的 viewable_formats 必须恰好等于「读取器证明可打开」的集合。

    若某格式在此失败：把它从 app.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS 移除，
    页面会显示不支持查看并禁用上传（本地保存不受影响）——不得虚列。"""
    proven = set()
    for fmt, path, kw in ((FORMAT_BF, native_fixtures["bf"], {}),
                          (FORMAT_FL, native_fixtures["fl"],
                           {"channels": True})):
        try:
            _open_and_read_region(path, **kw)
        except Exception:  # noqa: BLE001
            continue
        proven.add(fmt)
    listed = set(app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)
    assert listed == proven, (
        f"viewable_formats 与读取证明不一致：仅列出={sorted(listed - proven)} "
        f"仅证明={sorted(proven - listed)}")


# --------------------------------------------------------------------------- #
# 端点：登录 / 载荷字段 / demo 关闭
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _iso(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    app_mod.AUTH_ENABLED = True
    yield


@pytest.fixture()
def _cos_on(monkeypatch):
    """capability on + 池配置过门禁（同 tests/test_ingestion_api.py 的夹具）。"""
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000_000)
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    cos_pool_store.ensure_pool_state()
    yield


def _mkuser(user_id, role):
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


@pytest.fixture()
def owner_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("owner-c4", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-c4@x"
        s["user_id"] = "owner-c4"
        s["role"] = "owner"
        s["auth_version"] = 1
    return client


def test_capability_endpoint_requires_login(_iso):
    """/api/* 未登录 → 401 auth_required（不 302）。"""
    app_mod.app.config["TESTING"] = True
    r = app_mod.app.test_client().get("/api/tools/slides/upload-capability")
    assert r.status_code == 401
    assert r.get_json()["code"] == "auth_required"


def test_capability_endpoint_payload_fields(_iso, _cos_on, owner_client):
    """登录后 200；cos_upload 与工作台 bootstrap 同一权威载荷；格式表成对下发。"""
    r = owner_client.get("/api/tools/slides/upload-capability")
    assert r.status_code == 200
    body = r.get_json()
    caps = body["cos_upload"]
    assert caps["available"] is True
    # 与工作台同形（D3 十进制字节整数；manual_only；注册表派生词表）
    assert caps["manual_only"] is True
    assert caps["max_size_bytes"] == 800_000_000
    for k in ("part_bytes", "url_ttl_seconds", "max_concurrent_parts",
              "sign_batch_max_parts", "policy_version", "formats"):
        assert k in caps, f"缺字段 {k}"
    assert "tif" in caps["formats"]
    fmts = body["viewable_formats"]
    assert isinstance(fmts, list) and fmts
    assert all(isinstance(f, str) and f for f in fmts)
    assert set(fmts) == set(app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_capability_endpoint_off_still_200_with_reason(_iso, owner_client):
    """capability off：仍 200（页面要展示明确原因），available=False，
    viewable_formats 照常下发（格式判定与上传开关是两件事）。"""
    r = owner_client.get("/api/tools/slides/upload-capability")
    assert r.status_code == 200
    body = r.get_json()
    assert body["cos_upload"]["available"] is False
    assert set(body["viewable_formats"]) == \
        set(app_mod.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS)


def test_capability_endpoint_never_demo(_iso, owner_client, monkeypatch):
    """demo 恒关闭：载荷按 demo=False 计算（工具页用户不吃 demo 形态）。"""
    seen = {}

    def spy(demo):
        seen["demo"] = demo
        return {"available": False, "manual_only": True, "formats": ["tif"]}

    monkeypatch.setattr(app_mod, "_cos_upload_capability_payload", spy)
    r = owner_client.get("/api/tools/slides/upload-capability")
    assert r.status_code == 200
    assert seen["demo"] is False


# --------------------------------------------------------------------------- #
# 上传文件名的受理（注册表按后缀判定；荧光需完整 .ome.tif 后缀）
# --------------------------------------------------------------------------- #
def test_upload_filenames_accepted_by_registry():
    """明场 `<base>.tif` 与荧光 `<base>.ome.tif` 都按 native 单文件受理；
    平台侧 OME 识别要求完整 .ome.tif 复合后缀（注册表单列）。"""
    for name in ("x.tif", "x.ome.tif"):
        assert app_mod._cos_ingestion_kind_for(name) == ("native", None), name
    ome_exts = slide_format_registry.ome_extensions()
    assert ".ome.tif" in ome_exts, "注册表必须登记完整 .ome.tif 复合后缀"
    catalog = {f["id"]: f for f in slide_format_registry.public_catalog()}
    assert "ome-tiff" in catalog
    assert ".ome.tif" in catalog["ome-tiff"]["extensions"]
