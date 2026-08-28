# -*- coding: utf-8 -*-
"""金额计费存储层（admin-billing 方案 §6/§7，PR2：影子计价，不扣账本）。

PG-only：全部公共入口经 ``platform_features.require_pg_backend("billing")``
fail-closed（json/dual 返回稳定 ``pg_backend_required``，不降级进程内余额）。

内容：
  - usage event 严格校验器（手写，语义与 tests/fixtures/usage_events/
    schema_v1.json 一致；仓库无 jsonschema 依赖，不新增）；
  - canonical payload_hash（18 字段，规则以
    tests/fixtures/usage_events/README.md 为唯一依据，PR0 互锁用例校验）；
  - :func:`ingest_usage_event`：单事务投递端点数据层（§7.5）——dedup 比对
    payload_hash → §7.2 四步权威主体解析 → 时钟偏差/算术校验 → 双 price book
    计价写回（或 unpriced+reason）→ 同事务无敏感信息 audit。**影子阶段不写
    任何 billing_ledger_entries，demo 主体永不开户**；
  - price book 创建/激活（§6.3：固定 key ``pg_advisory_xact_lock`` + active
    区间重叠拒绝，明确不用 btree_gist）；
  - 账户/账本/余额快照基础读写（余额 = SUM(amount)，projection 可重建）；
  - provider 余额十进制字符串解析见 billing_pricing.parse_balance_to_nano
    （Decimal，禁 float 中转）。

审计红线：不落 prompt/输出文本/图片/API key/完整 IP/完整请求体；ingest
audit 只含 provider/model/subject_type/status/duplicate/unpriced_reason 等
非敏感字段。
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

import billing_pricing
import pg_store
import platform_features
import share_store_pg

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: canonical payload_hash 的固定 18 字段（PR0 README §1；多一个少一个都算错）
CANONICAL_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "call_id",
    "enqueued_at",
    "event_id",
    "model",
    "occurred_at",
    "output_tokens",
    "provider",
    "provider_request_id",
    "reasoning_tokens",
    "request_id",
    "schema_version",
    "session_id",
    "subject_id",
    "subject_type",
    "total_tokens",
    "user_id",
)

#: 五个 token 字段（必须显式出现：非负整数或 null）
TOKEN_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

#: §4 时钟规则：occurred_at 晚于 received_at + 5 分钟 → unpriced(clock_skew_future)
OCCURRED_AT_FUTURE_TOLERANCE_SECONDS = 300

#: §4 时钟规则：occurred_at 早于 received_at - N 天 → unpriced(occurred_at_out_of_range)。
#: 受控配置（BILLING_OCCURRED_AT_MAX_AGE_DAYS，默认 30）；放宽只影响新事件。
DEFAULT_OCCURRED_AT_MAX_AGE_DAYS = 30


def occurred_at_max_age_days() -> int:
    """读取 BILLING_OCCURRED_AT_MAX_AGE_DAYS（默认 30；非法值回退默认）。"""
    raw = (os.environ.get("BILLING_OCCURRED_AT_MAX_AGE_DAYS") or "").strip()
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_OCCURRED_AT_MAX_AGE_DAYS
    return val if val > 0 else DEFAULT_OCCURRED_AT_MAX_AGE_DAYS


#: unpriced_reason 稳定词表（§6.4/§7.5；不得出现敏感信息）
UNPRICED_ARITHMETIC_MISMATCH = "arithmetic_mismatch"
UNPRICED_CLOCK_SKEW_FUTURE = "clock_skew_future"
UNPRICED_OCCURRED_AT_OUT_OF_RANGE = "occurred_at_out_of_range"
UNPRICED_NO_FINAL_USAGE = "no_final_usage"
UNPRICED_NO_ACTIVE_PRICE_BOOK = "no_active_price_book"

#: ingest audit 动作名（detail 只含非敏感字段）
USAGE_INGEST_AUDIT_ACTION = "usage.ingest"

#: 测试专用钩子：dedup 检查之后、SAVEPOINT/INSERT 之前同步回调（生产恒
#: None）。tests/test_billing_store.py 用它在连接 A 的两条语句之间用连接 B
#: 提交同 event_id/call_id 行，确定性复现并发投递竞态（§7.5 步骤 2）。
_INGEST_PRE_INSERT_HOOK = None

#: price book 激活串行化 advisory key（§6.3，事务级；稳定 bigint "BLPB"）
_PRICE_BOOK_LOCK_KEY = 0x424C5042

SUBJECT_TYPES = ("owner", "user", "demo")

# 校验器正则（与 schema_v1.json 的 pattern 逐条一致）
_EVENT_ID_RE = re.compile(r"^use_[0-9a-f]{32}$")
_CALL_ID_RE = re.compile(r"^call_[0-9a-f]{32}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MODEL_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_DATETIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$")
_RAW_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: schema 顶层键（required 15 + optional 4；additionalProperties:false）
_REQUIRED_FIELDS = (
    "event_id", "call_id", "schema_version", "session_id", "subject_type",
    "subject_id", "provider", "model", "occurred_at", "enqueued_at",
) + TOKEN_FIELDS
_OPTIONAL_FIELDS = ("request_id", "user_id", "provider_request_id", "raw_usage")
_KNOWN_FIELDS = frozenset(_REQUIRED_FIELDS + _OPTIONAL_FIELDS)

#: raw_usage 中已声明类型的键（其余键只允许 token 计数或带 meta_version 的
#: 版本化元数据对象——schema additionalProperties 分支）
_RAW_TYPED_KEYS = ("finish_reason", "prompt_tokens", "completion_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


# --------------------------------------------------------------------------- #
# 业务异常（code 稳定，供路由映射插件错误信封）
# --------------------------------------------------------------------------- #
class BillingError(Exception):
    """billing 业务异常基类。"""

    code = "billing_error"
    retryable = False

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


class InvalidUsageEventError(BillingError):
    """request body 不符合 schema_v1（400 invalid_request）。"""

    code = "invalid_request"

    def __init__(self, errors):
        self.errors = [str(e) for e in errors]
        super().__init__("usage event 校验失败：%s" % "; ".join(self.errors[:5]))


class UsageEventConflictError(BillingError):
    """同 event_id/call_id 重放但 payload_hash 不同（409，不可重试）。"""

    code = "usage_event_conflict"


class UsageSubjectConflictError(BillingError):
    """权威主体与 body assertion 不一致（409，确定性，进 dead/P0）。"""

    code = "usage_subject_conflict"


class UsageSubjectNotReadyError(BillingError):
    """权威绑定行尚未提交/不可解析（409，retryable=true）。"""

    code = "usage_subject_not_ready"
    retryable = True


class PriceBookOverlapError(BillingError):
    """激活的 price book 与既有 active 区间重叠（§6.3 拒绝）。"""

    code = "price_book_overlap"


class BillingAccountExistsError(BillingError):
    """用户已有 billing account（user_id UNIQUE）。"""

    code = "billing_account_exists"


class BillingAccountNotFoundError(BillingError):
    """目标用户尚未开户（§9：不伪造 0 余额账户，refund/manual_adjustment 不隐式开户）。"""

    code = "billing_account_not_found"


class BillingCapsVersionConflictError(BillingError):
    """caps CAS 更新未命中（客户端携带的 version 已过期；不做 last-write-wins）。"""

    code = "version_conflict"


class BillingIdempotencyKeyConflictError(BillingError):
    """idempotency_key 已被其他账户/参数占用（不是同请求重放）。"""

    code = "idempotency_key_conflict"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 严格校验器（语义 == tests/fixtures/usage_events/schema_v1.json，手写实现）
# --------------------------------------------------------------------------- #
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_token_count(value, path, errors):
    """token_count：null 或 >=0 整数（bool 不算整数）。"""
    if value is None:
        return
    if not _is_int(value):
        errors.append("%s 需为非负整数或 null" % path)
        return
    if value < 0:
        errors.append("%s 需 >= 0" % path)


def _check_nullable_str(value, path, errors, min_len=1, max_len=128):
    """可空字符串：null 或长度在 [min_len, max_len] 的字符串。"""
    if value is None:
        return
    if not isinstance(value, str):
        errors.append("%s 需为字符串或 null" % path)
        return
    if not (min_len <= len(value) <= max_len):
        errors.append("%s 长度需在 %d..%d" % (path, min_len, max_len))


def _check_raw_usage(raw, path, errors):
    """raw_usage：只允许 token 计数、finish_reason 与带 meta_version 的
    版本化 provider 元数据（字符串<=128/bool/int/null 标量）；数组、长文本
    一律拒绝（防内容外泄通道，schema additionalProperties 分支）。"""
    if not isinstance(raw, dict):
        errors.append("%s 需为 object" % path)
        return
    for key, value in raw.items():
        child = "%s.%s" % (path, key)
        if not isinstance(key, str) or not _RAW_KEY_RE.match(key):
            errors.append("%s 字段名不合法（^[a-z][a-z0-9_]{0,63}$）" % child)
            continue
        if key == "finish_reason":
            if not isinstance(value, str) or not (1 <= len(value) <= 64):
                errors.append("%s 需为 1..64 字符" % child)
            continue
        if key in ("prompt_tokens", "completion_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            _check_token_count(value, child, errors)
            continue
        if value is None or _is_int(value):
            if value is not None and value < 0:
                errors.append("%s 需 >= 0" % child)
            continue
        if isinstance(value, dict):
            meta_version = value.get("meta_version")
            if not _is_int(meta_version) or meta_version < 1:
                errors.append("%s 元数据对象缺 meta_version（>=1 整数）" % child)
                continue
            for sub_key, sub in value.items():
                sub_path = "%s.%s" % (child, sub_key)
                if not isinstance(sub_key, str) or not _RAW_KEY_RE.match(sub_key):
                    errors.append("%s 字段名不合法" % sub_path)
                    continue
                if sub is None or isinstance(sub, bool) or _is_int(sub):
                    continue
                if isinstance(sub, str):
                    if len(sub) > 128:
                        errors.append("%s 字符串超长（>128）" % sub_path)
                    continue
                errors.append("%s 只允许 null/bool/整数/短字符串" % sub_path)
            continue
        errors.append("%s 只允许 token 计数或带 meta_version 的元数据对象" % child)


def validate_usage_event_body(body) -> list:
    """严格校验 usage event request body，返回错误列表（空 = 通过）。

    语义与 tests/fixtures/usage_events/schema_v1.json（draft 2020-12，
    additionalProperties:false）一致：字段集合、pattern、类型、null 规则。
    仓库无 jsonschema 依赖，本函数为唯一权威实现（不新增依赖）。
    """
    errors = []
    if not isinstance(body, dict):
        return ["request body 需为 JSON object"]
    for key in _REQUIRED_FIELDS:
        if key not in body:
            errors.append("缺必填字段 %r" % key)
    for key in body:
        if key not in _KNOWN_FIELDS:
            errors.append("不允许的额外字段 %r（additionalProperties:false）" % key)

    if "event_id" in body and (not isinstance(body["event_id"], str)
                               or not _EVENT_ID_RE.match(body["event_id"])):
        errors.append("event_id 需匹配 ^use_[0-9a-f]{32}$")
    if "call_id" in body and (not isinstance(body["call_id"], str)
                              or not _CALL_ID_RE.match(body["call_id"])):
        errors.append("call_id 需匹配 ^call_[0-9a-f]{32}$")
    if "schema_version" in body and not (_is_int(body["schema_version"])
                                         and body["schema_version"] == 1):
        errors.append("schema_version 必须为 1（不兼容变更须协商新版本）")
    if "subject_type" in body and body["subject_type"] not in SUBJECT_TYPES:
        errors.append("subject_type 需为 %s" % (SUBJECT_TYPES,))
    for key in ("session_id", "subject_id"):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or not (1 <= len(value) <= 128):
                errors.append("%s 需为 1..128 字符" % key)
    if "provider" in body and (not isinstance(body["provider"], str)
                               or not _PROVIDER_RE.match(body["provider"])):
        errors.append("provider 需匹配 ^[a-z][a-z0-9_-]{0,63}$")
    if "model" in body and (not isinstance(body["model"], str)
                            or not _MODEL_RE.match(body["model"])):
        errors.append("model 需匹配 ^[a-z][a-z0-9._-]{0,127}$")
    for key in ("request_id", "user_id"):
        if key in body:
            _check_nullable_str(body[key], key, errors, 1, 128)
    if "provider_request_id" in body:
        _check_nullable_str(body["provider_request_id"], "provider_request_id",
                            errors, 1, 255)
    for key in ("occurred_at", "enqueued_at"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or not _DATETIME_RE.match(value):
            errors.append("%s 需为带时区的 RFC3339 date-time" % key)
            continue
        try:
            billing_pricing.parse_rfc3339(value)
        except ValueError:
            errors.append("%s 不是合法日期时间" % key)
    for key in TOKEN_FIELDS:
        if key in body:
            _check_token_count(body[key], key, errors)
    if "raw_usage" in body:
        _check_raw_usage(body["raw_usage"], "raw_usage", errors)
    return errors


# --------------------------------------------------------------------------- #
# canonical payload_hash（tests/fixtures/usage_events/README.md 为唯一依据）
# --------------------------------------------------------------------------- #
def _canonical_time(value):
    """时间字符串 → UTC ISO-8601（微秒固定 6 位；Z/z/偏移输入归一）。"""
    if value is None:
        return None
    dt = billing_pricing.parse_rfc3339(value)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_payload_hash(event) -> str:
    """按 PR0 README 规则计算 payload_hash（SHA-256 小写 hex）。

    固定 18 字段（缺省可选键补 null）；时间统一 UTC 微秒格式；整数保持整数；
    ``json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":"),
    allow_nan=False)``。排除 received_at/HTTP header/raw_usage（整个键不参与，
    修改 raw_usage 不改变 hash）。
    """
    obj = {}
    for key in CANONICAL_FIELDS:
        if key in ("occurred_at", "enqueued_at"):
            obj[key] = _canonical_time(event.get(key))
        else:
            obj[key] = event.get(key)  # 缺省可选键 → null
    canonical = json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# §7.2 权威主体四步解析
# --------------------------------------------------------------------------- #
def _resolve_usage_subject(cur, event, installation_id):
    """按 §7.2 四步解析权威计费主体，返回 (subject_type, subject_id)。

    ① request_id → ai_budget_reservations：要求 state='consumed' 且
    histopilot_session_id 与事件 session_id 一致，以 reservation 的
    subject_type/subject_id 为准。生命周期核查结论（budget_store.consume/
    release + app.py _ai_budget_lifecycle）：histopilot_session_id **只在
    consume()（HistoPilot 2xx 接受后）写入**，release()/reclaim_expired()/
    重新预占都不写或显式置 NULL——因此 released 行结构上不可能带 session id，
    「released 且 session 匹配」分支不存在。abort/中断（已接受后中断，如
    fixture 04）的终态是 consumed（带 session），可正常解析；reserved（尚未
    确认）与 released（已退款，可能随后被重试重新 consume 并带 session）都
    落到后续步骤，最终无权威来源时按 usage_subject_not_ready 可重试。

    ② session_id → demo_sessions.histopilot_session_id：恢复 demo subject
    （capability id）。不过滤过期：计量归属是历史事实，过期 capability 的
    事件仍应入账（只计量、不开户、不写 ledger）。

    ③ run_grants 交叉校验：取该 session 绑定、且属于当前 installation 的
    grant（不过滤过期/撤销——grant 行仍记录 run 创建者）；同 session 多
    grant 创建者不一致 → 确定性冲突；resolved 主体为 owner/user 时创建者
    必须一致；无 ①② 来源时 grant 创建者可作为 owner/user 主体来源（run
    grant 只覆盖写能力 run，不因此影响只读调用的 ①② 路径）。

    ④ body 的 subject_type/subject_id/user_id 只是 assertion：与权威解析
    不一致 → 409 usage_subject_conflict（确定性）；完全没有权威来源 → 409
    usage_subject_not_ready（retryable），不得先按 body 入账。
    """
    session_id = event["session_id"]
    resolved = None

    request_id = event.get("request_id")
    reservation = None
    if request_id:
        cur.execute(
            "SELECT subject_type, subject_id, state, histopilot_session_id "
            "FROM ai_budget_reservations WHERE request_id=%s", (request_id,))
        reservation = cur.fetchone()
    if reservation is not None \
            and reservation["state"] == "consumed" \
            and reservation["histopilot_session_id"] == session_id:
        resolved = (reservation["subject_type"], reservation["subject_id"])

    if resolved is None:
        cur.execute(
            "SELECT id FROM demo_sessions WHERE histopilot_session_id=%s "
            "ORDER BY created_at DESC LIMIT 1", (session_id,))
        demo = cur.fetchone()
        if demo is not None:
            resolved = ("demo", demo["id"])

    cur.execute(
        "SELECT grant_id, installation_id, created_by_user_id "
        "FROM run_grants WHERE session_id=%s", (session_id,))
    grants = [dict(r) for r in cur.fetchall()]
    inst_grants = [g for g in grants if g["installation_id"] == installation_id]
    grant_users = {g["created_by_user_id"] for g in inst_grants
                   if g["created_by_user_id"]}
    if len(grant_users) > 1:
        raise UsageSubjectConflictError(
            "同一 session 的 run grants 创建者不一致（session 归属冲突）",
            session_id=session_id)
    grant_user = next(iter(grant_users), None)
    if resolved is not None and resolved[0] in ("owner", "user") \
            and grant_user and grant_user != resolved[1]:
        raise UsageSubjectConflictError(
            "run grant 创建者与权威主体不一致", session_id=session_id)
    if resolved is None and grant_user:
        cur.execute("SELECT role FROM users WHERE user_id=%s", (grant_user,))
        row = cur.fetchone()
        if row is not None and row["role"] in ("owner", "user"):
            resolved = (row["role"], grant_user)

    if resolved is None:
        raise UsageSubjectNotReadyError(
            "权威主体绑定行不存在或未就绪（reservation/demo session/run grant）",
            session_id=session_id)
    if (event["subject_type"], event["subject_id"]) != resolved:
        raise UsageSubjectConflictError(
            "body 主体 assertion 与权威解析不一致",
            asserted=(event["subject_type"], event["subject_id"]),
            resolved=resolved)
    body_user = event.get("user_id")
    if body_user is not None and resolved[0] != "demo" and body_user != resolved[1]:
        raise UsageSubjectConflictError(
            "body user_id assertion 与权威主体不一致", resolved=resolved)
    return resolved


# --------------------------------------------------------------------------- #
# usage event 读写 + ingest（§7.5 单事务）
# --------------------------------------------------------------------------- #
_EVENT_SEL = (
    "event_id, call_id, payload_hash, schema_version, request_id, session_id, "
    "subject_type, subject_id, user_id, provider, model, provider_request_id, "
    "cache_hit_input_tokens, cache_miss_input_tokens, output_tokens, "
    "reasoning_tokens, total_tokens, "
    "extract(epoch from occurred_at)::float8 AS occurred_at, "
    "extract(epoch from enqueued_at)::float8 AS enqueued_at, "
    "extract(epoch from received_at)::float8 AS received_at, "
    "status, unpriced_reason, provider_price_book_id, charge_price_book_id, "
    "provider_cost_nano_cny, charge_nano_cny, raw_usage"
)


def _event_out(row) -> dict:
    out = dict(row)
    for key in ("cache_hit_input_tokens", "cache_miss_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens",
                "provider_cost_nano_cny", "charge_nano_cny"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def get_usage_event(event_id):
    """按 event_id 读取 usage event 行；不存在返回 None。"""
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _EVENT_SEL +
                            " FROM ai_usage_events WHERE event_id=%s",
                            (event_id,))
                row = cur.fetchone()
        return _event_out(row) if row is not None else None
    finally:
        conn.close()


def _fetch_event_locked(cur, event_id):
    cur.execute("SELECT " + _EVENT_SEL +
                " FROM ai_usage_events WHERE event_id=%s FOR UPDATE",
                (event_id,))
    return cur.fetchone()


def _duplicate_result(row):
    return {
        "event_id": row["event_id"],
        "duplicate": True,
        "status": row["status"],
        "priced": row["status"] == "priced",
        "row": _event_out(row),
    }


def _classify_unpriced(event, occurred, received, max_age_days):
    """时钟（§4）与算术校验 → (status, unpriced_reason, storable_tokens)。

    优先级（确定性）：arithmetic_mismatch > clock_skew_future >
    occurred_at_out_of_range > no_final_usage > no_active_price_book。
    算术不符时 token 列必须整体置 NULL（数据库 CHECK 会拒绝原值），原始
    数字镜像进 raw_usage.reported_tokens_v1（不丢失排查信息）。
    """
    tokens = {key: event.get(key) for key in TOKEN_FIELDS}
    nulls = [tokens[key] is None for key in TOKEN_FIELDS]
    partial = any(nulls) and not all(nulls)
    arithmetic_bad = partial
    if not arithmetic_bad and not all(nulls):
        hit, miss, out = (tokens["cache_hit_input_tokens"],
                          tokens["cache_miss_input_tokens"],
                          tokens["output_tokens"])
        reasoning, total = tokens["reasoning_tokens"], tokens["total_tokens"]
        arithmetic_bad = (reasoning > out) or (total != hit + miss + out)
    if arithmetic_bad:
        return ("unpriced", UNPRICED_ARITHMETIC_MISMATCH, None)

    if occurred > received + timedelta(
            seconds=OCCURRED_AT_FUTURE_TOLERANCE_SECONDS):
        reason = UNPRICED_CLOCK_SKEW_FUTURE
    elif occurred < received - timedelta(days=int(max_age_days)):
        reason = UNPRICED_OCCURRED_AT_OUT_OF_RANGE
    elif all(nulls):
        reason = UNPRICED_NO_FINAL_USAGE
    else:
        reason = ""  # priced 候选（是否找到价格由调用方判定）
    stored = None if all(nulls) else tokens
    return ("priced" if not reason else "unpriced", reason, stored)


def ingest_usage_event(event, *, installation_id, plugin_id="histopilot",
                       max_age_days=None, now=None):
    """投递端点数据层（§7.5）：单事务入库 + 计价，返回结果 dict。

    步骤（同一 PostgreSQL 事务）：
      1. schema 校验 + canonical payload_hash；
      2. dedup：先按 event_id 锁行比对 hash（相同 → duplicate 原行；不同 →
         409 usage_event_conflict）；call_id 已绑定其他事件 → 409（hash 含
         event_id，同 call 不同 event_id 的 hash 必然不同）；
      3. §7.2 四步权威主体解析（conflict / not_ready 异常回滚，不入账）；
      4. 时钟（§4）与算术校验；按 occurred_at（不改用 received_at）查两套
         active price book，写死价格版本与两种金额；任一缺失/未知模型 →
         unpriced(no_active_price_book)；
      5. 同事务写无敏感信息的 ingest audit；
      6. **影子阶段不写任何 billing_ledger_entries**（§12.1）；demo 主体
         永不开户（§7.2）。

    ``now`` 为 received_at（测试注入口；缺省当前 UTC 时间）。计价时段与时钟
    偏差判定都用 occurred_at——服务端不得为「能计价」静默换用 received_at。
    """
    platform_features.require_pg_backend("billing")
    errors = validate_usage_event_body(event)
    if errors:
        raise InvalidUsageEventError(errors)
    payload_hash = canonical_payload_hash(event)
    occurred = billing_pricing.parse_rfc3339(event["occurred_at"])
    enqueued = billing_pricing.parse_rfc3339(event["enqueued_at"])
    received = now if now is not None else datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age_days = (occurred_at_max_age_days() if max_age_days is None
                else int(max_age_days))

    raw_usage = dict(event.get("raw_usage") or {})
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # -- 步骤 2：dedup（先比对，重放幂等返回原行，不重复计价） --
                existing = _fetch_event_locked(cur, event["event_id"])
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise UsageEventConflictError(
                            "同 event_id 重放的 payload 与原记录不一致",
                            event_id=event["event_id"])
                    return _duplicate_result(existing)
                cur.execute(
                    "SELECT event_id FROM ai_usage_events WHERE call_id=%s",
                    (event["call_id"],))
                clash = cur.fetchone()
                if clash is not None:
                    raise UsageEventConflictError(
                        "call_id 已绑定其他事件", call_id=event["call_id"])

                # -- 步骤 3：权威主体（异常则整体回滚，不先按 body 入账） --
                subject_type, subject_id = _resolve_usage_subject(
                    cur, event, installation_id)
                user_id = None
                if subject_type in ("owner", "user"):
                    cur.execute("SELECT user_id FROM users WHERE user_id=%s",
                                (subject_id,))
                    urow = cur.fetchone()
                    # users 不物理删除（disable 语义）；极端缺行时 user_id 列
                    # 置 NULL 入库（subject 列仍保留权威归因），不让 FK 阻断计量
                    user_id = urow["user_id"] if urow is not None else None

                # -- 步骤 4：时钟/算术/计价（occurred_at 为唯一时间依据） --
                status, reason, stored_tokens = _classify_unpriced(
                    event, occurred, received, age_days)
                if stored_tokens is None and reason == UNPRICED_ARITHMETIC_MISMATCH:
                    # 原始数字镜像进 raw_usage（token 列按 CHECK 置 NULL）
                    raw_usage["reported_tokens_v1"] = dict(
                        {"meta_version": 1}, **{k: event.get(k)
                                                for k in TOKEN_FIELDS})
                provider_book = charge_book = None
                provider_cost = charge = None
                if status == "priced":
                    provider_book = billing_pricing.find_active_rate(
                        cur, "provider_cost", event["provider"],
                        event["model"], occurred)
                    charge_book = billing_pricing.find_active_rate(
                        cur, "customer_charge", event["provider"],
                        event["model"], occurred)
                    if provider_book is None or charge_book is None:
                        status, reason = "unpriced", UNPRICED_NO_ACTIVE_PRICE_BOOK
                    else:
                        provider_cost = billing_pricing.price_tokens_nano(
                            stored_tokens["cache_hit_input_tokens"],
                            stored_tokens["cache_miss_input_tokens"],
                            stored_tokens["output_tokens"], provider_book)
                        charge = billing_pricing.price_tokens_nano(
                            stored_tokens["cache_hit_input_tokens"],
                            stored_tokens["cache_miss_input_tokens"],
                            stored_tokens["output_tokens"], charge_book)

                # SAVEPOINT：并发投递竞态时对方先提交同 event_id/call_id，
                # 本事务的 INSERT 抛 UniqueViolation——PG 中失败语句会把事务置
                # aborted（pg_store.transaction 是裸 commit/rollback，无自动
                # savepoint），必须先 ROLLBACK TO SAVEPOINT 恢复事务才能重读
                # 对方已提交的行比对 payload_hash（§7.5 步骤 2 的竞态分支）。
                if _INGEST_PRE_INSERT_HOOK is not None:
                    _INGEST_PRE_INSERT_HOOK(cur)
                cur.execute("SAVEPOINT sp_usage_insert")
                try:
                    cur.execute(
                        "INSERT INTO ai_usage_events "
                        "(event_id, call_id, payload_hash, schema_version, "
                        " request_id, session_id, subject_type, subject_id, "
                        " user_id, provider, model, provider_request_id, "
                        " cache_hit_input_tokens, cache_miss_input_tokens, "
                        " output_tokens, reasoning_tokens, total_tokens, "
                        " occurred_at, enqueued_at, received_at, status, "
                        " unpriced_reason, provider_price_book_id, "
                        " charge_price_book_id, provider_cost_nano_cny, "
                        " charge_nano_cny, raw_usage) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "RETURNING " + _EVENT_SEL,
                        (event["event_id"], event["call_id"], payload_hash,
                         int(event["schema_version"]), event.get("request_id"),
                         event["session_id"], subject_type, subject_id,
                         user_id, event["provider"], event["model"],
                         event.get("provider_request_id"),
                         stored_tokens["cache_hit_input_tokens"] if stored_tokens else None,
                         stored_tokens["cache_miss_input_tokens"] if stored_tokens else None,
                         stored_tokens["output_tokens"] if stored_tokens else None,
                         stored_tokens["reasoning_tokens"] if stored_tokens else None,
                         stored_tokens["total_tokens"] if stored_tokens else None,
                         occurred, enqueued, received, status, reason,
                         provider_book["price_book_id"] if provider_book else None,
                         charge_book["price_book_id"] if charge_book else None,
                         provider_cost, charge,
                         psycopg.types.json.Jsonb(raw_usage)))
                    row = cur.fetchone()
                except psycopg.errors.UniqueViolation as exc:
                    # 与并发投递竞态：对方先提交同 event_id/call_id → 回滚到
                    # savepoint 恢复事务，再重读对方行比 hash
                    name = (getattr(getattr(exc, "diag", None),
                                    "constraint_name", "") or "")
                    if name not in ("ai_usage_events_pkey",
                                    "ai_usage_events_call_id_key"):
                        raise
                    cur.execute("ROLLBACK TO SAVEPOINT sp_usage_insert")
                    again = _fetch_event_locked(cur, event["event_id"])
                    if again is not None and again["payload_hash"] == payload_hash:
                        return _duplicate_result(again)
                    raise UsageEventConflictError(
                        "并发投递冲突：event_id/call_id 已被其他 payload 占用",
                        event_id=event["event_id"]) from exc

                # -- 步骤 5：同事务无敏感信息 audit（§7.5 单事务语义：audit
                # 失败必须随事务回滚——吞掉失败会让事务带毒、commit 变
                # rollback、事件静默丢失而路由仍报成功；此处不 try/except，
                # 路由层 except Exception → 500 retryable，outbox 退避重投） --
                cur.execute(
                    "INSERT INTO audit_events "
                    "(event_id, ts, actor_user_id, actor_role, action, "
                    " target_type, target_id, slide, detail) "
                    "VALUES (%s, to_timestamp(%s), NULL, 'plugin', %s, "
                    " 'usage_event', %s, NULL, %s)",
                    ("aud_" + secrets.token_hex(16),
                     received.timestamp(), USAGE_INGEST_AUDIT_ACTION,
                     event["event_id"],
                     psycopg.types.json.Jsonb({
                         "provider": event["provider"],
                         "model": event["model"],
                         "subject_type": subject_type,
                         "status": status,
                         "duplicate": False,
                         "unpriced_reason": reason,
                         "installation_id": installation_id,
                         "plugin_id": plugin_id,
                     })))

                return {
                    "event_id": event["event_id"],
                    "duplicate": False,
                    "status": status,
                    "priced": status == "priced",
                    "row": _event_out(row),
                }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# price book：创建 / 激活（§6.3 事务串行化，不用 btree_gist）
# --------------------------------------------------------------------------- #
_BOOK_SEL = (
    "price_book_id, kind, currency, "
    "extract(epoch from effective_from)::float8 AS effective_from, "
    "extract(epoch from effective_to)::float8 AS effective_to, status, "
    "source_url, created_by, "
    "extract(epoch from created_at)::float8 AS created_at"
)


def _book_out(row) -> dict:
    out = dict(row)
    return out


def create_price_book(kind, rates, effective_from, effective_to=None, *,
                      source_url="", created_by=None, price_book_id=None,
                      timezone_name="Asia/Shanghai"):
    """创建 draft 价格书 + 价格行（只有 draft 可编辑，激活走 activate）。

    ``rates`` 为行 dict 列表：{provider, model, time_band,
    cache_hit_nano_per_million, cache_miss_nano_per_million,
    output_nano_per_million}（schedule 固定记录 peak 窗口定义）。
    返回 book dict（不含行；行经 list_rates 查询）。
    """
    platform_features.require_pg_backend("billing")
    if kind not in billing_pricing.PRICE_BOOK_KINDS:
        raise ValueError("kind 需为 %s" % (billing_pricing.PRICE_BOOK_KINDS,))
    if not isinstance(rates, list) or not rates:
        raise ValueError("rates 需为非空 list")
    if not isinstance(effective_from, datetime):
        raise ValueError("effective_from 需为 datetime")
    book_id = price_book_id or ("pb_" + secrets.token_hex(12))
    schedule = psycopg.types.json.Jsonb({
        "windows": [["09:00", "12:00"], ["14:00", "18:00"]],
        "weekdays_only": True,
    })
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_price_books "
                    "(price_book_id, kind, currency, effective_from, "
                    " effective_to, status, source_url, created_by) "
                    "VALUES (%s,%s,'CNY',%s,%s,'draft',%s,%s) "
                    "RETURNING " + _BOOK_SEL,
                    (book_id, kind, effective_from, effective_to,
                     source_url or "", created_by))
                book = cur.fetchone()
                for rate in rates:
                    if rate.get("time_band") not in billing_pricing.TIME_BANDS:
                        raise ValueError("time_band 需为 peak/off_peak")
                    cur.execute(
                        "INSERT INTO billing_rates "
                        "(price_book_id, provider, model, time_band, "
                        " cache_hit_nano_per_million, "
                        " cache_miss_nano_per_million, "
                        " output_nano_per_million, timezone, schedule) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (book_id, rate["provider"], rate["model"],
                         rate["time_band"],
                         int(rate["cache_hit_nano_per_million"]),
                         int(rate["cache_miss_nano_per_million"]),
                         int(rate["output_nano_per_million"]),
                         timezone_name, schedule))
                return _book_out(book)
    finally:
        conn.close()


def activate_price_book(price_book_id, *, actor=None, supersede=False):
    """激活 draft 价格书（§6.3：advisory xact lock + active 区间重叠拒绝）。

    固定 key 的 ``pg_advisory_xact_lock`` 把同 kind 的全部激活串行化；锁内
    查询同 kind、共享 (provider, model) 的 active 书与本册生效区间是否重叠
    （半开区间语义），重叠 → PriceBookOverlapError（整体回滚，并发激活只有
    一个成功）。已 active 的书幂等返回；retired 不可复活（不可变历史）。

    ``supersede=True``（调价接班的受控路径）：对确有重叠、且起点早于本册
    effective_from 的 active 书，在同一事务把其 effective_to 收口到本册
    effective_from，再激活本册。区间收口是 active 行唯一允许的变更——已
    入账事件的价格版本（event 行内的 price_book_id 与金额）不受影响，落在
    旧区间内的迟到事件仍按旧书计价；起点不早于本册的书无法收口，仍按
    重叠拒绝。方案 §6.3 未细化接班机制，此为其最小可运维实现（见 PR2
    总结：偏离点）。
    """
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)",
                            (_PRICE_BOOK_LOCK_KEY,))
                # 锁内取原始 timestamptz（区间比较不能用 epoch 浮点表示）
                cur.execute(
                    "SELECT price_book_id, kind, status, effective_from, "
                    "effective_to FROM billing_price_books "
                    "WHERE price_book_id=%s FOR UPDATE", (price_book_id,))
                locked = cur.fetchone()
                if locked is None:
                    raise ValueError("price book 不存在：%s" % price_book_id)
                if locked["status"] == "active":
                    cur.execute("SELECT " + _BOOK_SEL +
                                " FROM billing_price_books WHERE price_book_id=%s",
                                (price_book_id,))
                    return _book_out(cur.fetchone())  # 幂等
                if locked["status"] != "draft":
                    raise ValueError("retired 价格书不可重新激活")
                eff_from = locked["effective_from"]
                eff_to = locked["effective_to"]
                cur.execute(
                    "SELECT DISTINCT b.price_book_id, b.effective_from, "
                    "b.effective_to FROM billing_price_books b "
                    "JOIN billing_rates r ON r.price_book_id = b.price_book_id "
                    "JOIN billing_rates r0 ON r0.price_book_id = %s "
                    "  AND r.provider = r0.provider AND r.model = r0.model "
                    "WHERE b.status = 'active' AND b.kind = %s "
                    "  AND b.price_book_id <> %s "
                    "  AND b.effective_from < COALESCE(%s, 'infinity'::timestamptz)"
                    "  AND (b.effective_to IS NULL OR b.effective_to > %s)",
                    (price_book_id, locked["kind"], price_book_id,
                     eff_to, eff_from))
                overlaps = [dict(r) for r in cur.fetchall()]
                for other in overlaps:
                    if not (supersede and other["effective_from"] < eff_from):
                        raise PriceBookOverlapError(
                            "active 区间与既有价格书重叠（%s）"
                            % other["price_book_id"],
                            overlapped=other["price_book_id"])
                    # 收口：[other.from, ∞/x) → [other.from, eff_from)
                    cur.execute(
                        "UPDATE billing_price_books SET effective_to=%s "
                        "WHERE price_book_id=%s AND status='active'",
                        (eff_from, other["price_book_id"]))
                cur.execute(
                    "UPDATE billing_price_books SET status='active', "
                    "created_by=COALESCE(created_by, %s) "
                    "WHERE price_book_id=%s RETURNING " + _BOOK_SEL,
                    (actor, price_book_id))
                return _book_out(cur.fetchone())
    finally:
        conn.close()


def get_price_book(price_book_id):
    """按 id 读价格书（含行数）；不存在返回 None。"""
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _BOOK_SEL +
                            " FROM billing_price_books WHERE price_book_id=%s",
                            (price_book_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                out = _book_out(row)
                cur.execute(
                    "SELECT provider, model, time_band, "
                    "cache_hit_nano_per_million, cache_miss_nano_per_million, "
                    "output_nano_per_million FROM billing_rates "
                    "WHERE price_book_id=%s ORDER BY provider, model, time_band",
                    (price_book_id,))
                out["rates"] = [dict(r) for r in cur.fetchall()]
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 账户 / 账本 / 余额快照（影子阶段无任何路由写 debit；demo 永不开户）
# --------------------------------------------------------------------------- #
_LEDGER_ENTRY_ID_PREFIX = "ble_"


def create_billing_account(user_id, *, account_id=None, actor=None):
    """为注册用户开户（首次 grant/topup 或启用受控 debit 时显式创建；
    demo 主体永不调用本函数）。已开户抛 BillingAccountExistsError。"""
    platform_features.require_pg_backend("billing")
    acct = account_id or ("bac_" + secrets.token_hex(12))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO billing_accounts "
                        "(account_id, user_id) VALUES (%s,%s) "
                        "RETURNING account_id, user_id, currency, status, "
                        "soft_spend_cap_nano, hard_spend_cap_nano, version",
                        (acct, user_id))
                    return dict(cur.fetchone())
                except psycopg.errors.UniqueViolation as exc:
                    raise BillingAccountExistsError(
                        "该用户已开户（user_id 唯一）", user_id=user_id) from exc
    finally:
        conn.close()


def get_billing_account_by_user(user_id):
    """按 user_id 读账户；未开户返回 None（不伪造 0 余额账户）。"""
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id, user_id, currency, status, "
                    "soft_spend_cap_nano, hard_spend_cap_nano, version "
                    "FROM billing_accounts WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def append_ledger_entry(account_id, kind, amount_nano_cny, idempotency_key, *,
                        reason="", actor_user_id=None, metadata=None,
                        event_id=None):
    """追加一条不可变 ledger entry（符号语义由数据库 CHECK 强制；冲正只追加）。

    usage_debit 的幂等键固定 ``usage:<event_id>``（调用方保证；部分唯一索引
    兜底同 event 只一条 debit）。idempotency_key 重放 → 返回原行
    （duplicate=True），不重复入账。**影子阶段没有任何路由调用本函数写
    usage_debit**（§12.1）；保留原语供 Phase B 与测试验证符号/幂等约束。
    """
    platform_features.require_pg_backend("billing")
    if kind == "usage_debit" and (not event_id
                                  or idempotency_key != "usage:%s" % event_id):
        raise ValueError("usage_debit 必须携带 event_id 且幂等键固定为 "
                         "usage:<event_id>")
    entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_ledger_entries "
                    "(entry_id, account_id, event_id, kind, amount_nano_cny, "
                    " idempotency_key, reason, actor_user_id, metadata) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING entry_id, account_id, event_id, kind, "
                    "amount_nano_cny, idempotency_key, reason, actor_user_id, "
                    "extract(epoch from created_at)::float8 AS created_at",
                    (entry_id, account_id, event_id, kind,
                     int(amount_nano_cny), idempotency_key, reason or "",
                     actor_user_id,
                     psycopg.types.json.Jsonb(dict(metadata or {}))))
                row = cur.fetchone()
                if row is not None:
                    out = dict(row)
                    out["amount_nano_cny"] = int(out["amount_nano_cny"])
                    out["duplicate"] = False
                    return out
                cur.execute(
                    "SELECT entry_id, account_id, event_id, kind, "
                    "amount_nano_cny, idempotency_key, reason, actor_user_id, "
                    "extract(epoch from created_at)::float8 AS created_at "
                    "FROM billing_ledger_entries WHERE idempotency_key=%s",
                    (idempotency_key,))
                row = cur.fetchone()
                out = dict(row)
                out["amount_nano_cny"] = int(out["amount_nano_cny"])
                out["duplicate"] = True
                return out
    finally:
        conn.close()


def account_balance_nano(account_id):
    """权威可用余额 = 该账户 ledger 有符号金额合计（无账户 → None）。"""
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries WHERE account_id=%s",
                    (account_id,))
                bal = int(cur.fetchone()["bal"])
                cur.execute(
                    "SELECT 1 FROM billing_accounts WHERE account_id=%s",
                    (account_id,))
                if cur.fetchone() is None:
                    return None
        return bal
    finally:
        conn.close()


#: 人工调账允许的 kind（§9：grant/topup/refund 为正，manual_adjustment 非零；
#: usage_debit/expiry 不经 admin 调账入口——影子阶段无 debit，PR6 走受控路径）
ADJUSTMENT_KINDS = ("grant", "topup", "refund", "manual_adjustment")

#: 人工调账 reason 长度上限（§9：必填非空，上限 500）
ADJUSTMENT_REASON_MAX_LENGTH = 500

#: 同事务写 audit 的动作名（detail 不含敏感字段；idempotency_key 在 admin v1
#: 出口只保留后 8 字符，§10.5）
CAPS_AUDIT_ACTION = "billing.caps_update"
ADJUST_AUDIT_ACTION = "billing.adjust"

_ACCOUNT_SEL = ("account_id, user_id, currency, status, "
                "soft_spend_cap_nano, hard_spend_cap_nano, version, "
                "extract(epoch from created_at)::float8 AS created_at, "
                "extract(epoch from updated_at)::float8 AS updated_at")


def _validate_cap_value(value, field):
    """cap 值校验：None=清除（§9）；否则须为 ≥0 整数（不接受 bool）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 需为非负整数或 null" % field)
    if value < 0:
        raise ValueError("%s 需为非负整数（nano-CNY）" % field)
    return value


