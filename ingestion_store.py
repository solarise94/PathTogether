# -*- coding: utf-8 -*-
"""ingestion_jobs 状态机与容量准入（COS 直传 Phase 1，合同 §4/§6）。

裁决 A-presign-parts：transport 固定 ``presign_parts``，不建 B-SDK-STS
双栈（/credentials 不存在）。与 upload_tasks 完全分离（D1）。

状态机（§4 主状态，终态大写注释）：

    waiting_capacity → preparing → uploading → completing → queued
        → downloading → validating → ready → completed

    终态：cancelled / failed / expired（completed 亦终态）。
    ready = 本地原子提升 + metadata + 配额一次结算 + commit intent 收口
    已成功、未过 Viewer readiness；completed = readiness probe 通过且 CAS
    置 viewer_ready。cleanup_* 独立（§6.2：COS 删除失败不回滚本地切片）。

锁序（与 migrations/0066 头注释一致，所有触及既有 job 的路径恒定）：

    ingestion_jobs 行 → upload_reservations 行 → upload_user_quotas 行
    → cos_pool_state 行

持有 pool 行锁期间不回头等其它行（无环）。准入原子性：本地配额预占与
COS 池预约在同一事务；任一失败整体回滚，禁止半成功（§6.1）。

绝对期限（§6.3）：waiting_expires_at = created_at + COS_WAITING_MAX_AGE
（24h，续租不延长）；job_deadline_at = capacity_admitted_at +
COS_JOB_MAX_AGE（72h）。过期预约禁止复活：renew 走「不复活」语义，恢复
流程按锁序重新预占，失败则终止并清理远端。

json/dual 后端 fail-closed：仅 postgres 后端可用（调用方保证）。
事件 detail 禁止秘密：本模块 _sanitize_detail 拦截 sign/secret/token/url
形状的键值（防御性，上游也不得传）。
"""

import json
import secrets

import psycopg

import cos_config
import cos_pool_store
import pg_store
import upload_guard

# --------------------------------------------------------------------------- #
# 状态与转移
# --------------------------------------------------------------------------- #
WAITING = "waiting_capacity"
PREPARING = "preparing"
UPLOADING = "uploading"
COMPLETING = "completing"
QUEUED = "queued"
DOWNLOADING = "downloading"
VALIDATING = "validating"
READY = "ready"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
EXPIRED = "expired"

TERMINAL_STATES = frozenset({COMPLETED, CANCELLED, FAILED, EXPIRED})
#: 准入后、本地提交前：持有上传授权 + 本地预约 + COS 池预约的状态集。
ACTIVE_UPLOAD_STATES = frozenset(
    {PREPARING, UPLOADING, COMPLETING, QUEUED, DOWNLOADING, VALIDATING})
#: 仍持有 COS 池预约（未确认清理）的状态集：ACTIVE_UPLOAD_STATES + READY
#:（ready 已结算本地，但远端清理确认前 pool_reserved 不释放）。
POOL_HOLDING_STATES = ACTIVE_UPLOAD_STATES | {READY}

