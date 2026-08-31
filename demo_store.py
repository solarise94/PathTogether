# -*- coding: utf-8 -*-
"""匿名 Demo 存储原语（demo_sessions + demo_runs + demo_catalog + IP 请求速率桶）。

批次 E（docs ai-money-budget-bugfix-and-simplification-plan.md §4）起的模型：

demo_sessions（capability 载体）：
  - capability 是短期、路由限定的授权（token 明文只在浏览器，库中只存 hash）；
  - 只承担匿名身份 + 过期时间 + IP 前缀 hash。0006 的
    ``run_state`` 一次性状态机（available|reserved|consumed）退出新写入路径：
    历史行保留原语义可读（0026 不改 demo_sessions 列），新 run 一律落
    demo_runs 流水。

demo_runs（run 流水，0026）：
  - 每次 run 独立行，状态机 reserved|accepted|finished|released|expired，
    终态保留（append-only 语义，不物理删）；
  - 同 capability **顺序**多次 run：上一次终态后即可再开，无累计次数上限；
  - 同 capability 同时最多一个 reserved/accepted run：
    ``uq_demo_runs_single_active`` 部分唯一索引在 DB 层硬保证（并发第二个
    INSERT 直接唯一冲突），应用层另在 capability 行 FOR UPDATE 锁内做确定性
    判定（先锁后查后插，双保险）；
  - ``UNIQUE(capability_id, request_id)``：同 capability 同 request_id 一行；
    released 后同 ID 重试走 UPDATE 复位（attempt+1、rollback_epoch=0，防 ABA）；
    在途 reserved/accepted 重放只刷新 TTL 并递增 rollback_epoch（原请求的
    release CAS 失效）；
  - capability 过期不能新开 run（capability 行锁内校验 expires_at > now），
    既有终态流水保留；
  - accepted 的 expires_at 即重连窗口（accepted_at + 1h）；流正常结束回调
    ``finish_run`` 转 finished 终态，capability 立即可再开下一个 run。

demo_ip_request_rate（短窗口请求速率，0026）：
  - 每 IP 前缀每分钟**请求数**固定窗口计数（防刷/防 DoS，§1.2：不是可消费
    额度、不累计成功次数——替代已退役的 24h 成功 run 桶）；
  - PG 权威、FOR UPDATE 计数、json/dual fail-closed；env
    ``DEMO_IP_RATE_PER_MINUTE`` 可调（≤0 关闭）。

demo_catalog：
  - owner allowlist，独立于 public 语义（public ≠ 互联网匿名可见）；
  - add 校验 slide 在 slides 表存在；remove 联动 ``revoke_by_slide``。

revoke_by_slide（切片下架/删除/移出目录）：终止该切片的**在途** demo run
（reserved|accepted → expired，返回流水供上层对账预算 reservation）。capability
是多切片复用的匿名载体，不再整体失效；未来访问由目录成员资格拦截
（info/dzi/tile 404）。json/dual 后端 fail-closed（platform_features）。
"""

import math
import os
import secrets
import time

import psycopg

import pg_store
import platform_features

#: capability 默认 24 小时到期（docs §5.2）
DEMO_CAPABILITY_TTL_HOURS = 24
#: run 预占（reserved，HistoPilot 接受前）默认 10 分钟过期
DEMO_RUN_RESERVATION_TTL_SECONDS = 600
#: accepted run 的活跃/重连窗口（与旧 consumed_at + 1h 口径一致）：窗口内可
#: 重挂 stream 读轨迹；窗口到期后由对账/惰性路径转 expired 终态
DEMO_RUN_RECONNECT_WINDOW_SECONDS = 3600

#: Demo 每 IP 前缀每分钟**请求数**上限（§1.2/§4.1：短窗口防刷/防 DoS，不是
#: 消费额度）。默认 12：单人手点 + 网络重试的合理余量，同时拦截脚本洪泛；
#: 全站真实开销仍由 demo_max_concurrency、demo_task_max_steps 与 Demo 周金额
#: 窗口约束。env ``DEMO_IP_RATE_PER_MINUTE`` 可调；``0``/负数关闭该桶。
DEFAULT_DEMO_IP_RATE_PER_MINUTE = 12
#: 请求速率固定窗口长度（秒）
DEMO_IP_RATE_WINDOW_SECONDS = 60

