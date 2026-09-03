# -*- coding: utf-8 -*-
"""跨 worker 登录防爆破存储原语（auth_rate_limits，docs §6.3 末段/§9.5）。

替代 per-worker 内存字典的 PostgreSQL 权威记录：

- **每账号**与**每规范化 IP 前缀**各一个独立计数器（若用账号×IP 复合键，僵尸
  网络对单账号撞库时每条记录都是 fresh 的，永远达不到阈值——故必须两桶独立）；
- 任一桶达到各自阈值即锁定并保存 ``locked_until``；两个 gunicorn worker 看到
  同一失败次数与锁定截止时间；UI 的剩余等待时间来自服务端权威值；
- 每次失败在**同一事务**内 UPSERT account 与 ip_prefix 两条记录（窗口过期重置
  计数）；成功登录只清该账号与来源 IP 前缀两条；
- 记录按 TTL 惰性清理（写入路径顺带 DELETE 已过期且未锁定的行）。

subject 只存带盐 hash（scope=account：规范化账号标识的 hash；scope=ip_prefix：
IP 前缀带盐 hash），绝不存明文账号或完整 IP。json/dual 后端 fail-closed
（platform_features.require_pg_backend）——存储不可用时上层应保守拒绝登录写
操作，不能退化为无防爆破。
"""

import math
import os
import time

import psycopg

import pg_store

# --------------------------------------------------------------------------- #
# 常量（顶部定义，可 env 覆盖；docs §9.5 建议值：IP 前缀 5 次/窗、账号 10 次/窗）
# --------------------------------------------------------------------------- #
def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


AUTH_ACCOUNT_FAILURE_LIMIT = _int_env("AUTH_ACCOUNT_FAILURE_LIMIT", 10)
AUTH_IP_FAILURE_LIMIT = _int_env("AUTH_IP_FAILURE_LIMIT", 5)
AUTH_WINDOW_SECONDS = _int_env("AUTH_WINDOW_SECONDS", 900)
AUTH_LOCK_SECONDS = _int_env("AUTH_LOCK_SECONDS", 900)

SCOPE_ACCOUNT = "account"
SCOPE_IP_PREFIX = "ip_prefix"

# --------------------------------------------------------------------------- #
# 注册限流桶（P0-B docs §4.5；复用 auth_rate_limits 的 (scope, subject_hash)
# 通用计数行，scope 取值域扩展，无需新表）：
#   - reg_ip_short ：每 IP 前缀 15 分钟 10 次**失败**；
#   - reg_ip_daily ：每 IP 前缀 24 小时 30 次**尝试**（成功也计）；
#   - reg_invite   ：每 invite token_hash 15 分钟 5 次失败短时锁定；
#   - reg_owner_min / reg_owner_day：owner 创建邀请码每分钟 / 每日上限。
# IP 桶只是辅闸（FRP 可信链未定，docs §4.5）；主闸是邀请码高熵 + 单事务一次性
# 消费。subject 一律带盐 hash，不存明文 IP / token。
# --------------------------------------------------------------------------- #
SCOPE_REG_IP_SHORT = "reg_ip_short"
SCOPE_REG_IP_DAILY = "reg_ip_daily"
SCOPE_REG_INVITE = "reg_invite"
SCOPE_REG_OWNER_MIN = "reg_owner_min"
SCOPE_REG_OWNER_DAY = "reg_owner_day"

