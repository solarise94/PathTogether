# -*- coding: utf-8 -*-
"""后台切片转换任务（conversion_jobs，0046）。

Web 请求只创建 queued 行；worker 用 FOR UPDATE SKIP LOCKED 领取并续租。
"""

from __future__ import annotations

import os
import secrets

import pg_store
import psycopg.rows
from psycopg.errors import UniqueViolation

from kfb.manifest import CONVERTER_ID, CONVERTER_VERSION

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


class NameConflict(ConversionError):
    code = "name_unavailable"


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r is not None else None


def new_job_id():
    return "cvj_" + secrets.token_hex(12)


def _requeue_row(cur, job_id, *, source_name, upload_id, canonical_name):
    cur.execute(
        "UPDATE conversion_jobs SET state='queued', "
        "attempt=attempt+1, error_code=NULL, error_detail_internal=NULL, "
        "finished_at=NULL, canonical_name=%s, source_name=%s, upload_id=%s, "
        "lease_owner=NULL, lease_expires_at=NULL, "
        "canonical_settled_bytes=NULL "
        "WHERE id=%s RETURNING *",
        (canonical_name, source_name, upload_id, job_id))
    return _row(cur)


def create_job(*, owner_user_id, upload_id, source_name, source_sha256,
               source_format, canonical_name, product_exists=None):
    """创建或返回同一 owner+hash+converter 的已有任务（幂等）。

    failed/cancelled 重置为 queued。ready 且产物仍在则原样返回；ready 但
    产物缺失（删除后重传）重新排队。
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
                    if st in ("failed", "cancelled") or (
                            st == "ready" and product_exists is False):
                        try:
                            return _requeue_row(
                                cur, existing["id"],
                                source_name=source_name, upload_id=upload_id,
                                canonical_name=canonical_name)
                        except UniqueViolation as e:
                            raise NameConflict(
                                "canonical 名已被占用") from e
                    # 运行中任务资产路径冻结：同内容换名上传复用原任务，不改写
                    return existing
                job_id = new_job_id()
                try:
                    cur.execute(
                        "INSERT INTO conversion_jobs "
                        "(id, owner_user_id, upload_id, source_name, "
                        " source_sha256, source_format, canonical_name, "
                        " converter_id, converter_version, state) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued') "
                        "RETURNING *",
                        (job_id, owner_user_id, upload_id, source_name,
                         source_sha256, source_format, canonical_name,
                         CONVERTER_ID, CONVERTER_VERSION))
                    return _row(cur)
                except UniqueViolation as e:
                    raise NameConflict("canonical 名已被占用") from e
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
                    "SELECT * FROM conversion_jobs WHERE upload_id=%s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (upload_id,))
                return _row(cur)
    finally:
        conn.close()


def canonical_is_live(canonical_name):
    """是否有非终态/已就绪任务占用该 canonical 名。"""
    if not canonical_name:
        return False
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM conversion_jobs WHERE canonical_name=%s "
                    "AND state NOT IN ('failed', 'cancelled') LIMIT 1",
                    (canonical_name,))
                return cur.fetchone() is not None
    finally:
        conn.close()


def invalidate_by_canonical(canonical_name):
    """删除产物后作废占用该名的任务（含 ready），释放 live 唯一约束。"""
    if not canonical_name:
        return 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE conversion_jobs SET state='cancelled', "
                    "finished_at=now(), lease_owner=NULL, "
                    "lease_expires_at=NULL "
                    "WHERE canonical_name=%s AND state <> 'cancelled'",
                    (canonical_name,))
                return cur.rowcount
    finally:
        conn.close()


def get_job_by_canonical(canonical_name):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE canonical_name=%s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (canonical_name,))
                return _row(cur)
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


def complete_job(job_id, worker_id, canonical_name, *,
                 owner_user_id=None, settle_bytes=0):
    """标记 ready，并在同一事务内幂等结算 canonical 配额。

    canonical_settled_bytes 已有值时不再累加 used_bytes（崩溃重领不双记）。
    """
    settle_bytes = int(settle_bytes or 0)
    owner_user_id = owner_user_id or ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversion_jobs WHERE id=%s AND "
                    "lease_owner=%s FOR UPDATE",
                    (job_id, worker_id))
                row = _row(cur)
                if row is None:
                    raise StateConflict("租约丢失或任务不存在")
                already = int(row.get("canonical_settled_bytes") or 0)
                if already <= 0 and settle_bytes > 0 and owner_user_id:
                    cur.execute(
                        "UPDATE upload_user_quotas SET "
                        "used_bytes = used_bytes + %s, updated_at=now() "
                        "WHERE user_id=%s",
                        (settle_bytes, owner_user_id))
                    already = settle_bytes
                elif already <= 0:
                    already = max(settle_bytes, 0)
                cur.execute(
                    "UPDATE conversion_jobs SET state='ready', "
                    "canonical_name=%s, error_code=NULL, "
                    "error_detail_internal=NULL, finished_at=now(), "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "canonical_settled_bytes=%s, heartbeat_at=now() "
                    "WHERE id=%s RETURNING *",
                    (canonical_name, already, job_id))
                return _row(cur)
    finally:
        conn.close()