#: run 状态机（0026 起权威在 demo_runs；demo_sessions.run_state 仅历史行）
RUN_STATE_RESERVED = "reserved"
RUN_STATE_ACCEPTED = "accepted"
RUN_STATE_FINISHED = "finished"
RUN_STATE_RELEASED = "released"
RUN_STATE_EXPIRED = "expired"
#: 活跃（capability-blocking）状态：同 capability 同时至多一个
RUN_ACTIVE_STATES = (RUN_STATE_RESERVED, RUN_STATE_ACCEPTED)
#: 终态：终态后同 capability 可再开新 run；流水保留
RUN_TERMINAL_STATES = (RUN_STATE_FINISHED, RUN_STATE_RELEASED,
                       RUN_STATE_EXPIRED)


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def ip_rate_limit():
    """当前 Demo 每 IP 前缀每分钟请求上限（≤0 关闭该桶）。"""
    return _int_env("DEMO_IP_RATE_PER_MINUTE", DEFAULT_DEMO_IP_RATE_PER_MINUTE)


def ip_rate_window_seconds():
    return _int_env("DEMO_IP_RATE_WINDOW_SECONDS", DEMO_IP_RATE_WINDOW_SECONDS)


class RunAttemptConflict(Exception):
    """accept/release 的 expected_attempt / expected_request_id / expected_rollback_epoch
    与当前 run 行不一致（旧对账不得动新尝试）。"""


class DemoCapabilityExpired(Exception):
    """预占时 capability 已不存在/已过期（不能新开 run）。"""

    code = "demo_capability_expired"


class DemoRunActiveConflict(Exception):
    """该 capability 已有另一个在途 run（reserved/accepted）。"""

    code = "demo_run_in_progress"


class DemoRunFinalConflict(Exception):
    """同 (capability, request_id) 的 run 已处于 finished/expired 终态。"""

    code = "demo_run_request_final"


class DemoConcurrencyExceeded(Exception):
    """全站在途 Demo run（reserved/accepted）达到 demo_max_concurrency。

    批次 F（§7.3 阶段 2）：并发上限是**安全参数**，从 budget_store.reserve_turn
    的事务内迁居本模块（金额硬闸主体不再走 reserve_turn，闸必须独立于消费闸
    存活）；code 沿用既有字符串 ``demo_concurrency_exceeded``，app 层 HTTP
    映射（429）不变。上限读 settings_store ai_safety.demo_max_concurrency。
    context kwargs（limit/used）供路由/日志携带非敏感上下文。
    """

    code = "demo_concurrency_exceeded"

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


#: 全局 Demo 并发计数串行化 advisory key（事务级；"DMCC"）。并发计数跨
#: capability，无法靠 capability 行锁串行化——Demo 低 QPS，全局锁可接受。
_DEMO_CONCURRENCY_LOCK_KEY = 0x444D4343


def _demo_max_concurrency() -> int:
    """读 Demo 全局并发上限（settings_store ai_safety.*，缺省回落常量）。

    读取失败按缺省（2）处理：安全参数读取异常不放大并发（fail-closed）。
    json/dual 由调用方 fail-closed（reserve_run 的 PG 守卫先行）。
    """
    import settings_store
    try:
        return int(settings_store.get_ai_safety_settings()["demo_max_concurrency"])
    except Exception:  # pragma: no cover - settings 读取异常的保守回退
        return DEFAULT_DEMO_MAX_CONCURRENCY


#: Demo 全局并发上限缺省（与 budget_store.DEFAULT_DEMO_MAX_CONCURRENCY 同值；
#: 本地副本避免 demo_store 反向依赖 budget_store）
DEFAULT_DEMO_MAX_CONCURRENCY = 2


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

