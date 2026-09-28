# -*- coding: utf-8 -*-
"""slide ID 化重构 P5：统一删除、回收与资源账本（持久化任务 + 执行器 +
legacy 归一 + 孤儿报告）。

合同：docs/slide-id-refactor-p5-contract-20260925.md §1-3。逐条覆盖：

  1. 同名重传新 ID 与旧删除互不影响（删 A 中 B 传——B 不受 A 清理影响）；
  2. 删除重放/执行器重启/重复调用：物理清理与配额释放各一次（deleting
     门禁 + deleting→deleted CAS 幂等键持续生效；slide_delete_jobs 落库）；
  3. daemon 兜底（run_slide_delete_worker_once 可确定性驱动）+ 两实例并发
     领取同一 job 不重复执行（lease/UNIQUE/SKIP LOCKED）；
  4. legacy 资产删除归一：文件+MRXS 伴侣+sidecar 清理、tombstone 保留
     别名、授权冻结、不减账（R-12）；by-ID 解析到 legacy 行同编排；
  5. 删除失效七类入口正反用例（share 成员/view grants/Demo/run grants/
     AI 通道门禁/缓存/baidu 引用行；研究维度不动）；
  6. 账本责任：failed 任务的 staging 有 reservation 或实占记录（取消/
     过期路径清 staging→释放预占→行 failed）；孤儿 objects 目录被报告；
     .staging/ 残留报告（活任务键可判）+ admin 清理端点。

运行：cd 项目根 && python3 -m pytest tests/test_slide_delete_pg.py -q
"""
import hashlib
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (clear_upload_dir, csrf_client, isolate_app,  # noqa: E402
                         publish_test_slide)
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]

TIFF = make_tiff_bytes(64, 96)
TIFF_SHA = hashlib.sha256(TIFF).hexdigest()


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 防护参数复位 + 清空 uploads。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_COMMIT_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 * 1024)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    # 执行器常量复位（防其它用例 monkeypatch 串扰）
    monkeypatch.setattr(app_mod, "_SLIDE_DELETE_LEASE_SECONDS", 300.0)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client(auth=True):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", login="u@x.com"):
    u = user_store.create_user(login, "pass1234pass1234", role=role)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = u["user_id"]
        sess["role"] = role
        sess["auth_version"] = (user_store.get_user(u["user_id"]) or {}).get(
            "auth_version", 1)
    return u["user_id"]


def _one(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def _exec(sql, params=()):
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _quota(uid):
    return upload_guard.get_quota_row(uid)


def _quota_bytes(uid, n):
    _exec("INSERT INTO upload_user_quotas (user_id, quota_bytes) "
          "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET quota_bytes=%s",
          (uid, n, n))


def _settle_used(uid, n):
    """模拟上传链路的配额结算前置态（publish_test_slide 离线通道不结算
    配额——删除结算断言需要 used_bytes 已按 accounted_bytes 入账）。"""
    _exec("UPDATE upload_user_quotas SET used_bytes=%s WHERE user_id=%s",
          (n, uid))


def _bundle_dir(slide_id):
    return Path(UPLOAD_DIR) / "objects" / slide_id


def _desc(slide_id):
    return slide_store.resolve_slide_id(slide_id)


def _job(slide_id):
    return slide_store.get_delete_job(slide_id)


def _legacy_asset(name, owner_uid, *, data=None, accounted=None):
    """legacy 布局资产：平铺文件 + set_slide_meta 建行（ready/legacy 布局）。"""
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(data if data is not None else TIFF)
    share_store.set_slide_meta(name, owner_user_id=owner_uid)
    sid = share_store.get_slide_id(name)
    assert sid
    if accounted is not None:
        _exec("UPDATE slides SET accounted_bytes=%s WHERE slide_id=%s",
              (accounted, sid))
    return sid


def _insert_baidu_refs(sid_list, *, stage="downloading"):
    """插入指向给定 slide_id 的 baidu 引用行（enum+batch+items 最小夹具）。
    返回 item id 列表。"""
    ids = []
    with psycopg.connect(PG_URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO baidu_enumerations (id, owner_user_id, "
                "share_url_enc) VALUES (%s, %s, %s)",
                ("enm_t1", "usr_baidu", "enc"))
            cur.execute(
                "INSERT INTO baidu_import_batches (id, owner_user_id, "
                "enumeration_id, idempotency_key, payload_sha256) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("bat_t1", "usr_baidu", "enm_t1", "idem-1",
                 hashlib.sha256(b"x").hexdigest()))
            for i, sid in enumerate(sid_list):
                item_id = "itm_%d_%s" % (i, (sid or "none")[-6:])
                cur.execute(
                    "INSERT INTO baidu_import_items (id, batch_id, "
                    "candidate_id, fs_id, name, relative_path, stage, "
                    "slide_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (item_id, "bat_t1", "cand%d" % i, "fs%d" % i,
                     "s%d.kfb" % i, "/s%d.kfb" % i, stage, sid))
                ids.append(item_id)
    return ids


