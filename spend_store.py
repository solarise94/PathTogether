# -*- coding: utf-8 -*-
"""金额额度 policy/window 数据层（批次 B，docs
ai-money-budget-bugfix-and-simplification-plan.md §1.1/§3.1/§3.2/§7.3/§8）。

**本批仍为 shadow**：``spend_enforcement_mode`` 固定 ``"shadow"``（0023 迁移
种子），本模块的任何原语都**不**接入 run/hold/usage 请求路径——把窗口接进
billing_holds 链路是批次 C；turn 预算（budget_store）本批不动。

内容（仿 billing_store 惯例：模块级函数 + 中文注释 + PG-only fail-closed）：

- 窗口边界计算（§1.1）：calendar_week = 周一 00:00 Asia/Shanghai（含）→
  下周一 00:00（不含）；calendar_month = 每月 1 日 00:00（含）→ 次月 1 日
  00:00（不含）。输入任意 tz-aware datetime / RFC3339 / epoch 秒，naive 输入
  拒绝（无法判定口径）；输出 UTC 边界。边界永远由服务端生成，客户端不得
  提交任意 window_start/window_end；
- 策略解析（§3.1）：user → 先 ``user_override(user_id)`` 无则回退
  ``user_default``；demo → 唯一 ``demo_global``（多 capability 全归同一周
  窗口，subject_id 归一为 ``demo_global``）；owner → 独立 ``owner`` 策略，
  不与用户/Demo 共池。解析考虑 enabled 与 [effective_from, effective_to)
  区间；同 scope 同刻多条有效策略由 0023 部分唯一索引在 DB 层禁止；
- ``get_or_create_window``（§3.2）：按当前时刻解析策略 → 算边界 →
  UNIQUE(subject_type, subject_id, window_start, window_end) + ON CONFLICT
  兜底并发插入（并发只产生一行）；``limit_nano_snapshot`` 固定创建时刻的
  策略额度，策略后续修改**不追溯**已开窗口；新用户当月完整额度不折算
  （snapshot 直接取策略面值，不按剩余天数缩放）；
- 原子投影原语（§3.2 语义，全部事务内 ``SELECT ... FOR UPDATE`` 锁窗口行，
  并发安全是硬性要求）：
  - ``window_reserve``：检查 ``spent + reserved + estimated <=
    limit_nano_snapshot``，超限抛稳定 ``spend_budget_exhausted``（不改数）；
  - ``window_release``：``reserved -= estimated``（下限 0，越界释放记指标
    ——重放/乱序由批次 C 的 hold 状态机吸收，本原语只保证不变量）；
  - ``window_settle``：``reserved -= estimated``、``spent += actual``；
    ``actual > estimated`` 允许并按真实成本入账（overage 记指标）；
- 策略管理：``update_policy_limit``（version/CAS，冲突 409 语义；默认更新
  只影响新窗口）、``set_user_override`` / ``clear_user_override``（收口旧行
  + 新行，保留历史，满足部分唯一索引）；
- ``adjust_current_window``（§1.1「调整当前周期」）：只改
  ``limit_nano_snapshot``，CAS + audit（写 audit_events），不取消已完成
  消费；调低到低于 spent 后，下一次 ``window_reserve`` 必须拒绝（由
  spent+reserved+estimated<=limit 检查自然成立）；``confirm`` 参数为批次 D
  的二次确认语义预留（本层只如实记入 audit，不在这里裁决）；
- 对账器 ``reconcile_spend_windows``（§9.7）：从 usage events（priced、
  occurred_at >= max(窗口起点, pricing_v2_cutover_at)）与 open holds 重算
  各 open 窗口应有的 spent/reserved，报告 drift 清单，**不自动修**；
- 指标：拒绝/overage/越界释放计数沿用仓库现有惯例（进程内计数 + 单行
  JSON 日志，仿 billing_store._sim_debit_note_failure），不造新框架。

审计红线：不落 prompt/输出/API key/完整 IP/完整请求体；audit detail 只含
窗口/策略标识、金额（nano）与版本号等非敏感字段。
"""

import json
import logging
import secrets
from datetime import date, datetime, time, timedelta, timezone

import psycopg

import billing_pricing
import billing_store
import pg_store
import platform_features
import share_store_pg

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 业务周期时区（§1.1：全部业务周期以 Asia/Shanghai 计算，DB 存 UTC）
SPEND_TIMEZONE = billing_pricing.PRICING_TIMEZONE

#: scope_type / period_kind / subject_type 词表（与 0023 CHECK 一致）
POLICY_SCOPE_TYPES = ("demo_global", "user_default", "user_override", "owner")
PERIOD_KINDS = ("calendar_week", "calendar_month", "none")
WINDOW_SUBJECT_TYPES = ("demo", "user", "owner")

#: demo 主体的窗口 subject_id（所有浏览器/capability 归同一周窗口）
DEMO_GLOBAL_SUBJECT = "demo_global"