#: demo_runs 出口列（时间为 epoch 秒 float，对齐仓库惯例）
_RUN_SEL = (
    "demo_run_id, capability_id, request_id, state, histopilot_session_id, "
    "slide_id, asset_revision, attempt, COALESCE(rollback_epoch, 0) "
    "AS rollback_epoch, ip_prefix_hash, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at, "
    "extract(epoch from accepted_at)::float8 AS accepted_at, "
    "extract(epoch from finished_at)::float8 AS finished_at, "
    "extract(epoch from expires_at)::float8 AS expires_at"
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


def _run_out(row) -> dict:
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
# demo_sessions：capability（匿名身份载体）
# --------------------------------------------------------------------------- #
def create_capability(capability_id, token_hash, ttl_hours=DEMO_CAPABILITY_TTL_HOURS,
                      ip_prefix_hash=None):
    """签发一个 Demo capability（id/token_hash 由调用方生成）。

    token_hash 必须是明文 token 的 hash（明文绝不落库）。id 或 token_hash 已
    存在时抛 ValueError。返回新 capability dict（run 字段不再有意义：run 状态
    自 0026 起在 demo_runs）。
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


def get_session(session_id):
    """按 capability id 取 session 行（任意状态；不存在返回 None）。"""
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


# --------------------------------------------------------------------------- #
# demo_runs：run 流水（预占 / 接受 / 结束 / 释放 / 过期）
# --------------------------------------------------------------------------- #
def reserve_run(capability_id, request_id, slide_id, asset_revision,
                ttl_seconds=DEMO_RUN_RESERVATION_TTL_SECONDS,
                ip_prefix_hash=None):
    """为 capability 预占一个新 run（0026 模型）。

    单事务内完成（capability 行 FOR UPDATE 锁串行化同 capability 的全部
    预占；``uq_demo_runs_single_active`` 部分唯一索引是 DB 级兜底）：

    1. capability 不存在 → 返回 None；已过期（expires_at <= now）抛
       :class:`DemoCapabilityExpired`（不能新开 run，§4.1）；
    2. 惰性终态：该 capability 已过期（expires_at 到点）的 active run 转
       ``expired``（capability 立即可再开）；
    3. 同 (capability, request_id) 已有行：
       - ``reserved``/``accepted``（在途重放）→ 刷新 reserved TTL、递增
         ``rollback_epoch``（原请求的 release CAS 失效），返回 ``replayed=True``；
       - ``released``（上次尝试已退款）→ UPDATE 复位为 reserved，
         ``attempt+1``、``rollback_epoch=0``（网络重试属新执行尝试）；
       - ``finished``/``expired`` → 抛 :class:`DemoRunFinalConflict`
         （该请求已定局，客户端应换新 request_id）；
    4. 其它 active run 在途 → 抛 :class:`DemoRunActiveConflict`
       （同 capability 同时最多一个 active run）；
    5. 全局并发闸（批次 F 迁入）：在途 run（reserved/accepted，跨全部
       capability）达到 demo_max_concurrency → 抛
       :class:`DemoConcurrencyExceeded`（429；上限读 settings_store
       ai_safety.*）。全局计数用事务级 advisory lock 串行化（跨 capability，
       capability 行锁覆盖不到；Demo 低 QPS 可接受）；
    6. INSERT 新流水行（state=reserved，expires_at=now+ttl）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # capability 行锁：同 capability 的全部预占/惰性终态串行化；
                # 过滤 expires_at > now（不存在与已过期都在此分流）
                cur.execute(
                    "SELECT id FROM demo_sessions WHERE id=%s AND "
                    "expires_at > now() FOR UPDATE", (capability_id,))
                alive = cur.fetchone()
                if alive is None:
                    cur.execute(
                        "SELECT 1 FROM demo_sessions WHERE id=%s",
                        (capability_id,))
                    if cur.fetchone() is None:
                        return None
                    raise DemoCapabilityExpired(
                        "Demo capability 已过期，不能新开 run")
                # 惰性终态：过期的 active run 不再阻塞 capability
                cur.execute(
                    "UPDATE demo_runs SET state='expired', updated_at=now() "
                    "WHERE capability_id=%s AND state = ANY(%s) "
                    "AND expires_at <= now()",
                    (capability_id, list(RUN_ACTIVE_STATES)))
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE capability_id=%s AND request_id=%s FOR UPDATE",
                    (capability_id, request_id))
                row = cur.fetchone()
                if row is not None:
                    if row["state"] in RUN_ACTIVE_STATES:
                        if row["state"] == RUN_STATE_RESERVED:
                            cur.execute(
                                "UPDATE demo_runs SET "
                                "expires_at=now() + (%s * interval '1 second'), "
                                "rollback_epoch=rollback_epoch+1, "
                                "updated_at=now(), "
                                "ip_prefix_hash=COALESCE(%s, ip_prefix_hash) "
                                "WHERE demo_run_id=%s "
                                "RETURNING " + _RUN_SEL,
                                (float(ttl_seconds), ip_prefix_hash,
                                 row["demo_run_id"]))
                            updated = cur.fetchone()
                        else:
                            # accepted 重放：重连窗口不变，只递增 epoch
                            cur.execute(
                                "UPDATE demo_runs SET "
                                "rollback_epoch=rollback_epoch+1, "
                                "updated_at=now() "
                                "WHERE demo_run_id=%s "
                                "RETURNING " + _RUN_SEL,
                                (row["demo_run_id"],))
                            updated = cur.fetchone()
                        out = _run_out(updated if updated is not None else row)
                        out["replayed"] = True
                        return out
                    if row["state"] == RUN_STATE_RELEASED:
                        # released 复位属新执行尝试（attempt+1，防 ABA）：
                        # 与全新 INSERT 同样先过 active/全局并发闸（见下），
                        # 不提前 return，保证闸覆盖重试路径
                        pass
                    else:
                        raise DemoRunFinalConflict(
                            "该 request_id 的 run 已终态（%s），请换新 "
                            "request_id" % row["state"])
                # 确定性 active 检查（capability 行锁已串行化；部分唯一索引兜底）
                cur.execute(
                    "SELECT demo_run_id FROM demo_runs WHERE capability_id=%s "
                    "AND state = ANY(%s)", (capability_id,
                                            list(RUN_ACTIVE_STATES)))
                if cur.fetchone() is not None:
                    raise DemoRunActiveConflict(
                        "该浏览器已有进行中的 Demo 体验（同 capability 同时"
                        "最多一个 run）")
                # 全局并发闸（批次 F 迁入）：跨 capability 的在途 run 合计。
                # advisory xact lock 串行化「读计数 → 写入」窗口，杜绝并发
                # 超限；上限读 settings_store（ai_safety.demo_max_concurrency）。
                max_cc = _demo_max_concurrency()
                if max_cc > 0:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)",
                                (_DEMO_CONCURRENCY_LOCK_KEY,))
                    cur.execute(
                        "SELECT COUNT(*)::int AS n FROM demo_runs "
                        "WHERE state = ANY(%s)",
                        (list(RUN_ACTIVE_STATES),))
                    in_flight = int(cur.fetchone()["n"])
                    if in_flight + 1 > max_cc:
                        raise DemoConcurrencyExceeded(
                            "Demo 并发已达上限（%d/%d）" % (in_flight, max_cc),
                            limit=max_cc, used=in_flight)
                if row is not None:
                    # released 复位（上方已过闸）：UPDATE 回 reserved
                    cur.execute(
                        "UPDATE demo_runs SET state='reserved', "
                        "attempt=attempt+1, rollback_epoch=0, "
                        "slide_id=%s, asset_revision=%s, "
                        "histopilot_session_id=NULL, accepted_at=NULL, "
                        "finished_at=NULL, "
                        "expires_at=now() + (%s * interval '1 second'), "
                        "updated_at=now(), "
                        "ip_prefix_hash=COALESCE(%s, ip_prefix_hash) "
                        "WHERE demo_run_id=%s "
                        "RETURNING " + _RUN_SEL,
                        (slide_id, asset_revision, float(ttl_seconds),
                         ip_prefix_hash, row["demo_run_id"]))
                    return _run_out(cur.fetchone())
                cur.execute(
                    "INSERT INTO demo_runs "
                    "(demo_run_id, capability_id, request_id, state, slide_id, "
                    " asset_revision, attempt, rollback_epoch, "
                    " ip_prefix_hash, created_at, updated_at, expires_at) "
                    "VALUES (%s,%s,%s,'reserved',%s,%s,1,0,%s, now(), now(), "
                    " now() + (%s * interval '1 second')) "
                    "RETURNING " + _RUN_SEL,
                    ("dmr_" + secrets.token_hex(10), capability_id,
                     request_id, slide_id, asset_revision, ip_prefix_hash,
                     float(ttl_seconds)))
                return _run_out(cur.fetchone())
    finally:
        conn.close()


