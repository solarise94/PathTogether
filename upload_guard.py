# -*- coding: utf-8 -*-
"""上传资源防护（P0-A §3.3，docs/open-registration-security-remediation.md）。

三块职责：

1. **单请求字节上限（计数流）**：``UPLOAD_MAX_REQUEST_BYTES``。不信任
   ``Content-Length``（可缺省 / 可伪造），保存时逐块计数，超限立即停止并
   抛 :class:`RequestTooLarge`。Werkzeug 层的 ``MAX_CONTENT_LENGTH``（app.py
   接线，含少量 multipart 开销余量）负责第一层拦截，本模块的计数流是第二层
   权威——chunked / 伪造 Content-Length 都在上限处停止。
2. **磁盘保留水位**：``UPLOAD_RESERVED_FREE_BYTES``。写临时文件前与 ZIP
   解压过程中检查 ``UPLOAD_DIR`` 所在卷的可用空间，低于水位即拒绝
   （数据库与日志仍有安全余量）。
3. **PG 权威的用户配额 + reservation + 速率**（migrations/0013_upload_quotas.sql）：
   - ``reserve_upload``：单事务内 ``SELECT ... FOR UPDATE`` 锁配额行 → 惰性回收
     过期预占 → 在途数 / 每小时请求数判定 → ``used + reserved + n <= quota``
     条件判定后插入 reservation 并累加 reserved_bytes。同用户并发预占串行化，
     不会出现部分写或绕过；
   - ``topup_reservation``：ZIP 展开后实际总量超过预占时的原子补占；
   - ``release_reservation``（失败/超限释放）、``consume_reservation``
     （成功转实占：reserved → used）；
   - 每小时请求数上限以 reservation 行的 ``reserved_at`` 为计数源（计尝试
     次数，不论终态）；在途上限 = state='reserved' 且未过期的行数。

**后端适用范围（明确声明，不是静默退化）**：
  - 计数流与磁盘水位：纯进程内实现，json/dual/postgres 全部生效；
  - 配额 / 在途 / 每小时限流：仅 ``STORAGE_BACKEND=postgres`` 权威
    （platform_features.require_pg_backend fail-closed）。适用主体是
    ``role=user``（受邀账号，docs §3.3 的威胁模型）；owner 是运维者本人，
    不受限（owner 想自我约束时可直接向 upload_user_quotas 插行并改
    app 层判定）。AUTH_ENABLED=False 的本地免登录形态（user_id 为空）
    同样跳过——本地单机开发语义与现状一致。json 后端 + role=user 的
    上传按仓库既定哲学 fail-closed 返回 503（与 POST /login 在 json 后端
    503 同款），绝不退化进程内计数。

默认值依据（均 env 可调；标 [测] 的需上线前按真实 TCGA/MRXS 分布测
P95/P99 后复核，见模块尾部的 DEFAULTS_RATIONALE）。
"""

import os
import secrets
import shutil
import time

import pg_store
import platform_features

