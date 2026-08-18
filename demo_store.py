# -*- coding: utf-8 -*-
"""匿名 Demo 存储原语（demo_sessions + demo_catalog，docs §5.1-§5.3/§9.3）。

demo_sessions：
  - capability 是短期、路由限定的授权（token 明文只在浏览器，库中只存 hash）；
  - 每浏览器默认 1 个主 run：``reserve_run`` 用 CAS
    ``UPDATE ... WHERE run_state='available' AND expires_at>now`` 原子预占，
    多标签页/双击并发只有一个成功；
    - reserved 默认 10 分钟过期，``reclaim_expired_runs`` 惰性回收；
    - consumed 不可 release（防误退款），与 budget_store.release 语义对齐；
    - 同 request_id 在途重放递增 ``rollback_epoch``，原请求 release CAS 失效；
    - ``count_ip_runs`` 按 ``ip_prefix_hash`` 统计窗口内 reserved/consumed
      （Demo run IP 桶；缺 hash 归 ``unknown``，不得绕过）。

demo_catalog：
  - owner allowlist，独立于 public 语义（public ≠ 互联网匿名可见）；
  - add 校验 slide 在 slides 表存在；remove 联动 ``revoke_by_slide``。

revoke_by_slide（切片下架/删除/移出目录，§9.3）：capability 立即失效
（expires_at 置 now，get_valid_capability 立刻 None）；未完成（reserved）的
run 将 reservation_expires_at 缩短到 now，交由 ``reclaim_expired_runs`` 转回
available——capability 已失效故不会被再次消费，上层对账按返回清单释放对应
预算 reservation。json/dual 后端 fail-closed（platform_features）。
"""

import math
import os
import time

import psycopg

import pg_store
import platform_features

#: capability 默认 24 小时到期（docs §5.2）
DEMO_CAPABILITY_TTL_HOURS = 24
#: run 预占默认 10 分钟过期（docs §5.3）
DEMO_RUN_RESERVATION_TTL_SECONDS = 600
#: 同一 IP 前缀（v4 /24、v6 /64）在窗口内最多成功预占/消费多少次 Demo run。
#: 低于默认 Demo 子额度 5，避免清 cookie 换 capability 耗尽子额度。
#: 环境变量 ``DEMO_IP_RUN_LIMIT`` 可覆盖；``0`` 关闭该桶。
DEFAULT_DEMO_IP_RUN_LIMIT = 3
#: IP run 窗口（秒），默认 24 小时，与 capability TTL 对齐。
DEFAULT_DEMO_IP_RUN_WINDOW_SECONDS = 24 * 3600


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def ip_run_limit():
    """当前 Demo IP run 上限（``DEMO_IP_RUN_LIMIT``；≤0 关闭该桶）。"""
    return _int_env("DEMO_IP_RUN_LIMIT", DEFAULT_DEMO_IP_RUN_LIMIT)


def ip_run_window_seconds():
    return _int_env("DEMO_IP_RUN_WINDOW_SECONDS", DEFAULT_DEMO_IP_RUN_WINDOW_SECONDS)


RUN_STATE_AVAILABLE = "available"
RUN_STATE_RESERVED = "reserved"
RUN_STATE_CONSUMED = "consumed"


class RunAttemptConflict(Exception):
    """release_run/consume_run 的 expected_attempt 或 expected_request_id 与当前 reserved 行不一致。"""


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


_SESSION_SEL = (
    "id, token_hash, extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from expires_at)::float8 AS expires_at, run_state, "
    "attempt, COALESCE(rollback_epoch, 0) AS rollback_epoch, "
    "extract(epoch from reserved_at)::float8 AS reserved_at, "
    "extract(epoch from reservation_expires_at)::float8 AS "
    "reservation_expires_at, "
    "extract(epoch from consumed_at)::float8 AS consumed_at, "
    "histopilot_session_id, slide_id, asset_revision, request_id, "
    "ip_prefix_hash"
)

_CATALOG_SEL = (
    "slide_id, display_name, description, sort_order, is_default, added_by, "
    "extract(epoch from added_at)::float8 AS added_at"
)