def _fetch_run_locked(cur, demo_run_id):
    cur.execute(
        "SELECT " + _RUN_SEL + " FROM demo_runs WHERE demo_run_id=%s FOR UPDATE",
        (demo_run_id,))
    return cur.fetchone()


def _check_run_cas(row, expected_attempt=None, expected_request_id=None,
                   expected_rollback_epoch=None):
    """run 行身份 CAS：attempt / request_id / rollback_epoch 均可选。"""
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


def accept_run(demo_run_id, histopilot_session_id,
               active_window_seconds=DEMO_RUN_RECONNECT_WINDOW_SECONDS,
               expected_attempt=None, expected_request_id=None):
    """reserved → accepted（HistoPilot 2xx 接受后）。

    绑定 histopilot_session_id、记 accepted_at，并把 expires_at 推到
    accepted_at + active_window（重连窗口兼作 accepted run 的活跃上限；到期由
    finish_run / 对账 / 惰性路径转终态）。幂等：已 accepted 且 session 一致
    直接返回；session 不一致抛 :class:`RunAttemptConflict`（一个 run 只绑一个
    HP session）。终态拒绝（ValueError）。run 不存在返回 None。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_run_locked(cur, demo_run_id)
                if row is None:
                    return None
                if row["state"] == RUN_STATE_ACCEPTED:
                    if row["histopilot_session_id"] != histopilot_session_id:
                        raise RunAttemptConflict(
                            "accepted run 的 histopilot_session_id 不匹配"
                            "（expected=%s actual=%s）"
                            % (histopilot_session_id,
                               row["histopilot_session_id"]))
                    _check_run_cas(row, expected_attempt, expected_request_id)
                    return _run_out(row)  # 幂等
                if row["state"] != RUN_STATE_RESERVED:
                    raise ValueError(
                        "run state=%s 不能转为 accepted（仅 reserved 可）"
                        % row["state"])
                _check_run_cas(row, expected_attempt, expected_request_id)
                cur.execute(
                    "UPDATE demo_runs SET state='accepted', "
                    "histopilot_session_id=%s, accepted_at=now(), "
                    "expires_at=now() + (%s * interval '1 second'), "
                    "updated_at=now() WHERE demo_run_id=%s "
                    "RETURNING " + _RUN_SEL,
                    (histopilot_session_id, float(active_window_seconds),
                     demo_run_id))
                return _run_out(cur.fetchone())
    finally:
        conn.close()


def finish_run(demo_run_id):
    """accepted → finished（上游 SSE 流正常结束时）。

    finished 是终态：capability 立即可再开下一个 run（顺序多次体验）。
    幂等：已 finished 直接返回。reserved（接受回调丢失的防御路径）与其它
    终态不做状态变更（返回原行；reserved 交由确认式对账定局）。
    run 不存在返回 None。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_run_locked(cur, demo_run_id)
                if row is None:
                    return None
                if row["state"] != RUN_STATE_ACCEPTED:
                    return _run_out(row)  # 幂等/防御：不改状态
                cur.execute(
                    "UPDATE demo_runs SET state='finished', "
                    "finished_at=now(), updated_at=now() "
                    "WHERE demo_run_id=%s RETURNING " + _RUN_SEL,
                    (demo_run_id,))
                return _run_out(cur.fetchone())
    finally:
        conn.close()