def update_account_caps(user_id, soft_cap_nano, hard_cap_nano, expected_version,
                        *, actor_user_id, audit_detail=None):
    """CAS 更新账户 soft/hard spend cap，与 audit 同一事务（§9 caps 规则）。

    语义（PR5，方案 §9）：
      - ``soft_cap_nano`` / ``hard_cap_nano``：``None``=清除该上限；非空须为
        ≥0 整数；两者同存须 ``soft <= hard``；
      - ``expected_version``：客户端携带的当前 ``billing_accounts.version``，
        ``UPDATE ... WHERE version=%s`` 命中后 version+1；0 行命中抛
        :class:`BillingCapsVersionConflictError`（409，不做 last-write-wins）；
      - 未开户抛 :class:`BillingAccountNotFoundError`（404，绝不隐式开户）；
      - audit 经 :func:`share_store_pg.record_audit_tx` 在**同一事务**内写入，
        失败随事务回滚（审计不丢原则，PR2 已确立）。

    返回 ``{"account": <更新后账户行>, "balance_nano": <同事务余额合计>}``。
    """
    platform_features.require_pg_backend("billing")
    soft = _validate_cap_value(soft_cap_nano, "soft_cap_nano_cny")
    hard = _validate_cap_value(hard_cap_nano, "hard_cap_nano_cny")
    if soft is not None and hard is not None and soft > hard:
        raise ValueError("soft_cap_nano_cny 不可大于 hard_cap_nano_cny")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ValueError("version 需为整数")
    if expected_version < 1:
        raise ValueError("version 需为正整数")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id FROM billing_accounts WHERE user_id=%s",
                    (user_id,))
                row = cur.fetchone()
                if row is None:
                    raise BillingAccountNotFoundError(
                        "该用户尚未开户（caps 更新不隐式开户）", user_id=user_id)
                account_id = row["account_id"]
                cur.execute(
                    "UPDATE billing_accounts SET soft_spend_cap_nano=%s, "
                    "hard_spend_cap_nano=%s, version=version+1, updated_at=now() "
                    "WHERE account_id=%s AND version=%s",
                    (soft, hard, account_id, expected_version))
                if cur.rowcount != 1:
                    raise BillingCapsVersionConflictError(
                        "caps 版本冲突（数据已被他人修改，请刷新后重试）",
                        user_id=user_id, expected_version=expected_version)
                cur.execute("SELECT " + _ACCOUNT_SEL +
                            " FROM billing_accounts WHERE account_id=%s",
                            (account_id,))
                account = dict(cur.fetchone())
                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries WHERE account_id=%s",
                    (account_id,))
                balance = int(cur.fetchone()["bal"])
                detail = dict(audit_detail or {})
                detail.setdefault("user_id", user_id)
                detail.update({
                    "soft_cap_nano": soft,
                    "hard_cap_nano": hard,
                    "previous_version": expected_version,
                    "new_version": account["version"],
                })
                share_store_pg.record_audit_tx(
                    cur, CAPS_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="billing_account", target_id=account_id,
                    detail=detail)
        return {"account": account, "balance_nano": balance}
    finally:
        conn.close()