# --------------------------------------------------------------------------- #
# env 可调常量（import 期一次性读取；测试用 monkeypatch 改模块属性）
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: 单请求字节上限（含 zip 上传本体）。默认 10 GiB：TCGA SVS 单文件常见
#: 0.5–2.5 GiB、MRXS 连伴侣目录可达数 GiB，10 GiB 覆盖极大 specimen 且给
#: 边缘代理留出统一配置空间。[测] 上线前按真实分布 P99 收紧。
#: 注意：边缘代理（frp/nginx）的 body 上限必须与此值一致（docs §3.3-1）。
UPLOAD_MAX_REQUEST_BYTES = _env_int("UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)

#: 每用户空间配额字节上限（PG 权威，仅 role=user）。默认 20 GiB ≈ 2 个
#: 上限大小的整切片，够受邀账号做几次正常实验。[测] 按 deploy-host 可用磁盘
#: 与单用户合理保有量复核。
UPLOAD_USER_QUOTA_BYTES = _env_int("UPLOAD_USER_QUOTA_BYTES", 20 * 1024 ** 3)

#: 磁盘保留水位：UPLOAD_DIR 所在卷 free < 该值时拒绝新写入（507）。
#: 默认 20 GiB：需同时容纳 PG 数据目录、日志与解压暂存的并发余量。
#: [测] 按 deploy-host 磁盘规格与 PG/日志实际增速调整。
UPLOAD_RESERVED_FREE_BYTES = _env_int("UPLOAD_RESERVED_FREE_BYTES", 20 * 1024 ** 3)

#: 每用户在途上传数上限（state='reserved' 未过期）。默认 3：正常用户不会
#: 同时开 3 个以上大文件上传；防单账号并发铺满磁盘。
UPLOAD_MAX_INFLIGHT = max(1, _env_int("UPLOAD_MAX_INFLIGHT", 3))

#: 每用户每小时上传请求数上限（计尝试，不论成败）。默认 60：受邀协作
#: 场景远够用，同时把失败重试风暴压在每小时一个量级。
UPLOAD_HOURLY_REQUEST_LIMIT = max(1, _env_int("UPLOAD_HOURLY_REQUEST_LIMIT", 60))

#: reservation 过期秒数（防进程崩溃后预占永不释放）。默认 30 分钟：
#: 10 GiB 在 5 MB/s 慢链路上约需 35 分钟，取略宽上界；过期由下一次
#: reserve 惰性回收，不依赖后台任务。
UPLOAD_RESERVATION_TTL_SECONDS = _env_int("UPLOAD_RESERVATION_TTL_SECONDS", 1800)

#: Werkzeug MAX_CONTENT_LENGTH = 单请求上限 + multipart 开销余量（表单
#: 边界、字段名等）。真正的文件字节权威仍是计数流。
UPLOAD_MULTIPART_SLACK_BYTES = 1024 ** 2

#: 计数流读取块大小。
CHUNK_SIZE = 1024 * 1024


# --------------------------------------------------------------------------- #
# 业务异常（code 供路由映射稳定错误码）
# --------------------------------------------------------------------------- #
class UploadGuardError(Exception):
    """上传防护业务异常基类。"""

    code = "upload_guard_error"
    http_status = 400


class RequestTooLarge(UploadGuardError):
    """计数流超过单请求字节上限。"""

    code = "upload_too_large"
    http_status = 413


class DiskWatermarkExceeded(UploadGuardError):
    """磁盘可用空间低于保留水位。"""

    code = "disk_watermark_exceeded"
    http_status = 507


class QuotaExceeded(UploadGuardError):
    """用户空间配额不足（used + reserved + n > quota）。"""

    code = "upload_quota_exceeded"
    http_status = 413


class InflightLimitExceeded(UploadGuardError):
    """用户在途上传数达到上限。"""

    code = "upload_inflight_limit"
    http_status = 429


class RateLimitExceeded(UploadGuardError):
    """用户每小时上传请求数达到上限。"""

    code = "upload_rate_limited"
    http_status = 429


class ReservationInvalid(UploadGuardError):
    """reservation 不存在 / 已结算 / 已过期。"""

    code = "upload_reservation_invalid"
    http_status = 500


# --------------------------------------------------------------------------- #
# 1) 计数流 + 2) 磁盘水位（进程内，全后端生效）
# --------------------------------------------------------------------------- #
def save_limited(src_stream, dst_path, limit=None, chunk_size=CHUNK_SIZE):
    """把 src_stream 逐块复制到 dst_path，实际计数，超限立即停止。

    - 不参考任何声明长度（Content-Length / header），只信实读字节；
    - 超过 limit 抛 :class:`RequestTooLarge`，已写的一半文件由调用方清理
      （本函数抛错前尽量删除 dst_path，但调用方仍应兜底 unlink）；
    - 返回实际写入字节数。
    """
    limit = UPLOAD_MAX_REQUEST_BYTES if limit is None else int(limit)
    total = 0
    try:
        with open(dst_path, "wb") as dst:
            while True:
                chunk = src_stream.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RequestTooLarge(
                        "上传超过单请求字节上限（%d 字节）" % limit)
                dst.write(chunk)
    except Exception:
        try:
            os.unlink(dst_path)
        except OSError:
            pass
        raise
    return total


def check_disk_watermark(dir_path, need_bytes=0, reserved=None):
    """检查 dir_path 所在卷 free - need_bytes 是否仍高于保留水位。

    超水位抛 :class:`DiskWatermarkExceeded`。上传写临时文件前（need=上限的
    保守量或已知大小）与 ZIP 解压过程中（need=已展开量）调用。
    """
    reserved = UPLOAD_RESERVED_FREE_BYTES if reserved is None else int(reserved)
    free = shutil.disk_usage(str(dir_path)).free
    if free - int(need_bytes) < reserved:
        raise DiskWatermarkExceeded(
            "磁盘可用空间低于保留水位（free=%d, need=%d, reserved=%d）"
            % (free, int(need_bytes), reserved))


# --------------------------------------------------------------------------- #
# 3) PG 权威配额 / reservation / 速率
# --------------------------------------------------------------------------- #
def quota_features_available() -> bool:
    """配额 / 在途 / 每小时限流是否可用：仅 postgres。"""
    return platform_features.current_backend() == "postgres"


def quota_applies(ident) -> bool:
    """该身份是否需要走 PG 配额：role=user（受邀账号；owner/本地免登录跳过）。

    ident 为 app.current_identity() 形态的 dict（{"role", "user_id"}）。
    """
    return bool(ident) and ident.get("role") == "user" and bool(ident.get("user_id"))


def _connect():
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _reservation_out(row) -> dict:
    out = dict(row)
    for k in ("reserved_bytes", "settled_bytes"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    return out


def reservation_is_active(out) -> bool:
    """预占是否仍有效：state=reserved 且 expires_at 尚未到期。

    renew_reservation 对「已过期但尚未惰性回收」的行返回 state=reserved、
    expires_at 在过去——调用方必须用本函数判定，不能只看 state。
    """
    if not out or out.get("state") != "reserved":
        return False
    exp = out.get("expires_at")
    if exp is None:
        return False
    if hasattr(exp, "timestamp"):
        exp = exp.timestamp()
    try:
        return float(exp) > time.time()
    except (TypeError, ValueError):
        return False


def get_quota_row(user_id):
    """读取配额行（不存在则按 env 默认建行）；调试/测试用。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, UPLOAD_USER_QUOTA_BYTES))
                cur.execute(
                    "SELECT user_id, quota_bytes, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row["user_id"],
            "quota_bytes": int(row["quota_bytes"]),
            "used_bytes": int(row["used_bytes"]),
            "reserved_bytes": int(row["reserved_bytes"]),
        }
    finally:
        conn.close()


def reserve_upload(user_id, nbytes, *, inflight_limit=None, hourly_limit=None):
    """原子预占 nbytes 字节。返回 reservation dict。

    单事务内：锁配额行 → 惰性回收过期 reserved → 在途/每小时判定 →
    配额条件判定 → 插入 reservation + 累加 reserved_bytes。任一判定失败
    整体回滚（不会先扣一个维度再失败）。
    """
    nbytes = int(nbytes)
    if nbytes <= 0:
        raise ValueError("nbytes 需为正整数")
    inflight_limit = UPLOAD_MAX_INFLIGHT if inflight_limit is None else inflight_limit
    hourly_limit = (UPLOAD_HOURLY_REQUEST_LIMIT if hourly_limit is None
                    else hourly_limit)
    rid = "upr_" + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, UPLOAD_USER_QUOTA_BYTES))
                # 锁配额行：同用户并发 reserve 串行化（docs §3.3-3）
                cur.execute(
                    "SELECT quota_bytes, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas WHERE user_id = %s FOR UPDATE",
                    (user_id,))
                q = cur.fetchone()
                quota, used, reserved = (int(q["quota_bytes"]),
                                         int(q["used_bytes"]),
                                         int(q["reserved_bytes"]))
                # 惰性回收过期预占（进程崩溃兜底；锁内执行防并发双扣）
                cur.execute(
                    "SELECT COALESCE(SUM(reserved_bytes), 0) AS n "
                    "FROM upload_reservations "
                    "WHERE user_id = %s AND state = 'reserved' "
                    "AND expires_at <= now()", (user_id,))
                expired = int(cur.fetchone()["n"])
                if expired:
                    cur.execute(
                        "UPDATE upload_reservations SET state='released', "
                        "settled_at=now(), settled_bytes=0, updated_at=now() "
                        "WHERE user_id=%s AND state='reserved' "
                        "AND expires_at <= now()", (user_id,))
                    cur.execute(
                        "UPDATE upload_user_quotas SET reserved_bytes = "
                        "GREATEST(0, reserved_bytes - %s), updated_at=now() "
                        "WHERE user_id=%s", (expired, user_id))
                    reserved = max(0, reserved - expired)
                # 在途上限（回收后的真实在途数）
                cur.execute(
                    "SELECT COUNT(*)::int AS n FROM upload_reservations "
                    "WHERE user_id=%s AND state='reserved' AND expires_at > now()",
                    (user_id,))
                if int(cur.fetchone()["n"]) >= inflight_limit:
                    raise InflightLimitExceeded(
                        "在途上传数已达上限（%d）" % inflight_limit)
                # 每小时请求数上限（计尝试次数：不论终态的近 1 小时行数）
                cur.execute(
                    "SELECT COUNT(*)::int AS n FROM upload_reservations "
                    "WHERE user_id=%s AND reserved_at > now() - interval '1 hour'",
                    (user_id,))
                if int(cur.fetchone()["n"]) >= hourly_limit:
                    raise RateLimitExceeded(
                        "每小时上传请求数已达上限（%d）" % hourly_limit)
                # 配额条件判定
                if used + reserved + nbytes > quota:
                    raise QuotaExceeded(
                        "用户存储配额不足（quota=%d, used=%d, reserved=%d, "
                        "need=%d）" % (quota, used, reserved, nbytes))
                cur.execute(
                    "INSERT INTO upload_reservations "
                    "(reservation_id, user_id, reserved_bytes, state, "
                    " reserved_at, expires_at) "
                    "VALUES (%s, %s, %s, 'reserved', now(), "
                    " now() + make_interval(secs => %s))",
                    (rid, user_id, nbytes, UPLOAD_RESERVATION_TTL_SECONDS))
                cur.execute(
                    "UPDATE upload_user_quotas SET reserved_bytes = "
                    "reserved_bytes + %s, updated_at=now() WHERE user_id=%s",
                    (nbytes, user_id))
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (rid,))
                row = cur.fetchone()
        return _reservation_out(row)
    finally:
        conn.close()


def topup_reservation(reservation_id, extra_bytes):
    """ZIP 展开总量超过预占时的原子补占（reserved 条件加码）。

    锁序恒为 reservation → quota（reserve_upload 只锁 quota，无环）。
    配额不足抛 QuotaExceeded，整体回滚。
    """
    extra_bytes = int(extra_bytes)
    if extra_bytes <= 0:
        return get_reservation(reservation_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 过期判定放 SQL（expires_at 是 timestamptz，与 now() 同钟）
                cur.execute(
                    "SELECT user_id, reserved_bytes FROM upload_reservations "
                    "WHERE reservation_id=%s AND state='reserved' "
                    "AND expires_at > now() FOR UPDATE",
                    (reservation_id,))
                r = cur.fetchone()
                if r is None:
                    raise ReservationInvalid("预占不存在、已结算或已过期")
                cur.execute(
                    "SELECT quota_bytes, used_bytes, reserved_bytes "
                    "FROM upload_user_quotas WHERE user_id=%s FOR UPDATE",
                    (r["user_id"],))
                q = cur.fetchone()
                quota, used, reserved = (int(q["quota_bytes"]),
                                         int(q["used_bytes"]),
                                         int(q["reserved_bytes"]))
                if used + reserved + extra_bytes > quota:
                    raise QuotaExceeded(
                        "用户存储配额不足（需补占 %d 字节）" % extra_bytes)
                cur.execute(
                    "UPDATE upload_reservations SET reserved_bytes = "
                    "reserved_bytes + %s, expires_at = now() + "
                    "make_interval(secs => %s), updated_at=now() "
                    "WHERE reservation_id=%s",
                    (extra_bytes, UPLOAD_RESERVATION_TTL_SECONDS, reservation_id))
                cur.execute(
                    "UPDATE upload_user_quotas SET reserved_bytes = "
                    "reserved_bytes + %s, updated_at=now() WHERE user_id=%s",
                    (extra_bytes, r["user_id"]))
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (reservation_id,))
                row = cur.fetchone()
        return _reservation_out(row)
    finally:
        conn.close()


def get_reservation(reservation_id):
    """读取 reservation 行（不存在返回 None）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (reservation_id,))
                row = cur.fetchone()
        return _reservation_out(row) if row else None
    finally:
        conn.close()


def renew_reservation(reservation_id, ttl_seconds=None):
    """续租（Upload V2 §3.2.4）：把 reserved 预占的 expires_at 后移 now()+ttl。

    与 ``topup_reservation``（补字节）互补：本函数不改 reserved_bytes，只把
    ``UPLOAD_RESERVATION_TTL_SECONDS`` 的过期点整体后移，保证「任务活多久、
    预占保多久」——上传任务 TTL（默认 24h）远长于 reservation TTL（默认
    30min），不续租会让惰性回收把在途任务的额度释放掉，形成配额超占。

    单事务内 ``SELECT ... FOR UPDATE`` 锁 reservation 行后判定：

    - ``reserved`` 且未过期 → UPDATE expires_at（返回续租后的行）；
    - ``reserved`` 但已过期（尚未被惰性回收）→ **不复活**（此刻配额可能已被
      回收重分配，复活会双占），返回当前行，调用方据此判定预占失效；
    - ``consumed`` / ``released`` → 幂等 no-op，返回当前行；
    - 不存在 → None。
    """
    ttl = (UPLOAD_RESERVATION_TTL_SECONDS if ttl_seconds is None
           else int(ttl_seconds))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, reserved_bytes, state FROM "
                    "upload_reservations WHERE reservation_id=%s FOR UPDATE",
                    (reservation_id,))
                r = cur.fetchone()
                if r is None:
                    return None
                if r["state"] == "reserved":
                    # 过期判定放 SQL（与 now() 同钟，防应用/DB 时钟偏差）；
                    # 已过期（未被惰性回收）不复活——配额可能已被回收重分配。
                    cur.execute(
                        "UPDATE upload_reservations SET expires_at = now() + "
                        "make_interval(secs => %s), updated_at=now() "
                        "WHERE reservation_id=%s AND state='reserved' "
                        "AND expires_at > now()", (ttl, reservation_id))
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (reservation_id,))
                row = cur.fetchone()
        return _reservation_out(row)
    finally:
        conn.close()