def release_run(demo_run_id, expected_attempt=None, expected_request_id=None,
                expected_rollback_epoch=None):
    """释放 run 预占（HistoPilot 接受前失败时）：reserved → released。

    released 是终态：capability 立即可再开（同 ID 重试走 reserve_run 的
    released-复位分支，attempt+1）。幂等：released/finished/expired 直接返回。
    已 **accepted 拒绝释放**（防误退款——模型已开始执行不退额度）。
    run 不存在返回 None。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_run_locked(cur, demo_run_id)
                if row is None:
                    return None
                if row["state"] in RUN_TERMINAL_STATES:
                    return _run_out(row)  # 幂等
                if row["state"] == RUN_STATE_ACCEPTED:
                    raise ValueError("已 accepted 的 run 不能释放（防误退款）")
                _check_run_cas(row, expected_attempt, expected_request_id,
                               expected_rollback_epoch)
                cur.execute(
                    "UPDATE demo_runs SET state='released', updated_at=now() "
                    "WHERE demo_run_id=%s RETURNING " + _RUN_SEL,
                    (demo_run_id,))
                return _run_out(cur.fetchone())
    finally:
        conn.close()


def expire_run(demo_run_id):
    """active → expired 终态（窗口到期：对账线程 / reset 联动 / 撤销终止）。

    幂等：终态直接返回。run 不存在返回 None。**只动 run 状态**——对应预算
    reservation 由上层确认式对账定局（accepted 的额度已消费，不得退款）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_run_locked(cur, demo_run_id)
                if row is None:
                    return None
                if row["state"] in RUN_TERMINAL_STATES:
                    return _run_out(row)  # 幂等
                cur.execute(
                    "UPDATE demo_runs SET state='expired', updated_at=now() "
                    "WHERE demo_run_id=%s RETURNING " + _RUN_SEL,
                    (demo_run_id,))
                return _run_out(cur.fetchone())
    finally:
        conn.close()