def _entry_sel(prefix=""):
    return (prefix + "entry_id, account_id, event_id, kind, amount_nano_cny, "
            "idempotency_key, reason, actor_user_id, "
            "extract(epoch from created_at)::float8 AS created_at")


def apply_billing_adjustment(user_id, kind, amount_nano_cny, reason,
                             idempotency_key, *, actor_user_id,
                             audit_detail=None):
    """人工调账入账（grant/topup/refund/manual_adjustment），与 audit 同一事务。

    语义（§9 + §12.2 Phase B）：
      - grant/topup：用户尚无账户时**同事务显式开户**后入账（并发开户用
        SAVEPOINT 吸收 UniqueViolation 后改读既有行）；
      - refund/manual_adjustment：未开户抛
        :class:`BillingAccountNotFoundError`（404，不隐式开户）；
      - 符号先在路由层校验（400），DB CHECK 兜底；本函数同样先验（防御
        直调路径）：grant/topup/refund 必须 >0，manual_adjustment ≠0；
      - ``reason`` 必填非空（trim 后 ≥1），上限 500；``idempotency_key``
        必填非空（路由层缺省生成 ``adj_<hex>``）；
      - ``idempotency_key`` 重放：返回原 entry + ``duplicate=True``，不重复
        入账、不重复写 audit；key 已被**不同账户或不同参数**（kind/金额/
        reason 任一不一致）的请求占用抛
        :class:`BillingIdempotencyKeyConflictError`（409）；
      - ledger 入账 + audit 同一事务（record_audit_tx 失败即整体回滚）。

    返回 ``{"entry": <entry 行>, "duplicate": bool, "balance_nano": int,
    "account": <账户行>}``。
    """
    platform_features.require_pg_backend("billing")
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError("kind 需为 %s" % (ADJUSTMENT_KINDS,))
    if isinstance(amount_nano_cny, bool) or not isinstance(amount_nano_cny, int):
        raise ValueError("amount_nano_cny 需为整数（nano-CNY）")
    if kind in ("grant", "topup", "refund") and amount_nano_cny <= 0:
        raise ValueError("%s 金额必须为正数（nano-CNY）" % kind)
    if kind == "manual_adjustment" and amount_nano_cny == 0:
        raise ValueError("manual_adjustment 金额不可为 0")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason 必填（不可为空白）")
    if len(reason) > ADJUSTMENT_REASON_MAX_LENGTH:
        raise ValueError("reason 上限 %d 字符" % ADJUSTMENT_REASON_MAX_LENGTH)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key 必填（路由层缺省生成）")

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id FROM billing_accounts WHERE user_id=%s",
                    (user_id,))
                row = cur.fetchone()
                if row is None:
                    if kind not in ("grant", "topup"):
                        raise BillingAccountNotFoundError(
                            "该用户尚未开户（%s 不隐式开户）" % kind,
                            user_id=user_id)
                    # 首次 grant/topup 显式开户（§9）。并发竞态：对方先提交同
                    # user_id 账户 → INSERT 抛 UniqueViolation，rollback 到
                    # savepoint 恢复事务后改读既有行（pg_store.transaction 无
                    # 自动 savepoint，失败语句会置 aborted，先例见 ingest）。
                    cur.execute("SAVEPOINT sp_adjust_open_account")
                    try:
                        cur.execute(
                            "INSERT INTO billing_accounts (account_id, user_id) "
                            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING "
                            "RETURNING account_id",
                            ("bac_" + secrets.token_hex(12), user_id))
                        opened = cur.fetchone()
                    except psycopg.errors.UniqueViolation:
                        cur.execute(
                            "ROLLBACK TO SAVEPOINT sp_adjust_open_account")
                        cur.execute(
                            "SELECT account_id FROM billing_accounts "
                            "WHERE user_id=%s", (user_id,))
                        opened = cur.fetchone()
                    if opened is None:  # ON CONFLICT DO NOTHING 未插入
                        cur.execute(
                            "SELECT account_id FROM billing_accounts "
                            "WHERE user_id=%s", (user_id,))
                        opened = cur.fetchone()
                    account_id = opened["account_id"]
                else:
                    account_id = row["account_id"]

                entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
                # ON CONFLICT DO NOTHING 不置 aborted，可直接重读原行判重放
                cur.execute(
                    "INSERT INTO billing_ledger_entries "
                    "(entry_id, account_id, event_id, kind, amount_nano_cny, "
                    " idempotency_key, reason, actor_user_id, metadata) "
                    "VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING " + _entry_sel(),
                    (entry_id, account_id, kind, int(amount_nano_cny),
                     idempotency_key, reason, actor_user_id,
                     psycopg.types.json.Jsonb({})))
                new_row = cur.fetchone()
                duplicate = new_row is None
                if duplicate:
                    cur.execute(
                        "SELECT " + _entry_sel() +
                        " FROM billing_ledger_entries "
                        "WHERE idempotency_key=%s", (idempotency_key,))
                    existing = dict(cur.fetchone())
                    existing["amount_nano_cny"] = int(existing["amount_nano_cny"])
                    # 真重放 = 同 key 且请求载荷逐项一致；同 key 不同载荷是
                    # 客户端 bug 或伪造重放，必须 409 而非静默返回原行（与
                    # ingest payload_hash 冲突同原则，防止金额被张冠李戴）。
                    if (existing["account_id"] != account_id
                            or existing["kind"] != kind
                            or existing["amount_nano_cny"] != int(amount_nano_cny)
                            or existing["reason"] != reason):
                        raise BillingIdempotencyKeyConflictError(
                            "idempotency_key 已被不同参数的调账使用",
                            user_id=user_id)
                    entry = existing
                else:
                    entry = dict(new_row)
                    entry["amount_nano_cny"] = int(entry["amount_nano_cny"])

                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries WHERE account_id=%s",
                    (account_id,))
                balance = int(cur.fetchone()["bal"])
                cur.execute("SELECT " + _ACCOUNT_SEL +
                            " FROM billing_accounts WHERE account_id=%s",
                            (account_id,))
                account = dict(cur.fetchone())
                if not duplicate:
                    # 重放不重复写 audit（entry 只有一条，audit 与之一一对应）
                    detail = dict(audit_detail or {})
                    detail.setdefault("user_id", user_id)
                    detail.update({
                        "kind": kind,
                        "amount_nano_cny": int(amount_nano_cny),
                        "reason": reason,
                        "idempotency_key": idempotency_key,
                        "balance_after_nano": balance,
                    })
                    share_store_pg.record_audit_tx(
                        cur, ADJUST_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="billing_account", target_id=account_id,
                        detail=detail)
        return {"entry": entry, "duplicate": duplicate,
                "balance_nano": balance, "account": account}
    finally:
        conn.close()


