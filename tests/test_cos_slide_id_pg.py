# -*- coding: utf-8 -*-
"""slide ID 化重构 P4-b：COS 直传统一发布门禁测试。

合同：docs/slide-id-refactor-p4-contract-20260925.md §5（任务书 §8 矩阵的
COS 部分）。逐条覆盖：

  1. create 即绑 slide_id（staging/id_bundle 资产行同事务）；幂等键复用
     既有行时复用其 slide_id（不重新分配）；
  2. 下载 → validating → ready 全链经统一发布（objects/<slide_id>/ 落地、
     manifest/sha 逐文件核对、accounted_bytes/配额一次结算）；
  3. ready 前不可读（状态门禁：FS 已发布 ≠ 可见；staging 同样不可读）；
  4. 取消 → 任务暂存树清理 + 本地预约释放（资产行 failed）；
  5. 崩溃恢复幂等：intent 后 / FS 发布后 DB 前 —— 恢复收口一次、不重搬
     不重扣；
  6. 池预约仍在 finalize_cleanup 才释放（下载完成 ≠ 释放，§6.1）；
  7. 跨 owner 同名：各发各的 slide_id、隔离不猜归属、无 owner 修正；
  8. 旧 generation 不发布不结算（worker 复活/双 worker 场景——fencing）。

复用 worker 套件基建（FakeCos/链路推进助手），不跨套件复制实现。
"""
import hashlib
import json

import pytest

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: F401  # fixture 注册
import cos_ingest_worker as ciw
import cos_pool_store
import ingestion_store as ist
import slide_io
import slide_publish
import slide_storage
import slide_store


@pytest.fixture(autouse=True)
def _worker_env(_env):
    """复用 worker 套件的池/分块/水位/UPLOAD_DIR 环境。"""
    yield _env


def _quota_row(user_id):
    def op(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (user_id,))
        return cur.fetchone()
    return h._sql(op)


def _drive_full(monkeypatch, fake, st, owner="u1", role="user", size=150,
                name="a.svs"):
    """全链推进到 READY（统一发布完成），返回 (job, payload)。"""
    job, payload = h._drive_to_validating(fake, st, size=size, owner=owner,
                                          role=role, name=name)
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    return out, payload


# --------------------------------------------------------------------------- #
# 1) 创建即绑定 + 幂等复用
# --------------------------------------------------------------------------- #
def test_create_binds_slide_id_same_tx():
    job = h._mkjob(owner="u1", role="user", size=150)
    assert job["slide_id"] and job["slide_id"].startswith("sld_")
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert desc is not None
    assert desc.asset_state == slide_store.SlideState.STAGING  # 未发布不可读
    assert desc.storage_layout == "id_bundle"
    assert desc.owner_user_id == "u1"
    assert desc.original_filename == "a.svs"
    assert desc.storage_relpath == "objects/%s/data.svs" % job["slide_id"]
    assert desc.legacy_filename is None  # 新资产无冻结别名
    # 幂等重试：复用既有行 + 复用其 slide_id（不重新分配）
    again, created = ist.create_waiting_job(
        "u1", "user", "a.svs", "a.svs", "svs", 150,
        idempotency_key=job["idempotency_key"])
    assert not created
    assert again["job_id"] == job["job_id"]
    assert again["slide_id"] == job["slide_id"]

    def slide_rows(cur):
        cur.execute("SELECT COUNT(*)::int AS n FROM slides WHERE "
                    "original_filename='a.svs'")
        return cur.fetchone()["n"]

    assert h._sql(slide_rows) == 1  # 重试不产生第二个资产行


def test_create_same_name_concurrent_distinct_ids():
    """同名并发（不同 owner/同 owner）：各得各 ID——不查原名冲突。"""
    a = h._mkjob(owner="u1", role="user", size=100, name="same.svs")
    b = h._mkjob(owner="u2", role="user", size=100, name="same.svs")
    assert a["slide_id"] != b["slide_id"]
    da, db = (slide_store.resolve_slide_id(x["slide_id"]) for x in (a, b))
    assert da.storage_relpath != db.storage_relpath
    assert da.owner_user_id == "u1" and db.owner_user_id == "u2"