#: 合法转移表（非法跳转一律 IngestionStateError，fail-closed 不猜）。
LEGAL_TRANSITIONS = {
    WAITING: frozenset({PREPARING, CANCELLED, EXPIRED}),
    PREPARING: frozenset({UPLOADING, CANCELLED, FAILED}),
    #: completing→uploading：worker ListParts 核验发现缺块，回浏览器续传。
    UPLOADING: frozenset({COMPLETING, CANCELLED, FAILED}),
    COMPLETING: frozenset({QUEUED, UPLOADING, CANCELLED, FAILED}),
    QUEUED: frozenset({DOWNLOADING, CANCELLED, FAILED}),
    #: downloading→queued：下载中断回队（checkpoint 已持久）。
    DOWNLOADING: frozenset({VALIDATING, QUEUED, CANCELLED, FAILED}),
    VALIDATING: frozenset({READY, CANCELLED, FAILED}),
    #: ready 不因上传超期取消（§4）；只前进到 completed。
    READY: frozenset({COMPLETED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    FAILED: frozenset(),
    EXPIRED: frozenset(),
}

CLEANUP_NONE = "none"
CLEANUP_PENDING = "pending"
CLEANUP_CLEANED = "cleaned"
CLEANUP_FAILED = "failed"


class IngestionStateError(Exception):
    """非法状态转换 / CAS 冲突 / 已入库后取消等合同级拒绝。"""


class StaleLease(Exception):
    """worker 失租（generation 过期）后的收口被拒——fencing 生效。"""


class _AdmissionDeferred(Exception):
    """准入暂缓（容量/并发/对账暂停）——事务整体回滚，任务保持 waiting。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# 行映射
# --------------------------------------------------------------------------- #
_JOB_FIELDS = (
    "job_id", "owner_user_id", "owner_role", "idempotency_key", "filename",
    "safe_name", "format_ext", "declared_size", "state", "transport",
    "policy_version", "route_reason", "pool_reserved_bytes",
    "capacity_admitted_at", "local_reservation_id", "bucket", "object_key",
    "upload_id", "cos_version_id", "source_etag", "source_size_bytes",
    "part_plan_json", "download_checkpoint_json", "downloaded_bytes",
    "logical_download_bytes", "wire_download_bytes", "commit_intent_json",
    "commit_started_at", "local_ready_at", "slide_canonical_name",
    "sha256_actual", "viewer_ready", "cleanup_status", "cleanup_attempts",
    "cleanup_last_error", "cleanup_next_retry_at", "cleanup_lease_token",
    "cleanup_lease_expires_at", "worker_lease_token", "worker_lease_expires_at",
    "worker_generation", "fail_code", "waiting_expires_at", "job_deadline_at",
    "terminal_at", "created_at", "updated_at",
)

_INT_FIELDS = frozenset({
    "declared_size", "pool_reserved_bytes", "source_size_bytes",
    "downloaded_bytes", "logical_download_bytes", "wire_download_bytes",
    "cleanup_attempts", "worker_generation",
})
_TS_FIELDS = frozenset({
    "capacity_admitted_at", "commit_started_at", "local_ready_at",
    "cleanup_next_retry_at", "cleanup_lease_expires_at",
    "worker_lease_expires_at", "waiting_expires_at", "job_deadline_at",
    "terminal_at", "created_at", "updated_at",
})
_JSON_FIELDS = frozenset({
    "part_plan_json", "download_checkpoint_json", "commit_intent_json",
})


def _norm_row(row):
    if row is None:
        return None
    job = dict(row)
    for k in _INT_FIELDS:
        if job.get(k) is not None:
            job[k] = int(job[k])
    for k in _TS_FIELDS:
        v = job.get(k)
        job[k] = v.timestamp() if hasattr(v, "timestamp") else v
    for k in _JSON_FIELDS:
        v = job.get(k)
        if isinstance(v, str):
            try:
                job[k] = json.loads(v)
            except (TypeError, ValueError):
                # JSON 非权威 fail-closed（upload_task_store 同款口径）：
                # 计划/checkpoint/intent 损坏 → None，调用方按「记录丢失」
                # 冲突处理，绝不猜。
                job[k] = None
    job["viewer_ready"] = bool(job.get("viewer_ready"))
    return job


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


_FORBIDDEN_DETAIL_KEY_MARKS = ("sign", "secret", "token", "url", "password")


def _sanitize_detail(detail):
    """事件 detail 防御性脱敏：携带签名/凭证形状键值的事件直接拒绝落库。"""
    if detail is None:
        return None
    for key in detail:
        kl = str(key).lower()
        if any(mark in kl for mark in _FORBIDDEN_DETAIL_KEY_MARKS):
            raise IngestionStateError(
                "ingestion_events detail 禁止携带疑似秘密键 %r" % key)
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def _append_event(cur, job_id, kind, detail=None):
    cur.execute(
        "INSERT INTO ingestion_events (job_id, kind, detail) "
        "VALUES (%s, %s, %s)",
        (job_id, kind, _sanitize_detail(detail)))


# --------------------------------------------------------------------------- #
# 创建与准入
# --------------------------------------------------------------------------- #
def create_waiting_job(owner_user_id, owner_role, filename, safe_name,
                       format_ext, declared_size, *, idempotency_key=None,
                       policy_version=None, route_reason=None):
    """创建 waiting_capacity 任务（尚不预约任何容量/凭证，§4/§6.1）。

    幂等：同 (owner, idempotency_key) 存活/已完成任务唯一（0066 部分唯一
    索引兜底）——冲突时返回 (既有行, False)。返回 (job_dict, created)。
    """
    declared_size = int(declared_size)
    job_id = "inj_" + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                try:
                    with conn.transaction():  # SAVEPOINT：失败后事务可继续
                        cur.execute(
                            "INSERT INTO ingestion_jobs (job_id, owner_user_id, "
                            "owner_role, idempotency_key, filename, safe_name, "
                            "format_ext, declared_size, state, transport, "
                            "policy_version, route_reason, waiting_expires_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'presign_parts',"
                            "%s,%s, now() + make_interval(secs => %s))",
                            (job_id, owner_user_id, owner_role, idempotency_key,
                             filename, safe_name, format_ext, declared_size,
                             WAITING, policy_version, route_reason,
                             cos_config.COS_WAITING_MAX_AGE_SECONDS))
                except psycopg.errors.UniqueViolation as exc:
                    constraint = getattr(exc.diag, "constraint_name", "") or ""
                    # 幂等重试优先：同 (owner, key) 存活任务存在即返回既有行
                    #（与撞上哪个唯一索引无关——waiting/幂等两索引可能同时
                    # 被违反，检查顺序不保证）。
                    cur.execute(
                        "SELECT * FROM ingestion_jobs WHERE "
                        "owner_user_id=%s AND idempotency_key=%s AND "
                        "state NOT IN ('cancelled','failed','expired') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (owner_user_id, idempotency_key))
                    existing = cur.fetchone()
                    if existing is not None and idempotency_key is not None:
                        return _norm_row(existing), False
                    if constraint == "ingestion_jobs_one_waiting_per_owner":
                        raise IngestionStateError(
                            "cos_waiting_limit：该身份已有等待容量的任务")
                    raise
                _append_event(cur, job_id, "created", {
                    "declared_size": declared_size, "format_ext": format_ext,
                    "policy_version": policy_version,
                    "route_reason": route_reason})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone()), True
    finally:
        conn.close()


def _active_counts(cur, owner_user_id):
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ingestion_jobs "
        "WHERE owner_user_id=%s AND state = ANY(%s)",
        (owner_user_id, list(ACTIVE_UPLOAD_STATES)))
    per_identity = int(cur.fetchone()["n"])
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ingestion_jobs "
        "WHERE state = ANY(%s)", (list(ACTIVE_UPLOAD_STATES),))
    return per_identity, int(cur.fetchone()["n"])


def try_admit_job(job_id, *, disk_watermark_ok=True):
    """FIFO 准入尝试（容量调度器 / 创建后立即尝试共用）。

    单事务（锁序 job → reservation → quota → pool）：

    1. 锁 job 行；非 waiting_capacity → 返回现状（已准入/已终态）；
    2. 等待超时 → 就地转 expired（terminal）；
    3. 磁盘水位不过 → 暂缓（waiting, disk_watermark）；
    4. 每身份/全局活跃上限 → 暂缓（不因此终止）；
    5. quota 适用身份：reserve_upload_locked 预占本地配额——
       QuotaExceeded → 任务**终止**（cancelled, local_quota_infeasible，
       §6.1「准入时已不满足本地配额，任务终止且不接触 COS」）；
       Inflight/Rate 限 → 暂缓（瞬态）；
    6. 锁池行：对账暂停/容量不足 → 整体回滚（本地预占一并撤销，禁止半成功）
       → 暂缓（cos_capacity_reconcile_required / pool_capacity）；
    7. 成功：state=preparing、pool_reserved_bytes=declared、
       capacity_admitted_at=now()、job_deadline_at=now()+COS_JOB_MAX_AGE。

    返回 {"outcome": admitted|waiting|terminal, "reason": str|None, "job": …}。
    """
    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    return _try_admit_txn(
                        cur, job_id, disk_watermark_ok=disk_watermark_ok)
        except _AdmissionDeferred as deferred:
            # 事务已整体回滚（含本地预占撤销）；重读行返回现状
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                job = _norm_row(cur.fetchone())
            return {"outcome": "waiting", "reason": deferred.reason, "job": job}
    finally:
        conn.close()


def _try_admit_txn(cur, job_id, *, disk_watermark_ok=True):
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s FOR UPDATE",
                (job_id,))
    job = _norm_row(cur.fetchone())
    if job is None:
        raise IngestionStateError("ingestion job 不存在：%r" % job_id)
    if job["state"] != WAITING:
        return {"outcome": "waiting" if job["state"] in ACTIVE_UPLOAD_STATES
                else "terminal", "reason": None, "job": job}
    # 等待超时：24h 绝对期限（自 created_at，不因任何操作延长）；
    # epoch 比较放 SQL 侧与 now() 同钟（<= now 即超期）。
    cur.execute(
        "SELECT %s <= EXTRACT(EPOCH FROM now())::float8 AS overdue",
        (job["waiting_expires_at"],))
    if cur.fetchone()["overdue"]:
        cur.execute(
            "UPDATE ingestion_jobs SET state=%s, fail_code="
            "'waiting_timeout', terminal_at=now(), updated_at=now() "
            "WHERE job_id=%s AND state=%s", (EXPIRED, job_id, WAITING))
        _append_event(cur, job_id, "expired", {"reason": "waiting_timeout"})
        cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s", (job_id,))
        return {"outcome": "terminal", "reason": "waiting_timeout",
                "job": _norm_row(cur.fetchone())}
    if not disk_watermark_ok:
        raise _AdmissionDeferred("disk_watermark")
    per_identity, global_active = _active_counts(cur, job["owner_user_id"])
    if per_identity >= cos_config.COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY:
        raise _AdmissionDeferred("identity_active_limit")
    if global_active >= cos_config.COS_MAX_ACTIVE_UPLOADS_GLOBAL:
        raise _AdmissionDeferred("global_active_limit")

    reservation_id = None
    quota_applies = (job["owner_role"] == "user"
                     and bool(job["owner_user_id"]))
    if quota_applies:
        try:
            res = upload_guard.reserve_upload_locked(
                cur, job["owner_user_id"], job["declared_size"])
            reservation_id = res["reservation_id"]
        except upload_guard.QuotaExceeded:
            # §6.1：准入时已不满足本地配额 → 终止（不接触 COS）
            cur.execute(
                "UPDATE ingestion_jobs SET state=%s, fail_code="
                "'local_quota_infeasible', terminal_at=now(), updated_at=now() "
                "WHERE job_id=%s AND state=%s", (CANCELLED, job_id, WAITING))
            _append_event(cur, job_id, "cancelled",
                          {"reason": "local_quota_infeasible"})
            cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
            return {"outcome": "terminal", "reason": "local_quota_infeasible",
                    "job": _norm_row(cur.fetchone())}
        except (upload_guard.InflightLimitExceeded,
                upload_guard.RateLimitExceeded):
            raise _AdmissionDeferred("local_reservation_transient")

    # 池行最后锁；失败整体回滚（本地预占同时撤销——同一事务）
    pool = cos_pool_store.lock_pool(cur)
    if cos_pool_store.admission_paused(pool):
        raise _AdmissionDeferred("cos_capacity_reconcile_required")
    try:
        cos_pool_store.reserve_locked(cur, pool, job["declared_size"])
    except cos_pool_store.PoolExhausted:
        raise _AdmissionDeferred("pool_capacity")
    try:
        cur.execute(
            "UPDATE ingestion_jobs SET state=%s, pool_reserved_bytes=%s, "
            "capacity_admitted_at=now(), job_deadline_at=now() + "
            "make_interval(secs => %s), local_reservation_id=%s, updated_at=now() "
            "WHERE job_id=%s AND state=%s",
            (PREPARING, job["declared_size"],
             cos_config.COS_JOB_MAX_AGE_SECONDS, reservation_id, job_id, WAITING))
    except psycopg.errors.UniqueViolation as exc:
        if (getattr(exc.diag, "constraint_name", "") or "") == \
                "ingestion_jobs_one_active_per_owner":
            raise _AdmissionDeferred("identity_active_limit")
        raise
    _append_event(cur, job_id, "admitted", {
        "pool_reserved_bytes": job["declared_size"],
        "local_quota_reserved": quota_applies})
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s", (job_id,))
    return {"outcome": "admitted", "reason": None,
            "job": _norm_row(cur.fetchone())}


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_job(job_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
            return _norm_row(cur.fetchone())
    finally:
        conn.close()


def get_job_locked(cur, job_id):
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s FOR UPDATE",
                (job_id,))
    return _norm_row(cur.fetchone())


def list_jobs_for_owner(owner_user_id, *, limit=50):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ingestion_jobs WHERE owner_user_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (owner_user_id, limit))
            return [_norm_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def queue_position(job_id):
    """当前排队位置（0 = 队首）。0 基；非 waiting 任务返回 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int AS n FROM ingestion_jobs j "
                "WHERE j.state=%s AND (j.created_at, j.job_id) < "
                "(SELECT w.created_at, w.job_id FROM ingestion_jobs w "
                " WHERE w.job_id=%s)",
                (WAITING, job_id))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _require_transition(job, new_state):
    legal = LEGAL_TRANSITIONS.get(job["state"], frozenset())
    if new_state not in legal:
        raise IngestionStateError(
            "非法状态转换：%s → %s（job %s）" %
            (job["state"], new_state, job["job_id"]))


# --------------------------------------------------------------------------- #
# worker 租约与 CAS 转移
# --------------------------------------------------------------------------- #
def claim_next_job_for_worker(states, lease_seconds=None, holding_token=None):
    """SKIP LOCKED 领取一条可处理任务（lease/generation fencing）。

    可领取：state ∈ states 且（无租约、租约已过期，或当前租约 token ==
    holding_token——同一 worker 连续跨阶段推进自己的任务，不算抢锁）。
    领取即 worker_generation += 1（fencing token，单调递增）；旧
    generation 的收口一律 StaleLease——调用方必须始终使用自己最近一次
    领取返回的 generation。
    """
    lease = (cos_config.COS_WORKER_LEASE_SECONDS if lease_seconds is None
             else int(lease_seconds))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ingestion_jobs WHERE state = ANY(%s) AND "
                    "(worker_lease_expires_at IS NULL OR "
                    " worker_lease_expires_at <= now() "
                    "OR worker_lease_token = %s) "
                    "ORDER BY created_at, job_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (list(states), holding_token or ""))
                row = cur.fetchone()
                if row is None:
                    return None
                token = "wl_" + secrets.token_hex(12)
                cur.execute(
                    "UPDATE ingestion_jobs SET worker_lease_token=%s, "
                    "worker_lease_expires_at=now() + make_interval(secs => %s), "
                    "worker_generation = worker_generation + 1, updated_at=now() "
                    "WHERE job_id=%s", (token, lease, row["job_id"]))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (row["job_id"],))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


