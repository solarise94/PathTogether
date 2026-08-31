# -*- coding: utf-8 -*-
"""平台 AI 预算存储原子原语（ai_budget_*，docs §4.1/§4.2/§5.3/§9.4）。

数据模型（migrations/0006_demo_budget_auth.sql + 0007_reservation_attempt.sql
+ 0009_rollback_epoch.sql + 0010_demo_task_max_steps_20.sql
+ 0012_registration_invites.sql + 0014_demo_daily_window.sql）：
  - ai_budget_periods：预算周期行（closed_at IS NULL 即当前开放周期）。默认值
    与迁移 DDL 一致（0014 起 Demo 子池改每日口径）：总 30 / Demo 每日 50 /
    owner 保留 10 / user 共享池 15 / 每 user 3（可 env 覆盖缺省周期创建值）/
    平台单次 20 步 / own 硬上限 500 / Demo 单次 20 步。
    Demo 子池自 0014 起为「每日（滚动 24 小时窗口）」口径：demo_turn_limit
    语义是单日上限，不随预算周期累计，也不因周期重置而清零（窗口按
    reserved_at 滚动，见 _demo_window_used）。
  - ai_budget_usage：按 (period, subject_type, subject_id, credential_source)
    聚合的 accepted/reserved 计数。平台总额度 = 同 period 内
    credential_source=platform 的 Σ(accepted+reserved)；own 只记可观测用量。
  - ai_budget_reservations：request_id 幂等键的预占（reserved|consumed|released），
    attempt 为执行尝试版本（released 后重新预占才递增；在途 reserved 重放不升版本）。

关键事务/锁设计（禁止先扣一个维度再失败）：
  - 所有跨步操作在**单事务**（pg_store.transaction）内完成；
  - reserve_turn 先锁周期行（SELECT ... FOR UPDATE），使同周期全部
    reserve/consume/release/release 回收串行化，再在锁内计算「平台总量 +
    Demo 子量 + 每 user 量」三项判定，任一超限抛对应业务异常并整体回滚；
  - 周期创建用 pg_advisory_xact_lock 串行化，避免并发首启建出两个开放周期；
  - request_id 已存在时先校验主体（subject_type / subject_id /
    credential_source）一致，不一致一律冲突；然后：consumed → 原样返回；
    reserved → 刷新 TTL 且不升 attempt（``replayed``，并递增
    ``rollback_epoch`` 使原请求的 release CAS 失效）；
    已 released → 重新预占（上次尝试已退款，重试属新执行尝试，attempt+1）；
  - release/consume 可带 expected_attempt 做 CAS；release 还可带
    expected_rollback_epoch，使后来的 reserved 重放令旧 rollback 失败；
  - 「HistoPilot 不可达不释放、顺延」的对账语义由上层调用方负责（历史上
    曾有按时间盲回收的 reclaim_expired 原语，已随确认式对账删除）。

批次 F（docs §7.3 阶段 2）：金额硬闸（spend_enforcement_mode）覆盖的主体
其 turn 消费闸在**调用点**分流关闭——mode_is_hard(mode, subject) 为真时
app 层不再调用 reserve_turn（软闸回退路径逐字保留），本模块内部逻辑不变；
owner/user 的 run→主体权威绑定改由 ai_run_bindings（record_run_binding /
get_run_binding）承担。ai_budget_* 表/列全部保留（冻结历史 + 只读报表兼容）。

json/dual 后端：全部公共入口经 platform_features.require_pg_backend fail-closed
（不静默退化内存计数）。本模块不接 Flask 路由。
"""

import os
import time

import psycopg

import pg_store
import platform_features


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# 常量（缺省周期按这些值创建；0012 起 user/owner/user_pool 默认值可 env 覆盖，
# 对齐 docs §3.7 推荐测试期默认：总 30 = owner 保留 10 + user 共享 15；
# 单 user 初始 3 次。Demo 子池自 0014 起改每日滚动 24h 口径，缺省 50 次/日，
# 不再按周期累计，也不参与周期加和约束）
# --------------------------------------------------------------------------- #
DEFAULT_PLATFORM_TURN_LIMIT = 30
DEFAULT_DEMO_TURN_LIMIT = 50
DEFAULT_USER_TURN_LIMIT = _int_env("BUDGET_DEFAULT_USER_TURNS", 3)
DEFAULT_OWNER_RESERVED_TURN_LIMIT = _int_env(
    "BUDGET_DEFAULT_OWNER_RESERVED_TURNS", 10)
DEFAULT_USER_POOL_TURN_LIMIT = _int_env("BUDGET_DEFAULT_USER_POOL_TURNS", 15)
DEFAULT_PLATFORM_TASK_MAX_STEPS = 20
DEFAULT_OWN_TASK_MAX_STEPS_LIMIT = 500
DEFAULT_DEMO_TASK_MAX_STEPS = 20
DEFAULT_DEMO_ENABLED = False
#: 批次 E（§4.1）：每浏览器累计次数闸已退役。demo_per_browser_limit 列仍在
#: _PERIOD_LIMIT_COLUMNS（admin 兼容展示，批次 F 一并清理），但运行时不再读取。
DEFAULT_DEMO_MAX_CONCURRENCY = 2

#: reservation 默认 TTL（docs §5.3：reserved 默认 10 分钟过期）
DEFAULT_RESERVATION_TTL_SECONDS = 600

SUBJECT_TYPES = ("owner", "user", "demo")
CREDENTIAL_SOURCES = ("platform", "own")

#: 周期创建串行化 advisory key（事务级；任意稳定 bigint，"AIBP"）
_PERIOD_LOCK_KEY = 0x41494250