def release_reservation(reservation_id):
    """失败释放：reserved → released，reserved_bytes 归还配额行。

    已 consumed/released 的行幂等 no-op（返回其状态）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, reserved_bytes, state FROM "
                    "upload_reservations WHERE reservation_id=%s FOR UPDATE",
                    (reservation_id,))
                r = cur.fetchone()
                if r is None:
                    return None
                if r["state"] != "reserved":
                    return {"reservation_id": reservation_id,
                            "state": r["state"]}
                cur.execute(
                    "UPDATE upload_reservations SET state='released', "
                    "settled_at=now(), settled_bytes=0, updated_at=now() "
                    "WHERE reservation_id=%s", (reservation_id,))
                cur.execute(
                    "UPDATE upload_user_quotas SET reserved_bytes = "
                    "GREATEST(0, reserved_bytes - %s), updated_at=now() "
                    "WHERE user_id=%s", (int(r["reserved_bytes"]), r["user_id"]))
        return {"reservation_id": reservation_id, "state": "released"}
    finally:
        conn.close()


def consume_reservation_locked(cur, reservation_id, actual_bytes):
    """在调用方已打开的事务/cursor 内转实占（与 upload_tasks 收口同事务）。

    幂等：已 consumed 的行再次 consume 返回现状，不重复累计。
    """
    actual_bytes = int(actual_bytes)
    cur.execute(
        "SELECT user_id, reserved_bytes, state FROM "
        "upload_reservations WHERE reservation_id=%s FOR UPDATE",
        (reservation_id,))
    r = cur.fetchone()
    if r is None:
        raise ReservationInvalid("预占不存在：%r" % reservation_id)
    if r["state"] == "consumed":
        return {"reservation_id": reservation_id, "state": "consumed"}
    if r["state"] != "reserved":
        raise ReservationInvalid(
            "预占已释放，不能转实占：%r" % reservation_id)
    cur.execute(
        "SELECT 1 FROM upload_reservations WHERE reservation_id=%s "
        "AND expires_at > now()", (reservation_id,))
    if cur.fetchone() is None:
        raise ReservationInvalid(
            "预占已过期，不能转实占：%r" % reservation_id)
    cur.execute(
        "UPDATE upload_reservations SET state='consumed', "
        "settled_at=now(), settled_bytes=%s, updated_at=now() "
        "WHERE reservation_id=%s", (actual_bytes, reservation_id))
    cur.execute(
        "UPDATE upload_user_quotas SET "
        "reserved_bytes = GREATEST(0, reserved_bytes - %s), "
        "used_bytes = used_bytes + %s, updated_at=now() "
        "WHERE user_id=%s",
        (int(r["reserved_bytes"]), actual_bytes, r["user_id"]))
    return {"reservation_id": reservation_id, "state": "consumed",
            "settled_bytes": actual_bytes}


def consume_reservation(reservation_id, actual_bytes):
    """成功转实占：reserved → consumed，reserved 转 used（按实际字节数）。

    幂等：已 consumed 的行再次 consume 返回现状，不重复累计。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return consume_reservation_locked(cur, reservation_id, actual_bytes)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 默认值依据汇总（上线前复核清单；详见各常量处注释）