class _WorkerCas:
    """worker 收口的公共外壳：job 行锁 → generation CAS → 合法性 → 转移。"""

    def __init__(self, job_id, generation, expect_states, new_state):
        self.job_id, self.generation = job_id, generation
        self.expect_states, self.new_state = expect_states, new_state

    def __enter__(self):
        raise NotImplementedError  # 由 _worker_transition 实现使用


def _worker_transition(job_id, generation, expect_states, new_state,
                       extra_sql, extra_args, event_kind, event_detail):
    """worker CAS 转移（锁 job 行 → 校验 generation/状态 → 转移 + 事件）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease(
                        "generation 过期（%s != 当前 %s）——旧 worker 收口被拒"
                        % (generation, job["worker_generation"]))
                if job["state"] not in expect_states:
                    raise IngestionStateError(
                        "状态不符：%s 不在 %s（job %s）" %
                        (job["state"], sorted(expect_states), job_id))
                _require_transition(job, new_state)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now()" +
                    extra_sql + " WHERE job_id=%s",
                    (new_state,) + tuple(extra_args) + (job_id,))
                _append_event(cur, job_id, event_kind, event_detail)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_begin_uploading(job_id, generation, *, bucket, object_key,
                           upload_id, part_plan):
    """worker 完成 InitiateMultipartUpload：preparing → uploading。

    key/uploadId/分块计划自此冻结（浏览器不可影响）。part_plan 为
    [{part_number, offset, length}]，按 declared_size 与 COS_PART_BYTES
    计算后传入；此处校验计划总长与 declared 一致（不信任调用方拼装）。
    """
    total = sum(int(p["length"]) for p in part_plan)
    nums = sorted(int(p["part_number"]) for p in part_plan)
    if total <= 0 or nums != list(range(1, len(nums) + 1)):
        raise IngestionStateError("分块计划非法（总长 %s / 编号 %s…）"
                                  % (total, nums[:3]))
    return _worker_transition(
        job_id, generation, {PREPARING}, UPLOADING,
        ", bucket=%s, object_key=%s, upload_id=%s, part_plan_json=%s",
        (bucket, object_key, upload_id, json.dumps(part_plan)),
        "upload_started",
        {"bucket": bucket, "object_key": object_key, "parts": len(part_plan)})


def worker_back_to_uploading(job_id, generation, *, reason):
    """completing → uploading：ListParts 核验缺块，回浏览器续传（§4 resume）。"""
    return _worker_transition(
        job_id, generation, {COMPLETING}, UPLOADING, "",
        (), "complete_rejected", {"reason": reason})


def worker_pin_source(job_id, generation, *, version_id, etag, size_bytes):
    """worker ListParts 核对 + Complete + HEAD 后钉源：completing → queued。"""
    return _worker_transition(
        job_id, generation, {COMPLETING}, QUEUED,
        ", cos_version_id=%s, source_etag=%s, source_size_bytes=%s",
        (version_id, etag, int(size_bytes)),
        "source_pinned",
        {"version_bound": True, "size_bytes": int(size_bytes)})


def worker_begin_download(job_id, generation):
    return _worker_transition(
        job_id, generation, {QUEUED}, DOWNLOADING, "", (),
        "download_started", None)


def worker_download_retry(job_id, generation):
    """downloading → queued：中断回队（checkpoint 持久，重试不重传已确认段）。"""
    return _worker_transition(
        job_id, generation, {DOWNLOADING}, QUEUED, "", (),
        "download_retry", None)


def worker_update_download_progress(job_id, generation, *, downloaded_bytes,
                                    checkpoint, wire_delta, logical_delta):
    """CAS 进度更新（不改状态）。checkpoint 为可 JSON 序列化 dict。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（进度更新被拒）")
                if job["state"] != DOWNLOADING:
                    raise IngestionStateError(
                        "进度更新要求 downloading（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET downloaded_bytes=%s, "
                    "download_checkpoint_json=%s, wire_download_bytes = "
                    "wire_download_bytes + %s, logical_download_bytes = "
                    "logical_download_bytes + %s, updated_at=now() "
                    "WHERE job_id=%s",
                    (int(downloaded_bytes), json.dumps(checkpoint),
                     int(wire_delta), int(logical_delta), job_id))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_persist_commit_intent(job_id, generation, intent):
    """validating 内、原子提升之前持久化 commit intent（§4 提交恢复栅栏）。

    intent 至少含 target 路径、source version、SHA-256（由 worker 组装）；
    重启后 reconciler 按 intent 幂等补齐提升/metadata/配额结算。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（commit intent 被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "commit intent 要求 validating（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET commit_intent_json=%s, "
                    "commit_started_at=now(), updated_at=now() WHERE job_id=%s",
                    (json.dumps(intent), job_id))
                _append_event(cur, job_id, "commit_intent_persisted",
                              {"target": intent.get("target")})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_settle_ready(job_id, generation, *, slide_canonical_name,
                        sha256_actual, settle_bytes):
    """validating → ready：本地提升+metadata+配额一次结算（同事务）。

    锁序 job → reservation → quota（consume_reservation_locked 内）。
    cleanup 转为 pending（远端对象待删，§6.2 正常路径）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（结算被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "结算要求 validating（当前 %s）" % job["state"])
                if not job.get("commit_intent_json"):
                    raise IngestionStateError(
                        "结算前必须已持久化 commit intent（§4 提交恢复栅栏）")
                if job.get("local_reservation_id"):
                    upload_guard.consume_reservation_locked(
                        cur, job["local_reservation_id"], int(settle_bytes))
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, local_ready_at=now(), "
                    "slide_canonical_name=%s, sha256_actual=%s, "
                    "cleanup_status=%s, updated_at=now() WHERE job_id=%s",
                    (READY, slide_canonical_name, sha256_actual,
                     CLEANUP_PENDING, job_id))
                _append_event(cur, job_id, "local_ready", {
                    "slide": slide_canonical_name, "settle_bytes":
                    int(settle_bytes)})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_begin_validating(job_id, generation):
    return _worker_transition(
        job_id, generation, {DOWNLOADING}, VALIDATING, "", (),
        "download_complete", None)