def _session_out(row) -> dict:
    out = dict(row)
    if out.get("attempt") is not None:
        out["attempt"] = int(out["attempt"])
    out["rollback_epoch"] = int(out.get("rollback_epoch") or 0)
    return out


def _catalog_out(row) -> dict:
    out = dict(row)
    out["sort_order"] = int(out["sort_order"])
    out["is_default"] = bool(out["is_default"])
    return out


# --------------------------------------------------------------------------- #
# demo_sessions：capability 与一次性 run
# --------------------------------------------------------------------------- #
def create_capability(capability_id, token_hash, ttl_hours=DEMO_CAPABILITY_TTL_HOURS,
                      ip_prefix_hash=None):
    """签发一个 Demo capability（id/token_hash 由调用方生成）。

    token_hash 必须是明文 token 的 hash（明文绝不落库）。id 或 token_hash 已
    存在时抛 ValueError。返回新 capability dict（run_state=available）。
    """
    platform_features.require_pg_backend("demo_sessions")
    if not isinstance(capability_id, str) or not capability_id:
        raise ValueError("capability_id 不能为空")
    if not isinstance(token_hash, str) or not token_hash:
        raise ValueError("token_hash 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO demo_sessions "
                        "(id, token_hash, created_at, expires_at, run_state, "
                        " ip_prefix_hash, attempt) VALUES (%s,%s, now(), "
                        " now() + (%s * interval '1 hour'), 'available', %s, 0) "
                        "RETURNING " + _SESSION_SEL,
                        (capability_id, token_hash, float(ttl_hours),
                         ip_prefix_hash))
                    row = cur.fetchone()
                except psycopg.errors.UniqueViolation as exc:
                    raise ValueError(
                        "demo capability 已存在（id 或 token_hash 冲突）") from exc
                return _session_out(row)
    finally:
        conn.close()