# --------------------------------------------------------------------------- #
# 2) 全链统一发布 + 3) ready 门禁
# --------------------------------------------------------------------------- #
def test_full_chain_unified_publish_objects_bundle(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    sid = job["slide_id"]
    # 发布前（staging）：owner 也不可读（状态门禁）
    assert not slide_store.authorize_read(sid, actor_user_id="u1")
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    assert out["slide_id"] == sid  # 结算前后绑定不变

    # objects/<slide_id>/ 落地：data.svs + manifest.json 逐文件核对
    entry = slide_storage.bundle_dir(sid, root=h._env_dir()) / "data.svs"
    with open(entry, "rb") as fh:
        assert fh.read() == payload
    manifest = json.loads((entry.parent / "manifest.json").read_text())
    sha = hashlib.sha256(payload).hexdigest()
    assert manifest == {"entry": "data.svs",
                        "files": [{"path": "data.svs", "size": 150,
                                   "sha256": sha}]}
    assert slide_storage.verify_bundle(sid, manifest, root=h._env_dir())
    # 资产行：ready + accounted + revision（slide_assets sha 前缀）
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    assert desc.accounted_bytes == 150
    assert desc.revision == "sha256:%s" % sha[:16]
    assert out["sha256_actual"] == sha
    # 配额一次结算；暂存树收口清空
    q = _quota_row("u1")
    assert (int(q["used_bytes"]), int(q["reserved_bytes"])) == (150, 0)
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=h._env_dir()).exists()
    # ready 后 owner 可读、他人不可读
    assert slide_store.authorize_read(sid, actor_user_id="u1")
    assert not slide_store.authorize_read(sid, actor_user_id="u2")