def worker_mark_viewer_ready(job_id, generation):
    """ready → completed：readiness probe 通过后 CAS 置 viewer_ready（§4）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（readiness 收口被拒）")
                if job["state"] == COMPLETED and job["viewer_ready"]:
                    return _norm_row(job)  # 幂等
                if job["state"] != READY:
                    raise IngestionStateError(
                        "viewer_ready 要求 ready（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, viewer_ready=true, "
                    "terminal_at=now(), updated_at=now() WHERE job_id=%s",
                    (COMPLETED, job_id))
                _append_event(cur, job_id, "viewer_ready", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_note_readiness_retry(job_id, generation, *, error, next_retry_at):
    """readiness 暂时失败：保持 ready，持久化错误/重试点（§4，不重下载）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（readiness 记录被拒）")
                if job["state"] != READY:
                    raise IngestionStateError(
                        "readiness 重试要求 ready（当前 %s）" % job["state"])
                _append_event(cur, job_id, "readiness_retry",
                              {"error": str(error)[:200]})
                cur.execute("SELECT 1")
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 浏览器侧操作（无 generation；API 层已验 owner/CSRF）
# --------------------------------------------------------------------------- #
def request_upload_complete(job_id):
    """幂等记录「浏览器侧完成」：uploading → completing（§4）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (COMPLETING, QUEUED, DOWNLOADING,
                                    VALIDATING, READY, COMPLETED):
                    return _norm_row(job)  # 幂等：已进入后继阶段
                if job["state"] != UPLOADING:
                    raise IngestionStateError(
                        "upload-complete 要求 uploading（当前 %s）" % job["state"])
                _require_transition(job, COMPLETING)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now() "
                    "WHERE job_id=%s", (COMPLETING, job_id))
                _append_event(cur, job_id, "browser_complete_requested", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def request_resume(job_id):
    """请 worker 刷新可信 ListParts：completing → uploading 的 worker 侧入口。

    浏览器 resume 接口在 uploading/completing 态均可调用；completing 且
    worker 未收口时回 uploading 让浏览器按可信 ListParts 续传。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] == UPLOADING:
                    return _norm_row(job)
                if job["state"] != COMPLETING:
                    raise IngestionStateError(
                        "resume 要求 uploading/completing（当前 %s）"
                        % job["state"])
                _require_transition(job, UPLOADING)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now() "
                    "WHERE job_id=%s", (UPLOADING, job_id))
                _append_event(cur, job_id, "resume_requested", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def record_sign_batch(job_id, part_numbers):
    """parts/sign 速率记账：每任务每分钟批次上限（§6.3 签名速率硬停）。

    advisory-xact-lock per job 串行化并发签发请求的计数。返回 True=放行。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (job_id,))
                cur.execute(
                    "SELECT COUNT(*)::int AS n FROM ingestion_events "
                    "WHERE job_id=%s AND kind='part_sign' AND created_at > "
                    "now() - interval '60 seconds'", (job_id,))
                if int(cur.fetchone()["n"]) >= \
                        cos_config.COS_SIGN_BATCHES_PER_MINUTE:
                    return False
                _append_event(cur, job_id, "part_sign",
                              {"parts": sorted(int(n) for n in part_numbers)})
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 取消 / 失败 / 超期（调度器）
# --------------------------------------------------------------------------- #
def cancel_job(job_id, *, reason_code="cancelled_by_user"):
    """幂等取消（§4 cancel）。已入库 ready/completed → IngestionStateError。

    锁序 job → reservation → quota（释放本地预占）；pool_reserved 不动——
    确认远端清理完成后由 finalize_cleanup 释放（§6.2）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (READY, COMPLETED):
                    raise IngestionStateError(
                        "已本地入库（%s）——删除走既有切片删除合同，不上传取消"
                        % job["state"])
                if job["state"] in TERMINAL_STATES:
                    return _norm_row(job)  # 幂等
                _require_transition(job, CANCELLED)
                if job.get("local_reservation_id"):
                    upload_guard.release_reservation_locked(
                        cur, job["local_reservation_id"])
                cleanup = (CLEANUP_PENDING
                           if job["pool_reserved_bytes"] > 0 or
                           job.get("upload_id") or job.get("object_key")
                           else CLEANUP_NONE)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code=%s, "
                    "terminal_at=now(), cleanup_status=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (CANCELLED, reason_code, cleanup, job_id))
                _append_event(cur, job_id, "cancelled",
                              {"reason": reason_code})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def fail_job(job_id, generation, code):
    """worker 终态失败（释放本地预占，远端清理转 pending）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（fail 收口被拒）")
                if job["state"] in TERMINAL_STATES:
                    return _norm_row(job)
                _require_transition(job, FAILED)
                if job.get("local_reservation_id"):
                    upload_guard.release_reservation_locked(
                        cur, job["local_reservation_id"])
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code=%s, "
                    "terminal_at=now(), cleanup_status=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (FAILED, code, CLEANUP_PENDING, job_id))
                _append_event(cur, job_id, "failed", {"reason": code})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def sweep_expired_waiting():
    """批量超期：waiting 且 waiting_expires_at <= now → expired（可重建）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code="
                    "'waiting_timeout', terminal_at=now(), updated_at=now() "
                    "WHERE state=%s AND waiting_expires_at <= now() "
                    "RETURNING job_id", (EXPIRED, WAITING))
                ids = [r["job_id"] for r in cur.fetchall()]
                for jid in ids:
                    _append_event(cur, jid, "expired",
                                  {"reason": "waiting_timeout"})
                return ids
    finally:
        conn.close()