#: 可通过 reset_period / update_period_limits 修改的周期限制列（白名单）。
#: 注意 demo_turn_limit 为「每日上限」（滚动 24h 窗口口径，见 _demo_window_used），
#: 与其余按周期累计的列口径不同。
_PERIOD_LIMIT_COLUMNS = {
    "platform_turn_limit": int,
    "demo_turn_limit": int,
    "user_turn_limit": int,
    "owner_reserved_turn_limit": int,
    "user_pool_turn_limit": int,
    "platform_task_max_steps": int,
    "own_task_max_steps_limit": int,
    "demo_task_max_steps": int,
    "demo_enabled": bool,
    # 批次 E 起 demo_per_browser_limit 仅兼容展示（无运行时闸）；批次 F 退役列
    "demo_per_browser_limit": int,
    "demo_max_concurrency": int,
}


# --------------------------------------------------------------------------- #
# 业务异常（带 error code 字符串，供路由映射稳定错误码）
# --------------------------------------------------------------------------- #
class BudgetError(Exception):
    """AI 预算业务异常基类。"""

    code = "ai_budget_error"

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


class PlatformBudgetExhausted(BudgetError):
    """平台总预算耗尽（docs §5.3：不回退到其他凭据）。"""

    code = "platform_ai_budget_exhausted"


class DemoBudgetExhausted(BudgetError):
    """Demo 子额度耗尽（计入总量的子额度，非预留专属）。"""

    code = "demo_budget_exhausted"


class UserBudgetExhausted(BudgetError):
    """该注册用户本周期平台 AI 对话额度耗尽。"""

    code = "user_budget_exhausted"


class UserPoolBudgetExhausted(BudgetError):
    """全部注册 user 共享池（user_pool_turn_limit）耗尽（docs §3.7）。"""

    code = "user_pool_budget_exhausted"


class OwnerReserveProtected(BudgetError):
    """user/Demo 合计用量将侵占 owner 保留池（platform - owner_reserve 上限）。"""

    code = "owner_reserve_protected"


class RequestIdSubjectConflict(BudgetError):
    """request_id 已被其他主体占用，禁止跨主体复用。"""

    code = "request_id_subject_conflict"


class DemoConcurrencyExceeded(BudgetError):
    """Demo 在途 reserved 数达到 demo_max_concurrency。"""

    code = "demo_concurrency_exceeded"


class ReservationAttemptConflict(BudgetError):
    """release/consume 的 expected_attempt 或 release 的 rollback_epoch 与当前行不一致。"""

    code = "reservation_attempt_conflict"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


_PERIOD_SEL = (
    "id, extract(epoch from started_at)::float8 AS started_at, "
    "extract(epoch from closed_at)::float8 AS closed_at, "
    "platform_turn_limit, demo_turn_limit, user_turn_limit, "
    "owner_reserved_turn_limit, user_pool_turn_limit, "
    "platform_task_max_steps, own_task_max_steps_limit, demo_task_max_steps, "
    "demo_enabled, demo_per_browser_limit, demo_max_concurrency, created_by"
)

_RESERVATION_SEL = (
    "request_id, period_id, subject_type, subject_id, credential_source, state, "
    "attempt, COALESCE(rollback_epoch, 0) AS rollback_epoch, "
    "extract(epoch from reserved_at)::float8 AS reserved_at, "
    "extract(epoch from reservation_expires_at)::float8 AS "
    "reservation_expires_at, histopilot_session_id, "
    "extract(epoch from updated_at)::float8 AS updated_at"
)


def _period_out(row) -> dict:
    """周期行 → dict（时间戳统一 epoch 浮点，对齐 share_store_pg 风格）。"""
    out = dict(row)
    out["id"] = int(out["id"])
    for k in _PERIOD_LIMIT_COLUMNS:
        if k in out and k != "demo_enabled":
            out[k] = int(out[k])
    out["demo_enabled"] = bool(out.get("demo_enabled"))
    return out


def _reservation_out(row) -> dict:
    out = dict(row)
    if out.get("attempt") is not None:
        out["attempt"] = int(out["attempt"])
    out["rollback_epoch"] = int(out.get("rollback_epoch") or 0)
    return out


def _validate_limits(new_limits):
    """校验/规范化限制字典：键须在白名单内，值按列类型强转。"""
    if new_limits is None:
        return {}
    if not isinstance(new_limits, dict):
        raise ValueError("new_limits 需为 dict 或 None")
    out = {}
    for k, v in new_limits.items():
        if k not in _PERIOD_LIMIT_COLUMNS:
            raise ValueError("未知预算限制字段：%r（允许 %s）"
                             % (k, sorted(_PERIOD_LIMIT_COLUMNS)))
        typ = _PERIOD_LIMIT_COLUMNS[k]
        if typ is bool:
            out[k] = bool(v)
        else:
            iv = int(v)
            if iv < 0:
                raise ValueError("预算限制字段 %s 不能为负：%r" % (k, v))
            out[k] = iv
    return out


def _limits_of(period) -> dict:
    """从周期 dict 提取限制列（reset_period 开新周期时沿用旧值）。"""
    return {k: period[k] for k in _PERIOD_LIMIT_COLUMNS if k in period}


def _sum_used(cur, period_id, subject_type=None, subject_id=None,
              credential_source=None) -> int:
    """Σ(accepted_turns + reserved_turns)，可按维度过滤。"""
    clauses = ["period_id=%s"]
    params = [period_id]
    if subject_type is not None:
        clauses.append("subject_type=%s")
        params.append(subject_type)
    if subject_id is not None:
        clauses.append("subject_id=%s")
        params.append(subject_id)
    if credential_source is not None:
        clauses.append("credential_source=%s")
        params.append(credential_source)
    cur.execute(
        "SELECT COALESCE(SUM(accepted_turns + reserved_turns), 0)::int AS used "
        "FROM ai_budget_usage WHERE " + " AND ".join(clauses), params)
    return int(cur.fetchone()["used"])