REG_IP_SHORT_FAILURE_LIMIT = _int_env("REG_IP_SHORT_FAILURE_LIMIT", 10)
REG_IP_SHORT_WINDOW_SECONDS = _int_env("REG_IP_SHORT_WINDOW_SECONDS", 900)
REG_IP_SHORT_LOCK_SECONDS = _int_env("REG_IP_SHORT_LOCK_SECONDS", 900)
REG_IP_DAILY_ATTEMPT_LIMIT = _int_env("REG_IP_DAILY_ATTEMPT_LIMIT", 30)
REG_IP_DAILY_WINDOW_SECONDS = _int_env("REG_IP_DAILY_WINDOW_SECONDS", 86400)
REG_IP_DAILY_LOCK_SECONDS = _int_env("REG_IP_DAILY_LOCK_SECONDS", 86400)
REG_INVITE_FAILURE_LIMIT = _int_env("REG_INVITE_FAILURE_LIMIT", 5)
REG_INVITE_WINDOW_SECONDS = _int_env("REG_INVITE_WINDOW_SECONDS", 900)
REG_INVITE_LOCK_SECONDS = _int_env("REG_INVITE_LOCK_SECONDS", 900)
REG_OWNER_CREATE_PER_MINUTE = _int_env("REG_OWNER_INVITE_CREATE_PER_MINUTE", 10)
REG_OWNER_CREATE_PER_DAY = _int_env("REG_OWNER_INVITE_CREATE_PER_DAY", 100)


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _retry_after(locked_until_epoch, now) -> int:
    """权威剩余锁定秒数（向上取整；未锁/已过期为 0）。"""
    if locked_until_epoch is None:
        return 0
    remain = locked_until_epoch - now
    return int(math.ceil(remain)) if remain > 0 else 0


def _purge_expired(cur, now, window_seconds, lock_seconds):
    """惰性清理：窗口早已过期且未锁定的记录（写入路径顺带执行，短 TTL）。"""
    horizon = now - (window_seconds + lock_seconds)
    cur.execute(
        "DELETE FROM auth_rate_limits "
        "WHERE window_started_at < to_timestamp(%s) "
        "AND (locked_until IS NULL OR locked_until < to_timestamp(%s))",
        (horizon, now))


def _record_failure(cur, scope, subject_hash, limit, window_seconds,
                    lock_seconds, now) -> dict:
    """UPSERT 一个桶的失败计数（窗口过期重置），返回该桶状态。"""
    if limit <= 0:  # 阈值非法视为不设限（fail-open 只影响该桶，锁判定仍由另一桶兜底）
        return {"failed_count": 0, "locked": False, "locked_until": None,
                "retry_after_seconds": 0}
    cur.execute(
        "SELECT extract(epoch from window_started_at)::float8 AS ws, "
        "failed_count, extract(epoch from locked_until)::float8 AS locked_until "
        "FROM auth_rate_limits WHERE scope=%s AND subject_hash=%s FOR UPDATE",
        (scope, subject_hash))
    row = cur.fetchone()
    if row is None:
        failed_count = 1
        window_start = now
        locked_until = now + lock_seconds if failed_count >= limit else None
        cur.execute(
            "INSERT INTO auth_rate_limits "
            "(scope, subject_hash, window_started_at, failed_count, "
            " locked_until, updated_at) VALUES (%s,%s, to_timestamp(%s), %s, "
            "CASE WHEN %s THEN to_timestamp(%s) ELSE NULL END, now())",
            (scope, subject_hash, window_start, failed_count,
             locked_until is not None, locked_until))
    else:
        if row["ws"] is None or row["ws"] + window_seconds <= now:
            # 窗口过期：重置计数（新一轮从 1 起）
            failed_count = 1
            window_start = now
        else:
            failed_count = int(row["failed_count"]) + 1
            window_start = row["ws"]
        locked_until = row["locked_until"]
        if failed_count >= limit:
            candidate = now + lock_seconds
            # 已锁且截止更晚时保留原截止（不因窗口内继续失败而缩短）
            locked_until = (max(locked_until, candidate)
                            if locked_until is not None else candidate)
        cur.execute(
            "UPDATE auth_rate_limits SET window_started_at=to_timestamp(%s), "
            "failed_count=%s, locked_until=to_timestamp(%s), updated_at=now() "
            "WHERE scope=%s AND subject_hash=%s",
            (window_start, failed_count, locked_until, scope, subject_hash))
    retry = _retry_after(locked_until, now)
    return {
        "failed_count": failed_count,
        "locked": retry > 0,
        "locked_until": locked_until,
        "retry_after_seconds": retry,
    }


