# -*- coding: utf-8 -*-
"""C5-B 百度插件 worker × 真实 Flask app 集成测试（矩阵 T1 集成行）。

链路：真实平台（PG + producer import API + 百度驱动桥）→ **插件 worker 组件
在真实 HTTP 上跑**（werkzeug make_server 起线程）→ fake 源（绝不触网）→
共享原生核心转换（合成 KFB）→ 交付 → 发布 → 清理确认。

断言（平台侧真实状态，非 stub）：

- 条目经桥 claim/heartbeat/report 推进，item.slide_id 绑定（begin 侧
  baidu_item_id 替位）；
- producer import published/done、slide ready（slide_store 权威视角），
  项目含该 slide；
- final 预约 consumed、scratch 预约 released（upload_reservations SQL）；
- 受管根（平台派生路径）已清空、journal 终态且 write_token 抹除。

已知平台缺口（记录于 C5-B 报告）：桥条目视图未下发 fs_id——本测试以
「批次副本已转存」的对账路径覆盖（worker 的既有副本对账分支），fs_id 缺失
时 worker 以 ``fs_id_missing`` 显式失败。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "pathtogether-baidu-import"))
import _bootstrap  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import app as app_mod  # noqa: E402
import baidu_import_store as bstore  # noqa: E402
import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402
from _baidu_helpers import install_fake  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402

from worker import config as worker_config  # noqa: E402
from worker.batch_driver import BatchDriver  # noqa: E402
from worker.convert import Converter  # noqa: E402
from worker.grants import GrantRegistry  # noqa: E402
from worker.item_task import ItemContext  # noqa: E402
from worker.journal import Journal  # noqa: E402
from worker.platform_client import PlatformClient  # noqa: E402
from worker.source.fake import FakeSource  # noqa: E402

BAIDU_PLUGIN_ID = "dev.pathtogether.baidu-import"
CLI = Path(__file__).resolve().parents[1] / "slide-transform-core" / \
    "target" / "release" / "slide-transform"


@pytest.fixture(scope="module")
def kfb_bytes():
    """合成 KFB（原生 CLI 生成；不使用任何私有样本）。"""
    import subprocess
    if not CLI.is_file():
        pytest.skip("native CLI missing: %s" % CLI)
    out = Path(os.environ.get("TMPDIR", "/tmp")) / (
        "c5b-integr-%s.kfb" % os.urandom(3).hex())
    subprocess.run([str(CLI), "gen-kfb", str(out), "--width", "580",
                    "--height", "300"], check=True)
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data


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
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_INFLIGHT", 10)
    yield tmp_path


@pytest.fixture()
def http_app():
    """真实 Flask app over HTTP（线程化 werkzeug；端口随机）。"""
    server = make_server("127.0.0.1", 0, app_mod.app, threaded=True)
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="c5b-integr-app")
    t.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture()
def env(tmp_path, kfb_bytes):
    """owner 用户 + 项目 + 百度插件安装（白名单 id + slide:import 批准）+
    导入委托 grant。"""
    e = type("E", (), {})()
    e.user = user_store.create_user(
        "c5-%s@example.com" % os.urandom(4).hex(), "pass1234pass1234",
        role="user")
    e.uid = e.user["user_id"]
    e.project = share_store.create_project(
        "C5B-%s" % os.urandom(3).hex(), owner_user_id=e.uid)
    e.pid = e.project["pid"]
    inst = share_store.create_plugin_installation(
        BAIDU_PLUGIN_ID, approved_scopes=["slide:import"])
    e.installation = inst
    e.installation_id = inst["installation_id"]
    e.secret = inst["secret"]
    e.grant = pim.create_import_grant(
        e.uid, e.installation_id, BAIDU_PLUGIN_ID, e.pid)
    e.grant_id = e.grant["grant_id"]
    return e


def _sql_one(query, args=()):
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchone()


@pytest.fixture()
def batch(env, kfb_bytes, monkeypatch):
    """queued 批次（真实枚举→导入；fake 适配器只供分享元数据）。"""
    install_fake(monkeypatch, entries=[
        {"path": "/B1/big.kfb", "size": len(kfb_bytes)}])
    fake = bstore.get_adapter()
    out = bstore.create_enumeration(
        env.uid, "https://pan.baidu.com/s/1TestShareId77")
    result = bstore.run_enumeration(out["id"], fake)
    assert result["state"] == "ready", result
    cands = bstore.list_candidates(out["id"], env.uid)
    cand = [c for c in cands["items"] if c["name"] == "big.kfb"][0]

    def hook(user_id, nbytes):
        res = upload_guard.reserve_upload(user_id, nbytes)
        return res["reservation_id"]

    imp = bstore.create_import(
        env.uid, out["id"], [cand["id"]], target_project_id=env.pid,
        idempotency_key="c5b-integr-%s" % os.urandom(3).hex(),
        quota_hook=hook if upload_guard.quota_applies(
            {"role": "user", "user_id": env.uid}) else None)
    return imp


def test_worker_full_chain_against_real_app(http_app, env, batch, kfb_bytes,
                                            tmp_path):
    # ---- 插件 worker 组件（真实 HTTP 平台 + fake 源 + 原生核心） ---- #
    cfg = worker_config.Config({
        "PT_PLATFORM_URL": http_app,
        "PT_INSTALLATION_ID": env.installation_id,
        "PT_INSTALLATION_SECRET": env.secret,
        "SHARE_DATA_DIR": os.environ["SHARE_DATA_DIR"],   # 受管根同基座
        "PT_IMPORT_GRANTS": "%s:%s" % (env.pid, env.grant_id),
        "SLIDE_TRANSFORM_BIN": str(CLI),
        "SLIDE_TRANSFORM_TIMEOUT_SECONDS": "600",
        "PT_HTTP_MAX_ATTEMPTS": "3",
        "PT_HTTP_BACKOFF_BASE": "0.05",
        "PT_BAIDU_LEASE_SECONDS": "300",
        "PT_BAIDU_HEARTBEAT_INTERVAL": "0.5",
        "PT_CLEANUP_MAX_ATTEMPTS": "5",
        "PT_CLEANUP_BACKOFF_BASE": "0.05",
        "PT_RECEIPT_POLL_SECONDS": "0.1",
        "PT_RECEIPT_POLL_MAX": "60",
    })
    assert cfg.work_root == Path(share_store.SHARE_DATA_DIR) / "plugin-work"
    platform = PlatformClient(cfg)
    # fake 源：批次副本已转存（fs_id 缺口的对账路径；内容 = 合成 KFB）
    source = FakeSource(entries=[
        {"path": "/B1/big.kfb", "fs_id": "7000001",
         "size": len(kfb_bytes), "content": kfb_bytes}])
    batch_id = batch["id"]
    source.copies[batch_id] = {
        "big.kfb": {"fs_id": "7000001", "path": "/B1/big.kfb",
                    "name": "big.kfb", "size": len(kfb_bytes),
                    "content": kfb_bytes}}
    converter = Converter(cfg.slide_transform_bin,
                          timeout=cfg.convert_timeout)
    journal = Journal(cfg.journal_dir)
    grants = GrantRegistry(cfg.grants_path, seed=cfg.grant_seed)
    ctx = ItemContext(cfg, platform, source, converter, journal, grants)

    summary = BatchDriver(ctx).run_once()
    assert summary and summary.get("batch_id") == batch_id, summary
    (outcome,) = summary["outcomes"]
    assert outcome["stage"] == "done", outcome
    assert outcome["cleanup_status"] == "cleaned"
    assert outcome["slide_id"]

    # ---- 平台侧权威断言 ---- #
    import_id = outcome["import_id"]
    imp = pim.get_import(import_id)
    assert imp["state"] in ("published", "done")
    assert imp["plugin_cleanup_status"] == "cleaned"
    assert int(imp["scratch_confirmed_bytes"]) >= len(kfb_bytes)

    slide_id = imp["slide_id"]
    desc = slide_store.resolve_slide_id(slide_id)
    assert desc is not None and desc.asset_state == "ready", \
        "统一发布后资产 ready"
    # 项目关联（§2.5：settle 事务内）
    proj = share_store.get_project(env.pid)
    assert slide_id in (proj.get("slide_ids") or [])

    # 条目：桥回写 ready + slide_id 绑定（begin 侧 baidu_item_id）
    item = _sql_one(
        "SELECT stage, slide_id FROM baidu_import_items "
        "WHERE batch_id=%s ORDER BY created_at LIMIT 1", (batch_id,))
    assert item[0] == "ready"
    assert item[1] == slide_id
    # 批次收口（末条 report 后平台同事务 finalize）：终态 + 批次预算收口。
    # 条目产物已由 producer 任务自身的 final 预约计费，批次预算只释放——
    # 再 consume 就是双重计费。
    bstate, bres = _sql_one(
        "SELECT state, quota_reservation_id FROM baidu_import_batches "
        "WHERE id=%s", (batch_id,))
    assert bstate == "succeeded"
    assert bres, "测试用户受配额约束，批次应有预算预约"
    assert _sql_one("SELECT state FROM upload_reservations "
                    "WHERE reservation_id=%s", (bres,))[0] == "released"
    used, reserved = _sql_one(
        "SELECT used_bytes, reserved_bytes FROM upload_user_quotas "
        "WHERE user_id=%s", (env.uid,))
    assert int(used) == int(desc.accounted_bytes), \
        "实占只计产物一次（%s != %s）" % (used, desc.accounted_bytes)
    assert int(reserved) == 0

    # 容量对账：final consumed / scratch released（单任务两份预约）
    purposes = {}
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.purpose, r.state FROM upload_reservations r "
                "JOIN producer_imports p ON "
                "p.final_reservation_id=r.reservation_id OR "
                "p.scratch_reservation_id=r.reservation_id "
                "WHERE p.import_id=%s", (import_id,))
            for purpose, state in cur.fetchall():
                purposes[purpose] = state
    assert purposes.get("final") == "consumed", purposes
    assert purposes.get("scratch") == "released", purposes

    # 受管根（平台派生路径）已清空；journal 终态 + 凭证抹除
    root = pim.managed_root(env.installation_id, import_id)
    assert not root.exists()
    rec = journal.load_all()[import_id]
    assert rec["stage"] == "done"
    assert "write_token" not in rec
    assert rec["receipt"]["state"] in ("published", "done")
    # 源副本已清理（仅本条目；云端分享源不动）
    assert source.copies.get(batch_id) == {}


def test_worker_claim_reports_lease_fence_against_real_app(
        http_app, env, batch, kfb_bytes, monkeypatch):
    """租约被夺 → report 被 fence → 驱动器安静放弃（不覆盖新 owner）。"""
    cfg = worker_config.Config({
        "PT_PLATFORM_URL": http_app,
        "PT_INSTALLATION_ID": env.installation_id,
        "PT_INSTALLATION_SECRET": env.secret,
        "SHARE_DATA_DIR": os.environ["SHARE_DATA_DIR"],
        "PT_IMPORT_GRANTS": "%s:%s" % (env.pid, env.grant_id),
        "SLIDE_TRANSFORM_BIN": str(CLI),
        "PT_HTTP_MAX_ATTEMPTS": "3",
        "PT_HTTP_BACKOFF_BASE": "0.05",
        "PT_BAIDU_LEASE_SECONDS": "300",
        "PT_BAIDU_HEARTBEAT_INTERVAL": "0.5",
        "PT_CLEANUP_MAX_ATTEMPTS": "5",
        "PT_CLEANUP_BACKOFF_BASE": "0.05",
    })
    source = FakeSource(entries=[
        {"path": "/B1/big.kfb", "fs_id": "7000001",
         "size": len(kfb_bytes), "content": kfb_bytes}])
    source.copies[batch["id"]] = {
        "big.kfb": {"fs_id": "7000001", "path": "/B1/big.kfb",
                    "name": "big.kfb", "size": len(kfb_bytes),
                    "content": kfb_bytes}}
    ctx = ItemContext(
        cfg, PlatformClient(cfg), source,
        Converter(cfg.slide_transform_bin, timeout=cfg.convert_timeout),
        Journal(cfg.journal_dir),
        GrantRegistry(cfg.grants_path, seed=cfg.grant_seed))

    stolen = {"done": False}

    def on_item_outcome(outcome):
        # 第一个条目收口后：进程内执行者重领批次（租约过期 + 重领 = 夺租）
        if not stolen["done"] and outcome.get("stage") == "done":
            stolen["done"] = True
            from _baidu_helpers import expire_batch_lease
            expire_batch_lease(batch["id"])
            assert bstore.claim_batch(worker_id="other-worker") is not None

    summary = BatchDriver(
        ctx, hooks={"on_item_outcome": on_item_outcome}).run_once()
    assert summary.get("abandoned") == "lease_lost"
    assert stolen["done"] is True


def test_worker_retry_after_failed_item_against_real_app(
        http_app, env, batch, kfb_bytes):
    """首次尝试失败 → 用户 retry_items 重排（同 item_id）→ 插件以新幂等键
    续做并发布到条目绑定的同一 slide_id。"""
    cfg = worker_config.Config({
        "PT_PLATFORM_URL": http_app,
        "PT_INSTALLATION_ID": env.installation_id,
        "PT_INSTALLATION_SECRET": env.secret,
        "SHARE_DATA_DIR": os.environ["SHARE_DATA_DIR"],
        "PT_IMPORT_GRANTS": "%s:%s" % (env.pid, env.grant_id),
        "SLIDE_TRANSFORM_BIN": str(CLI),
        "SLIDE_TRANSFORM_TIMEOUT_SECONDS": "600",
        "PT_HTTP_MAX_ATTEMPTS": "3",
        "PT_HTTP_BACKOFF_BASE": "0.05",
        "PT_BAIDU_LEASE_SECONDS": "300",
        "PT_BAIDU_HEARTBEAT_INTERVAL": "0.5",
        "PT_CLEANUP_MAX_ATTEMPTS": "5",
        "PT_CLEANUP_BACKOFF_BASE": "0.05",
        "PT_RECEIPT_POLL_SECONDS": "0.1",
        "PT_RECEIPT_POLL_MAX": "60",
    })
    source = FakeSource(entries=[
        {"path": "/B1/big.kfb", "fs_id": "7000001",
         "size": len(kfb_bytes), "content": kfb_bytes}])
    batch_id = batch["id"]
    source.copies[batch_id] = {
        "big.kfb": {"fs_id": "7000001", "path": "/B1/big.kfb",
                    "name": "big.kfb", "size": len(kfb_bytes),
                    "content": kfb_bytes}}
    source.fail_download_names.add("big.kfb")
    ctx = ItemContext(
        cfg, PlatformClient(cfg), source,
        Converter(cfg.slide_transform_bin, timeout=cfg.convert_timeout),
        Journal(cfg.journal_dir),
        GrantRegistry(cfg.grants_path, seed=cfg.grant_seed))

    first = BatchDriver(ctx).run_once()
    (failed,) = first["outcomes"]
    assert failed["stage"] == "failed", failed
    item_id = failed["item_id"]
    stage, bound_slide = _sql_one(
        "SELECT stage, slide_id FROM baidu_import_items WHERE id=%s",
        (item_id,))
    assert stage == "failed" and bound_slide
    assert _sql_one("SELECT state, lease_token FROM baidu_import_batches "
                    "WHERE id=%s", (batch_id,)) == ("failed", None)
    assert slide_store.resolve_slide_id(bound_slide).asset_state == "staging"

    source.fail_download_names.clear()
    bstore.retry_items(batch_id, env.uid, [item_id])
    second = BatchDriver(ctx).run_once()
    assert second and second.get("batch_id") == batch_id, second
    (done,) = second["outcomes"]
    assert done["stage"] == "done", done
    assert done["import_id"] != failed["import_id"]
    assert done["slide_id"] == bound_slide
    assert slide_store.resolve_slide_id(bound_slide).asset_state == "ready"
    assert _sql_one("SELECT stage FROM baidu_import_items WHERE id=%s",
                    (item_id,))[0] == "ready"
    assert _sql_one("SELECT state FROM baidu_import_batches WHERE id=%s",
                    (batch_id,))[0] == "succeeded"