def extend_run_reservation(demo_run_id,
                           ttl_seconds=DEMO_RUN_RESERVATION_TTL_SECONDS):
    """顺延 reserved run 的预占过期时间（对账时 HistoPilot 不可达 → 不释放、
    顺延）。仅 reserved 可顺延；返回更新后的 run dict 或 None（不存在）。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_runs SET "
                    "expires_at=now() + (%s * interval '1 second'), "
                    "updated_at=now() WHERE demo_run_id=%s AND "
                    "state='reserved' RETURNING " + _RUN_SEL,
                    (float(ttl_seconds), demo_run_id))
                row = cur.fetchone()
        return _run_out(row) if row is not None else None
    finally:
        conn.close()


def get_run(demo_run_id):
    """按 demo_run_id 读流水行（任意状态；不存在返回 None）。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE demo_run_id=%s", (demo_run_id,))
                row = cur.fetchone()
        return _run_out(row) if row is not None else None
    finally:
        conn.close()


def get_run_by_request(capability_id, request_id):
    """按 (capability_id, request_id) 读流水行；不存在返回 None。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE capability_id=%s AND request_id=%s",
                    (capability_id, request_id))
                row = cur.fetchone()
        return _run_out(row) if row is not None else None
    finally:
        conn.close()


def latest_run_for_capability(capability_id):
    """该 capability 最近一次 run（任意状态）；无流水返回 None。

    供 /api/demo/config 呈现按钮态与重连信息。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE capability_id=%s "
                    "ORDER BY created_at DESC, demo_run_id DESC LIMIT 1",
                    (capability_id,))
                row = cur.fetchone()
        return _run_out(row) if row is not None else None
    finally:
        conn.close()