def record_auth_failure(account_hash, ip_prefix_hash,
                        account_limit=None, ip_limit=None,
                        window_seconds=None, lock_seconds=None):
    """记录一次登录失败：同事务 UPSERT account 与 ip_prefix 两条记录。

    任一桶达到阈值即置 locked_until=now+lock。返回结构化结果：
    ``{"locked": bool, "locked_until": epoch|None, "retry_after_seconds": int,
    "scopes": {"account": {...}, "ip_prefix": {...}}}``（未提供的桶不出现在
    scopes 中）。两个桶按固定顺序（account → ip_prefix）加锁，天然无死锁。
    """
    a_limit = AUTH_ACCOUNT_FAILURE_LIMIT if account_limit is None else int(account_limit)
    i_limit = AUTH_IP_FAILURE_LIMIT if ip_limit is None else int(ip_limit)
    w_sec = AUTH_WINDOW_SECONDS if window_seconds is None else int(window_seconds)
    l_sec = AUTH_LOCK_SECONDS if lock_seconds is None else int(lock_seconds)
    now = time.time()

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _purge_expired(cur, now, w_sec, l_sec)
                scopes = {}
                if account_hash:
                    scopes[SCOPE_ACCOUNT] = _record_failure(
                        cur, SCOPE_ACCOUNT, account_hash, a_limit, w_sec, l_sec,
                        now)
                if ip_prefix_hash:
                    scopes[SCOPE_IP_PREFIX] = _record_failure(
                        cur, SCOPE_IP_PREFIX, ip_prefix_hash, i_limit, w_sec,
                        l_sec, now)
        locked_until = None
        for s in scopes.values():
            lu = s.get("locked_until")
            if lu is not None and (locked_until is None or lu > locked_until):
                locked_until = lu
        return {
            "locked": _retry_after(locked_until, now) > 0,
            "locked_until": locked_until,
            "retry_after_seconds": _retry_after(locked_until, now),
            "scopes": scopes,
        }
    finally:
        conn.close()


def check_auth_locked(account_hash, ip_prefix_hash):
    """查询权威锁定状态：返回剩余锁定秒数（0=未锁）。

    ``{"locked": bool, "retry_after_seconds": int, "locked_until": epoch|None,
    "scopes": {...}}``；未提供的桶不出现在 scopes 中。
    """
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                scopes = {}
                for scope, subject_hash in (
                        (SCOPE_ACCOUNT, account_hash),
                        (SCOPE_IP_PREFIX, ip_prefix_hash)):
                    if not subject_hash:
                        continue
                    cur.execute(
                        "SELECT extract(epoch from locked_until)::float8 "
                        "AS locked_until FROM auth_rate_limits "
                        "WHERE scope=%s AND subject_hash=%s",
                        (scope, subject_hash))
                    row = cur.fetchone()
                    lu = row["locked_until"] if row is not None else None
                    retry = _retry_after(lu, now)
                    scopes[scope] = {
                        "locked": retry > 0,
                        "locked_until": lu,
                        "retry_after_seconds": retry,
                    }
        locked_until = None
        for s in scopes.values():
            lu = s.get("locked_until")
            if lu is not None and (locked_until is None or lu > locked_until):
                locked_until = lu
        retry = _retry_after(locked_until, now)
        return {
            "locked": retry > 0,
            "retry_after_seconds": retry,
            "locked_until": locked_until,
            "scopes": scopes,
        }
    finally:
        conn.close()


def clear_auth_failures(account_hash, ip_prefix_hash):
    """成功登录后清理该账号与来源 IP 前缀两条记录（不影响其他主体）。"""
    clauses = []
    params = []
    for scope, subject_hash in (
            (SCOPE_ACCOUNT, account_hash),
            (SCOPE_IP_PREFIX, ip_prefix_hash)):
        if subject_hash:
            clauses.append("(scope=%s AND subject_hash=%s)")
            params.extend((scope, subject_hash))
    if not clauses:
        return 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM auth_rate_limits WHERE "
                    + " OR ".join(clauses), params)
                return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 注册限流（P0-B docs §4.5）：全部 PostgreSQL 权威，多 worker 一致；
# 存储不可用时调用方 fail-closed 503，绝不退化进程内计数。
# --------------------------------------------------------------------------- #
def check_registration_locked(ip_prefix_hash, invite_hash=None):
    """查询注册限流锁定状态：返回剩余锁定秒数（0=未锁）。

    覆盖 reg_ip_short / reg_ip_daily / reg_invite（invite_hash 未给时跳过该桶）。
    """
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                retry = 0
                for scope, subject_hash in (
                        (SCOPE_REG_IP_SHORT, ip_prefix_hash),
                        (SCOPE_REG_IP_DAILY, ip_prefix_hash),
                        (SCOPE_REG_INVITE, invite_hash)):
                    if not subject_hash:
                        continue
                    cur.execute(
                        "SELECT extract(epoch from locked_until)::float8 "
                        "AS locked_until FROM auth_rate_limits "
                        "WHERE scope=%s AND subject_hash=%s",
                        (scope, subject_hash))
                    row = cur.fetchone()
                    if row is not None:
                        retry = max(retry, _retry_after(row["locked_until"], now))
        return retry
    finally:
        conn.close()