#: enforcement 开关（§7.3；0023 种子固定 "shadow"，本批不改）
SPEND_ENFORCEMENT_MODE_KEY = "spend_enforcement_mode"
SPEND_ENFORCEMENT_MODES = ("shadow", "registered", "all")
DEFAULT_ENFORCEMENT_MODE = "shadow"

#: 策略写路径串行化 advisory key（事务级；稳定 bigint "SPPW"）
_POLICY_LOCK_KEY = 0x53505057

#: 策略/窗口 id 前缀
_OVERRIDE_ID_PREFIX = "spp_uo_"
_WINDOW_ID_PREFIX = "spw_"

#: audit 动作名（detail 只含非敏感字段）
POLICY_UPDATE_AUDIT_ACTION = "spend.policy_update"
WINDOW_ADJUST_AUDIT_ACTION = "spend.window_adjust"

#: 0022 写入的 cutover 标志键（对账器只接纳 occurred_at >= cutover 的用量）
_PRICING_CUTOVER_KEY = billing_store.PRICING_V2_CUTOVER_SETTING_KEY

#: 进程内指标计数（重启归零；观测用，不做限流；仿 billing_store 计数惯例）
_METRICS = {}


def _metric(name, **fields):
    """指标：进程内计数 + 单行 JSON 日志（无敏感字段，日志侧可聚合）。"""
    _METRICS[name] = _METRICS.get(name, 0) + 1
    payload = {"metric": name, "value": _METRICS[name]}
    payload.update(fields)
    _LOG.warning("[spend] %s", json.dumps(payload, sort_keys=True,
                                          ensure_ascii=False))


def metrics_snapshot():
    """当前进程内指标快照（测试/观测用，只读）。"""
    return dict(_METRICS)


# --------------------------------------------------------------------------- #
# 业务异常（code 稳定，§3.3 建议错误码子集；供路由映射错误信封）
# --------------------------------------------------------------------------- #
class SpendError(Exception):
    """spend 业务异常基类。"""

    code = "spend_error"
    retryable = False

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


class SpendPolicyMissingError(SpendError):
    """解析不到任何有效策略（fail-closed，不落无额度窗口）。"""

    code = "spend_policy_missing"


class SpendWindowUnavailableError(SpendError):
    """窗口不存在（或已关闭不能再预占）。"""

    code = "spend_window_unavailable"


class SpendBudgetExhaustedError(SpendError):
    """spent+reserved+estimated 超过 limit_nano_snapshot（稳定拒绝，不改数）。"""

    code = "spend_budget_exhausted"


class SpendVersionConflictError(SpendError):
    """策略/窗口 CAS 更新未命中（客户端 version 过期 → 409 语义）。"""

    code = "version_conflict"


class InvalidSpendRequestError(SpendError):
    """参数校验失败（400 invalid_request）。"""

    code = "invalid_request"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# §1.1 窗口边界计算（纯函数；服务端生成，客户端不得提交）
# --------------------------------------------------------------------------- #
def _as_aware_at(at) -> datetime:
    """窗口时间参数归一：tz-aware datetime / RFC3339 字符串 / epoch 秒 → UTC dt。

    naive datetime 拒绝（无法判定「哪个时区的周一/月初」，fail-closed 不猜）。
    """
    if isinstance(at, str):
        return billing_pricing.parse_rfc3339(at)
    if isinstance(at, bool):
        raise ValueError("时间参数非法（bool）")
    if isinstance(at, (int, float)):
        return datetime.fromtimestamp(float(at), tz=timezone.utc)
    if isinstance(at, datetime):
        if at.tzinfo is None:
            raise ValueError(
                "时间参数需为 tz-aware datetime（naive 无法判定周期口径）")
        return at.astimezone(timezone.utc)
    raise ValueError("时间参数需为 datetime / RFC3339 字符串 / epoch 秒")


def _local_midnight_utc(day: date) -> datetime:
    """上海时区某日 00:00 → UTC。

    按本地日历逐日构造再转 UTC（不做 ``utc + timedelta`` 平移）——上海虽无
    夏令时，实现不假设固定偏移：任何时区的「自然日零点」都按当地日历取，
    下一个边界同样由日历推导（week +7 天取 Monday、month 取次月 1 日）。
    """
    return datetime.combine(day, time(0, 0),
                            tzinfo=SPEND_TIMEZONE).astimezone(timezone.utc)


def week_window_bounds(at):
    """calendar_week 边界（§1.1）：周一 00:00（含）→ 下周一 00:00（不含）。

    返回 ``(start_utc, end_utc)``（tz-aware UTC）。at 恰为周一 00:00 本地时
    落入新一周（左闭）；周日 23:59:59.999… 仍属上一周。
    """
    at = _as_aware_at(at)
    local_day = at.astimezone(SPEND_TIMEZONE).date()
    monday = local_day - timedelta(days=local_day.weekday())
    return (_local_midnight_utc(monday),
            _local_midnight_utc(monday + timedelta(days=7)))