def get_valid_capability(token_hash):
    """按 token_hash 取有效 capability；不存在或已过期返回 None。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SESSION_SEL + " FROM demo_sessions "
                    "WHERE token_hash=%s AND expires_at > now()", (token_hash,))
                row = cur.fetchone()
        return _session_out(row) if row is not None else None
    finally:
        conn.close()


def reserve_run(session_id, request_id, slide_id, asset_revision,
                ttl_seconds=DEMO_RUN_RESERVATION_TTL_SECONDS,
                from_states=(RUN_STATE_AVAILABLE,),
                ip_prefix_hash=None):
    """CAS 预占 run：from_states 中的状态 → reserved。

    默认只从 ``available`` 预占（每浏览器 1 次）。当周期
    ``demo_per_browser_limit > 1`` 时，调用方可把 ``consumed`` 纳入
    ``from_states``，让同一 capability 在限额内再跑。
    已 reserved 且 request_id 相同 → 刷新 TTL、不升 attempt（``replayed``，
    并递增 ``rollback_epoch``，使原请求的 release CAS 失效）。从
    available/consumed 预占时 ``attempt=COALESCE(attempt,0)+1``（单调递增，
    禁止重置为 1），``rollback_epoch=0``。
    可选 ``ip_prefix_hash``：写入当前请求 IP 前缀（供 Demo run IP 桶统计）。
    冲突（状态不符 / 已 reserved 但 request_id 不同 / capability 过期 / 不存在）
    返回 None。
    """
    platform_features.require_pg_backend("demo_sessions")
    allowed = tuple(from_states) or (RUN_STATE_AVAILABLE,)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_sessions SET run_state='reserved', "
                    "attempt=COALESCE(attempt,0)+1, rollback_epoch=0, "
                    "reserved_at=now(), reservation_expires_at="
                    " now() + (%s * interval '1 second'), request_id=%s, "
                    "slide_id=%s, asset_revision=%s, "
                    "histopilot_session_id=NULL, consumed_at=NULL, "
                    "ip_prefix_hash=COALESCE(%s, ip_prefix_hash) "
                    "WHERE id=%s AND run_state = ANY(%s) AND expires_at > now() "
                    "RETURNING " + _SESSION_SEL,
                    (float(ttl_seconds), request_id, slide_id, asset_revision,
                     ip_prefix_hash, session_id, list(allowed)))
                row = cur.fetchone()
                if row is not None:
                    return _session_out(row)
                cur.execute(
                    "UPDATE demo_sessions SET "
                    "reservation_expires_at="
                    " now() + (%s * interval '1 second'), "
                    "rollback_epoch=COALESCE(rollback_epoch,0)+1, "
                    "ip_prefix_hash=COALESCE(%s, ip_prefix_hash) "
                    "WHERE id=%s AND run_state='reserved' AND request_id=%s "
                    "AND expires_at > now() RETURNING " + _SESSION_SEL,
                    (float(ttl_seconds), ip_prefix_hash, session_id, request_id))
                replay = cur.fetchone()
        if replay is None:
            return None
        out = _session_out(replay)
        out["replayed"] = True
        return out
    finally:
        conn.close()


def _check_reserved_cas(row, expected_attempt=None, expected_request_id=None,
                        expected_rollback_epoch=None):
    """reserved 行的身份 CAS：session 已锁定。attempt / request_id /
    rollback_epoch 均可选。"""
    if expected_request_id is not None and row.get("request_id") != expected_request_id:
        raise RunAttemptConflict(
            "demo run request_id 不匹配（expected=%s actual=%s）"
            % (expected_request_id, row.get("request_id")))
    if expected_attempt is not None:
        actual = int(row.get("attempt") or 1)
        if actual != int(expected_attempt):
            raise RunAttemptConflict(
                "demo run attempt 不匹配（expected=%s actual=%s）"
                % (expected_attempt, actual))
    if expected_rollback_epoch is not None:
        actual_epoch = int(row.get("rollback_epoch") or 0)
        if actual_epoch != int(expected_rollback_epoch):
            raise RunAttemptConflict(
                "demo run rollback_epoch 不匹配（expected=%s actual=%s）"
                % (expected_rollback_epoch, actual_epoch))


def consume_run(session_id, histopilot_session_id, expected_attempt=None,
                expected_request_id=None):
    """reserved → consumed（收到 HistoPilot security_profile_applied 确认后）。

    幂等：已 consumed（且带同一 session id）直接返回。available 状态拒绝
    （未预占不能消费）。session 不存在返回 None。对 reserved 行可同时校验
    expected_attempt 与 expected_request_id（防 ABA：旧 run 的延迟对账不得
    消费新 request_id）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SESSION_SEL + " FROM demo_sessions "
                    "WHERE id=%s FOR UPDATE", (session_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                if row["run_state"] == RUN_STATE_CONSUMED:
                    return _session_out(row)  # 幂等
                if row["run_state"] != RUN_STATE_RESERVED:
                    raise ValueError(
                        "run_state=%s 不能转为 consumed（仅 reserved 可）"
                        % row["run_state"])
                _check_reserved_cas(row, expected_attempt, expected_request_id)
                cur.execute(
                    "UPDATE demo_sessions SET run_state='consumed', "
                    "consumed_at=now(), histopilot_session_id=%s WHERE id=%s "
                    "RETURNING " + _SESSION_SEL,
                    (histopilot_session_id, session_id))
                return _session_out(cur.fetchone())
    finally:
        conn.close()


def release_run(session_id, expected_attempt=None, expected_request_id=None,
                expected_rollback_epoch=None):
    """释放 run 预占（HistoPilot 接受前失败时）：reserved → available。

    幂等：已是 available 直接返回。已 consumed **拒绝释放**（防误退款）。
    session 不存在返回 None。对 reserved 行可同时校验 expected_attempt、
    expected_request_id 与 expected_rollback_epoch（后者使后来的 reserved
    重放令原请求 rollback CAS 失败；确认式对账不传 epoch）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SESSION_SEL + " FROM demo_sessions "
                    "WHERE id=%s FOR UPDATE", (session_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                if row["run_state"] == RUN_STATE_AVAILABLE:
                    return _session_out(row)  # 幂等（未预留/已释放）
                if row["run_state"] == RUN_STATE_CONSUMED:
                    raise ValueError("已 consumed 的 run 不能释放（防误退款）")
                _check_reserved_cas(
                    row, expected_attempt, expected_request_id,
                    expected_rollback_epoch)
                cur.execute(
                    "UPDATE demo_sessions SET run_state='available', "
                    "reserved_at=NULL, reservation_expires_at=NULL, "
                    "request_id=NULL, slide_id=NULL, asset_revision=NULL "
                    "WHERE id=%s", (session_id,))
                out = _session_out(row)
                out["run_state"] = RUN_STATE_AVAILABLE
                out["reserved_at"] = None
                out["reservation_expires_at"] = None
                out["request_id"] = None
                return out
    finally:
        conn.close()


def reclaim_expired_runs(now):
    """惰性回收过期 run 预占：reserved 且 reservation_expires_at < now → available。

    只按时间回收（对账/顺延语义在上层）。返回本次回收的 session 列表。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_sessions SET run_state='available', "
                    "reserved_at=NULL, reservation_expires_at=NULL, "
                    "request_id=NULL, slide_id=NULL, asset_revision=NULL "
                    "WHERE run_state='reserved' AND reservation_expires_at < "
                    "to_timestamp(%s) RETURNING id, token_hash, run_state",
                    (float(now),))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_reserved_expired(now):
    """列出 reserved 且 reservation_expires_at < now 的 run（对账用，不改状态）。

    上层按 request_id 经 HistoPilot /session/by-request/<rid> 反查确认终态后，
    再决定 consume / release / 顺延（docs §5.3-6；直接盲回收会误退款）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SESSION_SEL + " FROM demo_sessions "
                    "WHERE run_state='reserved' AND reservation_expires_at < "
                    "to_timestamp(%s) ORDER BY reserved_at", (float(now),))
                rows = cur.fetchall()
        return [_session_out(r) for r in rows]
    finally:
        conn.close()


def extend_run_reservation(session_id, ttl_seconds=DEMO_RUN_RESERVATION_TTL_SECONDS):
    """顺延 run 预占过期时间（对账时 HistoPilot 不可达 → 不释放、顺延，§5.3-6）。

    仅 reserved 状态可顺延（available/consumed 无预占可顺）；返回更新后的
    session dict 或 None（不存在）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_sessions SET reservation_expires_at="
                    " now() + (%s * interval '1 second') "
                    "WHERE id=%s AND run_state='reserved' "
                    "RETURNING " + _SESSION_SEL,
                    (float(ttl_seconds), session_id))
                row = cur.fetchone()
        return _session_out(row) if row is not None else None
    finally:
        conn.close()