# --------------------------------------------------------------------------- #
DEFAULTS_RATIONALE = {
    "UPLOAD_MAX_REQUEST_BYTES": (
        "10 GiB：覆盖 TCGA SVS（常见 0.5–2.5 GiB）与 MRXS+伴侣目录（数 GiB）"
        "的极大 specimen；上线前按真实分布 P99 收紧，且边缘代理 body 上限"
        "必须同步同值"),
    "UPLOAD_USER_QUOTA_BYTES": (
        "20 GiB ≈ 2 个上限大小切片：受邀账号正常实验量；按 deploy-host 可用磁盘"
        "复核"),
    "UPLOAD_RESERVED_FREE_BYTES": (
        "20 GiB：为 PG 数据目录、日志与解压暂存保留的余量；按磁盘规格与"
        "PG/日志实际增速复核"),
    "UPLOAD_MAX_INFLIGHT": "3：正常用户不会同时传 3 个以上大文件",
    "UPLOAD_HOURLY_REQUEST_LIMIT": "60：受邀协作场景远够用，压制重试风暴",
    "UPLOAD_RESERVATION_TTL_SECONDS": (
        "1800：10 GiB 在 5 MB/s 慢链路约 35 分钟的略宽上界；进程崩溃后由"
        "下一次 reserve 惰性回收"),
}
