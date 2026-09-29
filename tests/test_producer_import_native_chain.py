# -*- coding: utf-8 -*-
"""C5 T1 受控替身全链路（native core 真实转换产物）。

链条：native CLI ``slide-transform gen-kfb``（合成 KFB 源）→ ``convert``
（真实 BigTIFF 转换产物）→ producer 导入（begin/write/commit/cleanup-confirm
全程真实 API）→ slide_id ready 且 authorize_read 可读 / 项目含该 slide /
final consumed=实际字节恰一次 / scratch released 恰一次 / 对账双向核账
0 差异。

CLI 未构建时 skip（构建：PATH=$HOME/.cargo/bin:$PATH bash
scripts/build_slide_transform.sh）。真实百度下载为外部门禁——替身成功不
宣称真实账号可用（§9.1）。

运行：cd 项目根 && TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
tests/test_producer_import_native_chain.py -q
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
from _producer_import_helpers import PluginClient, ProducerEnv  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "slide-transform-core" / "target" / "release" / "slide-transform"

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_upload_capacity",
    str(REPO / "scripts" / "reconcile_upload_capacity.py"))
recon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recon)


@pytest.fixture()
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture()
def env(tmp_path):
    return ProducerEnv(tmp_path)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PRODUCER_IMPORT_WRITE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PRODUCER_IMPORT_WRITE_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    yield tmp_path


@pytest.fixture(scope="module")
def native_bigtiff(tmp_path_factory):
    """native core 真实转换产物（gen-kfb → convert；明场 BigTIFF 金字塔）。"""
    if not CLI.is_file():
        pytest.skip(
            "native CLI missing: %s（build: PATH=$HOME/.cargo/bin:$PATH "
            "bash scripts/build_slide_transform.sh）" % CLI)
    d = tmp_path_factory.mktemp("c5-t1-native")
    src = d / "bf.kfb"
    out = d / "bf-converted.tif"
    subprocess.run([str(CLI), "gen-kfb", str(src),
                    "--width", "580", "--height", "300"], check=True)
    done = subprocess.run([str(CLI), "convert", str(src), str(out),
                           "--overwrite"], check=True, capture_output=True,
                          text=True)
    report = json.loads(done.stdout)
    return {"path": out, "bytes": out.read_bytes(),
            "format": (report.get("result") or report).get("format")}


def test_t1_native_core_chain_visible_and_settled(client, env, tmp_path,
                                                  native_bigtiff):
    """T1：下载替身（本地源）→ native core 转换 → producer 导入 → 可见 +
    计账恰一次 + 对账 0 差异。"""
    data = native_bigtiff["bytes"]
    assert data[:4] in (b"II+\x00", b"MM+\x00"), "交付物须为真实 BigTIFF"
    plugin = PluginClient(client, env)

    # 「插件侧」scratch 补占（下载+转换推进时补占，§4.2）
    sha = hashlib.sha256(data).hexdigest()
    iid, wt, r = plugin.deliver_all(
        data, idem="t1-native-1", declared_sha256=sha, filename="bf-converted.tif")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["state"] == "published"
    assert body["accounted_bytes"] == len(data)
    slide_id = body["slide_id"]

    # slide_id ready 且 authorize_read 可读（owner 本人）
    desc = slide_store.resolve_slide_id(slide_id)
    assert desc.asset_state == slide_store.SlideState.READY
    assert slide_store.authorize_read(desc, actor_user_id=env.uid,
                                      actor_role="user")
    # 产物是 native core 转换出的 BigTIFF（平台 probe 已在 commit 内通过；
    # 这里再独立验证读取器金字塔层）
    import slide_io
    path = slide_storage_entry(slide_id)
    osr = slide_io.open_slide(str(path), format_hint="bf-converted.tif")
    try:
        assert int(getattr(osr, "level_count", 0) or 0) >= 1
    finally:
        osr.close()
    # 项目含该 slide
    proj = share_store.get_project(env.pid)
    assert slide_id in (proj.get("slide_ids") or [])

    # final consumed=实际字节恰一次；scratch released 恰一次
    imp = pim.get_import(iid)
    fr = upload_guard.get_reservation(imp["final_reservation_id"])
    assert fr["state"] == "consumed"
    assert int(fr["settled_bytes"]) == len(data)
    q = upload_guard.get_quota_row(env.uid)
    assert int(q["used_bytes"]) == len(data)
    # 清理确认（scratch released 恰一次）
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert sr["state"] == "released"
    q2 = upload_guard.get_quota_row(env.uid)
    assert int(q2["reserved_bytes"]) == 0
    assert int(q2["used_bytes"]) == len(data)  # 不双减

    # 对账脚本双向核账 0 差异（quota drift 为空、无 dangling/未知目录/
    # blocker；任务已 done 不再有未收口责任）
    conn = psycopg.connect(PG_URI)
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            state = recon.collect(cur, os.environ["UPLOAD_DIR"])
    finally:
        conn.close()
    actions, blockers = recon.plan_actions(state, repair_residuals=False)
    assert state["quota_drift"] == []
    assert state["dangling"] == []
    assert state["unknown_dirs"] == []
    assert blockers == []
    assert actions == []


def slide_storage_entry(slide_id):
    import slide_storage
    return slide_storage.resolve_descriptor_path(
        slide_store.resolve_slide_id(slide_id),
        root=Path(os.environ["UPLOAD_DIR"]))
