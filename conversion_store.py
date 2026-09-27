# -*- coding: utf-8 -*-
"""后台切片转换任务（conversion_jobs，0046）。

Web 请求只创建 queued 行；worker 用 FOR UPDATE SKIP LOCKED 领取并续租。

P4-app（合同 §3）：转换链切 slide ID 统一发布——
  - ``create_job`` 即**预分配产物 slide_id**（slide_store.allocate_slide，
    staging/id_bundle 行；owner=源 owner，空 owner 回落配置 owner）；幂等
    复用既有任务时**随任务复用其 slide_id**（绝不重新分配）；
  - canonical 名唯一锁（0047 的 idx_conversion_jobs_canonical_live，0069
    拆除）退役——**同名产物是独立资产**（各得各 slide_id/objects 目录）；
    canonical_name 仅保留展示快照；转换幂等键保持
    (owner, source_sha256, converter_id, converter_version)；
  - 产物发布经 ``slide_publish`` 的 conversion 通道
    （``ConversionPublishChannel``）：intent 与置 validating 同事务持久化
    （commit_intent_json，0069），结算（slides CAS + accounted_bytes +
    used_bytes + ready）并入 publish 事务——``canonical_settled_bytes``
    列保留作幂等键兼容（注释标注，P6 可随退役评审删除）；
  - 删除产物按 slide_id 作废任务（``invalidate_by_slide_id``——名占用
    语义退役）。
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import secrets

import pg_store
import psycopg.rows

from kfb.manifest import CONVERTER_ID, CONVERTER_VERSION
import slide_store
import upload_guard

STATES_OPEN = ("queued", "converting", "validating")
LEASE_SECONDS = int(os.environ.get("CONVERSION_LEASE_SECONDS") or 120)


class ConversionError(Exception):
    code = "conversion_error"


class JobNotFound(ConversionError):
    code = "conversion_not_found"


class StateConflict(ConversionError):
    code = "conversion_state_conflict"

    def __init__(self, message, job=None):
        super().__init__(message)
        self.job = job


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r is not None else None


def new_job_id():
    return "cvj_" + secrets.token_hex(12)


def _resolve_product_owner(owner_user_id):
    """产物资产 owner：源 owner；空 owner（本地免登录归一）回落配置 owner
    （share_store.get_owner_user_id——测试/运维注入点）。仍为空 → ValueError
    （不允许空 owner 自动认领，与 allocate_slide 同口径）。"""
    uid = (owner_user_id or "").strip()
    if uid:
        return uid
    import share_store
    fallback = (share_store.get_owner_user_id() or "").strip()
    if not fallback:
        raise ValueError(
            "无法解析转换产物 owner（本地态未配置 owner）——不允许空 owner "
            "自动认领")
    return fallback


def _allocate_product_asset(conn, owner_user_id, canonical_name):
    """在调用方事务内分配产物资产行（staging/id_bundle）；返回 slide_id。"""
    name = (canonical_name or "").strip() or "converted.tif"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "tif"
    if ext not in ("tif", "tiff", "ome"):
        ext = "tif"
    owner = _resolve_product_owner(owner_user_id)
    desc = slide_store.allocate_slide(
        owner, original_filename=name, format_ext=ext, conn=conn)
    return desc.slide_id


def _insert_source(cur, job_id, source_name, upload_id, source_sha256,
                   source_slide_id=None):
    cur.execute(
        "INSERT INTO conversion_job_sources "
        "(job_id, source_name, upload_id, source_sha256, source_slide_id) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (job_id, source_name) DO NOTHING",
        (job_id, source_name, upload_id, (source_sha256 or "").lower() or None,
         (source_slide_id or "").strip() or None))


def _rearm_product_asset(conn, cur, job):
    """重试（requeue）时重置产物资产绑定（调用方事务内）。

    - 绑定行的 slides 状态 failed → staging（复用 slide_id，重试不换 ID）；
    - 绑定行已 deleted/deleting/缺失（产物被删除后重试）→ 分配**新**
      slide_id 并改绑（删除后重传=新 ID 的资产语义）；
    - ready（产物仍在）→ 保持绑定不动（worker 发布路径幂等收口）。
    返回（可能新的）slide_id。
    """
    sid = (job.get("slide_id") or "").strip()
    if sid:
        cur.execute("SELECT asset_state FROM slides WHERE slide_id=%s", (sid,))
        srow = cur.fetchone()
        if srow is not None:
            state = srow["asset_state"]
            if state in ("deleted", "deleting"):
                new_sid = _allocate_product_asset(
                    conn, job.get("owner_user_id"), job.get("canonical_name"))
                cur.execute(
                    "UPDATE conversion_jobs SET slide_id=%s WHERE id=%s",
                    (new_sid, job["id"]))
                return new_sid
            if state == "failed":
                cur.execute(
                    "UPDATE slides SET asset_state=%s, updated_at=now() "
                    "WHERE slide_id=%s AND asset_state=%s",
                    (slide_store.SlideState.STAGING, sid,
                     slide_store.SlideState.FAILED))
            return sid
    new_sid = _allocate_product_asset(
        conn, job.get("owner_user_id"), job.get("canonical_name"))
    cur.execute("UPDATE conversion_jobs SET slide_id=%s WHERE id=%s",
                (new_sid, job["id"]))
    return new_sid


def _requeue_row(cur, conn, job_id, *, source_name, upload_id, canonical_name,
                 source_sha256):
    cur.execute("DELETE FROM conversion_job_sources WHERE job_id=%s", (job_id,))
    cur.execute(
        "UPDATE conversion_jobs SET state='queued', "
        "attempt=attempt+1, error_code=NULL, error_detail_internal=NULL, "
        "finished_at=NULL, canonical_name=%s, source_name=%s, upload_id=%s, "
        "lease_owner=NULL, lease_expires_at=NULL, "
        "commit_intent_json=NULL, canonical_settled_bytes=NULL "
        "WHERE id=%s RETURNING *",
        (canonical_name, source_name, upload_id, job_id))
    row = _row(cur)
    # 重试语义：failed 资产复用 ID 重置 staging；产物已删除 → 新 ID 改绑
    #（重试不重复扣配额：结算幂等键在 publish 事务内，见 worker_settle_ready）。
    sid = _rearm_product_asset(conn, cur, row)
    if sid != (row.get("slide_id") or ""):
        cur.execute("SELECT * FROM conversion_jobs WHERE id=%s", (job_id,))
        row = _row(cur)
    _insert_source(cur, job_id, source_name, upload_id, source_sha256)
    return row


def create_job(*, owner_user_id, upload_id, source_name, source_sha256,
               source_format, canonical_name, product_exists=None,
               target_project_id=None, source_slide_id=None):
    """创建或返回同一 owner+hash+converter 的已有任务（幂等）。

    P4-app：创建即预分配产物 slide_id（staging/id_bundle 资产行 + 任务绑定
    同一事务）；复用既有任务（ready/运行中/failed 重入）时**随任务复用其
    slide_id**。failed/cancelled 重置为 queued（删除后重传）。ready 一律保持
    原 source 关联，不因另一次换名上传而迁移产物。canonical 名唯一锁已拆
    （0069）：同名产物是独立资产，name_conflict 兼容壳（canonical_is_live /
    NameConflict 类）已随 P6 运行时退役删除。

    ``product_exists`` 参数已退役（名占用语义拆除）——保留形参兼容既有调用
    方（baidu_ingest），值被忽略。``source_slide_id``：源本身是切片资产时的
    绑定（worker 读源经 descriptor 优先；P4-app 的 V1/V2 KFB 源按现状语义
    不落资产——源副本归任务 staging，按 source_name 过渡）。
    """
    owner_user_id = owner_user_id or ""
    source_sha256 = (source_sha256 or "").lower()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE owner_user_id=%s "
                    "AND source_sha256=%s AND converter_id=%s "
                    "AND converter_version=%s FOR UPDATE",
                    (owner_user_id, source_sha256, CONVERTER_ID,
                     CONVERTER_VERSION))
                existing = _row(cur)
                if existing:
                    st = existing["state"]
                    if st in ("failed", "cancelled"):
                        return _requeue_row(
                            cur, conn, existing["id"],
                            source_name=source_name, upload_id=upload_id,
                            canonical_name=canonical_name,
                            source_sha256=source_sha256)
                    # ready / 运行中：资产路径冻结。同内容换名上传复用原产物
                    # 与原 slide_id，不得把 a.tif 改绑到 b.tif；登记别名以便
                    # 删产物时清 b.kfb。
                    _insert_source(cur, existing["id"], source_name,
                                   upload_id, source_sha256,
                                   source_slide_id=source_slide_id)
                    return existing
                job_id = new_job_id()
                assoc = ("pending" if target_project_id else "not_needed")
                slide_id = _allocate_product_asset(
                    conn, owner_user_id, canonical_name)
                cur.execute(
                    "INSERT INTO conversion_jobs "
                    "(id, owner_user_id, upload_id, source_name, "
                    " source_sha256, source_format, canonical_name, "
                    " converter_id, converter_version, state, "
                    " target_project_id, project_associate_state, slide_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s) "
                    "RETURNING *",
                    (job_id, owner_user_id, upload_id, source_name,
                     source_sha256, source_format, canonical_name,
                     CONVERTER_ID, CONVERTER_VERSION,
                     target_project_id or None, assoc, slide_id))
                row = _row(cur)
                _insert_source(cur, job_id, source_name, upload_id,
                               source_sha256, source_slide_id=source_slide_id)
                return row
    finally:
        conn.close()


def claim_job(job_id, worker_id, lease_seconds=LEASE_SECONDS):
    """领取指定 queued（或租约过期）任务，供百度入库同步转换。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE id=%s "
                    "AND state IN ('queued', 'converting', 'validating') "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "FOR UPDATE", (job_id,))
                row = _row(cur)
                if not row:
                    return None
                cur.execute(
                    "UPDATE conversion_jobs SET state='converting', "
                    "attempt=attempt+1, lease_owner=%s, "
                    "lease_expires_at=now() + (%s || ' seconds')::interval, "
                    "heartbeat_at=now(), started_at=COALESCE(started_at, now()) "
                    "WHERE id=%s RETURNING *",
                    (worker_id, str(int(lease_seconds)), job_id))
                return _row(cur)
    finally:
        conn.close()