def insert_provider_balance_snapshot(provider, currency, total_balance_nano,
                                     granted_balance_nano, topped_up_balance_nano,
                                     is_available, observed_at,
                                     *, snapshot_id=None):
    """插入 provider 总余额快照（金额须已由 parse_balance_to_nano 精确换算）。"""
    platform_features.require_pg_backend("billing")
    snap = snapshot_id or ("pbs_" + secrets.token_hex(12))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO provider_balance_snapshots "
                    "(snapshot_id, provider, currency, total_balance_nano, "
                    " granted_balance_nano, topped_up_balance_nano, "
                    " is_available, observed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_id, "
                    "provider, currency, total_balance_nano, "
                    "granted_balance_nano, topped_up_balance_nano, "
                    "is_available, extract(epoch from observed_at)::float8 "
                    "AS observed_at",
                    (snap, provider, currency, int(total_balance_nano),
                     int(granted_balance_nano), int(topped_up_balance_nano),
                     bool(is_available), observed_at))
                return dict(cur.fetchone())
    finally:
        conn.close()


def latest_provider_balance_snapshot(provider):
    """该 provider 最新余额快照；无则 None。"""
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_id, provider, currency, "
                    "total_balance_nano, granted_balance_nano, "
                    "topped_up_balance_nano, is_available, "
                    "extract(epoch from observed_at)::float8 AS observed_at "
                    "FROM provider_balance_snapshots WHERE provider=%s "
                    "ORDER BY observed_at DESC LIMIT 1", (provider,))
                row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Admin API v1 只读查询原语（PR3b，admin-billing 方案 §9）