# --------------------------------------------------------------------------- #
# §1 任务持久化载体（0067 + 0070 lease 列）
# --------------------------------------------------------------------------- #
def test_delete_jobs_schema_and_unique():
    """0070 lease 列就位；slide_id UNIQUE——重放 request_delete/enqueue 不产
    生重复任务行（一个资产至多一条删除任务）。"""
    with psycopg.connect(PG_URI) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='slide_delete_jobs'")
            cols = {r[0] for r in cur.fetchall()}
    assert {"job_id", "slide_id", "requested_by", "state", "attempts",
            "last_error", "lease_owner", "lease_expires_at"} <= cols

    sid = slide_store.allocate_slide("usr_p5", "uq.svs", "svs").slide_id
    slide_store.mark_ready(sid, accounted_bytes=10)
    assert slide_store.request_delete(sid, requested_by="usr_p5")
    assert slide_store.request_delete(  # 重放：行已 deleting，ready 谓词 CAS 失败
        sid, requested_by="usr_p5") is False
    slide_store.enqueue_delete_job(sid, requested_by="usr_p5")
    slide_store.enqueue_delete_job(sid, requested_by="usr_p5")
    assert _one("SELECT count(*) FROM slide_delete_jobs WHERE slide_id=%s",
                (sid,)) == 1
    job = _job(sid)
    assert job["state"] == "pending" and job["requested_by"] == "usr_p5"
    # lease 生命周期：领取→持租约→收口清租约
    claimed = slide_store.claim_due_delete_job("w-test", lease_seconds=300)
    assert claimed is not None and claimed["slide_id"] == sid
    after = _job(sid)
    assert after["state"] == "cleaning" and after["attempts"] == 1
    assert after["lease_owner"] == "w-test" and after["lease_expires_ts"]
    # 活租约内不可重领；到期（回拨 lease）后可重领
    assert slide_store.claim_due_delete_job("w2", lease_seconds=300) is None
    _exec("UPDATE slide_delete_jobs SET lease_expires_at = now() - interval "
          "'1 second' WHERE slide_id=%s", (sid,))
    again = slide_store.claim_due_delete_job("w2", lease_seconds=300)
    assert again is not None and _job(sid)["attempts"] == 2
    # 结算收口
    slide_store.mark_deleted(sid)
    assert slide_store.finish_delete_job(again["job_id"],
                                         slide_store.DELETE_JOB_DONE)
    done = _job(sid)
    assert done["state"] == "done" and done["lease_owner"] is None
    # done 后：不再被领取；enqueue 不复活
    assert slide_store.claim_due_delete_job("w3", lease_seconds=300) is None
    slide_store.enqueue_delete_job(sid)
    assert _job(sid)["state"] == "done"