def set_project_associate(job_id, target_project_id, state):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE conversion_jobs SET target_project_id=%s, "
                    "project_associate_state=%s WHERE id=%s",
                    (target_project_id, state, job_id))
    finally:
        conn.close()


def get_job(job_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT * FROM conversion_jobs WHERE id=%s",
                            (job_id,))
                return _row(cur)
    finally:
        conn.close()


def get_job_by_upload_id(upload_id):
    if not upload_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ("
                    " SELECT j.* FROM conversion_jobs j WHERE j.upload_id=%s "
                    " UNION "
                    " SELECT j.* FROM conversion_jobs j "
                    " JOIN conversion_job_sources s ON s.job_id=j.id "
                    " WHERE s.upload_id=%s"
                    ") x ORDER BY created_at DESC LIMIT 1",
                    (upload_id, upload_id))
                return _row(cur)
    finally:
        conn.close()


def list_source_names(job_id):
    """主源 + 别名（去重）。表缺失时仍返回空列表由调用方回退 job.source_name。"""
    if not job_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT source_name FROM conversion_job_sources "
                    "WHERE job_id=%s",
                    (job_id,))
                return [r["source_name"] for r in cur.fetchall()
                        if r and r.get("source_name")]
    finally:
        conn.close()


def list_sources(job_id):
    """conversion_job_sources 行（source_name/source_slide_id/upload_id）。

    P4-app：worker 读源按 source_slide_id（descriptor）优先、source_name
    alias 过渡（合同 §3.2）。无行返回 []。
    """
    if not job_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT source_name, upload_id, source_sha256, "
                    "source_slide_id FROM conversion_job_sources "
                    "WHERE job_id=%s ORDER BY source_name",
                    (job_id,))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_job_by_slide_id(slide_id):
    """按产物 slide_id 查任务（删除产物→按 ID 作废任务的锚点；无则 None）。"""
    if not slide_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE slide_id=%s "
                    "ORDER BY created_at DESC LIMIT 1", (slide_id,))
                return _row(cur)
    finally:
        conn.close()


