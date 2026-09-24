# -*- coding: utf-8 -*-
"""cos_pool_state 单行账本（COS 直传 Phase 1，合同 §6.1）。

容量准入必须在一个 PG 事务内 ``SELECT ... FOR UPDATE`` 锁本行并预约；
禁止进程内变量或先查后写。锁序（与 migrations/0066 头注释一致）：

- 创建/准入（不锁既有 job 行）：upload_user_quotas 行 → cos_pool_state 行；
- 已有 job 的收口：ingestion_jobs 行 → upload_reservations 行 →
  upload_user_quotas 行 → cos_pool_state 行（本模块只管最后一跳）；
- cos_pool_state 恒为最后取得的全局锁；持有期间不回头等其它行。

本模块不感知 ingestion_jobs 语义（状态机在 ingestion_store）；
只提供「锁行 → 校验算术 → 预约/释放/观测」的原语。json/dual 后端
fail-closed：调用方必须确认 STORAGE_BACKEND=postgres 才可用。
"""

import psycopg

import cos_config
import pg_store


class PoolExhausted(Exception):
    """capacity 不足以容纳 declared_size（调用方转 waiting_capacity）。"""


class PoolMisconfigured(RuntimeError):
    """池行缺失/配置非法——fail-fast，不得猜测默认值继续。"""


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def ensure_pool_state(conn=None):
    """确保池行存在且与 env 容量配置一致（应用启动/测试夹具调用）。

    返回行 dict。env 的 capacity/safety 与库行不一致时以 env 为准更新
    （运维调整容量的唯一入口；reserved/observed 不动）。独立短事务。
    """
    own = conn is None
    if own:
        conn = _connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cos_pool_state (id, capacity_bytes, safety_bytes) "
                    "VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (cos_config.COS_POOL_CAPACITY_BYTES,
                     cos_config.COS_POOL_SAFETY_BYTES))
                cur.execute(
                    "UPDATE cos_pool_state SET capacity_bytes=%s, safety_bytes=%s, "
                    "updated_at=now() WHERE id=1 AND (capacity_bytes IS DISTINCT FROM %s "
                    "OR safety_bytes IS DISTINCT FROM %s)",
                    (cos_config.COS_POOL_CAPACITY_BYTES,
                     cos_config.COS_POOL_SAFETY_BYTES,
                     cos_config.COS_POOL_CAPACITY_BYTES,
                     cos_config.COS_POOL_SAFETY_BYTES))
                cur.execute("SELECT * FROM cos_pool_state WHERE id=1 FOR UPDATE")
                row = cur.fetchone()
    finally:
        if own:
            conn.close()
    if not row:
        raise PoolMisconfigured("cos_pool_state 单行缺失（0066 迁移未应用？）")
    if row["capacity_bytes"] <= 0 or not 0 <= row["safety_bytes"] < row["capacity_bytes"]:
        raise PoolMisconfigured(
            "cos_pool_state 非法配置：capacity=%s safety=%s"
            % (row["capacity_bytes"], row["safety_bytes"]))
    return dict(row)


def admission_limit_bytes(pool_row):
    """可预约上限 = capacity - safety（派生值，§6.1）。"""
    return pool_row["capacity_bytes"] - pool_row["safety_bytes"]


def lock_pool(cur):
    """锁池行并返回 dict（必须在既定锁序的最后一步调用）。"""
    cur.execute("SELECT * FROM cos_pool_state WHERE id=1 FOR UPDATE")
    row = cur.fetchone()
    if not row:
        raise PoolMisconfigured("cos_pool_state 单行缺失")
    return dict(row)


def reserve_locked(cur, pool_row, nbytes):
    """锁内预约 nbytes；容量不足抛 PoolExhausted（事务由调用方回滚）。

    算术（§6.1）：reserved + nbytes <= capacity - safety。nbytes 必须已过
    「declared_size <= 上限」的单文件判定（单文件判定在调用方，返回 422）。
    """
    limit = admission_limit_bytes(pool_row)
    if pool_row["reserved_bytes"] + nbytes > limit:
        raise PoolExhausted(
            "pool reserved=%s + %s > admission limit %s"
            % (pool_row["reserved_bytes"], nbytes, limit))
    cur.execute(
        "UPDATE cos_pool_state SET reserved_bytes = reserved_bytes + %s, "
        "updated_at=now() WHERE id=1", (nbytes,))
    pool_row["reserved_bytes"] += nbytes


def release_locked(cur, nbytes):
    """锁内释放预约（清理确认完成后调用；禁止释放成负数）。"""
    cur.execute(
        "UPDATE cos_pool_state SET reserved_bytes = "
        "GREATEST(reserved_bytes - %s, 0), updated_at=now() WHERE id=1",
        (nbytes,))


def admission_paused(pool_row):
    """对账 fail-closed：reconcile_status != 'ok' 或观测超池 → 暂停准入/签发。"""
    if pool_row["reconcile_status"] != "ok":
        return True
    observed = pool_row.get("observed_remote_bytes")
    if observed is not None and observed > pool_row["capacity_bytes"]:
        return True
    return False


def record_observation(cur, observed_bytes, drift_pause):
    """reconciler 写回实测占用与对账状态（独立短事务由调用方管理）。

    drift_pause=True 时置 reconcile_required（暂停新准入/新凭证，但下载/
    取消/清理继续）；False 且观测回池内且漂移收敛 → 恢复 ok。
    """
    cur.execute(
        "UPDATE cos_pool_state SET observed_remote_bytes=%s, "
        "reconcile_status = CASE WHEN %s THEN 'reconcile_required' "
        "WHEN %s <= capacity_bytes THEN 'ok' ELSE reconcile_status END, "
        "reconciled_at=now(), updated_at=now() WHERE id=1",
        (observed_bytes, drift_pause, observed_bytes))


def get_pool_state():
    """只读快照（状态接口/测试）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cos_pool_state WHERE id=1")
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