#
# 设计约束：
#   - 全部 keyset/limit 分页（禁止一次返回全量 usage/ledger）；
#   - 输出字段白名单化：不携带 raw_usage / payload_hash（大对象与互锁细节
#     不是管理页需要的信息），敏感字段红线（api_key/完整 IP/outbox 路径/
#     credential fingerprint）在数据层就不存在；
#   - 与 ingest 同一权威表，无旁路聚合表（影子阶段量级不需要物化）。
# --------------------------------------------------------------------------- #
#: admin usage 列表输出列（_EVENT_SEL 的展示子集；raw_usage/payload_hash 除外）
_ADMIN_EVENT_SEL = (
    "event_id, call_id, schema_version, request_id, session_id, "
    "subject_type, subject_id, user_id, provider, model, "
    "cache_hit_input_tokens, cache_miss_input_tokens, output_tokens, "
    "reasoning_tokens, total_tokens, "
    "extract(epoch from occurred_at)::float8 AS occurred_at, "
    "extract(epoch from enqueued_at)::float8 AS enqueued_at, "
    "extract(epoch from received_at)::float8 AS received_at, "
    "status, unpriced_reason, provider_price_book_id, charge_price_book_id, "
    "provider_cost_nano_cny, charge_nano_cny"
)

#: 合法 usage 列表筛选 status（§9：unpriced 单独可过滤，不混入 0 元）
ADMIN_USAGE_STATUSES = ("priced", "unpriced")