def _count_reserved(cur, period_id, subject_type=None) -> int:
    """当前周期 state=reserved 的在途预占条数（并发上限用）。"""
    clauses = ["period_id=%s", "state='reserved'"]
    params = [period_id]
    if subject_type is not None:
        clauses.append("subject_type=%s")
        params.append(subject_type)
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ai_budget_reservations WHERE "
        + " AND ".join(clauses), params)
    return int(cur.fetchone()["n"])


#: Demo 每日子池滚动窗口长度（小时）。0014 起 demo_turn_limit 为单日上限，
#: 计数按 reserved_at 滚动窗口（不按周期累计、不因周期重置清零）。
_DEMO_WINDOW_HOURS = 24

#: 计入 Demo 每日窗口的 reservation 状态：consumed（已消费）+ reserved（预占中）。
#: released（显式释放；盲时间回收原语已删除）已退款，不计入。
_DEMO_WINDOW_STATES = ("consumed", "reserved")


def _demo_window_used(cur, hours=_DEMO_WINDOW_HOURS) -> int:
    """Demo 每日子池用量：滚动窗口内有效 reservation 条数。

    直接数 ai_budget_reservations 流水（不用 ai_budget_usage 聚合——那是
    周期累计口径）：subject_type=demo & credential_source=platform &
    state IN (consumed, reserved) & reserved_at > now() - interval。
    released（显式退款 / 过期惰性回收）不计入；released 后同 request_id
    重新预占会刷新 reserved_at，按新窗口重新计数。
    不按 period_id 过滤：滚动窗口跨周期，正是「周期不滚动导致子池永久熄火」
    的修复点（reset_period 开新周期不会清窗口计数，24h 后自然滚出）。
    """
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ai_budget_reservations "
        "WHERE subject_type='demo' AND credential_source='platform' "
        "AND state = ANY(%s) "
        "AND reserved_at > now() - (%s * interval '1 hour')",
        (list(_DEMO_WINDOW_STATES), int(hours)))
    return int(cur.fetchone()["n"])


def subject_turn_total(subject_type, subject_id, credential_source="platform"):
    """当前周期该主体已用对话数（accepted + reserved）。"""
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            period = get_or_create_current_period(c)
            with c.cursor() as cur:
                return _sum_used(cur, period["id"], subject_type, subject_id,
                                 credential_source)
    finally:
        conn.close()


def _shift_usage(cur, resv, accepted_delta, reserved_delta):
    """按 reservation 行定位 usage 并平移计数（同事务内配对加减）。"""
    cur.execute(
        "UPDATE ai_budget_usage SET accepted_turns=accepted_turns+%s, "
        "reserved_turns=reserved_turns+%s, updated_at=now() "
        "WHERE period_id=%s AND subject_type=%s AND subject_id=%s "
        "AND credential_source=%s",
        (accepted_delta, reserved_delta, resv["period_id"],
         resv["subject_type"], resv["subject_id"], resv["credential_source"]),
    )


def _fetch_reservation_locked(cur, request_id):
    cur.execute(
        "SELECT " + _RESERVATION_SEL +
        " FROM ai_budget_reservations WHERE request_id=%s FOR UPDATE",
        (request_id,))
    return cur.fetchone()