def invalidate_by_slide_id(slide_id, *, legacy_canonical=None):
    """删除产物后按 slide_id 作废任务（名占用语义退役，合同 §3.5/§7）。

    取消全部非 cancelled 的绑定任务（slide_id 匹配）；``legacy_canonical``
    仅服务**升级窗口**的旧行（P4 前创建、slide_id 为 NULL、产物为 legacy
    平铺名）——按 canonical 名匹配且仅限 slide_id IS NULL 的行（P6 排空后
    删除该参数）。返回受影响任务的源名集合（调用方据此清理源副本）。
    """
    if not slide_id and not legacy_canonical:
        return set()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if legacy_canonical:
                    cur.execute(
                        "UPDATE conversion_jobs SET state='cancelled', "
                        "finished_at=now(), lease_owner=NULL, "
                        "lease_expires_at=NULL, commit_intent_json=NULL "
                        "WHERE (slide_id=%s OR (slide_id IS NULL "
                        "AND canonical_name=%s)) AND state <> 'cancelled' "
                        "RETURNING id",
                        (slide_id or "", legacy_canonical))
                else:
                    cur.execute(
                        "UPDATE conversion_jobs SET state='cancelled', "
                        "finished_at=now(), lease_owner=NULL, "
                        "lease_expires_at=NULL, commit_intent_json=NULL "
                        "WHERE slide_id=%s AND state <> 'cancelled' "
                        "RETURNING id", (slide_id,))
                ids = [r["id"] for r in cur.fetchall()]
        sources = set()
        for jid in ids:
            try:
                sources.update(list_source_names(jid))
            except Exception:  # noqa: BLE001 - 清源尽力而为
                continue
        return sources
    finally:
        conn.close()