def sweep_expired_jobs():
    """批量超期：已准入未提交且 job_deadline_at <= now → cancelled+清理。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id FROM ingestion_jobs WHERE state = ANY(%s) "
                    "AND job_deadline_at <= now() "
                    "ORDER BY job_deadline_at",
                    (list(ACTIVE_UPLOAD_STATES),))
                ids = [r["job_id"] for r in cur.fetchall()]
                for jid in ids:
                    job = get_job_locked(cur, jid)
                    if job["state"] in TERMINAL_STATES:
                        continue
                    if job.get("local_reservation_id"):
                        upload_guard.release_reservation_locked(
                            cur, job["local_reservation_id"])
                    cur.execute(
                        "UPDATE ingestion_jobs SET state=%s, fail_code="
                        "'job_max_age', terminal_at=now(), cleanup_status=%s, "
                        "updated_at=now() WHERE job_id=%s",
                        (CANCELLED, CLEANUP_PENDING, jid))
                    _append_event(cur, jid, "cancelled",
                                  {"reason": "job_max_age"})
                return ids
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 容量调度器：续租 / 恢复 / FIFO 准入
# --------------------------------------------------------------------------- #
def renew_active_local_reservations():
    """§6.3 常驻续租：浏览器无请求期间维持本地预约。

    每任务短事务（锁序 job → reservation → quota）：

    - 状态仍活跃且未超绝对期限 → renew（TTL 后移）；
    - 预约已过期（不复活）→ 恢复流程：重新预占（新 rid）；QuotaExceeded →
      终止并清理（fail_code=local_reservation_lost）；
    - ready/completed 已结算 → 跳过（不再续）。

    续租**不延长** waiting/job 绝对期限（两者由 sweep 独立强制）。
    返回 {job_id: renewed|recovered|terminated|skipped}。
    """
    results = {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM ingestion_jobs WHERE state = ANY(%s) "
                "AND local_reservation_id IS NOT NULL "
                "ORDER BY created_at", (list(ACTIVE_UPLOAD_STATES),))
            ids = [r["job_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for jid in ids:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    job = get_job_locked(cur, jid)
                    if job is None or job["state"] not in ACTIVE_UPLOAD_STATES:
                        results[jid] = "skipped"
                        continue
                    row = upload_guard.renew_reservation_locked(
                        cur, job["local_reservation_id"])
                    if row is None:
                        results[jid] = "skipped"
                        continue
                    if upload_guard.reservation_is_active(row):
                        results[jid] = "renewed"
                        continue
                    if row.get("state") != "reserved":
                        # 已 consumed/released：状态机不应出现（结算后 state
                        # 已离开活跃集）——fail-closed 记事件待查
                        _append_event(cur, jid, "reservation_state_conflict",
                                      {"observed": row.get("state")})
                        results[jid] = "skipped"
                        continue
                    # 过期未复活 → 恢复流程重新预占（§6.3）
                    try:
                        res = upload_guard.reserve_upload_locked(
                            cur, job["owner_user_id"], job["declared_size"])
                        cur.execute(
                            "UPDATE ingestion_jobs SET "
                            "local_reservation_id=%s, updated_at=now() "
                            "WHERE job_id=%s",
                            (res["reservation_id"], jid))
                        _append_event(cur, jid, "local_reservation_recovered",
                                      None)
                        results[jid] = "recovered"
                    except upload_guard.QuotaExceeded:
                        upload_guard.release_reservation_locked(
                            cur, job["local_reservation_id"])
                        cur.execute(
                            "UPDATE ingestion_jobs SET state=%s, fail_code="
                            "'local_reservation_lost', terminal_at=now(), "
                            "cleanup_status=%s, updated_at=now() "
                            "WHERE job_id=%s",
                            (CANCELLED, CLEANUP_PENDING, jid))
                        _append_event(cur, jid, "cancelled",
                                      {"reason": "local_reservation_lost"})
                        results[jid] = "terminated"
        finally:
            conn.close()
    return results


def admit_waiting_fifo(*, disk_watermark_ok=True, max_admissions=1):
    """FIFO 准入扫描（§6.1：按 created_at,id；首发不允许后来者插队）。

    队首因「所属身份已有活跃任务」被跳过时继续看下一条（该原因属于
    队首自己的暂态占用，不阻塞全局）；队首因容量/对账暂停/水位暂缓时
    停止扫描（后面的任务同样过不了这两关）。每轮最多准入
    max_admissions 条（调度器串行推进，防一次放空池）。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM ingestion_jobs WHERE state=%s "
                "ORDER BY created_at, job_id", (WAITING,))
            waiting = [r["job_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    admitted = []
    for jid in waiting:
        if len(admitted) >= max_admissions:
            break
        out = try_admit_job(jid, disk_watermark_ok=disk_watermark_ok)
        if out["outcome"] == "admitted":
            admitted.append(jid)
        elif out["reason"] in ("identity_active_limit",):
            continue
        else:
            # pool_capacity / reconcile_required / disk_watermark /
            # global_active_limit / local_quota_*：停止本轮
            break
    return admitted


# --------------------------------------------------------------------------- #
# 远端清理器（§6.2；独立 lease/fencing，cleanup_* 独立于主状态）
# --------------------------------------------------------------------------- #
def claim_cleanup_job(lease_seconds=None):
    """领取一条待清理任务（pending 且重试点已到；SKIP LOCKED）。

    cleanup lease 独立于 worker lease；attempts 在领取时递增。
    """
    lease = (cos_config.COS_WORKER_LEASE_SECONDS if lease_seconds is None
             else int(lease_seconds))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ingestion_jobs WHERE cleanup_status=%s AND "
                    "(cleanup_next_retry_at IS NULL OR "
                    " cleanup_next_retry_at <= now()) AND "
                    "(cleanup_lease_expires_at IS NULL OR "
                    " cleanup_lease_expires_at <= now()) "
                    "ORDER BY terminal_at NULLS FIRST, local_ready_at "
                    "NULLS FIRST, created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (CLEANUP_PENDING,))
                row = cur.fetchone()
                if row is None:
                    return None
                token = "cl_" + secrets.token_hex(12)
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_lease_token=%s, "
                    "cleanup_lease_expires_at=now() + "
                    "make_interval(secs => %s), cleanup_attempts = "
                    "cleanup_attempts + 1, updated_at=now() WHERE job_id=%s",
                    (token, lease, row["job_id"]))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (row["job_id"],))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def finalize_cleanup(job_id, cleanup_token):
    """清理确认完成：pool 预约释放 + cleanup_status=cleaned（§6.2）。

    调用前提（worker 保证）：确切 key+versionId 已删、历史版本已删、
    未完成 multipart 已 Abort，且分页复核远端无残留——「一次列表为空」
    不构成提前释放的充分条件。锁序 job → pool。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["cleanup_status"] == CLEANUP_CLEANED:
                    return _norm_row(job)  # 幂等
                if not cleanup_token or job.get("cleanup_lease_token") != \
                        cleanup_token:
                    raise StaleLease("cleanup lease 不符（清理收口被拒）")
                released = job["pool_reserved_bytes"]
                if released > 0:
                    cos_pool_store.release_locked(cur, released)
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_status=%s, "
                    "pool_reserved_bytes=0, cleanup_lease_token=NULL, "
                    "cleanup_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s", (CLEANUP_CLEANED, job_id))
                _append_event(cur, job_id, "cleaned",
                              {"released_pool_bytes": released})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def record_cleanup_failure(job_id, cleanup_token, error):
    """清理失败：指数退避重试；超上限转 failed（保持 pool 预约，告警待人工）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if not cleanup_token or job.get("cleanup_lease_token") != \
                        cleanup_token:
                    raise StaleLease("cleanup lease 不符（失败记录被拒）")
                attempts = int(job.get("cleanup_attempts") or 0)
                status = (CLEANUP_FAILED
                          if attempts >= cos_config.COS_CLEANUP_MAX_ATTEMPTS
                          else CLEANUP_PENDING)
                delay = cos_config.COS_CLEANUP_RETRY_BASE_SECONDS * \
                    (2 ** max(0, attempts - 1))
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_status=%s, "
                    "cleanup_last_error=%s, cleanup_next_retry_at=now() + "
                    "make_interval(secs => %s), cleanup_lease_token=NULL, "
                    "cleanup_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s",
                    (status, str(error)[:300], delay, job_id))
                _append_event(cur, job_id,
                              "cleanup_exhausted" if status == CLEANUP_FAILED
                              else "cleanup_retry",
                              {"attempts": attempts})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def waiting_and_holding_counts():
    """调度/状态接口聚合：waiting 数与仍持池预约数。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE state=%s)::int AS waiting, "
                "COUNT(*) FILTER (WHERE cleanup_status IN (%s,%s))::int AS "
                "cleanup_backlog FROM ingestion_jobs",
                (WAITING, CLEANUP_PENDING, CLEANUP_FAILED))
            row = cur.fetchone()
            return {"waiting": int(row["waiting"]),
                    "cleanup_backlog": int(row["cleanup_backlog"])}
    finally:
        conn.close()


def release_worker_lease(job_id, token):
    """worker 瞬态失败后主动释放租约（不等 TTL），下轮立即重试。

    CAS：token 必须匹配当前租约；generation 不变（没有收口就没有新纪元）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    return False
                if job.get("worker_lease_token") != token:
                    return False
                cur.execute(
                    "UPDATE ingestion_jobs SET worker_lease_token=NULL, "
                    "worker_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s", (job_id,))
                return True
    finally:
        conn.close()
