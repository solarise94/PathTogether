# -*- coding: utf-8 -*-
"""bf-ome 转换产物走**真实**平台摄取链 + **真实**查看器的端到端验收。

覆盖链路（无任何 slide_io.open_slide 替身/monkeypatch——读取器与生产完全
同一路径）：

  1. 原生 CLI（slide-transform-core）合成 KFB → ``--profile bf-ome`` 转出
     ``x.ome.tif``（ome-bigtiff-subifd-rgb-jpeg-pyramid），另转 ``bf-classic``
     参照产物；CLI 缺失时整文件 skip（与 test_slide_transform_core /
     test_slide_tools_upload_capability 同一定位方式）。
  2. 以登录用户经 **HTTP** ``POST /api/ingestions`` 创建任务（文件名
     ``*.ome.tif``——这正是切片工具页「转换并上传」的服务端入口；与本地
     转换记录的绑定在浏览器侧，服务端门禁是 viewable_formats/注册表），
     用 FakeCos（tests/test_cos_ingest_worker.py 的实现）把**真实产物字节**
     按冻结分块计划 PUT 上去，HTTP ``upload-complete``，再由真实 worker
     duties 推进 preparing→completing→queued→downloading→validating→
     ready→completed（validating 的 open_slide 校验与 ready 的瓦片探针都
     用真实读取器打开真实字节）。
  3. Flask test client 以资产 owner 身份走查看器端点：``/api/slides/<id>/info``
     （层级数/尺寸/mpp/倍率/native_rgb）、``/api/slides/<id>/dzi`` +
     ``/api/slides/<id>/tiles/<level>/<x>_<y>.jpeg``（最高分辨率层、中间层、
     边缘瓦片），瓦片像素与 **classic 产物的 tifffile 参照解码**逐位对比
     （两个 profile 的 tile 载荷逐字节相同——见 reader 级断言；端点瓦片
     另有 JPEG 再编码，用小容差并论证）。
  4. 项目关联与重开：``POST /api/project/create``（slide_ids）、项目详情
     含该切片，再用**全新 client/session** 重取 info+瓦片，结果一致。
  5. 负向回归：读取器与 info 端点都不得把明场 RGB 当 3 个独立通道
     （channel_count==0 / is_native_rgb / image_mode=="native_rgb"）。

用法：
  TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest tests/test_bf_ome_platform_chain.py -q
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # noqa: F401  # session 目录 + openslide 自举

import app as app_mod  # noqa: E402
import cos_config  # noqa: E402
import cos_ingest_worker as ciw  # noqa: E402
import cos_pool_store  # noqa: E402
import ingestion_store as ist  # noqa: E402
import pg_store  # noqa: E402
import slide_io  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from test_cos_ingest_worker import FakeCos  # noqa: E402  # 内存 COS 桶（复用，不复制）

REPO_ROOT = Path(__file__).resolve().parent.parent

#: CLI 定位与 tests/test_slide_transform_core.py 同一口径：env 覆盖 →
#: release → debug；都没有则整文件 skip（不 fail）。
CLI_ENV = os.environ.get(
    "SLIDE_TRANSFORM_CLI",
    str(REPO_ROOT / "slide-transform-core" / "target" / "release" / "slide-transform"))
CLI_DEBUG = REPO_ROOT / "slide-transform-core" / "target" / "debug" / "slide-transform"
CLI = CLI_ENV if Path(CLI_ENV).is_file() else (
    CLI_DEBUG if CLI_DEBUG.is_file() else None)

FORMAT_BF_OME = "ome-bigtiff-subifd-rgb-jpeg-pyramid"
FORMAT_BF_CLASSIC = "classic-bigtiff-jpeg-pyramid"

#: 合成源尺寸：≥3 个金字塔层级（4 层）且 1500/1100 都非 256 倍数——
#: level0 有右/下边缘瓦片（见转换报告 edge_regions）。
SRC_W, SRC_H = 1500, 1100

#: 上传文件名（切片工具页「转换并上传」用的就是产物原名）。
UPLOAD_NAME = "pt-bf-chain.ome.tif"

#: 端点瓦片 JPEG 再编码容差（论证见 test_viewer_tiles_match_classic_reference
#: docstring）：viewer 把解码 RGB 再编码为 legacy native 档 JPEG——
#: app.JPEG_QUALITY=82 + 4:2:0 色度子采样。合成 KFB 图案是**逐像素彩色噪声**
#: （非平滑渐变），4:2:0 把色度降到半分辨率，对这种内容全分辨率逐像素
#: 偏差天然 ~23（亮度 MAD ~5.6）。因此端点瓦片断言用四层口径：
#:   * 亮度 MAD ≤ 10（实测 ~5.6）；
#:   * 每通道均值差 ≤ 2（实测 ~0.03——色彩正确、不偏色）；
#:   * 8×8 整数块均值图 MAD ≤ 6（实测 1.1~1.7——各向同性 8× 平均还原
#:     半分辨率子采样色度，残差只剩 q82 亮度量化。注意不能用 resize 到
#:     64×64 的各向异性 box：边缘瓦片短边仅 77px，横/纵缩放比悬殊时
#:     子采样色度残差不收敛，实测假阳性 MAD ~11.6）；
#:   * 通道配对负向：块均值图上 got[c] 与 ref[c] 相关系数 ≥0.8（实测
#:     0.975~0.995），而 got[c] 与 ref[c']（c≠c'）≤0.5（实测 ≤0.16）——
#:     换通道 / YCbCr 直出 / 通道混合类错误会让对角崩塌、非对角升高
#:     （此图案全分辨率换通道偏差仅 ~30、与子采样噪声同量级，逐像素口径
#:     分辨力不足，故负向放块均值口径）。
#: reader 级（read_region 直读）另用 **0 容差**逐位断言——两个 profile 的
#: tile 载荷逐字节相同，读取器不得引入任何再采样/色彩变换。
TILE_LUMA_MAD_TOL = 10.0
TILE_CHANNEL_MEAN_TOL = 2.0
TILE_BLOCK_MAD_TOL = 6.0
TILE_CHAN_CORR_MIN = 0.8
TILE_CHAN_CORR_CROSS_MAX = 0.5
_BLOCK = 8


def _luma(a):
    return (0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2])


def _block_mean(a):
    """各向同性 8×8 整数块均值（裁到 8 的倍数；纯 numpy，确定性）。"""
    arr = np.asarray(a, dtype=np.float64)
    h = arr.shape[0] // _BLOCK * _BLOCK
    w = arr.shape[1] // _BLOCK * _BLOCK
    arr = arr[:h, :w]
    return arr.reshape(h // _BLOCK, _BLOCK, w // _BLOCK, _BLOCK,
                       arr.shape[2]).mean(axis=(1, 3))


def _corr(a, b):
    av, bv = a.ravel() - a.mean(), b.ravel() - b.mean()
    denom = float(np.sqrt((av * av).sum() * (bv * bv).sum()))
    return float((av * bv).sum() / denom) if denom else 0.0


def _assert_tile_matches_reference(got, ref, where):
    """端点瓦片 vs classic 参照解码（JPEG 再编码容差口径，见常量注释）。"""
    assert got.shape == ref.shape, (where, got.shape, ref.shape)
    g32 = got.astype(np.int32)
    r32 = ref.astype(np.int32)
    luma_mad = float(np.abs(_luma(g32) - _luma(r32)).mean())
    assert luma_mad <= TILE_LUMA_MAD_TOL, (where, "luma", luma_mad)
    for c in range(3):
        dev = abs(float(got[..., c].mean()) - float(ref[..., c].mean()))
        assert dev <= TILE_CHANNEL_MEAN_TOL, (where, "chanmean", c, dev)
    gb, rb = _block_mean(got), _block_mean(ref)
    ds = float(np.abs(gb - rb).mean())
    assert ds <= TILE_BLOCK_MAD_TOL, (where, "block8", ds)
    for c in range(3):
        diag = _corr(gb[..., c], rb[..., c])
        assert diag >= TILE_CHAN_CORR_MIN, (where, "corr-diag", c, diag)
        for c2 in range(3):
            if c2 == c:
                continue
            cross = _corr(gb[..., c], rb[..., c2])
            assert cross <= TILE_CHAN_CORR_CROSS_MAX, (
                where, "corr-cross", c, c2, cross)
    return {"luma_mad": luma_mad, "block8_mad": ds}


# --------------------------------------------------------------------------- #
# 原生产物（module 级共享；转换只做一次）
# --------------------------------------------------------------------------- #
def _cli(*args):
    r = subprocess.run([str(CLI), *map(str, args)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, "slide-transform 失败: %s %s" % (
        r.stdout[-500:], r.stderr[-500:])
    return r.stdout


@pytest.fixture(scope="module")
def products(tmp_path_factory):
    if CLI is None:
        pytest.skip(
            "slide-transform CLI 未构建（%s；build: cd slide-transform-core "
            "&& cargo build --release）" % CLI_ENV)
    d = tmp_path_factory.mktemp("bf-ome-chain")
    src = d / "src.kfb"
    _cli("gen-kfb", src, "--width", SRC_W, "--height", SRC_H)
    # 二进制先于 bf-ome profile 构建的陈旧回退（如 target/debug 旧件）：
    # 干净 skip 并指明重建，而不是把环境陈旧当成转换回归
    stale = subprocess.run(
        [str(CLI), "convert", str(src), str(d / "probe.tif"),
         "--profile", "bf-ome", "--overwrite"],
        capture_output=True, text=True, timeout=600)
    if stale.returncode != 0 and "bf-ome" in (stale.stdout + stale.stderr):
        pytest.skip("slide-transform 二进制不支持 bf-ome profile（陈旧构建 "
                    "%s；请 cd slide-transform-core && cargo build --release "
                    "后重跑）" % CLI)
    ome = d / "out.ome.tif"
    classic = d / "ref-classic.tif"
    rep_ome = json.loads(_cli("convert", src, ome, "--profile", "bf-ome",
                              "--overwrite"))
    rep_cls = json.loads(_cli("convert", src, classic, "--profile",
                              "bf-classic", "--overwrite"))
    probe = json.loads(_cli("probe", src))
    return {"dir": d, "src": src, "ome": ome, "classic": classic,
            "rep_ome": rep_ome, "rep_cls": rep_cls, "probe": probe}


@pytest.fixture(scope="module")
def ome_bytes(products):
    return products["ome"].read_bytes()


# --------------------------------------------------------------------------- #
# 链路环境（每用例独立：conftest 会 TRUNCATE 业务表，链路须用例内推进）
# --------------------------------------------------------------------------- #
OWNER = "bfome-owner"


def _mk_user(user_id, role="user", quota=1 << 30):
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, quota))
    finally:
        conn.close()


def _session_client():
    """owner 会话的 CSRF test client（登录态与真实前端同源）。"""
    app_mod.app.config["TESTING"] = True
    _mk_user(OWNER, "user")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = OWNER + "@x"
        s["user_id"] = OWNER
        s["role"] = "user"
        s["auth_version"] = 1
    return client


def _fresh_session_client():
    """「重开」语义：全新 client + 全新 session（无共享 cookie/句柄）。"""
    return _session_client()


@pytest.fixture()
def chain_env(monkeypatch, tmp_path):
    """单用例链路环境：隔离存储 + COS 池/分块缩放 + 能力开启。

    与 tests/test_cos_ingest_worker.py::_env / test_ingestion_api.py::_pool
    同款参数化，但 UPLOAD_DIR 同时供给 app（isolate_app）与 worker
    （env UPLOAD_DIR → slide_storage.upload_root）——发布包落在 app 服务的
    同一根 objects/ 树上，查看器端点直接读它。
    """
    data_dir = tmp_path / "share-data"
    upload_dir = tmp_path / "uploads"
    isolate_app(monkeypatch, data_dir, upload_dir=upload_dir)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 100_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 1_000_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000_000)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 512 * 1024)  # 多分块
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    cos_pool_store.ensure_pool_state()
    logging.getLogger("svs.cos_ingest").setLevel(logging.ERROR)
    yield {"data_dir": data_dir, "upload_dir": upload_dir}


def _drive_ingestion(chain_env, products, ome_bytes):
    """真实字节走完整 COS 摄取链到 completed（真实读取器，零 monkeypatch）。

    返回 dict：client/slide_id/job/entry（发布包入口路径）。
    """
    client = _session_client()
    size = len(ome_bytes)
    sha = hashlib.sha256(ome_bytes).hexdigest()
    r = client.post("/api/ingestions", json={
        "filename": UPLOAD_NAME, "declared_size": size,
        "sha256_expected": sha})
    assert r.status_code == 202, (r.status_code, r.get_json())
    job_id = r.get_json()["job_id"]
    if ist.get_job(job_id)["state"] != ist.PREPARING:
        out = ist.try_admit_job(job_id)
        assert out["outcome"] == "admitted", out["reason"]
        assert ist.get_job(job_id)["state"] == ist.PREPARING

    fake, st = FakeCos(), {}
    assert ciw.process_preparing(cos=fake, state=st) == job_id
    job = ist.get_job(job_id)
    # 平台对 .ome.tif 的受理口径：注册表按复合后缀判 native 单文件，任务
    # 的 format_ext 取净化名末段（tif）——staging/发布入口都是 data.tif。
    assert job["format_ext"] == "tif"
    assert job["filename"] == UPLOAD_NAME
    # 浏览器直传的真实产物字节按冻结分块计划 PUT（多分块）
    assert len(job["part_plan_json"]) >= 2
    for spec in job["part_plan_json"]:
        fake.put_part(job["object_key"], job["upload_id"],
                      spec["part_number"],
                      ome_bytes[spec["offset"]:spec["offset"] + spec["length"]])
    r2 = client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r2.status_code == 202, r2.get_json()
    assert ciw.process_completing(cos=fake, state=st) == job_id
    assert ciw.process_queued(cos=fake, state=st) == job_id
    assert ciw.process_downloading(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["state"] == ist.VALIDATING
    # validating 的 open_slide 试开 + ready 的瓦片探针：真实读取器开真实
    # 字节（不 patch slide_io.open_slide——这正是被验收的生产行为）
    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    assert ciw.process_ready(cos=fake, state=st) == job_id
    final = ist.get_job(job_id)
    assert final["state"] == ist.COMPLETED and final["viewer_ready"] is True
    assert final["sha256_actual"] == sha
    # 收口远端清理（completed → cleanup_cleaned，池预约释放）
    assert ciw.process_cleanup(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["cleanup_status"] == ist.CLEANUP_CLEANED
    slide_id = final["slide_id"]
    assert slide_id and slide_id.startswith("sld_")
    entry = slide_storage.bundle_dir(
        slide_id, root=str(chain_env["upload_dir"])) / "data.tif"
    return {"client": client, "slide_id": slide_id, "job": final,
            "entry": entry, "sha": sha}


@pytest.fixture()
def chain(products, ome_bytes, chain_env):
    return _drive_ingestion(chain_env, products, ome_bytes)


def _probe_doc(products):
    """CLI probe 报告的文档级字段（宽高/mpp/倍率——bf-ome 的 OME-XML 与
    classic 的 JSON 描述同源于此）。"""
    pr = products["probe"]
    return pr.get("document") or pr


def _report_levels(report):
    return report["levels"] if "levels" in report else report["result"]["levels"]


# --------------------------------------------------------------------------- #
# 0) CLI 产物前置事实（供后续用例论证容差）
# --------------------------------------------------------------------------- #
def test_products_shape_and_shared_payloads(products):
    """转换报告：两个 profile 层级一致（4 层）；两产物逐层解码逐位相同。

    这条事实是瓦片对比容差论证的基础：classic 参照解码与 bf-ome 的平台
    解码在 reader 层面应当零偏差，端点瓦片的任何偏差只来自 JPEG 再编码。
    """
    import tifffile

    rep_o, rep_c = products["rep_ome"], products["rep_cls"]
    assert (rep_o.get("result") or rep_o)["format"] == FORMAT_BF_OME
    assert (rep_c.get("result") or rep_c)["format"] == FORMAT_BF_CLASSIC
    lv_o = _report_levels(rep_o)
    lv_c = _report_levels(rep_c)
    assert len(lv_o) == len(lv_c) >= 3
    assert [(l["width"], l["height"]) for l in lv_o] == \
        [(l["width"], l["height"]) for l in lv_c]
    # level0 右/下边缘瓦片存在（宽高非 256 整数倍）
    assert lv_o[0]["tiles_across"] * 256 > SRC_W
    assert lv_o[0]["tiles_down"] * 256 > SRC_H
    with tifffile.TiffFile(str(products["ome"])) as tf:
        assert tf.is_ome and tf.is_bigtiff
        pg = tf.pages[0]
        assert int(pg.photometric) == 6 and int(pg.compression) == 7
    for level in range(len(lv_o)):
        a = tifffile.imread(str(products["ome"]), level=level)
        b = tifffile.imread(str(products["classic"]), level=level)
        assert a.shape == b.shape
        assert int(np.abs(a.astype(np.int32) - b.astype(np.int32)).max()) == 0


# --------------------------------------------------------------------------- #
# 1) 摄取链：真实字节 → 统一发布 → ready/completed（真实读取器探针）
# --------------------------------------------------------------------------- #
def test_ingestion_chain_publishes_real_ome_bytes(chain, ome_bytes):
    out = chain
    entry = out["entry"]
    assert entry.is_file()
    with open(entry, "rb") as fh:
        assert fh.read() == ome_bytes  # 上传的真实产物字节逐字节落地
    manifest = json.loads((entry.parent / "manifest.json").read_text())
    assert manifest["entry"] == "data.tif"
    assert manifest["files"][0]["sha256"] == out["sha"]
    # 资产行：ready + 归属 + 配额一次结算
    desc = slide_store.resolve_slide_id(out["slide_id"])
    assert desc.asset_state == "ready"
    assert desc.owner_user_id == OWNER
    assert desc.accounted_bytes == len(ome_bytes)
    assert desc.storage_layout == "id_bundle"
    assert slide_store.authorize_read(out["slide_id"], actor_user_id=OWNER)
    assert not slide_store.authorize_read(out["slide_id"], actor_user_id="other")

    def quota_row(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (OWNER,))
        return cur.fetchone()

    conn = pg_store.connect()
    try:
        conn.row_factory = psycopg.rows.dict_row
        with conn.cursor() as cur:
            q = quota_row(cur)
    finally:
        conn.close()
    assert (int(q["used_bytes"]), int(q["reserved_bytes"])) == \
        (len(ome_bytes), 0)


# --------------------------------------------------------------------------- #
# 2) 查看器 info：层级/尺寸/mpp/倍率 + 原生 RGB
# --------------------------------------------------------------------------- #
def _info(chain):
    r = chain["client"].get("/api/slides/%s/info" % chain["slide_id"])
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_viewer_info_matches_cli_report(chain, products):
    info = _info(chain)
    probe = _probe_doc(products)
    levels = _report_levels(products["rep_ome"])
    assert info["width"] == SRC_W and info["height"] == SRC_H
    assert info["size_bytes"] == chain["entry"].stat().st_size
    assert info["slide_id"] == chain["slide_id"]
    assert info["original_filename"] == UPLOAD_NAME
    assert info["format_ext"] == "tif"
    # mpp/倍率来自 OME-XML，与 CLI 对源的 probe 报告一致（metadata 而非估算）
    assert info["mpp_source"] == "metadata"
    assert float(info["mpp_x"]) == pytest.approx(
        float(probe["mpp_x"]), rel=1e-9)
    assert float(info["mpp_y"]) == pytest.approx(
        float(probe["mpp_y"]), rel=1e-9)
    assert float(info["objective"]) == pytest.approx(
        float(probe["objective"]), rel=1e-9)
    # 原生 RGB：info 的 render 字段（flag 关时 build_render_info 早退，
    # channels 键整体不下发——与 3 通道荧光面板形态天然可区分）
    assert info["image_mode"] == "native_rgb"
    assert info.get("channels") in ([], None)
    # DeepZoom 元数据（查看器按它排瓦片）
    dz = info["deepzoom"]
    assert dz["tile_size"] == app_mod.DZ_TILE_SIZE
    assert dz["overlap"] == app_mod.DZ_OVERLAP
    assert dz["max_level"] >= 1
    # 金字塔层级数：真实读取器打开**发布包入口**（与查看器同一路径）
    osr = slide_io.open_slide(chain["entry"])
    try:
        assert type(osr).__name__ == "TiffFileSlide"
        assert osr.level_count == len(levels)
        assert [(w, h) for w, h in osr.level_dimensions] == \
            [(l["width"], l["height"]) for l in levels]
        assert osr.dimensions == (SRC_W, SRC_H)
        assert float(osr.properties["openslide.mpp-x"]) == pytest.approx(
            float(probe["mpp_x"]), rel=1e-9)
    finally:
        osr.close()


# --------------------------------------------------------------------------- #
# 3) 查看器瓦片：最高分辨率层 / 中间层 / 边缘瓦片 vs classic 参照解码
# --------------------------------------------------------------------------- #
def _classic_reference_decoder(products):
    """classic 产物的参照解码器 + DZ 几何（真实 OpenSlide 只取几何坐标，
    像素一律 tifffile 解码 classic——与被测路径完全独立的解码链）。"""
    import openslide
    import tifffile

    cls_path = str(products["classic"])
    osr = openslide.OpenSlide(cls_path)
    from openslide.deepzoom import DeepZoomGenerator
    dzg = DeepZoomGenerator(osr, tile_size=app_mod.DZ_TILE_SIZE,
                            overlap=app_mod.DZ_OVERLAP, limit_bounds=True)
    arrays = {lv: tifffile.imread(cls_path, level=lv)
              for lv in range(osr.level_count)}

    def ref_tile(dz_level, col, row):
        (loc_x, loc_y), osr_level, (w, h) = dzg.get_tile_coordinates(
            dz_level, (col, row))
        ds = osr.level_downsamples[osr_level]
        x0 = int(round(loc_x / ds))
        y0 = int(round(loc_y / ds))
        arr = arrays[osr_level]
        return np.asarray(arr[y0:y0 + h, x0:x0 + w, :3], dtype=np.uint8)

    return osr, dzg, ref_tile


def _endpoint_tile(client, slide_id, dz_level, col, row):
    r = client.get("/api/slides/%s/tiles/%d/%d_%d.jpeg"
                   % (slide_id, dz_level, col, row))
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.headers["Content-Type"].startswith("image/jpeg")
    from PIL import Image
    img = Image.open(io.BytesIO(r.data))
    assert img.format == "JPEG"
    return np.asarray(img.convert("RGB"), dtype=np.uint8), r.data


def test_viewer_tiles_match_classic_reference(chain, products, ome_bytes):
    """端点瓦片 vs classic 参照解码（最高分辨率层/中间层/边缘瓦片）。

    容差口径（见模块常量注释）：端点把 DZG 瓦片再编码为 legacy native 档
    JPEG（q82 + 4:2:0 色度子采样）；合成图案是逐像素彩色噪声，4:2:0 的
    全分辨率逐像素偏差天然 ~23（亮度 MAD ~5.6、8× 下采样后 ~1.7），故用
    亮度/通道均值/下采样三口径 + 通道配对相关负向（换通道/YCbCr 直出类
    错误会让对角相关崩塌）。reader 级 read_region 直读则是**零容差**：
    两 profile tile 载荷逐字节相同（见 test_products_...），读取器不得
    引入任何再采样或色彩变换。
    """
    client, slide_id = chain["client"], chain["slide_id"]
    # DZI XML：尺寸 = 源尺寸；瓦片 URL 指向 ID 原生瓦片端点
    r = client.get("/api/slides/%s/dzi" % slide_id)
    assert r.status_code == 200
    root = ET.fromstring(r.data)
    assert root.get("Format") == "jpeg"
    assert root.get("TileSize") == str(app_mod.DZ_TILE_SIZE)
    size = root.find("{http://schemas.microsoft.com/deepzoom/2008}Size")
    assert (int(size.get("Width")), int(size.get("Height"))) == (SRC_W, SRC_H)

    osr_cls, dzg_cls, ref_tile = _classic_reference_decoder(products)
    try:
        max_level = dzg_cls.level_count - 1
        # 采样：最高分辨率层内部瓦片 + 右下边缘瓦片 + 中间层（ds=2/ds=4，
        # 对应原生 level1/level2——DZG 无重采样，参照即原生层裁剪）
        across, down = dzg_cls.level_tiles[max_level]
        samples = [
            (max_level, 0, 0),
            (max_level, across - 1, down - 1),          # 边缘瓦片（裁边）
            (max_level - 1, 0, 0),                      # ds=2（原生 level1）
            (max_level - 2, 0, 0),                      # ds=4（原生 level2）
        ]
        for dz_level, col, row in samples:
            ref = ref_tile(dz_level, col, row)
            got, _raw = _endpoint_tile(client, slide_id, dz_level, col, row)
            _assert_tile_matches_reference(
                got, ref, "tile %d (%d,%d)" % (dz_level, col, row))
    finally:
        osr_cls.close()

    # reader 级零容差：平台读取器（发布包入口，与查看器同一路径）对
    # level0 区域的 read_region 与 classic 的 tifffile 参照逐位相同。
    import tifffile
    ref0 = np.asarray(tifffile.imread(str(products["classic"]), level=0),
                      dtype=np.uint8)
    osr = slide_io.open_slide(chain["entry"])
    try:
        for (x0, y0, w, h) in ((0, 0, 256, 256),
                               (SRC_W - 200, SRC_H - 128, 200, 128)):
            img = osr.read_region((x0, y0), 0, (w, h)).convert("RGB")
            got = np.asarray(img, dtype=np.uint8)
            ref = ref0[y0:y0 + h, x0:x0 + w]
            assert int(np.abs(got.astype(np.int32)
                              - ref.astype(np.int32)).max()) == 0
    finally:
        osr.close()


def test_viewer_tiles_with_multichannel_flag_on(chain, products, monkeypatch):
    """荧光多通道能力开启（PATHTOGETHER_MULTICHANNEL_ENABLED=1）时，明场
    bf-ome 仍走原生 RGB 瓦片路径——这是读取器修复的直接受益形态（旧读取
    器把 YCbCr S 轴当 3 逻辑通道，flag 开时瓦片会走伪彩合成而偏色）。"""
    monkeypatch.setenv("PATHTOGETHER_MULTICHANNEL_ENABLED", "1")
    info = _info(chain)
    assert info["image_mode"] == "native_rgb"
    assert info["channels"] == []
    osr_cls, dzg_cls, ref_tile = _classic_reference_decoder(products)
    try:
        max_level = dzg_cls.level_count - 1
        ref = ref_tile(max_level, 0, 0)
    finally:
        osr_cls.close()
    got, _ = _endpoint_tile(chain["client"], chain["slide_id"],
                            max_level, 0, 0)
    _assert_tile_matches_reference(got, ref, "flag-on tile")


# --------------------------------------------------------------------------- #
# 4) 项目关联 + 重开（全新 client/session 重取 info 与瓦片）
# --------------------------------------------------------------------------- #
def test_project_association_and_reopen(chain, products):
    client, slide_id = chain["client"], chain["slide_id"]
    r = client.post("/api/project/create", json={
        "name": "bf-ome-chain", "slide_ids": [slide_id]})
    assert r.status_code == 200, r.get_json()
    pid = r.get_json()["pid"]
    assert pid
    # 工作台项目详情：成员双字段，slide_id 在列
    r = client.get("/api/project/%s" % pid)
    assert r.status_code == 200
    body = r.get_json()
    proj = body["project"]
    assert slide_id in (proj.get("slide_ids") or [])
    assert any(a.get("slide_id") == slide_id for a in body["slide_annotations"])
    # 项目列表可见
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert any(p.get("pid") == pid for p in r.get_json())

    # 「重开」：全新 client + 全新 session，从 DB/FS 重新解析资产
    fresh = _fresh_session_client()
    r = fresh.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 200
    info2 = r.get_json()
    assert (info2["width"], info2["height"]) == (SRC_W, SRC_H)
    assert info2["image_mode"] == "native_rgb"
    assert float(info2["mpp_x"]) == pytest.approx(
        float(_probe_doc(products)["mpp_x"]), rel=1e-9)
    # 重开后的瓦片与会话内取到的一致（确定性编码；ETag 同源）
    osr_cls, dzg_cls, _ref = _classic_reference_decoder(products)
    try:
        max_level = dzg_cls.level_count - 1
    finally:
        osr_cls.close()
    _, raw_a = _endpoint_tile(client, slide_id, max_level, 0, 0)
    tile_b = fresh.get("/api/slides/%s/tiles/%d/%d_%d.jpeg"
                       % (slide_id, max_level, 0, 0))
    assert tile_b.status_code == 200
    assert tile_b.data == raw_a
    assert tile_b.headers["ETag"] == '"%s"' % hashlib.sha256(raw_a).hexdigest()
    # 未授权主体不可见（会话隔离仍成立）
    anon = app_mod.app.test_client()
    assert anon.get("/api/slides/%s/info" % slide_id).status_code == 401


# --------------------------------------------------------------------------- #
# 5) 负向回归：不得呈现为 3 个独立通道
# --------------------------------------------------------------------------- #
def test_not_presented_as_three_channels(chain):
    slide_id = chain["slide_id"]
    # 读取器面：原生 RGB、无逻辑通道轴
    osr = slide_io.open_slide(chain["entry"])
    try:
        assert osr.is_native_rgb is True
        assert osr.channel_count == 0
        assert osr.channel_axis is None
        assert osr.plane_sizes["size_c"] == 0
        import slide_render
        assert slide_render.slide_image_mode(osr) == "native_rgb"
    finally:
        osr.close()
    # info 面：channels 空 + native_rgb（不是 3 通道荧光面板；flag 关时
    # channels 键不下发）
    info = _info(chain)
    assert info["image_mode"] == "native_rgb"
    assert info.get("channels") in ([], None)
    assert info.get("server_capability", {}).get("multichannel") is False
    # 列表面（工作台）：同一资产按 ID 出现且无错误项
    r = chain["client"].get("/api/slides")
    assert r.status_code == 200
    mine = [s for s in r.get_json() if s.get("slide_id") == slide_id]
    assert len(mine) == 1
    assert mine[0].get("error") is None