# --------------------------------------------------------------------------- #
# 周期
# --------------------------------------------------------------------------- #
def get_or_create_current_period(conn, created_by=None):
    """取当前开放周期（closed_at IS NULL 的最新一行）；无则按默认值创建。

    在**调用方事务**内执行（conn 由调用方给出，pg_store.transaction 管理）。
    创建路径用 pg_advisory_xact_lock 串行化，防止并发首启建出两个开放周期。
    返回周期 dict（值为行内权威值——上层不得信任自己传入的任何默认参数）。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PERIOD_LOCK_KEY,))
        cur.execute(
            "SELECT " + _PERIOD_SEL + " FROM ai_budget_periods "
            "WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row is not None:
            return _period_out(row)
        cur.execute(
            "INSERT INTO ai_budget_periods (started_at, created_by, "
            "user_turn_limit, owner_reserved_turn_limit, user_pool_turn_limit) "
            "VALUES (now(), %s, %s, %s, %s) RETURNING " + _PERIOD_SEL,
            (created_by, DEFAULT_USER_TURN_LIMIT,
             DEFAULT_OWNER_RESERVED_TURN_LIMIT,
             DEFAULT_USER_POOL_TURN_LIMIT))
        return _period_out(cur.fetchone())


def get_current_period():
    """独立事务版：取当前开放周期（无则创建默认周期）。"""
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            return get_or_create_current_period(c)
    finally:
        conn.close()


def update_period_limits(new_limits):
    """只改当前开放周期行的限制列，不清用量（docs §4.2：保存上限不清空已用）。

    new_limits 为限制字段 dict（白名单见 _PERIOD_LIMIT_COLUMNS）；无开放周期时
    先按默认值创建再更新。返回更新后的周期 dict。
    """
    platform_features.require_pg_backend("ai_budget")
    vals = _validate_limits(new_limits)
    if not vals:
        raise ValueError("new_limits 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            period = get_or_create_current_period(c)
            sets = ", ".join("%s=%%s" % k for k in vals)  # 键来自白名单，无注入面
            params = list(vals.values()) + [period["id"]]
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ai_budget_periods SET " + sets +
                    " WHERE id=%s RETURNING " + _PERIOD_SEL, params)
                return _period_out(cur.fetchone())
    finally:
        conn.close()


def reset_period(new_limits=None, created_by=None):
    """关闭当前周期并按新限制开新周期（docs §4.2：开启新预算周期）。

    - 当前开放周期置 closed_at=now()；旧周期行与 usage **保留**（排查用）；
    - 新周期限制 = 旧周期限制被 new_limits 覆盖（None → 全沿用旧值）；
    - 用量归零来自「新周期无 usage 行」，不物理删除旧数据。
    返回新周期 dict。
    """
    platform_features.require_pg_backend("ai_budget")
    limits_overlay = _validate_limits(new_limits)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)",
                            (_PERIOD_LOCK_KEY,))
                cur.execute(
                    "SELECT " + _PERIOD_SEL + " FROM ai_budget_periods "
                    "WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1 FOR UPDATE")
                current = cur.fetchone()
                limits = {}
                if current is not None:
                    limits = _limits_of(_period_out(current))
                    cur.execute(
                        "UPDATE ai_budget_periods SET closed_at=now() WHERE id=%s",
                        (current["id"],))
                limits.update(limits_overlay)
                cols = sorted(limits)
                cur.execute(
                    "INSERT INTO ai_budget_periods (started_at, created_by"
                    + ("".join(", %s" % k for k in cols)) +
                    ") VALUES (now(), %s"
                    + (", %s" * len(cols)) + ") RETURNING " + _PERIOD_SEL,
                    [created_by] + [limits[k] for k in cols])
                return _period_out(cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 预占 / 消费 / 释放 / 回收
# --------------------------------------------------------------------------- #
def reserve_turn(request_id, subject_type, subject_id, credential_source,
                 ttl_seconds=DEFAULT_RESERVATION_TTL_SECONDS):
    """原子预占一次 AI 对话额度（docs §5.3 / §9.4）。

    单事务内完成：request_id 幂等检查 → 锁周期行 → 三项额度判定 → INSERT
    reservation(state=reserved, expires=now+ttl) + UPSERT usage(reserved+1)。
    任一维度超限抛 PlatformBudgetExhausted / DemoBudgetExhausted /
    UserBudgetExhausted（整体回滚，绝不先扣一个维度再失败）。

    request_id 幂等语义（按既有 reservation 状态区分）：
      - 任一状态若 subject_type / subject_id / credential_source 与本次不一致
        → 409 冲突（禁止跨主体复用同一 request_id）；
      - consumed 且主体一致 → 原样返回（已消费重试不重复扣、不升版本）；
      - reserved 且主体一致 → **不升 attempt**，只刷新 TTL，递增
        ``rollback_epoch``，并标记 ``replayed=True``（普通网络重试不得
        接管原执行；原请求随后失败不得用捕获到的旧 epoch 去 release）。
        换代只允许在 released 后重新预占（确认 missing/abandoned 后退款
        再开新尝试，attempt+1）；
      - released 且主体一致 → **重新预占**（同 rid 的上一次尝试已确定失败并退款；
        网关重试属于新的执行尝试，attempt+1，仍走额度判定）。

    - subject_type=demo 且 platform 凭据：查「每浏览器额度」之外的三项之一
      ——Demo 每日子额度（滚动 24h 窗口内 subject_type=demo & platform 的
      有效 reservation 计数，见 _demo_window_used）；
    - subject_type=user 且 platform 凭据：额外查该 user 本周期用量；
    - credential_source=own：不扣平台总量（无超限判定），仍落
      reservation/usage 供可观测。
    """
    platform_features.require_pg_backend("ai_budget")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id 不能为空")
    if subject_type not in SUBJECT_TYPES:
        raise ValueError("subject_type 需为 %s" % (SUBJECT_TYPES,))
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("subject_id 不能为空")
    if credential_source not in CREDENTIAL_SOURCES:
        raise ValueError("credential_source 需为 %s" % (CREDENTIAL_SOURCES,))
    ttl = int(ttl_seconds)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            period = get_or_create_current_period(c)
            with c.cursor() as cur:
                # 锁周期行：同周期全部额度变更串行化（杜绝并发超扣）。
                # 幂等 SELECT 必须在锁之后：两事务若都先看到「无此 request_id」
                # 再抢锁，先提交者插入后，后到者必须重新看见已有行，否则
                # INSERT 撞主键、整事务回滚，重试会变成 500 而不是幂等命中。
                cur.execute(
                    "SELECT closed_at FROM ai_budget_periods WHERE id=%s "
                    "FOR UPDATE", (period["id"],))
                locked = cur.fetchone()
                if locked is None or locked["closed_at"] is not None:
                    # 与 reset_period 并发的极小窗口：本事务回滚，调用方重试
                    raise BudgetError(
                        "预算周期已关闭（与 reset 并发），请重试",
                        period_id=period["id"])

                cur.execute(
                    "SELECT " + _RESERVATION_SEL +
                    " FROM ai_budget_reservations WHERE request_id=%s",
                    (request_id,))
                existing = cur.fetchone()
                if existing is not None:
                    # 主体一致性必须在所有状态分支之前校验：否则匿名调用者可
                    # 固定同一 request_id、清 cookie 换新 capability，HistoPilot
                    # 因 owner 不同仍启动新 run，预算却始终只计一次。
                    if (existing["subject_type"] != subject_type
                            or existing["subject_id"] != subject_id
                            or existing["credential_source"] != credential_source):
                        raise RequestIdSubjectConflict(
                            "request_id 已被其他主体使用，不能复用",
                            request_id=request_id)
                    if existing["state"] == "consumed":
                        return _reservation_out(existing)
                    if existing["state"] == "reserved":
                        # 在途重放：只刷新 TTL，不升 attempt；递增 rollback_epoch
                        # 使原请求捕获的 release CAS 失效。调用方看到
                        # replayed=True 时，连接失败不得 release 原执行。
                        cur.execute(
                            "UPDATE ai_budget_reservations SET "
                            "reservation_expires_at=now() + "
                            "(%s * interval '1 second'), "
                            "rollback_epoch=COALESCE(rollback_epoch,0)+1, "
                            "updated_at=now() "
                            "WHERE request_id=%s AND state='reserved' "
                            "RETURNING " + _RESERVATION_SEL,
                            (ttl, request_id))
                        bumped = cur.fetchone()
                        out = _reservation_out(bumped if bumped is not None
                                               else existing)
                        out["replayed"] = True
                        return out
                    # released：上次尝试已退款 → 重试按新预占（见 docstring）

                if credential_source == "platform":
                    _check_platform_quota(cur, period, subject_type, subject_id)

                cur.execute(
                    "INSERT INTO ai_budget_reservations "
                    "(request_id, period_id, subject_type, subject_id, "
                    " credential_source, state, attempt, reserved_at, "
                    " reservation_expires_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,'reserved', 1, now(), "
                    " now() + (%s * interval '1 second'), now()) "
                    "ON CONFLICT (request_id) DO UPDATE SET "
                    "state='reserved', period_id=EXCLUDED.period_id, "
                    "attempt=ai_budget_reservations.attempt+1, "
                    "rollback_epoch=0, "
                    "reserved_at=now(), "
                    "reservation_expires_at=EXCLUDED.reservation_expires_at, "
                    "histopilot_session_id=NULL, updated_at=now() "
                    "RETURNING " + _RESERVATION_SEL,
                    (request_id, period["id"], subject_type, subject_id,
                     credential_source, ttl))
                row = cur.fetchone()
                cur.execute(
                    "INSERT INTO ai_budget_usage "
                    "(period_id, subject_type, subject_id, credential_source, "
                    " accepted_turns, reserved_turns, updated_at) "
                    "VALUES (%s,%s,%s,%s,0,1,now()) "
                    "ON CONFLICT (period_id, subject_type, subject_id, "
                    " credential_source) DO UPDATE SET "
                    " reserved_turns=ai_budget_usage.reserved_turns+1, "
                    " updated_at=now()",
                    (period["id"], subject_type, subject_id, credential_source),
                )
                return _reservation_out(row)
    finally:
        conn.close()


def _check_platform_quota(cur, period, subject_type, subject_id):
    """平台凭据额度判定（须在周期行 FOR UPDATE 锁内调用）。

    顺序：平台总量 → 非主体细分判定。任一超限抛对应业务异常（事务整体回滚）。

    docs §3.7 池隔离（P0-B）：
      - owner：只受平台总量约束（保留池是「为 owner 保住的下限」，不设独立
        上限；下限由 user/Demo 的闸保证）；
      - user：单 user 上限 → user 共享池（Σ subject_type=user）→ owner 保留
        保护（Σ user + Σ demo ≤ platform_turn_limit - owner_reserved_turn_limit）；
      - demo：Demo 每日子额度（滚动 24h 窗口，_demo_window_used；0014 起
        demo_turn_limit 为单日上限，与周期总量口径不同，不再可比）/并发 →
        owner 保留保护（同上；owner 保留闸仍按周期累计口径判定）。批次 E 起
        每浏览器累计次数闸已退役（见下）；同 capability 单 active run 约束在
        demo_store.demo_runs（部分唯一索引），不在此处。
    """
    pid = period["id"]
    used_total = _sum_used(cur, pid, credential_source="platform")
    if used_total + 1 > period["platform_turn_limit"]:
        raise PlatformBudgetExhausted(
            "平台 AI 总预算已耗尽（%d/%d）" % (
                used_total, period["platform_turn_limit"]),
            limit=period["platform_turn_limit"], used=used_total)
    if subject_type == "demo":
        # 每日子池闸：滚动 24h 窗口计数 vs demo_turn_limit（单日上限）
        used_demo_window = _demo_window_used(cur)
        if used_demo_window + 1 > period["demo_turn_limit"]:
            raise DemoBudgetExhausted(
                "Demo 每日额度已耗尽（滚动 24 小时，%d/%d）" % (
                    used_demo_window, period["demo_turn_limit"]),
                limit=period["demo_turn_limit"], used=used_demo_window)
        # 批次 E（§1.2/§4.1）：每浏览器累计次数闸（demo_per_browser_limit /
        # DemoPerBrowserExhausted / demo_run_already_used）已退役——capability
        # 与 run 分离后同 capability 可顺序多次 run，无累计成功次数上限。
        # 「同 capability 同时至多一个 active run」由 demo_store.demo_runs 的
        # 部分唯一索引保证，不在本判定内。
        max_cc = int(period.get("demo_max_concurrency")
                     or DEFAULT_DEMO_MAX_CONCURRENCY)
        if max_cc > 0:
            in_flight = _count_reserved(cur, pid, subject_type="demo")
            if in_flight + 1 > max_cc:
                raise DemoConcurrencyExceeded(
                    "Demo 并发已达上限（%d/%d）" % (in_flight, max_cc),
                    limit=max_cc, used=in_flight)
        # owner 保留保护仍按周期累计口径判定（与平台总量闸同口径），
        # 单独重算周期值，不复用上面的 24h 窗口计数
        used_demo = _sum_used(cur, pid, subject_type="demo",
                              credential_source="platform")
        _check_owner_reserve_guard(cur, period, used_demo=used_demo)
    elif subject_type == "user":
        used_user = _sum_used(cur, pid, subject_type="user",
                              subject_id=subject_id,
                              credential_source="platform")
        if used_user + 1 > period["user_turn_limit"]:
            raise UserBudgetExhausted(
                "该用户本周期平台 AI 额度已耗尽（%d/%d）" % (
                    used_user, period["user_turn_limit"]),
                limit=period["user_turn_limit"], used=used_user,
                subject_id=subject_id)
        # user 共享池（docs §3.7：所有 user 共享，而非仅「每 user + 全站总」）
        used_user_pool = _sum_used(cur, pid, subject_type="user",
                                   credential_source="platform")
        if used_user_pool + 1 > period["user_pool_turn_limit"]:
            raise UserPoolBudgetExhausted(
                "用户共享 AI 额度已耗尽（%d/%d）" % (
                    used_user_pool, period["user_pool_turn_limit"]),
                limit=period["user_pool_turn_limit"], used=used_user_pool)
        _check_owner_reserve_guard(cur, period, used_user_pool=used_user_pool)


def _check_owner_reserve_guard(cur, period, used_user_pool=0, used_demo=0):
    """owner 保留池保护：user+Demo 合计不得越过 platform - owner_reserve。

    必须在周期行 FOR UPDATE 锁内调用（与 _check_platform_quota 同事务）。
    owner 自身不受本闸约束（保留池就是给 owner 留的）。

    guard = platform_turn_limit - owner_reserved_turn_limit < 0 视为配置错误
    （保留池大于总池；app 层校验已阻止，直接 store 调用可绕过）——此时本闸
    不再额外阻断，平台总量闸仍兜底，避免小总池场景把语义吞成 owner_reserve。
    """
    pid = period["id"]
    if "user_pool_turn_limit" not in period or \
            "owner_reserved_turn_limit" not in period:
        return  # 旧周期行缺列（迁移前）：DDL 兜底默认已补，正常不可达
    used_users = int(used_user_pool or _sum_used(
        cur, pid, subject_type="user", credential_source="platform"))
    used_demo_total = int(used_demo or _sum_used(
        cur, pid, subject_type="demo", credential_source="platform"))
    reserve = int(period["owner_reserved_turn_limit"])
    guard = int(period["platform_turn_limit"]) - reserve
    if guard < 0:
        return  # 保留池配置大于总池：交由平台总量闸兜底（见 docstring）
    if used_users + used_demo_total + 1 > guard:
        raise OwnerReserveProtected(
            "用户与 Demo 合计用量已达上限（%d/%d），owner 保留 %d 次不可被占用"
            % (used_users + used_demo_total, guard, reserve),
            limit=guard, used=used_users + used_demo_total,
            owner_reserved=reserve)


def consume(request_id, histopilot_session_id, expected_attempt=None):
    """预占 → 已消费（HistoPilot 确认接受后调用）。

    reserved→consumed，usage reserved-1 / accepted+1。幂等：已 consumed 直接
    返回；released 状态拒绝（不能凭空消费）。reservation 不存在返回 None。
    expected_attempt 给出时按 attempt CAS：版本落后则
    ReservationAttemptConflict（对账不得消费已被新尝试接替的行）。
    """
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_reservation_locked(cur, request_id)
                if row is None:
                    return None
                if row["state"] == "consumed":
                    return _reservation_out(row)  # 幂等
                if row["state"] != "reserved":
                    raise ValueError(
                        "reservation 状态为 %s，不能转为 consumed（仅 reserved 可）"
                        % row["state"])
                if expected_attempt is not None:
                    actual = int(row.get("attempt") or 1)
                    if actual != int(expected_attempt):
                        raise ReservationAttemptConflict(
                            "reservation attempt 不匹配（expected=%s actual=%s）"
                            % (expected_attempt, actual),
                            request_id=request_id,
                            expected_attempt=int(expected_attempt),
                            actual_attempt=actual)
                cur.execute(
                    "UPDATE ai_budget_reservations SET state='consumed', "
                    "histopilot_session_id=%s, updated_at=now() "
                    "WHERE request_id=%s",
                    (histopilot_session_id, request_id))
                _shift_usage(cur, row, accepted_delta=1, reserved_delta=-1)
                out = _reservation_out(row)
                out["state"] = "consumed"
                out["histopilot_session_id"] = histopilot_session_id
                return out
    finally:
        conn.close()


def release(request_id, expected_attempt=None, expected_rollback_epoch=None):
    """释放预占（HistoPilot 接受前失败时调用）。

    reserved→released，usage reserved-1。幂等：已 released 直接返回；已
    consumed **拒绝释放**（防误退款——模型已开始执行不退额度）。不存在返回 None。
    expected_attempt 给出时按 attempt CAS：版本落后则
    ReservationAttemptConflict（对账不得释放已被同 ID 新尝试接替的行）。
    expected_rollback_epoch 给出时按 rollback_epoch CAS：在途重放已 +1 则
    原请求的即时退款失败（确认式对账不传本参数，仍可在 missing 后释放）。
    """
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_reservation_locked(cur, request_id)
                if row is None:
                    return None
                if row["state"] == "released":
                    return _reservation_out(row)  # 幂等
                if row["state"] == "consumed":
                    raise ValueError(
                        "已 consumed 的 reservation 不能释放（防误退款）")
                if expected_attempt is not None:
                    actual = int(row.get("attempt") or 1)
                    if actual != int(expected_attempt):
                        raise ReservationAttemptConflict(
                            "reservation attempt 不匹配（expected=%s actual=%s）"
                            % (expected_attempt, actual),
                            request_id=request_id,
                            expected_attempt=int(expected_attempt),
                            actual_attempt=actual)
                if expected_rollback_epoch is not None:
                    actual_epoch = int(row.get("rollback_epoch") or 0)
                    if actual_epoch != int(expected_rollback_epoch):
                        raise ReservationAttemptConflict(
                            "reservation rollback_epoch 不匹配（expected=%s actual=%s）"
                            % (expected_rollback_epoch, actual_epoch),
                            request_id=request_id,
                            expected_rollback_epoch=int(expected_rollback_epoch),
                            actual_rollback_epoch=actual_epoch)
                cur.execute(
                    "UPDATE ai_budget_reservations SET state='released', "
                    "updated_at=now() WHERE request_id=%s", (request_id,))
                _shift_usage(cur, row, accepted_delta=0, reserved_delta=-1)
                out = _reservation_out(row)
                out["state"] = "released"
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# run→主体绑定（批次 F：金额时代 ai_run_bindings，替代 reservations 的绑定角色）
# --------------------------------------------------------------------------- #
#: ai_run_bindings 出口列（时间为 epoch 秒 float，对齐仓库惯例）
_RUN_BINDING_SEL = (
    "request_id, subject_type, subject_id, histopilot_session_id, "
    "installation_id, extract(epoch from created_at)::float8 AS created_at"
)

#: 允许入 ai_run_bindings 的主体（demo 绑定归 demo_runs.histopilot_session_id，
#: 0026；本表不覆盖 demo——CHECK 约束同口径）
_RUN_BINDING_SUBJECT_TYPES = ("owner", "user")


def record_run_binding(request_id, session_id, subject_type, subject_id,
                       installation_id=None):
    """记录一条金额时代的 run→主体权威绑定（批次 F，docs §7.3 阶段 2）。

    金额硬闸（spend_enforcement_mode 覆盖 owner/user）下起跑不再写
    ai_budget_reservations：run 的 request_id 幂等与计费主体归属由
    ai_run_bindings 承担。本函数在 HistoPilot 2xx 接受后调用（on_accepted
    携带 histopilot_session_id），供随后到达的 usage event / hold authorize
    做 §7.2 第①步解析。

    幂等/冲突语义对齐旧 reserve_turn 的 request_id 行为：
      - 首次写入 → 返回行，``replayed=False``；
      - 已有行且 subject_type/subject_id/histopilot_session_id 一致 → 原样
        返回并标 ``replayed=True``（网络重试不产生第二行）；
      - 已有行但主体或 session 不一致 → :class:`RequestIdSubjectConflict`
        （禁止跨主体复用同一 request_id；HTTP 映射 409，与旧路径一致）；
      - subject_type 仅接受 owner/user（demo 不入本表，ValueError）。
    """
    platform_features.require_pg_backend("ai_budget")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id 不能为空")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    if subject_type not in _RUN_BINDING_SUBJECT_TYPES:
        raise ValueError("subject_type 需为 %s（demo 绑定归 demo_runs）"
                         % (_RUN_BINDING_SUBJECT_TYPES,))
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("subject_id 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_run_bindings "
                    "(request_id, subject_type, subject_id, "
                    " histopilot_session_id, installation_id) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (request_id) DO NOTHING "
                    "RETURNING " + _RUN_BINDING_SEL,
                    (request_id, subject_type, subject_id, session_id,
                     installation_id))
                row = cur.fetchone()
                if row is not None:
                    out = dict(row)
                    out["replayed"] = False
                    return out
                # 冲突回读：主体一致性在返回前校验（与 reserve_turn 的
                # 「所有状态分支之前校验主体」同一安全属性）
                cur.execute(
                    "SELECT " + _RUN_BINDING_SEL +
                    " FROM ai_run_bindings WHERE request_id=%s",
                    (request_id,))
                existing = cur.fetchone()
                if (existing["subject_type"] != subject_type
                        or existing["subject_id"] != subject_id
                        or existing["histopilot_session_id"] != session_id):
                    raise RequestIdSubjectConflict(
                        "request_id 已被其他主体或会话使用，不能复用",
                        request_id=request_id)
                out = dict(existing)
                out["replayed"] = True
                return out
    finally:
        conn.close()


def get_run_binding(request_id):
    """按 request_id 读 run 绑定（只读）；不存在返回 None。"""
    platform_features.require_pg_backend("ai_budget")
    if not isinstance(request_id, str) or not request_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RUN_BINDING_SEL +
                    " FROM ai_run_bindings WHERE request_id=%s", (request_id,))
                row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_reservation(request_id):
    """按 request_id 查预占（只读）；不存在返回 None。"""
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RESERVATION_SEL +
                    " FROM ai_budget_reservations WHERE request_id=%s",
                    (request_id,))
                row = cur.fetchone()
        return _reservation_out(row) if row is not None else None
    finally:
        conn.close()


def list_reserved_expired(now=None):
    """列出 reserved 且 reservation_expires_at < now 的预占（对账用，不改状态）。

    上层按 request_id 经 HistoPilot /session/by-request/<rid> 反查确认终态后，
    再决定 consume / release / 顺延（docs §5.3-6；历史上的盲时间回收原语
    reclaim_expired 已删除——它会把 HistoPilot 已接受的执行误退款）。
    """
    platform_features.require_pg_backend("ai_budget")
    ts = float(now if now is not None else time.time())
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _RESERVATION_SEL +
                    " FROM ai_budget_reservations "
                    "WHERE state='reserved' AND reservation_expires_at < "
                    "to_timestamp(%s) ORDER BY reserved_at", (ts,))
                rows = cur.fetchall()
        return [_reservation_out(r) for r in rows]
    finally:
        conn.close()


def extend_reservation(request_id, ttl_seconds=DEFAULT_RESERVATION_TTL_SECONDS):
    """顺延预占过期时间（对账时 HistoPilot 不可达 → 不释放、顺延，§5.3-6）。

    仅 reserved 状态可顺延；返回更新后的 reservation dict 或 None（不存在）。
    """
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ai_budget_reservations SET "
                    "reservation_expires_at=now() + (%s * interval '1 second'), "
                    "updated_at=now() WHERE request_id=%s AND state='reserved' "
                    "RETURNING " + _RESERVATION_SEL,
                    (float(ttl_seconds), request_id))
                row = cur.fetchone()
        return _reservation_out(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 用量报表（owner 后台「AI 预算」卡片数据源，docs §4.2）
# --------------------------------------------------------------------------- #
def usage_report():
    """当前周期用量报告：总量、Demo/user/owner 构成、每 user 明细、own 可观测。

    口径注意（0014 起）：demo.total 为**滚动 24h 窗口计数**（_demo_window_used，
    与 Demo 每日子额度闸同口径、跨周期），demo.limit 即单日上限
    demo_turn_limit；demo.accepted/reserved 仍为周期累计（来自 usage 聚合），
    故 total != accepted + reserved（窗口会把 24h 外的周期用量滚出）。
    其余区段（platform/user_pool/owner/per_user/own）保持周期累计口径。
    """
    platform_features.require_pg_backend("ai_budget")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            period = get_or_create_current_period(c)
            pid = period["id"]
            with c.cursor() as cur:
                # Demo 每日子池：滚动 24h 窗口计数（与闸同口径；不用 usage 聚合）
                demo_window_total = _demo_window_used(cur)
                cur.execute(
                    "SELECT COALESCE(SUM(accepted_turns),0)::int AS accepted, "
                    "COALESCE(SUM(reserved_turns),0)::int AS reserved "
                    "FROM ai_budget_usage "
                    "WHERE period_id=%s AND credential_source='platform'",
                    (pid,))
                platform_agg = dict(cur.fetchone())
                cur.execute(
                    "SELECT subject_type, "
                    "COALESCE(SUM(accepted_turns),0)::int AS accepted, "
                    "COALESCE(SUM(reserved_turns),0)::int AS reserved "
                    "FROM ai_budget_usage "
                    "WHERE period_id=%s AND credential_source='platform' "
                    "GROUP BY subject_type", (pid,))
                by_type = {r["subject_type"]: dict(r)
                           for r in cur.fetchall()}
                cur.execute(
                    "SELECT subject_id, accepted_turns, reserved_turns "
                    "FROM ai_budget_usage "
                    "WHERE period_id=%s AND subject_type='user' "
                    "AND credential_source='platform' ORDER BY subject_id",
                    (pid,))
                per_user = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT COALESCE(SUM(accepted_turns),0)::int AS accepted, "
                    "COALESCE(SUM(reserved_turns),0)::int AS reserved "
                    "FROM ai_budget_usage "
                    "WHERE period_id=%s AND credential_source='own'", (pid,))
                own_agg = dict(cur.fetchone())

        def _tot(d):
            return int(d["accepted"]) + int(d["reserved"])

        demo = by_type.get("demo", {"accepted": 0, "reserved": 0})
        owner = by_type.get("owner", {"accepted": 0, "reserved": 0})
        users_all = by_type.get("user", {"accepted": 0, "reserved": 0})
        return {
            "period": period,
            "platform": {
                "accepted": int(platform_agg["accepted"]),
                "reserved": int(platform_agg["reserved"]),
                "total": _tot(platform_agg),
                "limit": period["platform_turn_limit"],
            },
            "demo": {
                # total：滚动 24h 窗口计数（与闸同口径）；accepted/reserved：
                # 周期累计（见 docstring 口径说明）
                "accepted": int(demo["accepted"]),
                "reserved": int(demo["reserved"]),
                "total": demo_window_total,
                "limit": period["demo_turn_limit"],
            },
            # owner 侧用量与保留池（docs §3.7：owner 可用下限 = reserve - 已用）
            "owner": {
                "accepted": int(owner["accepted"]),
                "reserved": int(owner["reserved"]),
                "total": _tot(owner),
                "reserved_limit": int(period.get(
                    "owner_reserved_turn_limit",
                    DEFAULT_OWNER_RESERVED_TURN_LIMIT)),
            },
            # 全部注册 user 共享池（docs §3.7）
            "user_pool": {
                "accepted": int(users_all["accepted"]),
                "reserved": int(users_all["reserved"]),
                "total": _tot(users_all),
                "limit": int(period.get("user_pool_turn_limit",
                                        DEFAULT_USER_POOL_TURN_LIMIT)),
            },
            # Demo/user/owner 构成（均指 platform 凭据；own 单列）
            "by_subject_type": {
                t: {
                    "accepted": int(by_type[t]["accepted"]),
                    "reserved": int(by_type[t]["reserved"]),
                    "total": _tot(by_type[t]),
                } for t in SUBJECT_TYPES if t in by_type
            },
            "per_user": [
                {
                    "subject_id": r["subject_id"],
                    "accepted": int(r["accepted_turns"]),
                    "reserved": int(r["reserved_turns"]),
                    "total": int(r["accepted_turns"]) + int(r["reserved_turns"]),
                    "limit": period["user_turn_limit"],
                } for r in per_user
            ],
            "own": {
                "accepted": int(own_agg["accepted"]),
                "reserved": int(own_agg["reserved"]),
                "total": _tot(own_agg),
            },
        }
    finally:
        conn.close()