def get_session(session_id):
    """按 capability id 取 session 行（任意状态；不存在返回 None）。

    供 catalog_remove / revoke_by_slide 后按 terminated_runs 释放对应预算
    reservation（需要其 request_id）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SESSION_SEL + " FROM demo_sessions WHERE id=%s",
                    (session_id,))
                row = cur.fetchone()
        return _session_out(row) if row is not None else None
    finally:
        conn.close()


def count_ip_runs(ip_prefix_hash, window_seconds=DEFAULT_DEMO_IP_RUN_WINDOW_SECONDS,
                  now=None):
    """统计该 IP 前缀在窗口内的 reserved/consumed Demo run 数。

    返回 ``{"count", "retry_after_seconds"}``。``ip_prefix_hash`` 空则按
    ``unknown`` 共用一桶（缺 IP 不得绕过）。released/available 不计入。
    """
    platform_features.require_pg_backend("demo_sessions")
    key = ip_prefix_hash if (isinstance(ip_prefix_hash, str) and ip_prefix_hash) else "unknown"
    window = int(window_seconds or DEFAULT_DEMO_IP_RUN_WINDOW_SECONDS)
    if window <= 0:
        window = DEFAULT_DEMO_IP_RUN_WINDOW_SECONDS
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if now is None:
                    cur.execute(
                        "SELECT COUNT(*)::int AS n, "
                        "extract(epoch from MIN(COALESCE(reserved_at, consumed_at, created_at)))::float8 "
                        "AS oldest "
                        "FROM demo_sessions "
                        "WHERE ip_prefix_hash=%s "
                        "AND run_state IN ('reserved', 'consumed') "
                        "AND COALESCE(reserved_at, consumed_at, created_at) > "
                        "now() - (%s * interval '1 second')",
                        (key, float(window)))
                else:
                    ts = float(now)
                    cur.execute(
                        "SELECT COUNT(*)::int AS n, "
                        "extract(epoch from MIN(COALESCE(reserved_at, consumed_at, created_at)))::float8 "
                        "AS oldest "
                        "FROM demo_sessions "
                        "WHERE ip_prefix_hash=%s "
                        "AND run_state IN ('reserved', 'consumed') "
                        "AND COALESCE(reserved_at, consumed_at, created_at) > "
                        "to_timestamp(%s) - (%s * interval '1 second')",
                        (key, ts, float(window)))
                row = cur.fetchone() or {"n": 0, "oldest": None}
        count = int(row["n"] or 0)
        oldest = row.get("oldest")
        retry_after = 0
        if count > 0 and oldest is not None:
            horizon = float(oldest) + window
            ref = float(now) if now is not None else time.time()
            remain = horizon - ref
            retry_after = int(math.ceil(remain)) if remain > 0 else 1
        return {"count": count, "retry_after_seconds": retry_after}
    finally:
        conn.close()


def count_run_states():
    """未过期 Demo capability 按 run_state 计数（owner 用量卡片）。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT run_state, COUNT(*)::int AS n FROM demo_sessions "
                    "WHERE expires_at > now() GROUP BY run_state")
                rows = {r["run_state"]: int(r["n"]) for r in cur.fetchall()}
        available = int(rows.get(RUN_STATE_AVAILABLE) or 0)
        reserved = int(rows.get(RUN_STATE_RESERVED) or 0)
        consumed = int(rows.get(RUN_STATE_CONSUMED) or 0)
        return {
            "available": available,
            "reserved": reserved,
            "consumed": consumed,
            "total": available + reserved + consumed,
        }
    finally:
        conn.close()