def admin_usage_events_page(*, cursor=None, limit=50, model=None, user_id=None,
                            subject_type=None, status=None):
    """admin usage 明细分页（§10.4：按模型/用户/状态筛选，最新在前）。

    keyset 游标：(occurred_at epoch, event_id) 降序。``status`` 限
    priced/unpriced；``user_id`` 按 ai_usage_events.user_id（权威主体解析后
    的镜像列）精确过滤；未就绪的 user_id 缺行按 NULL 存储时不会命中筛选
    （subject_type/subject_id 仍保留在行内供归因展示）。
    返回 ``{"items": [...], "next_cursor": str|None}``。
    """
    platform_features.require_pg_backend("billing")
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if model is not None:
        where.append("model=%s")
        params.append(str(model))
    if user_id is not None:
        where.append("user_id=%s")
        params.append(str(user_id))
    if subject_type is not None:
        where.append("subject_type=%s")
        params.append(str(subject_type))
    if status is not None:
        if status not in ADMIN_USAGE_STATUSES:
            raise ValueError("status 需为 %s" % (ADMIN_USAGE_STATUSES,))
        where.append("status=%s")
        params.append(status)
    after = None
    if cursor is not None:
        try:
            occurred, event_id = cursor
            after = (float(occurred), str(event_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where.append("(occurred_at < to_timestamp(%s) OR "
                     "(occurred_at = to_timestamp(%s) AND event_id < %s))")
        params.extend([after[0], after[0], after[1]])
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _ADMIN_EVENT_SEL + " FROM ai_usage_events"
                    + sql_where +
                    " ORDER BY occurred_at DESC, event_id DESC LIMIT %s",
                    params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        for key in ("provider_cost_nano_cny", "charge_nano_cny"):
            if row.get(key) is not None:
                row[key] = int(row[key])
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = (last["occurred_at"], last["event_id"])
    return {"items": rows, "next_cursor": next_cursor}


def admin_ledger_page(*, cursor=None, limit=50):
    """admin ledger 只读分页（不可变账本，最新在前；§10.4 只读表）。

    keyset 游标：(created_at epoch, entry_id) 降序。
    """
    platform_features.require_pg_backend("billing")
    limit = max(1, min(int(limit or 50), 200))
    where, params = "", []
    if cursor is not None:
        try:
            created, entry_id = cursor
            created = float(created)
            entry_id = str(entry_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where = (" WHERE (extract(epoch from created_at) < %s OR "
                 "(extract(epoch from created_at) = %s AND entry_id < %s))")
        params = [created, created, entry_id]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT entry_id, account_id, event_id, kind, "
                    "amount_nano_cny, idempotency_key, reason, actor_user_id, "
                    "extract(epoch from created_at)::float8 AS created_at "
                    "FROM billing_ledger_entries" + where +
                    " ORDER BY created_at DESC, entry_id DESC LIMIT %s",
                    params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["amount_nano_cny"] = int(row["amount_nano_cny"])
    next_cursor = None
    if has_more and rows:
        next_cursor = (rows[-1]["created_at"], rows[-1]["entry_id"])
    return {"items": rows, "next_cursor": next_cursor}


def _as_aware_dt(value):
    """窗口参数归一：epoch 秒（float/int）/ RFC3339 字符串 / datetime → aware dt。"""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return billing_pricing.parse_rfc3339(value)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def admin_overview_usage_stats(period_start=None, today_start=None):
    """概览用量聚合（§10.1；窗口：周期起点 / 今日零点，均为含端点）。

    返回 dict（全部整数或 None）：
      - ``model_calls_today`` / ``model_calls_period``：ai_usage_events 计数；
      - token 合计（cache hit/miss input、output）与 ``cache_hit_ratio``
        （hit/(hit+miss)，无输入 token 时 None）——周期窗口；
      - ``provider_cost_nano_cny`` / ``charge_nano_cny``：priced 事件金额
        合计（unpriced 不按 0 元混入）——周期窗口；
      - ``unpriced_count``：unpriced 事件数——周期窗口；
      - ``ingestion_lag_seconds_max`` / ``ingestion_lag_seconds_avg``：
        received_at - occurred_at 的最大/均值——周期窗口。

    窗口参数接受 epoch 秒 / RFC3339 字符串 / datetime（budget_store 的
    usage_report 周期起点是 epoch float）。
    """
    platform_features.require_pg_backend("billing")
    period_start = _as_aware_dt(period_start)
    today_start = _as_aware_dt(today_start)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                out = {}
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events"
                    + (" WHERE occurred_at >= %s" if today_start else ""),
                    ([today_start] if today_start else []))
                out["model_calls_today"] = int(cur.fetchone()["n"])
                period_where = " WHERE occurred_at >= %s" if period_start else ""
                period_and = " AND occurred_at >= %s" if period_start else ""
                period_params = [period_start] if period_start else []
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events"
                    + period_where, period_params)
                out["model_calls_period"] = int(cur.fetchone()["n"])
                cur.execute(
                    "SELECT "
                    "COALESCE(SUM(cache_hit_input_tokens),0)::bigint AS hit, "
                    "COALESCE(SUM(cache_miss_input_tokens),0)::bigint AS miss, "
                    "COALESCE(SUM(output_tokens),0)::bigint AS output, "
                    "COALESCE(SUM(provider_cost_nano_cny),0)::bigint AS cost, "
                    "COALESCE(SUM(charge_nano_cny),0)::bigint AS charge "
                    "FROM ai_usage_events WHERE status='priced'" + period_and,
                    period_params)
                agg = dict(cur.fetchone())
                hit, miss = int(agg["hit"]), int(agg["miss"])
                out["cache_hit_input_tokens"] = hit
                out["cache_miss_input_tokens"] = miss
                out["output_tokens"] = int(agg["output"])
                out["cache_hit_ratio"] = (
                    hit / (hit + miss)) if (hit + miss) > 0 else None
                out["provider_cost_nano_cny"] = int(agg["cost"])
                out["charge_nano_cny"] = int(agg["charge"])
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events "
                    "WHERE status='unpriced'" + period_and, period_params)
                out["unpriced_count"] = int(cur.fetchone()["n"])
                cur.execute(
                    "SELECT COALESCE(MAX(extract(epoch from received_at) "
                    "- extract(epoch from occurred_at)),0)::float8 AS lag_max, "
                    "COALESCE(AVG(extract(epoch from received_at) "
                    "- extract(epoch from occurred_at)),0)::float8 AS lag_avg "
                    "FROM ai_usage_events" + period_where, period_params)
                lag = dict(cur.fetchone())
                out["ingestion_lag_seconds_max"] = float(lag["lag_max"])
                out["ingestion_lag_seconds_avg"] = float(lag["lag_avg"])
        return out
    finally:
        conn.close()