def public_view(job):
    """给客户端的脱敏视图（不含内部错误细节）。"""
    if not job:
        return None
    return {
        "conversion_job_id": job["id"],
        "state": job["state"],
        "source_name": job["source_name"],
        "canonical_name": job["canonical_name"],
        # P4-app（合同 §3.6）：产物身份按任务绑定 slide_id（创建即分配；
        # ready 前后都在——轮询打开目标不再按 canonical 名 resolve）
        "slide_id": (job.get("slide_id") or None),
        "source_format": job["source_format"],
        "attempt": int(job.get("attempt") or 0),
        "error_code": job.get("error_code"),
        "created_at": job["created_at"].isoformat()
        if job.get("created_at") and hasattr(job["created_at"], "isoformat")
        else job.get("created_at"),
        "finished_at": job["finished_at"].isoformat()
        if job.get("finished_at") and hasattr(job["finished_at"], "isoformat")
        else job.get("finished_at"),
    }


def claim_one(worker_id, lease_seconds=LEASE_SECONDS):
    """领取一条 queued 或租约过期的 converting/validating 任务。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs "
                    "WHERE state IN ('queued', 'converting', 'validating') "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "ORDER BY created_at "
                    "FOR UPDATE SKIP LOCKED LIMIT 1")
                row = _row(cur)
                if not row:
                    return None
                cur.execute(
                    "UPDATE conversion_jobs SET state='converting', "
                    "attempt=attempt+1, lease_owner=%s, "
                    "lease_expires_at=now() + (%s || ' seconds')::interval, "
                    "heartbeat_at=now(), started_at=COALESCE(started_at, now()) "
                    "WHERE id=%s RETURNING *",
                    (worker_id, str(int(lease_seconds)), row["id"]))
                return _row(cur)
    finally:
        conn.close()


def heartbeat(job_id, worker_id, lease_seconds=LEASE_SECONDS):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE conversion_jobs SET heartbeat_at=now(), "
                    "lease_expires_at=now() + (%s || ' seconds')::interval "
                    "WHERE id=%s AND lease_owner=%s AND state IN "
                    "('converting', 'validating')",
                    (str(int(lease_seconds)), job_id, worker_id))
                return cur.rowcount == 1
    finally:
        conn.close()


def mark_state(job_id, worker_id, state, **fields):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                sets = ["state=%s", "heartbeat_at=now()"]
                args = [state]
                if state in ("ready", "failed", "cancelled"):
                    sets.append("finished_at=now()")
                    sets.append("lease_owner=NULL")
                    sets.append("lease_expires_at=NULL")
                if "error_code" in fields:
                    sets.append("error_code=%s")
                    args.append(fields["error_code"])
                if "error_detail_internal" in fields:
                    sets.append("error_detail_internal=%s")
                    args.append(fields["error_detail_internal"])
                if "canonical_name" in fields:
                    sets.append("canonical_name=%s")
                    args.append(fields["canonical_name"])
                args.extend([job_id, worker_id])
                cur.execute(
                    "UPDATE conversion_jobs SET " + ", ".join(sets) +
                    " WHERE id=%s AND lease_owner=%s RETURNING *",
                    tuple(args))
                row = _row(cur)
                if row is None:
                    raise StateConflict("租约丢失或任务不存在")
                return row
    finally:
        conn.close()


def fail_job(job_id, worker_id, error_code, detail=None):
    return mark_state(job_id, worker_id, "failed",
                      error_code=error_code,
                      error_detail_internal=(detail or "")[:2000])


# --------------------------------------------------------------------------- #
# 统一发布接线（P4-app 合同 §3.3/§3.4）：intent 持久化 + 结算并入 publish 事务
# --------------------------------------------------------------------------- #
def persist_commit_intent(job_id, worker_id, intent):
    """validating 内、统一发布之前持久化 publish intent（提交恢复栅栏）。

    intent 是 slide_publish 六步第 1 步的权威证据（task_ref/generation/
    commit_token/slide_id/owner_user_id/manifest/sha256/accounted_bytes；
    generation=commit_token=本次领取的 attempt）。租约失守（他人重领）被拒
    ——旧 worker 的后续发布由 fencing 拒绝，新 worker 按新代次重转。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE id=%s "
                    "AND lease_owner=%s FOR UPDATE", (job_id, worker_id))
                row = _row(cur)
                if row is None:
                    raise StateConflict("租约丢失或任务不存在")
                if row["state"] != "validating":
                    raise StateConflict(
                        "commit intent 要求 validating（当前 %s）"
                        % row["state"], job=row)
                cur.execute(
                    "UPDATE conversion_jobs SET commit_intent_json=%s, "
                    "heartbeat_at=now() WHERE id=%s",
                    (json.dumps(intent), job_id))
                cur.execute("SELECT * FROM conversion_jobs WHERE id=%s",
                            (job_id,))
                return _row(cur)
    finally:
        conn.close()