def month_window_bounds(at):
    """calendar_month 边界（§1.1）：每月 1 日 00:00（含）→ 次月 1 日（不含）。

    月末/月初、2 月、闰年（28/29 天）与 12 月→次年 1 月均按本地日历取次月
    1 日（不假设每月等长）。
    """
    at = _as_aware_at(at)
    local_day = at.astimezone(SPEND_TIMEZONE).date()
    first = local_day.replace(day=1)
    year, month = first.year, first.month
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    return _local_midnight_utc(first), _local_midnight_utc(next_first)


def window_bounds(period_kind, at):
    """period_kind → (start_utc, end_utc)。``none`` 无窗口语义（本批未定义，
    显式拒绝而非悄悄给一个「无限窗口」）。"""
    if period_kind == "calendar_week":
        return week_window_bounds(at)
    if period_kind == "calendar_month":
        return month_window_bounds(at)
    raise InvalidSpendRequestError(
        "period_kind=%r 无窗口边界语义（calendar_week/calendar_month）"
        % (period_kind,), period_kind=period_kind)


# --------------------------------------------------------------------------- #
# §3.1 策略解析（cursor 注入变体 + 独立事务版）
# --------------------------------------------------------------------------- #
_POLICY_SEL = (
    "policy_id, scope_type, scope_id, period_kind, limit_nano_cny, enabled, "
    "effective_from, effective_to, version, updated_by, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at"
)


def _policy_out(row) -> dict:
    out = dict(row)
    for key in ("limit_nano_cny", "version"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    out["enabled"] = bool(out["enabled"])
    return out


def _scope_candidates(subject_type, subject_id):
    """主体 → 按 §3.1 优先级排列的 (scope_type, scope_id) 候选。

    demo 主体任何 capability 都归 demo_global（subject_id 不参与解析）；
    owner 独立解析，不消费用户/Demo 池。
    """
    if subject_type == "user":
        return [("user_override", subject_id), ("user_default", None)]
    if subject_type == "demo":
        return [("demo_global", None)]
    if subject_type == "owner":
        return [("owner", None)]
    raise InvalidSpendRequestError(
        "subject_type 需为 %s" % (WINDOW_SUBJECT_TYPES,),
        subject_type=subject_type)


def _resolve_policy_tx(cur, subject_type, subject_id, at) -> "dict | None":
    """解析 at 时刻的有效策略（cursor 注入，调用方事务内）。

    逐候选按序取第一条 ``enabled 且 effective_from <= at < effective_to``
    的策略（同候选多条时取 effective_from 最新——0023 部分唯一索引已保证
    至多一条未收口行，这里只是确定性兜底）。
    """
    for scope_type, scope_id in _scope_candidates(subject_type, subject_id):
        cur.execute(
            "SELECT " + _POLICY_SEL + " FROM ai_spend_policies "
            "WHERE scope_type=%s AND scope_id IS NOT DISTINCT FROM %s "
            "  AND enabled AND effective_from <= %s "
            "  AND (effective_to IS NULL OR effective_to > %s) "
            "ORDER BY effective_from DESC LIMIT 1",
            (scope_type, scope_id, at, at))
        row = cur.fetchone()
        if row is not None:
            return _policy_out(row)
    return None


def resolve_policy(subject_type, subject_id, at=None):
    """独立事务版：解析 at 时刻的有效策略；无则 None（调用方裁决）。"""
    platform_features.require_pg_backend("spend")
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _resolve_policy_tx(cur, subject_type, subject_id, at)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §3.2 窗口 get_or_create（UNIQUE 兜底并发，一行到底）
# --------------------------------------------------------------------------- #
#: 出口列（时间为 epoch 秒 float，与 billing_store admin 出口同口径）
_WINDOW_SEL = (
    "window_id, policy_id, policy_version, subject_type, subject_id, "
    "extract(epoch from window_start)::float8 AS window_start, "
    "extract(epoch from window_end)::float8 AS window_end, "
    "limit_nano_snapshot, spent_nano_cny, reserved_nano_cny, status, version, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at"
)

_WINDOW_INT_KEYS = ("policy_version", "limit_nano_snapshot", "spent_nano_cny",
                    "reserved_nano_cny", "version")
_WINDOW_EPOCH_KEYS = ("window_start", "window_end", "created_at",
                      "updated_at")


def _window_out(row) -> dict:
    out = dict(row)
    for key in _WINDOW_INT_KEYS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in _WINDOW_EPOCH_KEYS:
        if out.get(key) is not None:
            out[key] = float(out[key])
    return out


def window_remaining_nano(window) -> int:
    """剩余额度 = limit_snapshot - spent - reserved（可为负：overage 观测）。"""
    return int(window["limit_nano_snapshot"]) - int(window["spent_nano_cny"]) \
        - int(window["reserved_nano_cny"])


def _get_or_create_window_tx(cur, subject_type, subject_id, at) -> dict:
    """单窗口 get_or_create（cursor 注入；并发安全语义见 get_or_create_window）。"""
    if subject_type not in WINDOW_SUBJECT_TYPES:
        raise InvalidSpendRequestError(
            "subject_type 需为 %s" % (WINDOW_SUBJECT_TYPES,),
            subject_type=subject_type)
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise InvalidSpendRequestError("subject_id 需为非空字符串")
    # demo：所有 capability/浏览器归同一 demo_global 周窗口（§3.2）
    if subject_type == "demo":
        subject_id = DEMO_GLOBAL_SUBJECT

    policy = _resolve_policy_tx(cur, subject_type, subject_id, at)
    if policy is None:
        raise SpendPolicyMissingError(
            "无有效金额策略（fail-closed，不落无额度窗口）",
            subject_type=subject_type, subject_id=subject_id)
    start, end = window_bounds(policy["period_kind"], at)
    cur.execute(
        "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
        "WHERE subject_type=%s AND subject_id=%s AND window_start=%s "
        "  AND window_end=%s",
        (subject_type, subject_id, start, end))
    row = cur.fetchone()
    if row is not None:
        return _window_out(row)
    # ON CONFLICT DO NOTHING + 重读：并发同窗口只有一个 INSERT 生效
    # （UNIQUE(subject_type, subject_id, window_start, window_end) 兜底，
    # 不依赖应用层自觉；DO NOTHING 不置事务 aborted，可直接重读）
    cur.execute(
        "INSERT INTO ai_spend_windows "
        "(window_id, policy_id, policy_version, subject_type, subject_id, "
        " window_start, window_end, limit_nano_snapshot) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (subject_type, subject_id, window_start, window_end) "
        "DO NOTHING RETURNING " + _WINDOW_SEL,
        (_WINDOW_ID_PREFIX + secrets.token_hex(12), policy["policy_id"],
         policy["version"], subject_type, subject_id, start, end,
         policy["limit_nano_cny"]))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
            "WHERE subject_type=%s AND subject_id=%s AND window_start=%s "
            "  AND window_end=%s",
            (subject_type, subject_id, start, end))
        row = cur.fetchone()
    return _window_out(row)