def admin_account_summaries(user_ids):
    """一批用户的账户+余额+caps 摘要（余额 = ledger 有符号合计）。

    未开户用户不在返回 dict 中（调用方按 ``account: null`` 语义渲染，不伪造
    0 余额账户）。返回 ``{user_id: {account_id, status, currency, version,
    soft_spend_cap_nano, hard_spend_cap_nano, balance_nano}}``。
    """
    platform_features.require_pg_backend("billing")
    ids = [str(u) for u in (user_ids or []) if u]
    if not ids:
        return {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT a.user_id, a.account_id, a.currency, a.status, "
                    "a.version, a.soft_spend_cap_nano, a.hard_spend_cap_nano, "
                    "COALESCE(SUM(l.amount_nano_cny),0)::bigint AS balance "
                    "FROM billing_accounts a "
                    "LEFT JOIN billing_ledger_entries l "
                    " ON l.account_id = a.account_id "
                    "WHERE a.user_id = ANY(%s) "
                    "GROUP BY a.user_id, a.account_id, a.currency, a.status, "
                    " a.version, a.soft_spend_cap_nano, a.hard_spend_cap_nano",
                    (ids,))
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    out = {}
    for row in rows:
        out[row["user_id"]] = {
            "account_id": row["account_id"],
            "currency": row["currency"],
            "status": row["status"],
            "version": int(row["version"]),
            "soft_spend_cap_nano": row["soft_spend_cap_nano"],
            "hard_spend_cap_nano": row["hard_spend_cap_nano"],
            "balance_nano": int(row["balance"]),
        }
    return out


def admin_last_ai_call_by_user():
    """每用户最近一次 AI 调用时间（ai_usage_events.occurred_at 最大值）。

    返回 ``{user_id: epoch 秒}``；无任何事件的用户不在 dict 中。
    """
    platform_features.require_pg_backend("billing")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, extract(epoch from max(occurred_at))"
                    "::float8 AS last_call FROM ai_usage_events "
                    "WHERE user_id IS NOT NULL GROUP BY user_id")
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {r["user_id"]: float(r["last_call"]) for r in rows}