def worker_settle_ready(job_id, worker_id, generation, *, slide_id,
                        canonical_name, sha256, settle_bytes):
    """validating → ready：统一发布结算短事务（P4-app 合同 §3.4）。

    同一事务完成（任一步失败整体回滚——FS 已发布、DB 未提交 → 不可见，
    恢复重试收口）：

      1. advisory ``slide:<slide_id>``（第一把锁；0067 锁序）；
      2. conversion_jobs 行 FOR UPDATE（租约 + 代次 CAS：lease_owner 归本
         worker 且 attempt == generation——旧 worker 的结算被 fencing 拒）；
      3. slides 行 CAS（staging→ready + accounted_bytes；R-12）；
      4. slide_assets 内容 revision（``sha256:<hex 前缀>``）；
      5. quota：used_bytes += settle_bytes——幂等键 ``canonical_settled_bytes``
         （0047 列**保留作兼容**：已有值不再累加，崩溃重领不双记；结算已从
         complete_job 的独立事务**挪进本 publish 事务**，合同 §3.4 裁决）；
      6. job 收口 UPDATE（state=ready + canonical_name 展示快照 + 清 intent/
         租约）。

    幂等：state=ready 视为已收口返回 (job, True)（重复调用/恢复重入）。
    """
    settle_bytes = int(settle_bytes or 0)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE id=%s FOR UPDATE",
                    (job_id,))
                row = _row(cur)
                if row is None:
                    raise StateConflict("租约丢失或任务不存在")
                if row["state"] == "ready":
                    return row, True  # 已收口（重复调用/恢复重入）
                if row["lease_owner"] != worker_id \
                        or str(row["attempt"]) != str(generation):
                    raise StateConflict(
                        "租约失守或代次失效（结算被拒，job=%s）" % job_id,
                        job=row)
                if row["state"] != "validating":
                    raise StateConflict(
                        "结算要求 validating（当前 %s）" % row["state"],
                        job=row)
                if not row.get("commit_intent_json"):
                    raise StateConflict(
                        "结算前必须已持久化 commit intent", job=row)
                if (row.get("slide_id") or "") != slide_id:
                    raise StateConflict(
                        "结算 slide_id 与任务绑定不一致（%s != %s）"
                        % (slide_id, row.get("slide_id")), job=row)
                cur.execute(
                    "UPDATE slides SET asset_state=%s, published_at=now(), "
                    "accounted_bytes=%s, updated_at=now() "
                    "WHERE slide_id=%s AND asset_state=%s",
                    (slide_store.SlideState.READY, settle_bytes, slide_id,
                     slide_store.SlideState.STAGING))
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT asset_state, accounted_bytes FROM slides "
                        "WHERE slide_id=%s", (slide_id,))
                    srow = cur.fetchone()
                    if not (srow
                            and srow["asset_state"] == slide_store.SlideState.READY
                            and srow["accounted_bytes"] is not None
                            and int(srow["accounted_bytes"])
                            == int(settle_bytes)):
                        raise StateConflict(
                            "产物资产不在 staging 且非同参 ready（state=%r "
                            "accounted=%r）——fail-closed 不猜"
                            % (srow and srow["asset_state"],
                               srow and srow["accounted_bytes"]), job=row)
                slide_store.record_revision(
                    slide_id, "sha256:%s" % str(sha256 or "").lower()[:16],
                    conn=conn)
                owner = (row.get("owner_user_id") or "").strip()
                already = int(row.get("canonical_settled_bytes") or 0)
                if already <= 0 and settle_bytes > 0 and owner:
                    # 0072 生命周期：used_bytes 财务 SQL 唯一实现收口在
                    # upload_guard（此前本模块直更配额行是与守卫并行的
                    # 第二份实现）；幂等键仍由 canonical_settled_bytes
                    # （上方 already 判定）承担。
                    upload_guard.add_used_bytes_locked(cur, owner, settle_bytes)
                    already = settle_bytes
                elif already <= 0:
                    already = max(settle_bytes, 0)
                cur.execute(
                    "UPDATE conversion_jobs SET state='ready', "
                    "canonical_name=%s, error_code=NULL, "
                    "error_detail_internal=NULL, finished_at=now(), "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "commit_intent_json=NULL, canonical_settled_bytes=%s, "
                    "heartbeat_at=now() WHERE id=%s RETURNING *",
                    (canonical_name, already, job_id))
                return _row(cur), False
    finally:
        conn.close()