def reset_demo_runs():
    """owner 一键重置：reserved/consumed 退回 available，放开每浏览器与 IP 辅闸。

    不删行、不使 cookie 失效（同一浏览器可立刻再跑）。进行中 reserved 一并退回。
    返回被重置的 session id 列表。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_sessions SET run_state='available', "
                    "reserved_at=NULL, reservation_expires_at=NULL, "
                    "consumed_at=NULL, histopilot_session_id=NULL, "
                    "request_id=NULL, slide_id=NULL, asset_revision=NULL "
                    "WHERE run_state IN ('reserved', 'consumed') "
                    "RETURNING id")
                ids = [r["id"] for r in cur.fetchall()]
        return ids
    finally:
        conn.close()


def _revoke_by_slide_tx(cur, slide_id):
    """revoke_by_slide 的事务内实现（供 catalog_remove 同事务联动）。"""
    # 1) capability 立即失效：expires_at 置 now（get_valid_capability 立刻 None）
    cur.execute(
        "UPDATE demo_sessions SET expires_at=now() "
        "WHERE slide_id=%s AND expires_at > now()", (slide_id,))
    expired = cur.rowcount
    # 2) 未完成 run 标记终止：reserved 的预占到期时间缩短到 now，
    #    reclaim_expired_runs 将其转回 available（capability 已失效，不会再次消费）
    cur.execute(
        "SELECT id FROM demo_sessions "
        "WHERE slide_id=%s AND run_state='reserved' FOR UPDATE", (slide_id,))
    terminated_ids = [r["id"] for r in cur.fetchall()]
    if terminated_ids:
        cur.execute(
            "UPDATE demo_sessions SET reservation_expires_at=now() "
            "WHERE id = ANY(%s)", (terminated_ids,))
    return {"expired_capabilities": expired, "terminated_runs": terminated_ids}


def revoke_by_slide(slide_id):
    """切片下架/删除时撤销：capability 立即失效，未完成 run 标记终止（§9.3）。

    返回 ``{"expired_capabilities": int, "terminated_runs": [id...]}``；
    terminated_runs 供上层对账释放对应预算 reservation。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _revoke_by_slide_tx(cur, slide_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# demo_catalog：Demo 切片目录（owner allowlist）
# --------------------------------------------------------------------------- #
def catalog_add(slide_id, display_name=None, description=None, sort_order=0,
                added_by=None):
    """加入/更新 Demo 目录条目（UPSERT；is_default 不在此处改动）。

    校验 slide_id 在 slides 表存在（Demo allowlist 只接受已入库的稳定切片
    身份），不存在抛 ValueError。返回条目 dict。
    """
    platform_features.require_pg_backend("demo_catalog")
    if not isinstance(slide_id, str) or not slide_id:
        raise ValueError("slide_id 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1 FROM slides WHERE slide_id=%s",
                            (slide_id,))
                if cur.fetchone() is None:
                    raise ValueError("slide 不存在，不能加入 Demo 目录：%s"
                                     % slide_id)
                cur.execute(
                    "INSERT INTO demo_catalog "
                    "(slide_id, display_name, description, sort_order, "
                    " added_by, added_at) VALUES (%s,%s,%s,%s,%s, now()) "
                    "ON CONFLICT (slide_id) DO UPDATE SET "
                    "display_name=EXCLUDED.display_name, "
                    "description=EXCLUDED.description, "
                    "sort_order=EXCLUDED.sort_order, "
                    "added_by=EXCLUDED.added_by, added_at=now() "
                    "RETURNING " + _CATALOG_SEL,
                    (slide_id, display_name, description, int(sort_order),
                     added_by))
                return _catalog_out(cur.fetchone())
    finally:
        conn.close()


def catalog_remove(slide_id):
    """从 Demo 目录移除，并在同事务内联动 ``revoke_by_slide``（§9.3）。

    返回 None（条目不存在）或 ``{"entry": 条目dict, "revoke": {...}}``。
    """
    platform_features.require_pg_backend("demo_catalog")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM demo_catalog WHERE slide_id=%s RETURNING "
                    + _CATALOG_SEL, (slide_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                revoke = _revoke_by_slide_tx(cur, slide_id)
                return {"entry": _catalog_out(row), "revoke": revoke}
    finally:
        conn.close()


def catalog_list_ordered():
    """按 sort_order、added_at、slide_id 返回 Demo 目录条目列表。"""
    platform_features.require_pg_backend("demo_catalog")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _CATALOG_SEL + " FROM demo_catalog "
                    "ORDER BY sort_order, added_at, slide_id")
                rows = cur.fetchall()
        return [_catalog_out(r) for r in rows]
    finally:
        conn.close()


def catalog_get(slide_id):
    """取单个 Demo 目录条目；不在目录内返回 None（allowlist 校验用）。"""
    platform_features.require_pg_backend("demo_catalog")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _CATALOG_SEL + " FROM demo_catalog "
                    "WHERE slide_id=%s", (slide_id,))
                row = cur.fetchone()
        return _catalog_out(row) if row is not None else None
    finally:
        conn.close()