def record_registration_attempt(ip_prefix_hash):
    """记录一次注册 POST 尝试（24 小时桶，成功也计；达 30 次锁 24h）。

    返回锁定剩余秒数（0=未锁）。"""
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _purge_expired(cur, now, REG_IP_DAILY_WINDOW_SECONDS,
                               REG_IP_DAILY_LOCK_SECONDS)
                if not ip_prefix_hash:
                    return 0
                state = _record_failure(
                    cur, SCOPE_REG_IP_DAILY, ip_prefix_hash,
                    REG_IP_DAILY_ATTEMPT_LIMIT, REG_IP_DAILY_WINDOW_SECONDS,
                    REG_IP_DAILY_LOCK_SECONDS, now)
        return int(state.get("retry_after_seconds") or 0)
    finally:
        conn.close()


def record_registration_failure(ip_prefix_hash, invite_hash=None):
    """记录一次注册失败：IP 前缀短窗桶 + invite token_hash 桶（同事务）。

    任一桶达到阈值即锁定（返回锁定剩余秒数，0=未锁）。"""
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _purge_expired(cur, now, max(REG_IP_SHORT_WINDOW_SECONDS,
                                             REG_INVITE_WINDOW_SECONDS),
                               max(REG_IP_SHORT_LOCK_SECONDS,
                                   REG_INVITE_LOCK_SECONDS))
                retry = 0
                if ip_prefix_hash:
                    s = _record_failure(
                        cur, SCOPE_REG_IP_SHORT, ip_prefix_hash,
                        REG_IP_SHORT_FAILURE_LIMIT,
                        REG_IP_SHORT_WINDOW_SECONDS,
                        REG_IP_SHORT_LOCK_SECONDS, now)
                    retry = max(retry, int(s.get("retry_after_seconds") or 0))
                if invite_hash:
                    s = _record_failure(
                        cur, SCOPE_REG_INVITE, invite_hash,
                        REG_INVITE_FAILURE_LIMIT, REG_INVITE_WINDOW_SECONDS,
                        REG_INVITE_LOCK_SECONDS, now)
                    retry = max(retry, int(s.get("retry_after_seconds") or 0))
        return retry
    finally:
        conn.close()


def check_owner_invite_creation_locked(owner_hash):
    """owner 邀请码创建限流：达到每分钟/每日上限时返回剩余锁定秒数（0=可建）。"""
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                retry = 0
                for scope in (SCOPE_REG_OWNER_MIN, SCOPE_REG_OWNER_DAY):
                    cur.execute(
                        "SELECT extract(epoch from locked_until)::float8 "
                        "AS locked_until FROM auth_rate_limits "
                        "WHERE scope=%s AND subject_hash=%s",
                        (scope, owner_hash))
                    row = cur.fetchone()
                    if row is not None:
                        retry = max(retry, _retry_after(row["locked_until"], now))
        return retry
    finally:
        conn.close()


def record_owner_invite_creation(owner_hash):
    """记录一次邀请码创建（每分钟/每日两桶）；返回锁定剩余秒数（0=未锁）。"""
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _purge_expired(cur, now, 86400, 86400)
                retry = 0
                for scope, limit, window_sec, lock_sec in (
                        (SCOPE_REG_OWNER_MIN, REG_OWNER_CREATE_PER_MINUTE,
                         60, 60),
                        (SCOPE_REG_OWNER_DAY, REG_OWNER_CREATE_PER_DAY,
                         86400, 86400)):
                    s = _record_failure(
                        cur, scope, owner_hash, limit, window_sec, lock_sec,
                        now)
                    retry = max(retry, int(s.get("retry_after_seconds") or 0))
        return retry
    finally:
        conn.close()