class ConversionPublishChannel:
    """slide_publish 六步编排的 conversion_jobs 通道适配（P4-app §3.3）。

    代次/fencing：``attempt``（claim_job/claim_one 递增）+ ``lease_owner``。
    intent 的 generation/commit_token 均为领取代次（str(attempt)）；结算复用
    ``worker_settle_ready``（slides CAS + accounted_bytes + 内容 revision +
    used_bytes 同事务）。FS 发布先于 advisory 锁的顺序（P3 偏差 #1）在本通道
    的 worker lease 模型下成立：validating+intent 是不可撤销提交段，旧代次
    被 attempt fencing 拒绝结算，no-clobber + verify_bundle 幂等吸收重复
    FS 发布，可见性只由结算事务的 asset_state CAS 裁定；跨任务目标
    objects/<slide_id>/ 由预分配 ID 唯一化（uq_conversion_jobs_slide_id），
    无同名竞争面。
    """

    def load_task(self, task_ref):
        return get_job(task_ref)

    def is_settled(self, task):
        return bool(task) and task.get("state") == "ready"

    def decode_intent(self, task):
        import upload_task_store
        return upload_task_store.decode_commit_intent(
            task.get("commit_intent_json"))

    def task_commit_token(self, task):
        return str(task.get("attempt")) if task else None

    def precheck_locked(self, cur, task_ref, generation, slide_id,
                        owner_user_id, intent):
        import slide_publish
        cur.execute(
            "SELECT * FROM conversion_jobs WHERE id=%s FOR UPDATE",
            (task_ref,))
        job = _row(cur)
        if job is None:
            raise slide_publish.PublishError(
                "task_not_found", "任务不存在：%s" % task_ref,
                deterministic=True)
        if job["lease_owner"] != (intent.get("worker_id")
                                  or job["lease_owner"]) \
                or str(job["attempt"]) != str(generation):
            raise StateConflict(
                "租约失守或代次失效（发布被拒，job=%s）" % task_ref, job=job)
        if job["state"] != "validating":
            raise slide_publish.PublishError(
                "generation_mismatch",
                "任务不在 validating（state=%r）——不猜" % job["state"],
                deterministic=True, task=job)
        if (job.get("slide_id") or "") != slide_id:
            raise slide_publish.PublishError(
                "task_slide_mismatch", "任务绑定的资产与本发布不一致",
                deterministic=True, task=job)
        intent_owner = (intent.get("owner_user_id") or "").strip()
        if intent_owner and intent_owner != (job.get("owner_user_id") or ""):
            raise slide_publish.PublishError(
                "owner_mismatch",
                "intent owner 与任务 owner 不一致（%r）——不自动修正"
                % intent_owner, deterministic=True, task=job)
        if owner_user_id is not None \
                and (owner_user_id or "").strip() != intent_owner:
            raise slide_publish.PublishError(
                "owner_mismatch", "发布发起者与资产 owner 不一致（拒绝，"
                "不自动修正）", deterministic=True, task=job)
        return job

    def settle(self, task_ref, generation, slide_id, sha256, accounted_bytes):
        # worker_id/canonical 快照从任务行读（lease_owner 即当前持有 worker；
        # worker_settle_ready 锁内再核租约/代次——失守即 StateConflict）。
        job = get_job(task_ref)
        if job is None:
            raise StateConflict("租约丢失或任务不存在")
        out, _already = worker_settle_ready(
            task_ref, job.get("lease_owner"), generation, slide_id=slide_id,
            canonical_name=(job.get("canonical_name") or ""),
            sha256=sha256, settle_bytes=int(accounted_bytes))
        return out, out.get("state") == "ready"