def get_run_for_session(capability_id, histopilot_session_id):
    """按 (capability, HP session) 找已接受/已结束的 run（会话读通道守卫）。

    只匹配 accepted/finished（reserved 未绑定 session；released/expired 不再
    可读）。返回最新一行或 None。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE capability_id=%s AND histopilot_session_id=%s "
                    "AND state IN ('accepted', 'finished') "
                    "ORDER BY accepted_at DESC NULLS LAST, demo_run_id DESC "
                    "LIMIT 1", (capability_id, histopilot_session_id))
                row = cur.fetchone()
        return _run_out(row) if row is not None else None
    finally:
        conn.close()


def list_active_expired(now):
    """列出 active（reserved/accepted）且 expires_at < now 的 run（对账用，
    不改状态）。上层按 request_id 经 HistoPilot /session/by-request/<rid>
    反查确认终态后再 accept/release/expire/顺延（直接盲终态会误退款）。"""
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_SEL + " FROM demo_runs "
                    "WHERE state = ANY(%s) AND expires_at < to_timestamp(%s) "
                    "ORDER BY created_at",
                    (list(RUN_ACTIVE_STATES), float(now)))
                rows = cur.fetchall()
        return [_run_out(r) for r in rows]
    finally:
        conn.close()


def count_run_states():
    """demo_runs 按 state 计数（owner 用量卡片）。

    返回 ``{"reserved", "accepted", "finished", "released", "expired",
    "active", "total"}``（active = reserved + accepted，即占用 capability 的
    在途数；0026 前 demo_sessions.run_state 口径已退役）。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT state, COUNT(*)::int AS n FROM demo_runs "
                    "GROUP BY state")
                rows = {r["state"]: int(r["n"]) for r in cur.fetchall()}
        out = {s: int(rows.get(s) or 0)
               for s in (RUN_STATE_RESERVED, RUN_STATE_ACCEPTED,
                         RUN_STATE_FINISHED, RUN_STATE_RELEASED,
                         RUN_STATE_EXPIRED)}
        out["active"] = out[RUN_STATE_RESERVED] + out[RUN_STATE_ACCEPTED]
        out["total"] = sum(v for k, v in out.items() if k != "active")
        return out
    finally:
        conn.close()


def reset_demo_runs():
    """owner 一键重置（批次 E 后的残余语义）：在途 run 全部转 expired 终态。

    每浏览器累计次数闸与 24h 成功次数 IP 桶已随批次 E 退役（无对象可清）；
    周金额窗口是全站投影，不随预算周期重置。本函数唯一保留的语义是把
    reserved/accepted 的在途 run 置为 expired，让对应 capability 立即可再开
    新 run（卡死的体验解锁）。**不触碰** ai_budget_reservations（预算侧由
    确认式对账按 HistoPilot 反查定局，避免把已接受的执行误退款）。
    返回被终态化的 demo_run_id 列表。
    """
    platform_features.require_pg_backend("demo_sessions")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE demo_runs SET state='expired', updated_at=now() "
                    "WHERE state = ANY(%s) RETURNING demo_run_id",
                    (list(RUN_ACTIVE_STATES),))
                return [r["demo_run_id"] for r in cur.fetchall()]
    finally:
        conn.close()


def _revoke_by_slide_tx(cur, slide_id):
    """revoke_by_slide 的事务内实现（供 catalog_remove 同事务联动）。

    终止该切片的在途 run（reserved|accepted → expired）。capability 是多切片
    复用的匿名载体，不再整体失效（旧 demo_sessions.slide_id 整体失效语义随
    capability/run 分离退役）；未来访问由目录成员资格拦截。
    返回 ``{"expired_capabilities": 0, "terminated_runs": [run dict...]}``
    （expired_capabilities 恒 0，保留键为载荷形状兼容；terminated_runs 带
    request_id/attempt 供上层确认式对账预算 reservation）。
    """
    cur.execute(
        "SELECT " + _RUN_SEL + " FROM demo_runs WHERE slide_id=%s "
        "AND state = ANY(%s) ORDER BY created_at FOR UPDATE",
        (slide_id, list(RUN_ACTIVE_STATES)))
    terminated = [_run_out(r) for r in cur.fetchall()]
    if terminated:
        cur.execute(
            "UPDATE demo_runs SET state='expired', updated_at=now() "
            "WHERE demo_run_id = ANY(%s)",
            ([r["demo_run_id"] for r in terminated],))
    return {"expired_capabilities": 0, "terminated_runs": terminated}


