# -*- coding: utf-8 -*-
"""C5 百度插件驱动桥测试（合同 §6.3；矩阵 T16 平台侧）。

- 鉴权：slide:import scope + 百度插件白名单（镜像 _USAGE_INGEST_PLUGIN_IDS
  形态）——非白名单插件 403；
- fencing：插件 claim 与进程内 claim_batch 同一 SKIP LOCKED + lease_token
  原语——两执行者不可能双双持批；租约被夺后 heartbeat/report 被拒；
- begin 侧 item.slide_id 绑定：baidu_item_id 走 producer begin（_allocate_
  item_slide 的替位）——预分配落库、重试复用同一 slide_id。

真实百度下载为外部门禁；受控替身成功不宣称真实账号可用（§9.1）。
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import baidu_import_store as bstore  # noqa: E402
import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402
from _baidu_helpers import install_fake  # noqa: E402
from _producer_import_helpers import (PluginClient, ProducerEnv,  # noqa: E402
                                       build_deliverable, sql_one)
from _pt_helpers import isolate_app  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]
BAIDU_PLUGIN_ID = "dev.pathtogether.baidu-import"


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
    # 本文件多通道并存（baidu 批次预算 + producer final/scratch 双预约），
    # 在途上限放宽（env 可调语义；不改变护栏本身）
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_INFLIGHT", 10)
    install_fake(monkeypatch)
    yield tmp_path


@pytest.fixture()
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture()
def env(tmp_path):
    """百度驱动插件安装（白名单 plugin_id + slide:import 批准）。"""
    e = ProducerEnv(tmp_path)
    inst = share_store.create_plugin_installation(
        BAIDU_PLUGIN_ID, approved_scopes=["slide:import"])
    e.installation = inst
    e.installation_id = inst["installation_id"]
    e.secret = inst["secret"]
    e.grant = pim.create_import_grant(
        e.uid, inst["installation_id"], BAIDU_PLUGIN_ID, e.pid)
    e.grant_id = e.grant["grant_id"]
    return e


@pytest.fixture()
def plugin(client, env):
    return PluginClient(client, env)


@pytest.fixture()
def deliverable(tmp_path):
    return build_deliverable(tmp_path / "bf-bridge.tif")


@pytest.fixture()
def batch(env):
    """造一个 queued 批次（枚举→导入；quota_hook 走真实预占）。"""
    fake = bstore.get_adapter()
    out = bstore.create_enumeration(env.uid,
                                    "https://pan.baidu.com/s/1TestShareId77")
    result = bstore.run_enumeration(out["id"], fake)
    assert result["state"] == "ready", result
    cands = bstore.list_candidates(out["id"], env.uid)
    cand = [c for c in cands["items"]
            if c["name"] == "sample.svs"][0]

    def hook(user_id, nbytes):
        res = upload_guard.reserve_upload(user_id, nbytes)
        return res["reservation_id"]

    imp = bstore.create_import(env.uid, out["id"], [cand["id"]],
                               target_project_id=env.pid,
                               idempotency_key="c5-bridge-%s"
                               % os.urandom(3).hex(),
                               quota_hook=hook if
                               upload_guard.quota_applies(
                                   {"role": "user", "user_id": env.uid})
                               else None)
    return imp


def _err_code(r):
    return (r.get_json() or {}).get("error", {}).get("code")


def _h(plugin):
    return {"Authorization": "Bearer " + plugin.token()}


# --------------------------------------------------------------------------- #
# 鉴权与白名单
# --------------------------------------------------------------------------- #
def test_bridge_requires_baidu_plugin_allowlist(client, env, tmp_path):
    """非百度白名单插件（即便批准了 slide:import）→ 403。"""
    other = ProducerEnv(tmp_path / "other")
    other_plugin = PluginClient(client, other)
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(other_plugin), json={})
    assert r.status_code == 403 and _err_code(r) == "forbidden"
    r = client.post("/api/plugin/v1/baidu/items/bitem_x/report",
                    headers=_h(other_plugin),
                    json={"batch_id": "bib_x", "lease_token": "t",
                          "fields": {"stage": "downloading"}})
    assert r.status_code == 403
    r = client.post("/api/plugin/v1/baidu/batches/heartbeat",
                    headers=_h(other_plugin),
                    json={"batch_id": "bib_x", "lease_token": "t"})
    assert r.status_code == 403


def test_bridge_claim_heartbeat_report_roundtrip(client, env, plugin, batch):
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(plugin), json={"lease_seconds": 600})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["claimed"] is True
    claim = body["batch"]
    assert claim["id"] == batch["id"]
    assert claim["share_url"].startswith("https://pan.baidu.com/")
    assert claim["lease"]["lease_token"]
    item = body["items"][0]
    # heartbeat 续租
    r = client.post("/api/plugin/v1/baidu/batches/heartbeat",
                    headers=_h(plugin),
                    json={"batch_id": claim["id"],
                          "lease_token": claim["lease"]["lease_token"]})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # report（stage 推进 + 白名单字段）
    r = client.post("/api/plugin/v1/baidu/items/%s/report" % item["id"],
                    headers=_h(plugin),
                    json={"batch_id": claim["id"],
                          "lease_token": claim["lease"]["lease_token"],
                          "fields": {"stage": "downloading",
                                     "source_sha256": "a" * 64}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["item"]["stage"] == "downloading"
    # 白名单外字段拒绝
    r = client.post("/api/plugin/v1/baidu/items/%s/report" % item["id"],
                    headers=_h(plugin),
                    json={"batch_id": claim["id"],
                          "lease_token": claim["lease"]["lease_token"],
                          "fields": {"owner_user_id": "usr_evil"}})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# T16：lease 竞态被 fence（进程内 worker vs 插件）
# --------------------------------------------------------------------------- #
def test_t16_plugin_and_inprocess_cannot_both_hold(client, env, plugin,
                                                   batch):
    # 进程内 worker 先领
    inner = bstore.claim_batch(worker_id="worker-inproc")
    assert inner is not None and inner["batch"]["id"] == batch["id"]
    # 插件 claim 同批：SKIP LOCKED 跳过在租约内的行 → 无可领
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(plugin), json={})
    assert r.status_code == 200 and r.get_json()["claimed"] is False
    # 插件用自己的 worker_id 续租内进程的租约 → 拒绝（owner 不匹配）
    wid = "plugin:%s" % env.installation_id
    assert not bstore.plugin_heartbeat_batch(
        batch["id"], wid, inner["batch"]["lease_token"])
    # 内进程租约过期 → 插件可重领（单执行者接管，新 lease_token）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE baidu_import_batches SET lease_expires_at = "
                   "now() - interval '1 second' WHERE id=%s", (batch["id"],))
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(plugin), json={})
    assert r.status_code == 200 and r.get_json()["claimed"] is True
    new_token = r.get_json()["batch"]["lease"]["lease_token"]
    assert new_token != inner["batch"]["lease_token"]
    # 旧执行者（内进程）续租/写回被拒——绝不覆盖新 owner（fencing）
    assert not bstore.heartbeat_batch(
        batch["id"], "worker-inproc", inner["batch"]["lease_token"])
    item_id = inner["items"][0]["id"]
    r = client.post("/api/plugin/v1/baidu/items/%s/report" % item_id,
                    headers=_h(plugin),
                    json={"batch_id": batch["id"],
                          "lease_token": inner["batch"]["lease_token"],
                          "fields": {"stage": "downloading"}})
    assert r.status_code == 409 and _err_code(r) == "conflict"
    # 新持有者（插件）report 正常
    r = client.post("/api/plugin/v1/baidu/items/%s/report" % item_id,
                    headers=_h(plugin),
                    json={"batch_id": batch["id"], "lease_token": new_token,
                          "fields": {"stage": "downloading"}})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# begin 侧 item.slide_id 绑定（§6.3：_allocate_item_slide 的替位）
# --------------------------------------------------------------------------- #
def test_begin_binds_baidu_item_slide_id(plugin, env, batch, deliverable):
    claim = bstore.plugin_claim_batch("plugin:%s" % env.installation_id)
    assert claim is not None
    item = claim["items"][0]
    assert item["slide_id"] is None
    payload = plugin.begin_payload(
        "sample.svs", deliverable,
        extra={"baidu_item_id": item["id"]})
    r = plugin.begin(payload, "bridge-bind-1")
    assert r.status_code == 201, r.get_json()
    iid = r.get_json()["import_id"]
    imp = pim.get_import(iid)
    # 绑定落库：item.slide_id == 本次预分配
    row = sql_one("SELECT slide_id FROM baidu_import_items WHERE id=%s",
                  (item["id"],))
    assert row["slide_id"] == imp["slide_id"]
    # 重试复用同一 slide_id（幂等域重放不换资产）
    r2 = plugin.begin(payload, "bridge-bind-1")
    assert r2.status_code == 200
    assert pim.get_import(r2.get_json()["import_id"])["slide_id"] == \
        imp["slide_id"]
    # 交付收口：发布 + 条目 ready 回写（插件经 report 标 stage）。
    # （scratch_bytes=0：baidu 批次预算已占该用户在途名额，避免 inflight 上限）
    iid2, wt, rc = plugin.deliver_all(deliverable, idem="bridge-bind-2",
                                      scratch_bytes=0)
    assert rc.status_code == 200
    # baidu_item_id 只对白名单安装有意义：非百度插件携带 → 400
    other = ProducerEnv(Path(os.environ["SHARE_DATA_DIR"]).parent / "other")
    other_plugin = PluginClient(plugin.c, other)
    r = other_plugin.begin(
        other_plugin.begin_payload("x.tif", deliverable,
                                   extra={"baidu_item_id": item["id"]}),
        "bridge-bind-3")
    assert r.status_code == 400


def test_begin_baidu_item_of_other_owner_rejected(plugin, env, batch,
                                                  deliverable):
    victim = user_store.create_user("bridge-victim@x.co", "pass1234pass1234",
                                    role="user")
    claim = bstore.plugin_claim_batch("plugin:%s" % env.installation_id)
    item = claim["items"][0]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE baidu_import_batches SET owner_user_id=%s "
                   "WHERE id=%s", (victim["user_id"], item["batch_id"]))
    r = plugin.begin(plugin.begin_payload(
        "sample.svs", deliverable, extra={"baidu_item_id": item["id"]}),
        "bridge-bind-4")
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"


# --------------------------------------------------------------------------- #
# 验收复审补充：claim 视图 fs_id、心跳回带取消、失败尝试后重试复用绑定资产
# --------------------------------------------------------------------------- #
def test_claim_exposes_fs_id_and_heartbeat_reports_cancel(client, env, plugin,
                                                          batch):
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(plugin), json={})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    claim = body["batch"]
    assert body["items"][0]["fs_id"]
    hb = {"batch_id": claim["id"],
          "lease_token": claim["lease"]["lease_token"]}
    r = client.post("/api/plugin/v1/baidu/batches/heartbeat",
                    headers=_h(plugin), json=hb)
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "cancel_requested": False}
    bstore.request_cancel(claim["id"], env.uid)
    r = client.post("/api/plugin/v1/baidu/batches/heartbeat",
                    headers=_h(plugin), json=hb)
    assert r.status_code == 200
    assert r.get_json()["cancel_requested"] is True


def test_retry_after_cancelled_attempt_reuses_bound_staging_slide(
        plugin, env, batch, deliverable):
    """retry_items 重排的失败条目：新幂等键 begin 复用条目绑定的 slide_id。
    终态收口不得作废条目持久绑定的 staging 行（否则条目永远无法重试）。"""
    import slide_store
    claim = bstore.plugin_claim_batch("plugin:%s" % env.installation_id)
    item = claim["items"][0]
    payload = plugin.begin_payload(
        "sample.svs", deliverable, scratch_bytes=0,
        extra={"baidu_item_id": item["id"]})
    r = plugin.begin(payload, "bridge-retry-a")
    assert r.status_code == 201, r.get_json()
    first = r.get_json()
    r = plugin.cancel(first["import_id"], first["write_token"])
    assert r.status_code == 200, r.get_json()
    assert pim.get_import(first["import_id"])["state"] in ("cancelled",
                                                           "done")
    assert slide_store.resolve_slide_id(first["slide_id"]).asset_state == \
        slide_store.SlideState.STAGING

    r = plugin.begin(payload, "bridge-retry-b")
    assert r.status_code == 201, r.get_json()
    second = r.get_json()
    assert second["import_id"] != first["import_id"]
    assert second["slide_id"] == first["slide_id"]
    r = plugin.write(second["import_id"], second["write_token"], 0,
                     deliverable)
    assert r.status_code == 200, r.get_json()
    r = plugin.commit(second["import_id"], second["write_token"])
    assert r.status_code == 200, r.get_json()
    assert pim.get_import(second["import_id"])["state"] in (
        "published", "done")
    assert slide_store.resolve_slide_id(first["slide_id"]).asset_state == \
        slide_store.SlideState.READY


def test_batch_finalized_after_last_report_and_on_reclaim(client, env, plugin,
                                                          batch):
    """插件路径的批次收口：末条终态 report → 批次终态 + 预算收口；末条
    report 后、收口前崩溃的批次在下一次 claim 时补做收口（不再被当作
    可执行批次反复领取）。"""
    claim = bstore.plugin_claim_batch("plugin:%s" % env.installation_id)
    token = claim["batch"]["lease"]["lease_token"]
    item = claim["items"][0]
    # 模拟「条目已终态、收口未发生」：绕过 report 直接写条目
    bstore._update_item(item["id"], {"stage": "failed",
                                     "error_code": "download_failed"},
                        batch_id=batch["id"], lease_token=token)
    assert sql_one("SELECT state FROM baidu_import_batches WHERE id=%s",
                   (batch["id"],))["state"] == "running"
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE baidu_import_batches SET lease_expires_at = "
                   "now() - interval '1 second' WHERE id=%s", (batch["id"],))
    r = client.post("/api/plugin/v1/baidu/batches/claim",
                    headers=_h(plugin), json={})
    assert r.status_code == 200 and r.get_json()["claimed"] is False
    row = sql_one("SELECT state, lease_token, quota_reservation_id "
                  "FROM baidu_import_batches WHERE id=%s", (batch["id"],))
    assert row["state"] == "failed" and row["lease_token"] is None
    if row["quota_reservation_id"]:
        assert sql_one("SELECT state FROM upload_reservations "
                       "WHERE reservation_id=%s",
                       (row["quota_reservation_id"],))["state"] == "released"

    # 用户重试 → 再次 claim → 末条 report（ready 前先 failed 以免依赖发布）
    bstore.retry_items(batch["id"], env.uid, [item["id"]])
    claim = bstore.plugin_claim_batch("plugin:%s" % env.installation_id)
    token = claim["batch"]["lease"]["lease_token"]
    r = client.post("/api/plugin/v1/baidu/items/%s/report" % item["id"],
                    headers=_h(plugin),
                    json={"batch_id": batch["id"], "lease_token": token,
                          "fields": {"stage": "failed",
                                     "error_code": "download_failed"}})
    assert r.status_code == 200, r.get_json()
    row = sql_one("SELECT state, lease_token FROM baidu_import_batches "
                  "WHERE id=%s", (batch["id"],))
    assert row["state"] == "failed" and row["lease_token"] is None