#: conversion 通道单例（无状态；conversion_worker 经它接入统一发布）。
CONVERSION_PUBLISH_CHANNEL = ConversionPublishChannel()


# --------------------------------------------------------------------------- #
# W4：任务列表（owner 工作区）+ 失败重试（同 id 重新入队）
# --------------------------------------------------------------------------- #
#: 列表页大小合同：1–100，默认 50
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100

#: recent 组默认窗口（天）：终态任务按 finished_at（缺省 created_at）截留
RECENT_WINDOW_DAYS = 7


def _encode_page_cursor(created_at, job_id):
    """keyset 游标 → 不透明 base64url 字符串（客户端只回传，不解析）。"""
    raw = json.dumps(
        {"c": created_at.isoformat() if hasattr(created_at, "isoformat")
         else str(created_at),
         "i": str(job_id)},
        separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode(
        "ascii").rstrip("=")


def _decode_page_cursor(cursor):
    """不透明游标 → (created_at, job_id)；非法游标抛 ValueError（→ 400）。"""
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(
            base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
        return _dt.datetime.fromisoformat(str(data["c"])), str(data["i"])
    except Exception as exc:
        raise ValueError("cursor 非法") from exc


def list_jobs(*, owner_user_id, group="open", limit=LIST_LIMIT_DEFAULT,
              cursor=None, recent_days=RECENT_WINDOW_DAYS):
    """owner 工作区任务分页列表（W4；只读，不加锁）。

    - ``owner_user_id`` 必填（**空串是合法 owner**——本地免登录归一工作区，
      仍按等值过滤，绝不跨 owner 泄露）；
    - ``group='open'``：进行中（queued/converting/validating）；
    - ``group='recent'``：近 ``recent_days`` 天（默认 7）终态任务
      （ready/failed/cancelled 按 finished_at，缺省 created_at）**及全部
      进行中任务**；
    - 排序稳定：``created_at DESC, id DESC``；keyset 游标（base64url JSON
      of created_at iso + id），无跨页重复/漏项；
    - 返回 ``{"items": [public_view(job)], "next_cursor": str|None}``——
      public_view 不含 error_detail_internal。

    ``group`` 非法 / ``cursor`` 解不开 → ValueError（调用方映射 400）。
    """
    owner = "" if owner_user_id is None else str(owner_user_id)
    if group not in ("open", "recent"):
        raise ValueError("group 需为 open|recent")
    try:
        limit = int(limit if limit is not None else LIST_LIMIT_DEFAULT)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 需为整数") from exc
    limit = max(1, min(limit, LIST_LIMIT_MAX))

    where = ["owner_user_id = %s"]
    params = [owner]
    if group == "open":
        where.append("state IN ('queued', 'converting', 'validating')")
    else:
        where.append(
            "(state IN ('queued', 'converting', 'validating') "
            "OR (state IN ('ready', 'failed', 'cancelled') "
            "AND COALESCE(finished_at, created_at) >= "
            "now() - (%s || ' days')::interval))")
        params.append(str(int(recent_days)))
    if cursor is not None:
        created, last_id = _decode_page_cursor(cursor)
        where.append(
            "(created_at < %s OR (created_at = %s AND id < %s))")
        params.extend([created, created, last_id])

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE "
                    + " AND ".join(where)
                    + " ORDER BY created_at DESC, id DESC LIMIT %s",
                    tuple(params) + (limit + 1,))
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_page_cursor(last["created_at"], last["id"])
    return {"items": [public_view(r) for r in rows],
            "next_cursor": next_cursor}


def retry_job(job_id, *, owner_user_id, source_available):
    """重试 failed/cancelled 任务：**同 id** 重新入队，不建第二个任务。

    - 源文件是否仍在由调用方判定（``source_available``，store 不触碰
      UPLOAD_DIR）；不在 → StateConflict("source_unavailable")；
    - owner 不匹配与不存在同口径抛 JobNotFound（不向其他用户泄露存在性）；
    - ready/进行中 → StateConflict；
    - 复用 ``_requeue_row``（attempt+1、state=queued、清错误字段与租约；
      产物绑定复用 slide_id，资产已删除则改绑新 ID）；配额结算由
      worker_settle_ready 的 canonical_settled_bytes 幂等键守护（结算已并入
      publish 事务，P4-app §3.4），重试路径不二次结算。
    """
    owner = "" if owner_user_id is None else str(owner_user_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE id=%s FOR UPDATE",
                    (job_id,))
                row = _row(cur)
                if row is None or (row.get("owner_user_id") or "") != owner:
                    raise JobNotFound("转换任务不存在")
                state = row["state"]
                if state not in ("failed", "cancelled"):
                    raise StateConflict(
                        "任务状态 %s 不可重试" % state, job=row)
                if not source_available:
                    raise StateConflict("source_unavailable", job=row)
                requeued = _requeue_row(
                    cur, conn, row["id"],
                    source_name=row["source_name"],
                    upload_id=row.get("upload_id"),
                    canonical_name=row.get("canonical_name"),
                    source_sha256=row.get("source_sha256"))
                return public_view(requeued)
    finally:
        conn.close()