def test_not_readable_between_fs_publish_and_settle(monkeypatch):
    """FS 发布后、DB 结算前（崩溃窗口）：包已在 objects 但对所有人不可读。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    sid = job["slide_id"]
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)

    def crash(*args, **kwargs):
        raise RuntimeError("crash-after-fs-before-db")

    monkeypatch.setattr(ist, "worker_settle_ready", crash)
    with pytest.raises(RuntimeError):
        ciw.process_validating(cos=fake, state=st)
    assert (slide_storage.bundle_dir(sid, root=h._env_dir()) /
            "data.svs").is_file()  # FS 已发布
    assert ist.get_job(job["job_id"])["state"] == ist.VALIDATING
    for actor, role in (("u1", None), ("u2", None),
                        (None, slide_store.ROLE_ADMIN)):
        assert not slide_store.authorize_read(sid, actor_user_id=actor,
                                              actor_role=role)  # 门禁兜住


# --------------------------------------------------------------------------- #
# 4) 取消收口
# --------------------------------------------------------------------------- #
def test_cancel_cleans_staging_and_releases_reservation(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    # 下载代在暂存树留有断点件（VALIDATING 态）
    staged = h._staged(job["job_id"])
    assert staged is not None and staged.is_file()
    out = ist.cancel_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=h._env_dir()).exists()  # 暂存树清理
    q = _quota_row("u1")
    assert (int(q["used_bytes"]), int(q["reserved_bytes"])) == (0, 0)  # 预约释放
    assert not slide_storage.bundle_dir(
        job["slide_id"], root=h._env_dir()).exists()  # 从未发布
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert desc.asset_state == "failed"  # 证据行保留且不可读
    assert not slide_store.authorize_read(job["slide_id"],
                                          actor_user_id="u1")


# --------------------------------------------------------------------------- #
# 5) 崩溃恢复幂等（intent 后 / FS 发布后 DB 前）
# --------------------------------------------------------------------------- #
def test_recovery_after_intent_republishes_exactly_once(monkeypatch):
    """崩溃在 intent 持久化后、FS 发布前：恢复换代收养断点件，重新以当代
    持久化 intent 后发布——收口一次、一个包、一次结算。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    real_persist = ist.worker_persist_commit_intent

    def persist_then_crash(job_id, generation, intent):
        real_persist(job_id, generation, intent)
        raise RuntimeError("crash-after-intent")

    monkeypatch.setattr(ist, "worker_persist_commit_intent",
                        persist_then_crash)
    with pytest.raises(RuntimeError):
        ciw.process_validating(cos=fake, state=st)
    mid = ist.get_job(job["job_id"])
    assert mid["state"] == ist.VALIDATING
    assert mid["commit_intent_json"]  # 栅栏已立
    assert not slide_storage.bundle_dir(
        mid["slide_id"], root=h._env_dir()).exists()  # FS 未发布

    monkeypatch.setattr(ist, "worker_persist_commit_intent", real_persist)
    h._sql(lambda cur: cur.execute(  # 租约 TTL 到期 → 新纪元接管
        "UPDATE ingestion_jobs SET worker_lease_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    sha = hashlib.sha256(payload).hexdigest()
    assert out["sha256_actual"] == sha
    with open(h._bundle_data(out["slide_id"]), "rb") as fh:
        assert fh.read() == payload
    # 恢复后收口恰一次：配额只扣一次
    assert int(_quota_row("u1")["used_bytes"]) == 150
    # 结算入口幂等重入：返回现状，不重复扣
    again = ist.worker_settle_ready(
        job["job_id"], out["worker_generation"], sha256_actual=sha,
        settle_bytes=150, slide_id=out["slide_id"])
    assert again["state"] == ist.READY
    assert int(_quota_row("u1")["used_bytes"]) == 150


def test_recovery_after_publish_settles_db_only(monkeypatch):
    """崩溃在 FS 发布后、DB 前：恢复走 verify 分支只做 DB 收口（见
    test_cos_ingest_worker 同型用例；此处补配额口径断言）。"""
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    real_settle = ist.worker_settle_ready

    def crash(*args, **kwargs):
        raise RuntimeError("crash-after-fs")

    monkeypatch.setattr(ist, "worker_settle_ready", crash)
    with pytest.raises(RuntimeError):
        ciw.process_validating(cos=fake, state=st)
    monkeypatch.setattr(ist, "worker_settle_ready", real_settle)
    h._sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET worker_lease_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    assert int(_quota_row("u1")["used_bytes"]) == 150  # 一次结算


# --------------------------------------------------------------------------- #
# 6) 池预约到 finalize_cleanup 才释放
# --------------------------------------------------------------------------- #
def test_pool_reservation_released_only_at_finalize_cleanup(monkeypatch):
    fake, st = h.FakeCos(), {}
    out, payload = _drive_full(monkeypatch, fake, st)
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 150  # ready 未释放
    assert ciw.process_ready(cos=fake, state=st) == out["job_id"]
    assert ist.get_job(out["job_id"])["state"] == ist.COMPLETED
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 150  # completed 仍未
    assert ciw.process_cleanup(cos=fake, state=st) == out["job_id"]
    assert ist.get_job(out["job_id"])["cleanup_status"] == ist.CLEANUP_CLEANED
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0  # finalize 才释放


# --------------------------------------------------------------------------- #
# 7) 跨 owner 同名
# --------------------------------------------------------------------------- #
def test_same_name_two_owners_publish_isolated_ids(monkeypatch):
    fake, st = h.FakeCos(), {}
    out1, payload = _drive_full(monkeypatch, fake, st, owner="u1",
                                role="user", name="same.svs")
    # 同 FakeCos 桶内第二个任务（同名不同 owner）全链
    out2, _ = _drive_full(monkeypatch, fake, st, owner="u2", role="user",
                          name="same.svs")
    assert out1["slide_id"] != out2["slide_id"]  # 各发各的 ID
    d1 = slide_store.resolve_slide_id(out1["slide_id"])
    d2 = slide_store.resolve_slide_id(out2["slide_id"])
    assert d1.owner_user_id == "u1" and d2.owner_user_id == "u2"  # 隔离
    assert d1.storage_relpath != d2.storage_relpath
    # 互不可读（不猜归属、无 owner 修正）
    assert slide_store.authorize_read(out1["slide_id"], actor_user_id="u1")
    assert not slide_store.authorize_read(out1["slide_id"], actor_user_id="u2")
    assert slide_store.authorize_read(out2["slide_id"], actor_user_id="u2")
    assert not slide_store.authorize_read(out2["slide_id"], actor_user_id="u1")
    with open(h._bundle_data(out1["slide_id"]), "rb") as fh:
        assert fh.read() == payload  # 内容不串
    # 状态视图 slide 引用：slide_id 从任务绑定读（P4-app 消费的输出缝）
    ref = ist.job_slide_ref(out1)
    assert ref["slide_id"] == out1["slide_id"]
    assert ref["slide"] == "same.svs"  # 展示快照


# --------------------------------------------------------------------------- #
# 8) 旧 generation 不发布不结算（worker 复活/双 worker）
# --------------------------------------------------------------------------- #
def test_stale_generation_never_publishes_or_settles(monkeypatch):
    fake, st = h.FakeCos(), {}
    job, payload = h._drive_to_validating(fake, st, owner="u1", role="user")
    sid = job["slide_id"]
    monkeypatch.setattr(slide_io, "open_slide", h._ok_open_slide)
    # worker A 领取 validating（gen A），持久化 intent 后「死亡」
    stale = ist.claim_next_job_for_worker([ist.VALIDATING])
    gen_a = stale["worker_generation"]
    staged = h._staged(job["job_id"])
    gen_dir = slide_storage.staging_dir(job["job_id"], gen_a,
                                        root=h._env_dir())
    gen_dir.mkdir(parents=True, exist_ok=True)
    staged = staged.replace(gen_dir / "data.svs")
    sha = hashlib.sha256(payload).hexdigest()
    ist.worker_persist_commit_intent(job["job_id"], gen_a, {
        "task_ref": job["job_id"], "generation": gen_a,
        "commit_token": str(gen_a), "slide_id": sid,
        "owner_user_id": "u1", "sha256": sha, "accounted_bytes": 150,
        "declared_size": 150})
    # 租约过期，worker B（gen B）接管（generation 递增——旧纪元自此被 fence）
    h._sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET worker_lease_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    fresh = ist.claim_next_job_for_worker([ist.VALIDATING])
    assert fresh["worker_generation"] == gen_a + 1
    ist.release_worker_lease(job["job_id"], fresh["worker_lease_token"])

    # worker A 复活：以 gen A 走统一发布 → 入口代次核对即拒（FS 都不发布；
    # 新纪元已递增，任务行凭证 != 旧 intent 凭证）。结算入口另由
    # worker fencing（StaleLease）拒绝——两层都不放行旧 generation。
    manifest = slide_publish.build_manifest("data.svs", 150, sha)
    with pytest.raises(slide_publish.PublishError) as ei:
        slide_publish.publish_with_channel(
            job["job_id"], gen_a, sid, ist.INGESTION_PUBLISH_CHANNEL,
            manifest=manifest, upload_root=h._env_dir())
    assert ei.value.code == "generation_mismatch"
    assert not slide_storage.bundle_dir(sid, root=h._env_dir()).exists()
    with pytest.raises(ist.StaleLease):
        ist.worker_settle_ready(job["job_id"], gen_a, sha256_actual=sha,
                                settle_bytes=150, slide_id=sid)
    # DB 面未推进：任务仍 validating、未扣账、资产不可读
    assert ist.get_job(job["job_id"])["state"] == ist.VALIDATING
    assert int(_quota_row("u1")["used_bytes"]) == 0
    assert not slide_store.authorize_read(sid, actor_user_id="u1")

    # worker B 以当代发布：一次收口
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    with open(h._bundle_data(sid), "rb") as fh:
        assert fh.read() == payload
    assert int(_quota_row("u1")["used_bytes"]) == 150  # 仅一次