def revoke_by_slide(slide_id):
    """切片下架/删除时撤销：终止该切片的在途 demo run。

    返回 ``{"expired_capabilities": 0, "terminated_runs": [run dict...]}``；
    terminated_runs 供上层对账释放/消费对应预算 reservation。
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
# demo_ip_request_rate：短窗口请求速率（防刷/防 DoS；不是消费额度）
# --------------------------------------------------------------------------- #
def hit_ip_request_rate(ip_prefix_hash, limit=None,
                        window_seconds=None, now=None):
    """计数并判定该 IP 前缀在当前固定窗口内的 Demo run **请求数**。

    单事务 FOR UPDATE 计数（跨 worker 权威；json/dual fail-closed）：
      - 窗口过期（window_started_at 距今 ≥ window）→ 整桶重置后计 1；
      - 否则 request_count+1；
      - ``count > limit`` → ``allowed=False``，``retry_after_seconds`` 为窗口
        剩余时间（≥1）。

    ``ip_prefix_hash`` 空则归 ``unknown`` 共用一桶（缺 IP 不得绕过）。
    ``limit`` 缺省读 env（:func:`ip_rate_limit`）。本函数只计数不判定开关：
    调用方需先按 ``ip_rate_limit() <= 0`` 判定桶已关闭（关闭时不计数）。
    """
    platform_features.require_pg_backend("demo_sessions")
    key = ip_prefix_hash if (isinstance(ip_prefix_hash, str)
                             and ip_prefix_hash) else "unknown"
    lim = int(limit if limit is not None else ip_rate_limit())
    window = int(window_seconds if window_seconds is not None
                 else ip_rate_window_seconds())
    if window <= 0:
        window = DEMO_IP_RATE_WINDOW_SECONDS
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if now is None:
                    cur.execute(
                        "INSERT INTO demo_ip_request_rate "
                        "(ip_prefix_hash, window_started_at, request_count, "
                        " updated_at) VALUES (%s, now(), 0, now()) "
                        "ON CONFLICT (ip_prefix_hash) DO NOTHING", (key,))
                    cur.execute(
                        "SELECT extract(epoch from "
                        "window_started_at)::float8 AS ws, request_count "
                        "FROM demo_ip_request_rate WHERE ip_prefix_hash=%s "
                        "FOR UPDATE", (key,))
                else:
                    ts = float(now)
                    cur.execute(
                        "INSERT INTO demo_ip_request_rate "
                        "(ip_prefix_hash, window_started_at, request_count, "
                        " updated_at) VALUES (%s, to_timestamp(%s), 0, "
                        "to_timestamp(%s)) "
                        "ON CONFLICT (ip_prefix_hash) DO NOTHING",
                        (key, ts, ts))
                    cur.execute(
                        "SELECT extract(epoch from "
                        "window_started_at)::float8 AS ws, request_count "
                        "FROM demo_ip_request_rate WHERE ip_prefix_hash=%s "
                        "FOR UPDATE", (key,))
                row = cur.fetchone()
                ws = float(row["ws"]) if row is not None else 0.0
                count = int(row["request_count"]) if row is not None else 0
                ref = float(now) if now is not None else time.time()
                stale = (ref - ws) >= window
                window_started = ws if not stale else ref
                count = 0 if stale else count
                count += 1
                cur.execute(
                    "UPDATE demo_ip_request_rate SET "
                    "window_started_at=to_timestamp(%s), request_count=%s, "
                    "updated_at=now() WHERE ip_prefix_hash=%s",
                    (window_started, count, key))
        horizon = window_started + window
        remain = horizon - (float(now) if now is not None else time.time())
        retry_after = int(math.ceil(remain)) if remain > 0 else 1
        return {"allowed": count <= lim, "count": count,
                "limit": lim, "window_seconds": window,
                "retry_after_seconds": max(1, retry_after)}
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
    """设置默认 Demo 切片（唯一默认：先清旧再置）。不在目录内抛 ValueError。"""
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