def resolve_slide_filename(slide_id):
    """把目录内 slide_id 解析回 legacy_filename（slides 表）；无映射返回 None。

    Demo 目录 API 只接受 slide_id；服务瓦片/info 时反查稳定映射。行缺失或
    legacy_filename 为 NULL → None（上层 fail-closed 拒绝，绝不按文件名猜）。
    """
    platform_features.require_pg_backend("demo_catalog")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_filename FROM slides WHERE slide_id=%s",
                    (slide_id,))
                row = cur.fetchone()
        return (row["legacy_filename"] or None) if row is not None else None
    finally:
        conn.close()


def catalog_set_default(slide_id):
    """设置默认 Demo 切片（唯一默认：先清旧再置新）。不在目录内抛 ValueError。"""
    platform_features.require_pg_backend("demo_catalog")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1 FROM demo_catalog WHERE slide_id=%s",
                            (slide_id,))
                if cur.fetchone() is None:
                    raise ValueError("slide 不在 Demo 目录内：%s" % slide_id)
                cur.execute(
                    "UPDATE demo_catalog SET is_default=FALSE "
                    "WHERE is_default AND slide_id <> %s", (slide_id,))
                cur.execute(
                    "UPDATE demo_catalog SET is_default=TRUE WHERE slide_id=%s "
                    "RETURNING " + _CATALOG_SEL, (slide_id,))
                return _catalog_out(cur.fetchone())
    finally:
        conn.close()