def get_or_create_window(subject_type, subject_id, at=None):
    """取/建当前窗口（独立事务版）：按 at 解析策略→算边界→UNIQUE 兜底并发。

    ``limit_nano_snapshot`` 固定创建时刻的策略额度（策略后续修改不追溯，
    §3.2）；新用户/新主体的首个窗口拿完整额度（不按剩余天数折算，§1.1）。
    """
    platform_features.require_pg_backend("spend")
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _get_or_create_window_tx(cur, subject_type,
                                                subject_id, at)
    finally:
        conn.close()


def get_window(window_id):
    """按 window_id 读窗口；不存在返回 None。"""
    platform_features.require_pg_backend("spend")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _WINDOW_SEL +
                            " FROM ai_spend_windows WHERE window_id=%s",
                            (window_id,))
                row = cur.fetchone()
        return _window_out(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §3.2 原子投影原语（FOR UPDATE 锁行 + version 自增；并发安全硬性要求）
# --------------------------------------------------------------------------- #
def _validate_nano(value, field):
    """金额入参校验：>= 0 整数 nano-CNY（bool 不是整数；负数拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSpendRequestError("%s 需为整数（nano-CNY）" % field)
    if value < 0:
        raise InvalidSpendRequestError("%s 需为非负整数（nano-CNY）" % field)
    return value


def _window_arg_id(window):
    """window 参数归一：window dict 或 window_id 字符串 → window_id。"""
    wid = window.get("window_id") if isinstance(window, dict) else window
    if not isinstance(wid, str) or not wid:
        raise InvalidSpendRequestError("window 需为窗口 dict 或 window_id")
    return wid


def _fetch_window_locked(cur, window_id):
    """锁窗口行（FOR UPDATE）；不存在抛 SpendWindowUnavailableError。"""
    cur.execute("SELECT " + _WINDOW_SEL +
                " FROM ai_spend_windows WHERE window_id=%s FOR UPDATE",
                (window_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendWindowUnavailableError("窗口不存在", window_id=window_id)
    return _window_out(row)


def window_reserve(window, estimated_nano):
    """预占（§3.2）：锁窗口行后检查 ``spent + reserved + estimated <= limit``。

    - 超限抛 :class:`SpendBudgetExhaustedError`
      （code=``spend_budget_exhausted``，稳定拒绝，**不改任何数**）并计
      ``spend_reserve_denied_total`` 指标；
    - 成功则 ``reserved += estimated``、version+1（FOR UPDATE 串行化，两个
      并发 reserve 合计只有额度内的能越过临界点）；
    - 窗口不存在/已关闭抛 :class:`SpendWindowUnavailableError`（closed 窗口
      不能再开新预占）。
    """
    platform_features.require_pg_backend("spend")
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                win = _fetch_window_locked(cur, window_id)
                if win["status"] != "open":
                    raise SpendWindowUnavailableError(
                        "窗口已关闭，不能再预占", window_id=window_id,
                        status=win["status"])
                spent = int(win["spent_nano_cny"])
                reserved = int(win["reserved_nano_cny"])
                limit = int(win["limit_nano_snapshot"])
                if spent + reserved + estimated > limit:
                    _metric("spend_reserve_denied_total",
                            subject_type=win["subject_type"])
                    raise SpendBudgetExhaustedError(
                        "窗口额度不足：spent+reserved+estimated > "
                        "limit_nano_snapshot",
                        window_id=window_id, spent_nano_cny=spent,
                        reserved_nano_cny=reserved, estimated_nano=estimated,
                        limit_nano_snapshot=limit)
                cur.execute(
                    "UPDATE ai_spend_windows SET "
                    "reserved_nano_cny = reserved_nano_cny + %s, "
                    "version = version + 1, updated_at = now() "
                    "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
                    (estimated, window_id))
                return _window_out(cur.fetchone())
    finally:
        conn.close()


def window_release(window, estimated_nano):
    """释放预占（§3.2）：``reserved -= estimated``（下限 0）。

    与 reserve 不同，release/settle **不要求**窗口 open——迟到的释放/结算仍
    要收敛 reserved，拒绝它只会留下永久虚占（§3.4.6/§3.4.7 方向）。estimated
    超过当前 reserved（重放/乱序/漂移）时夹到 0 并记
    ``spend_release_clamp_total`` 指标，保住 ``reserved >= 0`` 的 DB 不变量；
    调用级幂等（同一次调用不重复释放）由批次 C 的 hold 状态机负责。
    """
    platform_features.require_pg_backend("spend")
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                win = _fetch_window_locked(cur, window_id)
                reserved = int(win["reserved_nano_cny"])
                release = estimated
                if estimated > reserved:
                    _metric("spend_release_clamp_total",
                            requested_nano=estimated, reserved_nano=reserved)
                    release = reserved
                cur.execute(
                    "UPDATE ai_spend_windows SET "
                    "reserved_nano_cny = reserved_nano_cny - %s, "
                    "version = version + 1, updated_at = now() "
                    "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
                    (release, window_id))
                return _window_out(cur.fetchone())
    finally:
        conn.close()


def window_settle(window, estimated_nano, actual_nano):
    """结算（§3.2/§3.4）：``reserved -= estimated``、``spent += actual``。

    - ``actual > estimated`` 允许：按真实成本入账（不拒绝已发生的消费），
      overage 记 ``spend_settle_overage_total`` 指标；
    - ``actual < estimated``：差额随 reserved 扣减自然释放；
    - estimated 超过当前 reserved（重放/乱序/漂移）时夹 0 记
      ``spend_settle_release_clamp_total``；spent 永远按 actual 累加；
    - 返回窗口 dict，附加 ``overage_nano = max(0, actual - estimated)``。
    """
    platform_features.require_pg_backend("spend")
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    actual = _validate_nano(actual_nano, "actual_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                win = _fetch_window_locked(cur, window_id)
                reserved = int(win["reserved_nano_cny"])
                overage = max(0, actual - estimated)
                release = estimated
                if estimated > reserved:
                    _metric("spend_settle_release_clamp_total",
                            requested_nano=estimated, reserved_nano=reserved)
                    release = reserved
                if overage > 0:
                    _metric("spend_settle_overage_total",
                            overage_nano=overage)
                cur.execute(
                    "UPDATE ai_spend_windows SET "
                    "reserved_nano_cny = reserved_nano_cny - %s, "
                    "spent_nano_cny = spent_nano_cny + %s, "
                    "version = version + 1, updated_at = now() "
                    "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
                    (release, actual, window_id))
                out = _window_out(cur.fetchone())
                out["overage_nano"] = overage
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 策略管理（CAS；默认更新只影响新窗口）
# --------------------------------------------------------------------------- #
def _validate_expected_version(expected_version):
    if isinstance(expected_version, bool) \
            or not isinstance(expected_version, int) or expected_version < 1:
        raise InvalidSpendRequestError("expected_version 需为正整数")
    return expected_version


def update_policy_limit(policy_id, new_limit_nano_cny, expected_version, *,
                        updated_by=None, audit=True, actor_user_id=None):
    """CAS 更新策略额度（version 命中才更新；未命中抛 409 语义冲突）。

    默认语义：只影响**之后新建**的窗口——本函数不触碰任何 ai_spend_windows
    行（已开窗口的 limit_nano_snapshot 固定；要立即影响当前周期走
    :func:`adjust_current_window`，那是独立事务 + 审计 + 二次确认的显式操作）。
    审计 action=``spend.policy_update``（同事务，失败整体回滚）。
    """
    platform_features.require_pg_backend("spend")
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ai_spend_policies SET limit_nano_cny=%s, "
                    "version=version+1, updated_at=now(), updated_by=%s "
                    "WHERE policy_id=%s AND version=%s "
                    "RETURNING " + _POLICY_SEL,
                    (new_limit, updated_by, policy_id, expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "策略版本冲突（数据已被他人修改，请刷新后重试）",
                        policy_id=policy_id, expected_version=expected_version)
                out = _policy_out(row)
                if audit:
                    share_store_pg.record_audit_tx(
                        cur, POLICY_UPDATE_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="spend_policy", target_id=policy_id,
                        detail={
                            "previous_version": expected_version,
                            "new_version": out["version"],
                            "limit_nano_cny": out["limit_nano_cny"],
                            "scope_type": out["scope_type"],
                            "scope_id": out["scope_id"],
                            "period_kind": out["period_kind"],
                        })
                return out
    finally:
        conn.close()


def _close_open_policies_tx(cur, scope_type, scope_id, at, updated_by):
    """收口该 scope 当前所有 enabled 未收口策略（effective_to=at）。

    写路径在固定 key ``pg_advisory_xact_lock`` 内执行（与 price book 激活
    同思路）：0023 部分唯一索引是并发硬兜底，锁内收口让「新行
    effective_from = 旧行 effective_to」的接班边界成为常规路径而非冲突路径。
    """
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_POLICY_LOCK_KEY,))
    cur.execute(
        "UPDATE ai_spend_policies SET effective_to=%s, updated_at=now(), "
        "updated_by=%s WHERE scope_type=%s AND scope_id IS NOT DISTINCT FROM %s "
        "  AND enabled AND effective_to IS NULL",
        (at, updated_by, scope_type, scope_id))
    return cur.rowcount


def set_user_override(user_id, limit_nano_cny, *, updated_by=None, at=None,
                      audit=True, actor_user_id=None):
    """设置/替换某用户的月额度覆盖（user_override，calendar_month）。

    实现：收口该用户现有 open 覆盖（保留历史行，effective_to=at）+ 插入新
    覆盖行（version=1，effective_from=at）。新覆盖只影响**之后新建**的窗口
    ——当前已开月窗口的 limit_nano_snapshot 不变（§1.1 默认只影响新周期）。
    """
    platform_features.require_pg_backend("spend")
    limit = _validate_nano(limit_nano_cny, "limit_nano_cny")
    if not isinstance(user_id, str) or not user_id.strip():
        raise InvalidSpendRequestError("user_id 需为非空字符串")
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    policy_id = _OVERRIDE_ID_PREFIX + secrets.token_hex(10)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                closed = _close_open_policies_tx(
                    cur, "user_override", user_id, at, updated_by)
                cur.execute(
                    "INSERT INTO ai_spend_policies "
                    "(policy_id, scope_type, scope_id, period_kind, "
                    " limit_nano_cny, enabled, effective_from, effective_to, "
                    " version, updated_by) "
                    "VALUES (%s,'user_override',%s,'calendar_month',%s,true,"
                    "%s,NULL,1,%s) RETURNING " + _POLICY_SEL,
                    (policy_id, user_id, limit, at, updated_by))
                out = _policy_out(cur.fetchone())
                if audit:
                    share_store_pg.record_audit_tx(
                        cur, POLICY_UPDATE_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="spend_policy", target_id=policy_id,
                        detail={
                            "op": "set_user_override",
                            "user_id": user_id,
                            "limit_nano_cny": limit,
                            "replaced_open_policies": int(closed),
                        })
                return out
    finally:
        conn.close()


def clear_user_override(user_id, *, updated_by=None, at=None, audit=True,
                        actor_user_id=None):
    """清除某用户的月额度覆盖：收口 open 覆盖行（保留历史）。

    清除后：当前已开窗口不受影响（snapshot 不变），**下一个**窗口解析回退
    user_default（§9.2）。返回是否确有行被收口。
    """
    platform_features.require_pg_backend("spend")
    if not isinstance(user_id, str) or not user_id.strip():
        raise InvalidSpendRequestError("user_id 需为非空字符串")
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                closed = _close_open_policies_tx(
                    cur, "user_override", user_id, at, updated_by)
                if audit:
                    share_store_pg.record_audit_tx(
                        cur, POLICY_UPDATE_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="spend_policy", target_id=None,
                        detail={
                            "op": "clear_user_override",
                            "user_id": user_id,
                            "replaced_open_policies": int(closed),
                        })
                return bool(closed)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §1.1「调整当前窗口」（只改 snapshot；CAS + audit；不取消已完成消费）
# --------------------------------------------------------------------------- #
def adjust_current_window(window_id, new_limit_nano_cny, expected_version, *,
                          actor_user_id=None, confirm=False,
                          audit_detail=None):
    """显式调整当前窗口额度：只改 ``limit_nano_snapshot``（CAS）+ 审计。

    - ``expected_version`` 为窗口当前 version，未命中抛
      :class:`SpendVersionConflictError`（409 语义，不做 last-write-wins）；
    - **不修改** spent/reserved（已完成消费不取消）；调低到低于 spent 后，
      下一次 :func:`window_reserve` 因 ``spent+reserved+estimated<=limit``
      必然拒绝——本函数自己不拒绝（否则无法把额度调回真实成本线以下）；
    - ``confirm`` 是批次 D UI/HTTP 层的二次确认语义位：本层只如实写入
      audit detail（不在这里裁决）；
    - audit action=``spend.window_adjust``（同事务，失败整体回滚）。
    """
    platform_features.require_pg_backend("spend")
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                window = _fetch_window_locked(cur, window_id)
                cur.execute(
                    "UPDATE ai_spend_windows SET limit_nano_snapshot=%s, "
                    "version=version+1, updated_at=now() "
                    "WHERE window_id=%s AND version=%s "
                    "RETURNING " + _WINDOW_SEL,
                    (new_limit, window_id, expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "窗口版本冲突（数据已被他人修改，请刷新后重试）",
                        window_id=window_id,
                        expected_version=expected_version)
                out = _window_out(row)
                detail = dict(audit_detail or {})
                detail.update({
                    "window_id": window_id,
                    "subject_type": out["subject_type"],
                    "subject_id": out["subject_id"],
                    "window_start_epoch": out["window_start"],
                    "previous_limit_nano_snapshot":
                        int(window["limit_nano_snapshot"]),
                    "new_limit_nano_snapshot": new_limit,
                    "previous_version": expected_version,
                    "new_version": out["version"],
                    "spent_nano_cny": int(out["spent_nano_cny"]),
                    "reserved_nano_cny": int(out["reserved_nano_cny"]),
                    "confirm": bool(confirm),
                })
                share_store_pg.record_audit_tx(
                    cur, WINDOW_ADJUST_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="spend_window", target_id=window_id,
                    detail=detail)
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §7.3 enforcement 开关（只读；本批恒 shadow）
# --------------------------------------------------------------------------- #
def _enforcement_mode_tx(cur) -> str:
    """同事务读 enforcement 开关（缺省/非法 → shadow，只读不改）。"""
    cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                (SPEND_ENFORCEMENT_MODE_KEY,))
    row = cur.fetchone()
    if row is None or row["value"] is None:
        return DEFAULT_ENFORCEMENT_MODE
    value = row["value"]
    if isinstance(value, str) and value in SPEND_ENFORCEMENT_MODES:
        return value
    _LOG.warning("platform_settings.%s 存量值非法（%r），按 shadow 处理",
                 SPEND_ENFORCEMENT_MODE_KEY, value)
    return DEFAULT_ENFORCEMENT_MODE


def enforcement_mode() -> str:
    """读 ``platform_settings.spend_enforcement_mode``（缺省/非法 → shadow）。

    只读：本批没有任何路径改这个键（0023 迁移只在不存在时种 "shadow"）。
    """
    platform_features.require_pg_backend("spend")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _enforcement_mode_tx(cur)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 对账器（§9.7：报告 drift，不自动修）
# --------------------------------------------------------------------------- #
def _reconcile_window_tx(cur, window, cutover, now):
    """单窗口对账：usage events / open holds 重算应有 spent/reserved。"""
    subject_type, subject_id = window["subject_type"], window["subject_id"]
    start_dt, end_dt = window["_start_dt"], window["_end_dt"]
    # 窗口口径起点 = max(窗口起点, pricing cutover)：旧错误价格的影子数据
    # （§7.2 legacy 口径）不进入窗口对账；cutover 缺失时按窗口起点并给标记
    effective_from = start_dt
    cutover_missing = False
    if cutover is not None:
        effective_from = max(effective_from, cutover)
    else:
        cutover_missing = True

    if subject_type == "demo":
        # demo 所有 capability 归同一周窗口：任何 demo 主体事件都计入
        subj_where, subj_params = "subject_type='demo'", []
    else:
        subj_where = "subject_type=%s AND subject_id=%s"
        subj_params = [subject_type, subject_id]

    cur.execute(
        "SELECT COALESCE(SUM(charge_nano_cny), 0)::bigint AS spent "
        "FROM ai_usage_events WHERE status='priced' AND " + subj_where +
        " AND occurred_at >= %s AND occurred_at < %s",
        subj_params + [effective_from, end_dt])
    expected_spent = int(cur.fetchone()["spent"])

    # open holds 的 reserved 重建：批次 B 的 billing_holds 只有 owner/user
    # 主体（demo hold 是批次 C 的「Demo 不再 skip」），demo 窗口恒 0；
    # estimated NULL 的行贡献 0（无估算即无占用可计，与 authorize 口径一致）
    cur.execute(
        "SELECT COALESCE(SUM(estimated_nano_cny), 0)::bigint AS reserved "
        "FROM billing_holds WHERE status='open' AND expires_at >= %s "
        "AND " + subj_where +
        " AND created_at >= %s AND created_at < %s "
        "AND estimated_nano_cny IS NOT NULL",
        [now] + subj_params + [start_dt, end_dt])
    expected_reserved = int(cur.fetchone()["reserved"])

    actual_spent = int(window["spent_nano_cny"])
    actual_reserved = int(window["reserved_nano_cny"])
    item = {
        "window_id": window["window_id"],
        "subject_type": subject_type,
        "subject_id": subject_id,
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "limit_nano_snapshot": int(window["limit_nano_snapshot"]),
        "expected_spent_nano": expected_spent,
        "actual_spent_nano": actual_spent,
        "spent_drift_nano": actual_spent - expected_spent,
        "expected_reserved_nano": expected_reserved,
        "actual_reserved_nano": actual_reserved,
        "reserved_drift_nano": actual_reserved - expected_reserved,
    }
    if cutover_missing:
        item["cutover_missing"] = True
    item["matches"] = (item["spent_drift_nano"] == 0
                       and item["reserved_drift_nano"] == 0)
    return item


def reconcile_spend_windows(*, at=None):
    """对账所有 open 窗口（§9.7）：重算应有 spent/reserved，报告 drift 清单。

    口径（§7.2/§3.2）：
      - expected spent = ai_usage_events 中 priced、主体匹配、
        ``occurred_at ∈ [max(window_start, pricing_v2_cutover_at),
        window_end)`` 的 ``charge_nano_cny`` 合计——cutover 前的旧错误价格
        影子数据不进窗口；
      - expected reserved = billing_holds 中 open 未过期、主体匹配、
        ``created_at ∈ [window_start, window_end)`` 的 estimated 合计；
      - **只报告不自动修**（修数必须走人工/批次 C 的强一致路径）。

    返回 ``{"checked", "drift_windows", "items", "pricing_cutover_epoch",
    "enforcement_mode"}``。
    """
    platform_features.require_pg_backend("spend")
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 对账需要原始 timestamptz 做区间比较键（epoch 浮点不参与）
                cur.execute(
                    "SELECT window_id, subject_type, subject_id, "
                    "window_start, window_end, limit_nano_snapshot, "
                    "spent_nano_cny, reserved_nano_cny, version "
                    "FROM ai_spend_windows WHERE status='open' "
                    "ORDER BY window_start")
                windows = []
                for row in cur.fetchall():
                    win = dict(row)
                    win["window_start"] = float(
                        win["window_start"].timestamp())
                    win["window_end"] = float(win["window_end"].timestamp())
                    win["_start_dt"] = row["window_start"]
                    win["_end_dt"] = row["window_end"]
                    windows.append(win)
                cutover = None
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (_PRICING_CUTOVER_KEY,))
                marker = cur.fetchone()
                if marker is not None and marker["value"] is not None:
                    cutover = datetime.fromtimestamp(
                        float(marker["value"]), tz=timezone.utc)
                items = [_reconcile_window_tx(cur, w, cutover, at_dt)
                         for w in windows]
                mode = _enforcement_mode_tx(cur)
        return {
            "checked": len(items),
            "drift_windows": sum(1 for i in items if not i["matches"]),
            "items": items,
            "pricing_cutover_epoch":
                cutover.timestamp() if cutover is not None else None,
            "enforcement_mode": mode,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# admin 只读查询（Admin API v1 数据源；金额出口十进制字符串化在 app 层）
# --------------------------------------------------------------------------- #
def admin_list_policies(*, at=None):
    """全部策略 + 三类全局 scope 当前生效解析（§6.1 只读）。

    返回 ``{"items": [...], "resolved": {...}, "enforcement_mode": ...}``；
    resolved 键为 demo_global / user_default / owner（user_default 用不可能
    存在覆盖的哨兵 subject 解析，保证走回退分支）。
    """
    platform_features.require_pg_backend("spend")
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _POLICY_SEL +
                            " FROM ai_spend_policies "
                            "ORDER BY scope_type, scope_id, effective_from")
                items = [_policy_out(r) for r in cur.fetchall()]
                resolved = {
                    "demo_global": _resolve_policy_tx(
                        cur, "demo", DEMO_GLOBAL_SUBJECT, at_dt),
                    "user_default": _resolve_policy_tx(
                        cur, "user", "__no_such_user__", at_dt),
                    "owner": _resolve_policy_tx(cur, "owner", "", at_dt),
                }
                mode = _enforcement_mode_tx(cur)
        return {"items": items, "resolved": resolved,
                "enforcement_mode": mode}
    finally:
        conn.close()
