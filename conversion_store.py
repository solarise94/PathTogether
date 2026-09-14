# -*- coding: utf-8 -*-
"""后台切片转换任务（conversion_jobs，0046）。

Web 请求只创建 queued 行；worker 用 FOR UPDATE SKIP LOCKED 领取并续租。
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
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


def _insert_source(cur, job_id, source_name, upload_id, source_sha256):
    cur.execute(
        "INSERT INTO conversion_job_sources "
        "(job_id, source_name, upload_id, source_sha256) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (job_id, source_name) DO NOTHING",
        (job_id, source_name, upload_id, (source_sha256 or "").lower() or None))


def _requeue_row(cur, job_id, *, source_name, upload_id, canonical_name,
                 source_sha256):
    cur.execute("DELETE FROM conversion_job_sources WHERE job_id=%s", (job_id,))
    cur.execute(
        "UPDATE conversion_jobs SET state='queued', "
        "attempt=attempt+1, error_code=NULL, error_detail_internal=NULL, "
        "finished_at=NULL, canonical_name=%s, source_name=%s, upload_id=%s, "
        "lease_owner=NULL, lease_expires_at=NULL, "
        "canonical_settled_bytes=NULL "
        "WHERE id=%s RETURNING *",
        (canonical_name, source_name, upload_id, job_id))
    row = _row(cur)
    _insert_source(cur, job_id, source_name, upload_id, source_sha256)
    return row


def create_job(*, owner_user_id, upload_id, source_name, source_sha256,
               source_format, canonical_name, product_exists=None):
    """创建或返回同一 owner+hash+converter 的已有任务（幂等）。

    failed/cancelled 重置为 queued（删除后重传）。ready 一律保持原
    source/canonical 关联，不因另一次换名上传而迁移产物。
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
                        try:
                            return _requeue_row(
                                cur, existing["id"],
                                source_name=source_name, upload_id=upload_id,
                                canonical_name=canonical_name,
                                source_sha256=source_sha256)
                        except UniqueViolation as e:
                            raise NameConflict(
                                "canonical 名已被占用") from e
                    # ready / 运行中：资产路径冻结。同内容换名上传复用原产物，
                    # 不得把 a.tif 改绑到 b.tif；登记别名以便删产物时清 b.kfb。
                    _insert_source(cur, existing["id"], source_name,
                                   upload_id, source_sha256)
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
                    row = _row(cur)
                    _insert_source(cur, job_id, source_name, upload_id,
                                   source_sha256)
                    return row
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
    - 复用 ``_requeue_row``（attempt+1、state=queued、清错误字段与租约）；
      配额结算由 complete_job 的 canonical_settled_bytes 幂等键守护，
      重试路径不二次结算。
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
                    cur, row["id"],
                    source_name=row["source_name"],
                    upload_id=row.get("upload_id"),
                    canonical_name=row.get("canonical_name"),
                    source_sha256=row.get("source_sha256"))
                return public_view(requeued)
    finally:
        conn.close()