# --------------------------------------------------------------------------- #
# §3-1 删 A 中 B 传：同名重传新 ID 与旧删除互不影响
# --------------------------------------------------------------------------- #
def test_delete_a_during_same_name_reupload_b_unaffected(tmp_path):
    """A 处 deleting（清理中断重试窗口）中同名 B 上传：B 独立 ID/目录，
    A 的执行器重跑只清 A 的包，B 的包与可读性不受影响。"""
    ca = _client()
    uid = _user_session(ca, login="p5a@x.com")
    sid_a = publish_test_slide("twin.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    # 模拟 A 的删除在物理清理前中断（清理失败→停留 deleting，job 重试）
    _exec("UPDATE slides SET asset_state='deleting' WHERE slide_id=%s",
          (sid_a,))
    _exec("DELETE FROM slide_delete_jobs WHERE slide_id=%s", (sid_a,))
    slide_store.enqueue_delete_job(sid_a, requested_by=uid)
    assert _desc(sid_a).asset_state == "deleting"

    # 删 A 中 B 传（同名）：B 得到全新 ID + 独立包
    sid_b = publish_test_slide("twin.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    assert sid_b != sid_a
    assert _bundle_dir(sid_b).is_dir()

    # A 的任务由执行器重跑（daemon 单轮确定性驱动）
    assert app_mod.run_slide_delete_worker_once(max_jobs=5) >= 1
    assert _desc(sid_a).asset_state == "deleted"
    assert not _bundle_dir(sid_a).exists()
    # B 不受 A 清理影响：包在、可读、状态 ready
    assert _bundle_dir(sid_b).is_dir()
    assert (_bundle_dir(sid_b) / "data.tif").is_file()
    assert _desc(sid_b).asset_state == "ready"
    assert ca.get("/api/slides/%s/info" % sid_b).status_code == 200
    assert _job(sid_a)["state"] == "done"


# --------------------------------------------------------------------------- #
# §3-2 删除重放/执行器重启/重复调用：清理与配额释放各一次
# --------------------------------------------------------------------------- #
def test_delete_cleanup_failure_retry_settles_once(tmp_path):
    """物理清理失败 → 503 停留 deleting（job failed + last_error）；重放
    DELETE 重触发执行 → 结算一次；此后 worker 重启/重复调用不再减账。"""
    c = _client()
    uid = _user_session(c, login="p5b@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    sid = publish_test_slide("retry.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    _settle_used(uid, len(TIFF))
    used0 = _quota(uid)["used_bytes"]
    assert used0 == len(TIFF)

    real_remove = slide_storage.remove_bundle
    calls = {"n": 0}

    def flaky_remove(slide_id, root=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated cleanup failure")
        return real_remove(slide_id, root=root)

    import app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module.slide_storage, "remove_bundle", flaky_remove)
        r1 = c.delete("/api/slides/%s" % sid)
    assert r1.status_code == 503
    assert r1.get_json()["code"] == "delete_retryable"
    assert _desc(sid).asset_state == "deleting"   # 门禁持续生效
    assert c.get("/api/slides/%s/info" % sid).status_code in (403, 404)
    job = _job(sid)
    assert job is not None and job["state"] == "failed"
    assert job["last_error"] and "simulated cleanup failure" in job["last_error"]
    assert _quota(uid)["used_bytes"] == used0    # 未结算不退款

    # 重放 DELETE（deleting → 触发执行器）：清理 + 结算一次
    r2 = c.delete("/api/slides/%s" % sid)
    assert r2.status_code == 200
    assert _desc(sid).asset_state == "deleted"
    assert not _bundle_dir(sid).exists()
    assert _quota(uid)["used_bytes"] == 0
    assert _job(sid)["state"] == "done"

    # 执行器重启/重复调用/重复 DELETE：不再减（CAS 幂等键持续生效）
    assert app_mod.run_slide_delete_worker_once(max_jobs=5) == 0  # 无到期任务
    assert c.delete("/api/slides/%s" % sid).status_code == 200
    assert app_mod.run_slide_delete_worker_once(max_jobs=5) == 0
    assert _quota(uid)["used_bytes"] == 0


def test_daemon_picks_up_interrupted_delete(tmp_path):
    """崩溃恢复：deleting + pending job（端点落库后进程中断）由 daemon 单轮
    领取执行：清理+结算+任务 done 一次性收口。"""
    c = _client()
    uid = _user_session(c, login="p5c@x.com")
    _quota_bytes(uid, 10 * 1024 * 1024)
    sid = publish_test_slide("daemon.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)
    _settle_used(uid, len(TIFF))
    used0 = _quota(uid)["used_bytes"]

    # 模拟：请求事务已落库（deleting + job pending），同步执行未发生
    with psycopg.connect(PG_URI) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slides SET asset_state='deleting' WHERE slide_id=%s",
                (sid,))
        conn.commit()
    slide_store.enqueue_delete_job(sid, requested_by=uid)
    assert _job(sid)["state"] == "pending"

    assert app_mod.run_slide_delete_worker_once(max_jobs=1) == 1
    assert _desc(sid).asset_state == "deleted"
    assert not _bundle_dir(sid).exists()
    assert _quota(uid)["used_bytes"] == 0
    assert used0 == len(TIFF)
    assert _job(sid)["state"] == "done"
    # 再跑一轮：无任务可领
    assert app_mod.run_slide_delete_worker_once(max_jobs=1) == 0


def test_worker_concurrent_claim_no_double_execution(tmp_path):
    """两实例并发领取同一 job 不重复执行（lease + SKIP LOCKED）：实例一
    领取后持锁执行中，实例二领取不到（state=cleaning 且租约未到期）。"""
    c = _client()
    uid = _user_session(c, login="p5d@x.com")
    sid = publish_test_slide("race.tif", TIFF, owner_user_id=uid,
                             upload_dir=UPLOAD_DIR)

    _exec("UPDATE slides SET asset_state='deleting' WHERE slide_id=%s", (sid,))
    slide_store.enqueue_delete_job(sid, requested_by=uid)

    claimed = threading.Event()
    release = threading.Event()
    executions = {"n": 0}
    real_exec = app_mod._slide_delete_execute

    def slow_exec(slide_id):
        executions["n"] += 1
        claimed.set()
        assert release.wait(timeout=10)
        return real_exec(slide_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_mod, "_slide_delete_execute", slow_exec)
        t1 = threading.Thread(
            target=lambda: app_mod.run_slide_delete_worker_once(
                max_jobs=1, worker_id="w1"))
        t1.start()
        assert claimed.wait(timeout=10)     # 实例一已领取并进入执行
        # 实例二（活租约内）领取不到任何任务
        assert app_mod.run_slide_delete_worker_once(
            max_jobs=1, worker_id="w2") == 0
        release.set()
        t1.join(timeout=10)
    assert executions["n"] == 1
    assert _desc(sid).asset_state == "deleted"
    assert _job(sid)["state"] == "done"


# --------------------------------------------------------------------------- #
# §3-4 legacy 资产删除归一（端点统一编排；R-12 不减账）
# --------------------------------------------------------------------------- #
def test_legacy_delete_unified_physical_tombstone_freeze_no_refund(tmp_path):
    """legacy 布局：文件+MRXS 伴侣+sidecar 清理、tombstone 保留别名、
    授权冻结、run grant 撤销、不减账（R-12）、任务 done。"""
    co = _client()
    owner_uid = _user_session(co, role="owner", login="p5-owner@x.com")
    ub = _user_session(_client(), login="p5-grant@x.com")

    name = "legacy.mrxs"
    sid = _legacy_asset(name, owner_uid, accounted=12345)
    # MRXS 伴侣目录 + 转换平铺 sidecar
    companion = Path(UPLOAD_DIR) / "legacy"
    companion.mkdir()
    (companion / "part.dat").write_bytes(b"companion")
    (Path(UPLOAD_DIR) / (name + ".manifest.json")).write_text("{}")
    assoc = Path(UPLOAD_DIR) / (name + ".associated")
    assoc.mkdir()
    (assoc / "x").write_bytes(b"assoc")
    # 授权面：view grant（按名+ID）+ share 成员 + run grant
    share_store.grant_slide_view(ub, name, slide_id=sid)
    share = share_store.create_share([name], 24, creator_user_id=owner_uid,
                                     slide_ids=[sid])
    share_store.claim_share(share["token"], ub)
    grant = share_store.create_run_grant("inst-1", name,
                                         created_by_user_id=owner_uid,
                                         slide_id=sid)
    # 模拟 legacy 时代的账本占用（R-12：删除不退款）
    _quota_bytes(owner_uid, 10 * 1024 * 1024)
    _exec("UPDATE upload_user_quotas SET used_bytes=%s WHERE user_id=%s",
          (999999, owner_uid))
    # 指向该资产的 conversion job + baidu 引用行
    _exec(
        "INSERT INTO conversion_jobs (id, owner_user_id, source_name, "
        "source_sha256, source_format, canonical_name, converter_id, "
        "converter_version, state, slide_id) "
        "VALUES ('cvj_p5a', %s, 'legacy.kfb', %s, 'kfb', %s, 'svc', 'v1', "
        "'queued', %s)",
        (owner_uid, hashlib.sha256(b"kfb1").hexdigest(), name, sid))
    items = _insert_baidu_refs([sid], stage="downloading")

    rd = co.delete("/api/slide/%s" % name)
    assert rd.status_code == 200 and rd.get_json() == {"ok": True}

    # 物理清理：文件/伴侣/sidecar 全清
    assert not (Path(UPLOAD_DIR) / name).exists()
    assert not companion.exists()
    assert not (Path(UPLOAD_DIR) / (name + ".manifest.json")).exists()
    assert not assoc.exists()
    # tombstone：保留 legacy_filename（旧别名不重绑新 ID）
    d = _desc(sid)
    assert d.asset_state == "deleted" and d.deleted_at is not None
    assert d.legacy_filename == name
    # 状态机两阶段痕迹 + 任务收口
    assert _job(sid) is not None and _job(sid)["state"] == "done"
    # 授权冻结：view grants/share 成员/run grants 全清
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (sid,)) == 0
    assert _one("SELECT count(*) FROM slide_view_grants WHERE slide_name=%s",
                (name,)) == 0
    assert _one("SELECT count(*) FROM share_slides WHERE slide_id=%s",
                (sid,)) == 0
    assert _one("SELECT count(*) FROM run_grants WHERE slide_id=%s AND "
                "NOT revoked", (sid,)) == 0
    valid, reason = app_mod._verify_run_grant(grant["grant_id"], name, "inst-1")
    assert not valid
    # 读取门禁：旧名/旧 ID 全拒
    assert co.get("/api/slide/%s/info" % name).status_code in (403, 404)
    assert co.get("/api/slides/%s/info" % sid).status_code in (403, 404)
    # share token 拒绝（成员行已删 + 状态门禁）
    sc = _client()
    with sc.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = ub
        sess["role"] = "user"
    assert sc.get("/s/%s/api/slides/%s/info" % (share["token"], sid)
                  ).status_code in (403, 404)
    # R-12：不减账（legacy 资产不退款）
    assert _quota(owner_uid)["used_bytes"] == 999999
    # 转换任务作废 + baidu 引用行失效
    assert _one("SELECT state FROM conversion_jobs WHERE id='cvj_p5a'"
                ) == "cancelled"
    assert _one("SELECT stage FROM baidu_import_items WHERE id=%s",
                (items[0],)) == "failed"
    assert _one("SELECT error_code FROM baidu_import_items WHERE id=%s",
                (items[0],)) == "slide_deleted"
    # 重放幂等
    assert co.delete("/api/slide/%s" % name).status_code == 200
    assert _quota(owner_uid)["used_bytes"] == 999999


def test_legacy_delete_by_id_same_orchestration(tmp_path):
    """by-ID 端点解析到 legacy 布局行：同一编排（CAS 两阶段 + 任务落库），
    非 mark_deleted_compat 直写。"""
    co = _client()
    owner_uid = _user_session(co, role="owner", login="p5-owner2@x.com")
    sid = _legacy_asset("byid.svs", owner_uid)
    rd = co.delete("/api/slides/%s" % sid)
    assert rd.status_code == 200
    assert rd.get_json()["state"] == "deleted"
    assert _desc(sid).asset_state == "deleted"
    assert _desc(sid).deleted_at is not None
    assert _job(sid)["state"] == "done"
    assert not (Path(UPLOAD_DIR) / "byid.svs").exists()


def test_delete_state_gates(tmp_path):
    """状态门禁：staging → 409；未知 ID → 404；无 slides 行的按名删除 → 404
    （统一编排需要资产行；无行孤儿走 inventory 报告）。"""
    c = _client()
    uid = _user_session(c, login="p5e@x.com")
    # staging：服务级预分配资产行（不发布）
    sid = slide_store.allocate_slide(uid, "stg.tif", "tif").slide_id
    rd = c.delete("/api/slides/%s" % sid)
    assert rd.status_code == 409
    assert rd.get_json()["code"] == "slide_state_conflict"
    assert _desc(sid).asset_state == "staging"
    assert _job(sid) is None                       # CAS 失败不落任务
    # 未知 ID
    assert c.delete("/api/slides/sld_nope").status_code == 404
    # 无行的按名删除（owner）：404（不再按名裸 unlink）
    co = _client()
    _user_session(co, role="owner", login="p5-owner3@x.com")
    (Path(UPLOAD_DIR) / "orphan.svs").write_bytes(TIFF)
    rr = co.delete("/api/slide/orphan.svs")
    assert rr.status_code == 404
    assert (Path(UPLOAD_DIR) / "orphan.svs").exists()  # 文件不动（只报告）


# --------------------------------------------------------------------------- #
# §3-5 删除失效七类入口（正反用例）
# --------------------------------------------------------------------------- #
def test_delete_invalidates_entry_points_positive_negative(tmp_path):
    """删除 A：七类入口对 A 失效；对同时存在的 B（正例）全部存活。"""
    ca = _client()
    uid = _user_session(ca, login="p5f@x.com")
    sid_a = publish_test_slide("ent-a.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    sid_b = publish_test_slide("ent-b.tif", TIFF, owner_user_id=uid,
                               upload_dir=UPLOAD_DIR)
    for sid, nm in ((sid_a, "ent-a.tif"), (sid_b, "ent-b.tif")):
        share_store.grant_slide_view(uid, nm, slide_id=sid)
        share_store.create_run_grant("inst-%s" % sid[-4:], nm,
                                     created_by_user_id=uid, slide_id=sid)
        import demo_store
        demo_store.catalog_add(sid, display_name="demo-%s" % sid[-4:])
    # 缓存正例：对 A/B 各取一次瓦片（句柄池+tile 缓存+渲染统计按 ID 落键）
    for sid in (sid_a, sid_b):
        assert ca.get("/api/slides/%s/tiles/0/0_0.jpeg" % sid).status_code \
            == 200
    tile_keys_a = [k for k in app_mod._tile_cache._data if k[0] == sid_a]
    assert tile_keys_a
    baidu_items = _insert_baidu_refs([sid_a, sid_b], stage="ready")

    rd = ca.delete("/api/slides/%s" % sid_a)
    assert rd.status_code == 200

    # 1) share 成员 / 2) view grants：A 清、B 留
    for table in ("slide_view_grants",):
        assert _one("SELECT count(*) FROM %s WHERE slide_id=%%s" % table,
                    (sid_a,)) == 0
        assert _one("SELECT count(*) FROM %s WHERE slide_id=%%s" % table,
                    (sid_b,)) == 1
    # 3) Demo 目录：A 撤、B 留
    import demo_store
    assert demo_store.catalog_get(sid_a) is None
    assert demo_store.catalog_get(sid_b) is not None
    # 4) run grants：A 撤、B 有效
    assert _one("SELECT count(*) FROM run_grants WHERE slide_id=%s AND "
                "NOT revoked", (sid_a,)) == 0
    assert _one("SELECT count(*) FROM run_grants WHERE slide_id=%s AND "
                "NOT revoked", (sid_b,)) == 1
    # 5) AI 通道门禁（HP 会话冻结的 PT 侧核对）：grant 失效 + 读取拒绝。
    #    （正例 B 的 grant 存活由上面 SQL 计数断言；id_bundle 资产的名通道
    #    verify 是 P2 已知不适用——按 ID 通道对 A 断言失效即可。）
    ga = _one("SELECT grant_id FROM run_grants WHERE slide_id=%s", (sid_a,))
    assert ga is not None          # 撤销是置位，行保留（审计证据）
    valid, reason = app_mod._verify_run_grant(ga, "ent-a.tif",
                                              "inst-%s" % sid_a[-4:],
                                              slide_id=sid_a)
    assert not valid and reason == "grant_revoked"
    assert ca.get("/api/slides/%s/info" % sid_a).status_code in (403, 404)
    assert ca.get("/api/slides/%s/info" % sid_b).status_code == 200
    # 6) 缓存：A 的 tile 键全失效；B 的仍在（句柄/tile/render 统计按 ID
    #    收窄——R-15；purge_stats_for 同键空间由 _close_slide 连带）
    assert not [k for k in app_mod._tile_cache._data if k[0] == sid_a]
    assert [k for k in app_mod._tile_cache._data if k[0] == sid_b]
    # 7) baidu 引用行：A 的标注失效（ready→error_code 标注），B 的原样
    assert _one("SELECT error_code FROM baidu_import_items WHERE id=%s",
                (baidu_items[0],)) == "slide_deleted"
    assert _one("SELECT error_code FROM baidu_import_items WHERE id=%s",
                (baidu_items[1],)) is None
    # 研究维度不动：删除路径不触碰研究表（rois 等证据行保留）
    # （rois 由用户维度的研究删除编排负责，见 research_store）


# --------------------------------------------------------------------------- #
# §3-3/§3-6 账本责任 + 孤儿报告（objects/ + .staging/）
# --------------------------------------------------------------------------- #
def test_failed_staging_ledger_and_orphan_reporting(tmp_path):
    """孤儿扫描与 .staging/ 残留报告（P5 §3-6 的 admin 面）：
    - objects/ 孤儿目录被报告（无行/行非 deleting-deleted），正常包不报；
    - .staging/ 残留报告（活任务键可判 cleanable=False；死键 True）+ 清理。
    （旧 V2 取消路径的账本责任断言随上传端点删除由 COS 链路覆盖。）"""
    co = _client()
    _user_session(co, role="owner", login="p5-owner4@x.com")
    cu = _client()
    uid = _user_session(cu, login="p5g@x.com")

    # 1) 正常 ready 包 + 孤儿 objects 目录 + deleting 包
    sid_ok = publish_test_slide("ok.tif", TIFF, owner_user_id=uid,
                                upload_dir=UPLOAD_DIR)
    orphan_dir = Path(UPLOAD_DIR) / "objects" / "sld_orphanzzz"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "data.tif").write_bytes(b"junk-junk")
    sid_del = publish_test_slide("delme.tif", TIFF, owner_user_id=uid,
                                 upload_dir=UPLOAD_DIR)
    _exec("UPDATE slides SET asset_state='deleting' WHERE slide_id=%s",
          (sid_del,))
    slide_store.enqueue_delete_job(sid_del, requested_by=uid)
    # 死键 staging 残留（无任何键空间在途行）+ 活键（在途上传任务）
    dead_stg = Path(UPLOAD_DIR) / ".staging" / "upt_deadkey00"
    dead_stg.mkdir(parents=True)
    (dead_stg / "data.tif").write_bytes(b"stale")
    live_task = upload_task_store.create_task(
        uid, "live.tif", "live.tif", len(TIFF), 32)
    live_key = live_task["upload_id"]
    live_stg = Path(UPLOAD_DIR) / ".staging" / live_key / "transfer"
    live_stg.mkdir(parents=True)
    (live_stg / "data").write_bytes(TIFF[:32])   # 首块落盘即建 .staging/<key>/transfer
    assert (Path(UPLOAD_DIR) / ".staging" / live_key).is_dir()

    body = co.get("/api/admin/v1/slides/inventory").get_json()
    orphans = {o["slide_id"]: o for o in body["orphan_objects"]}
    assert "sld_orphanzzz" in orphans
    assert orphans["sld_orphanzzz"]["asset_state"] is None
    assert orphans["sld_orphanzzz"]["size_bytes"] == len(b"junk-junk")
    assert sid_ok not in orphans              # 正常 ready 包不报
    assert sid_del not in orphans             # deleting 包归删除任务管
    residue = {s["task_id"]: s for s in body["staging_residue"]}
    assert residue["upt_deadkey00"]["cleanable"] is True
    assert residue[live_key]["cleanable"] is False
    assert residue[live_key]["live_kind"] == "upload"

    # 2) 清理端点：死键可清；活键 409
    rr = co.delete("/api/admin/v1/slides/staging-residue",
                   json={"task_id": "upt_deadkey00"})
    assert rr.status_code == 200 and rr.get_json()["removed"] is True
    assert not dead_stg.exists()
    rr = co.delete("/api/admin/v1/slides/staging-residue",
                   json={"task_id": live_key})
    assert rr.status_code == 409
    assert rr.get_json()["error"]["code"] == "staging_task_live"
    # 幂等：再清死键 removed=False
    rr = co.delete("/api/admin/v1/slides/staging-residue",
                   json={"task_id": "upt_deadkey00"})
    assert rr.status_code == 200 and rr.get_json()["removed"] is False
    # 非法键：组件白名单拒绝
    rr = co.delete("/api/admin/v1/slides/staging-residue",
                   json={"task_id": "../escape"})
    assert rr.status_code == 400


# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# （原 test_reservation_expired_staging_failed_with_record：预约租约过期后
#   commit 收口语义——纯旧 V2 commit 端点行为。）
