# -*- coding: utf-8 -*-
"""人工读片行为采集研究存储（P3：docs/agent-plan-20260921-registration-
consent-research.md §6.2/§6.3/§7.1/§7.2/§7.3）。

本模块是研究读片会话与事件写入的**唯一服务端入口**（迁移 0063）：

- ``research_subjects``：随机伪名 ↔ user_id 隔离映射；研究查询默认不联
  users/email，本表是撤回/删除链的受控来源入口（§6.2）。
- ``create_viewing_session``：POST /api/research/viewing-sessions 的存储层。
  服务端创建随机会话并绑定 subject/consent_epoch；切片只存**研究伪名**
  （带盐 keyed hash，绝不复制真实文件名/路径/患者标签）。没有授权不创建。
- ``append_viewer_events``：POST /api/research/viewer-events 的存储层。
  从 session 解析主体（session → subject → user），**不信任客户端**提交的
  user/owner/资源映射；批次 ≤50 条 / 64 KiB；逐事件白名单校验（enum、seq、
  id、数值范围、字段白名单），**额外字段整批拒绝**；相同 event_id 重传
  幂等（内容一致 → replayed），不同内容复用 ID → 409 冲突（不覆盖旧事件）。
- **撤回即时阻断（§6.3-3）**：ingestion 与 ``research_consent_store.withdraw``
  使用同一 ``user_research_consents`` 行的 ``SELECT ... FOR UPDATE`` 锁序——
  撤回事务提交后的 ingestion 事务在锁上排队、重读后看到 withdrawn 而失败；
  撤回前刚提交的数据由删除作业清理。
- **observe_pause 无时长字段（§7.2）**：白名单里只有 bbox /
  image_zoom_ratio / evidence=inferred_stable_view——结构上不可能出现
  duration_ms / dwell_ms / 起止时间。server_received_at 只用于接收、保留期
  与运维，不用于计算等待时长。
- **速率限制（§7.3）**：每用户/每会话有界窗口（auth_rate_limits 通用桶表，
  scope 专用前缀 research_），超限 429 + Retry-After；观测事件可丢，不阻塞
  标注或主业务。
- **采集开关默认关闭（§1/P0 决定）**：``RESEARCH_COLLECTION_ENABLED`` 未
  开启时 create/append 一律 NotAuthorizedError(collection_disabled)——旧按钮
  /旧字段无法重新打开未授权采集。授权权威与开关判定复用
  ``research_consent_store``（§6.1 唯一权威），本模块不另建第二套 consent。
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time

import psycopg

import agreement_store
import pg_store
import research_consent_store

#: 事件 schema 版本（客户端 schema_version 必须等于本值）
SCHEMA_VERSION = "research-viewer-events-v1"

#: 事件动作词表（与 0063 迁移 CHECK 一致）
EVENT_ACTIONS = (
    "zoom_in", "zoom_out", "pan", "observe_pause",
    "annotation_create", "annotation_update", "annotation_delete",
    "annotation_accept", "annotation_reject",
)

#: 输入方式词表（§7.1：滚轮/触控缩放/按钮/快捷键/拖拽/双击）
INPUT_KINDS = ("wheel", "pinch", "drag", "keyboard", "button", "dblclick")

#: 标注工具/形状类型词表（§7.4：只收操作类别/形状/几何，不收自由文本）
TOOL_TYPES = ("rect", "arrow", "freehand")

#: observe_pause 的固定证据标记（§7.2：推断的稳定视野，不是注意力证据）
OBSERVE_EVIDENCE = "inferred_stable_view"

#: 批次上限（§7.3：批次最大 50 条、64 KiB）
BATCH_MAX_EVENTS = 50
BATCH_MAX_BYTES = 64 * 1024

#: 研究动作保留期（§8：每条最多 90 天，到期删除）
RETENTION_DAYS = 90

#: 会话保持 active 的窗口之外自动过期（懒判定；与保留期同源）
SESSION_TTL_DAYS = RETENTION_DAYS

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_SESSION_ID_RE = re.compile(r"^rvs_[A-Za-z0-9_-]{8,64}$")


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


#: 速率限制（§7.3 有界；env 可调，窗口/锁定秒数固定 60/60）。默认值模块级，
#: 实际阈值每次调用时重读 env（测试可 monkeypatch.setenv 即时生效）。
RATE_SESSION_CREATE_LIMIT = 30
RATE_EVENTS_USER_LIMIT = 120
RATE_EVENTS_SESSION_LIMIT = 60
_RATE_WINDOW_SECONDS = 60
_RATE_LOCK_SECONDS = 60


def _session_create_limit():
    return _int_env("RESEARCH_SESSION_CREATE_BURST",
                    RATE_SESSION_CREATE_LIMIT)


def _events_user_limit():
    return _int_env("RESEARCH_EVENTS_USER_BURST", RATE_EVENTS_USER_LIMIT)


def _events_session_limit():
    return _int_env("RESEARCH_EVENTS_SESSION_BURST", RATE_EVENTS_SESSION_LIMIT)


# --------------------------------------------------------------------------- #
# 异常（路由层按 code 映射 4xx/429/503；§7.3）
# --------------------------------------------------------------------------- #
class ResearchStoreError(RuntimeError):
    """研究存储业务异常基类。"""

    code = "research_error"


class NotAuthorizedError(ResearchStoreError):
    """未授权（开关关闭/未同意/已撤回/文档过期/删除任务未清）→ 403。"""

    code = "research_not_authorized"

    def __init__(self, reason):
        super().__init__("当前没有有效的数据共享研究授权")
        self.reason = reason


class BatchTooLargeError(ResearchStoreError):
    """批次超过 50 条 → 400。"""

    code = "batch_too_large"


class PayloadTooLargeError(ResearchStoreError):
    """请求体超过 64 KiB → 413。"""

    code = "payload_too_large"


class EventValidationError(ResearchStoreError):
    """事件结构/白名单校验失败（整批拒绝）→ 400。"""

    code = "invalid_event"


class EpochMismatchError(ResearchStoreError):
    """consent_epoch 与会话/当前不一致 → 409。"""

    code = "epoch_mismatch"


class EventIdConflictError(ResearchStoreError):
    """相同 event_id 携带不同内容 → 409（不覆盖旧事件）。"""

    code = "event_id_conflict"


class SeqConflictError(ResearchStoreError):
    """相同 seq 携带不同事件 → 409。"""

    code = "seq_conflict"


class SessionNotFoundError(ResearchStoreError):
    """会话不存在或不属于当前用户 → 404。"""

    code = "session_not_found"


class SessionClosedError(ResearchStoreError):
    """会话已关闭/过期/撤销 → 409。"""

    code = "session_closed"


class RateLimitedError(ResearchStoreError):
    """有界速率限制超限 → 429 + Retry-After。"""

    code = "rate_limited"

    def __init__(self, retry_after):
        super().__init__("研究上报过于频繁，请稍后重试")
        self.retry_after = max(1, int(retry_after))


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 切片研究伪名（§6.2：不复制真实文件名/路径/患者标签）
# --------------------------------------------------------------------------- #
_PSEUDONYM_SALT_CACHE = {"value": None}


def _pseudonym_salt(cur) -> str:
    """伪名盐：env 显式配置优先，否则 platform_settings 持久化随机盐。

    盐只影响伪名不可逆性（与 user_id 的对应关系由 research_subjects 受控
    持有），泄露单个盐不暴露任何账号信息；创建走 INSERT ... ON CONFLICT
    DO NOTHING，多 worker 并发首启只落一份。
    """
    env_salt = (os.environ.get("RESEARCH_PSEUDONYM_SALT") or "").strip()
    if env_salt:
        return env_salt
    if _PSEUDONYM_SALT_CACHE["value"]:
        return _PSEUDONYM_SALT_CACHE["value"]
    cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                ("research_pseudonym_salt",))
    row = cur.fetchone()
    if row is not None and isinstance(row["value"], dict) \
            and row["value"].get("salt"):
        _PSEUDONYM_SALT_CACHE["value"] = str(row["value"]["salt"])
        return _PSEUDONYM_SALT_CACHE["value"]
    salt = secrets.token_hex(32)
    cur.execute(
        "INSERT INTO platform_settings (key, value, updated_at, updated_by) "
        "VALUES ('research_pseudonym_salt', %s::jsonb, now(), 'research_store') "
        "ON CONFLICT (key) DO NOTHING",
        (json.dumps({"salt": salt}),))
    cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                ("research_pseudonym_salt",))
    row = cur.fetchone()
    _PSEUDONYM_SALT_CACHE["value"] = str(row["value"]["salt"])
    return _PSEUDONYM_SALT_CACHE["value"]


def slide_pseudonym(slide_name, cur) -> str:
    """业务切片 ID → 研究伪名（带盐 keyed hash，截断 32 hex）。

    必须在既有事务游标上调用（盐的懒创建 INSERT 随调用方事务提交）。
    """
    salt = _pseudonym_salt(cur)
    digest = hmac.new(("rspd:" + salt).encode("utf-8"),
                      ("slide:" + slide_name).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return "sl_" + digest[:32]


# --------------------------------------------------------------------------- #
# 事件白名单校验（§7.1/§7.2/§7.3：额外字段整批拒绝）
# --------------------------------------------------------------------------- #
def _err(index, why):
    raise EventValidationError("events[%d] %s" % (index, why))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def _check_unit(value, index, field):
    """[0,1] 归一化数值：有限、边界内、四位小数（§7.1）。"""
    if not _is_number(value):
        _err(index, "%s 必须是有限数值" % field)
    if not (0 <= value <= 1):
        _err(index, "%s 必须在 [0,1] 归一化范围内" % field)
    if round(float(value), 4) != float(value):
        _err(index, "%s 必须是四位小数" % field)


def _check_bbox(bbox, index, field):
    if not isinstance(bbox, list) or len(bbox) != 4:
        _err(index, "%s 必须是 [x,y,w,h] 四元数组" % field)
    for v in bbox:
        _check_unit(v, index, field)
    if not (bbox[2] > 0 and bbox[3] > 0):
        _err(index, "%s 的 w/h 必须大于 0" % field)


def _check_zoom_ratio(value, index):
    if not _is_number(value) or not (0 < value <= 1000):
        _err(index, "image_zoom_ratio 必须是 (0,1000] 内的有限数值")
    if round(float(value), 4) != float(value):
        _err(index, "image_zoom_ratio 必须是四位小数")


#: 每个动作的 payload 字段白名单与结构口径（避免散落 if）
_PAYLOAD_SPECS = {
    "zoom_in": {
        "keys": {"bbox_before", "bbox_after", "image_zoom_ratio",
                 "input_kind", "changed_center"},
        "bbox": ("bbox_before", "bbox_after"),
        "zoom": True, "input_kind": True,
    },
    "zoom_out": None,  # 与 zoom_in 同规格（下方复制）
    "pan": {
        "keys": {"bbox_before", "bbox_after", "input_kind"},
        "bbox": ("bbox_before", "bbox_after"),
        "zoom": False, "input_kind": True,
    },
    "observe_pause": {
        "keys": {"bbox", "image_zoom_ratio", "evidence"},
        "bbox": ("bbox",), "zoom": True, "input_kind": False,
        "evidence": True,
    },
    "annotation_create": {
        "keys": {"tool_type", "shape_type", "bbox", "annotation_local_id",
                 "origin"},
        "bbox": ("bbox",), "zoom": False, "input_kind": False,
        "annotation": True,
    },
    "annotation_update": None,   # 同 annotation_create
    "annotation_delete": {
        "keys": {"annotation_local_id", "origin"},
        "bbox": (), "zoom": False, "input_kind": False,
        "annotation": True, "no_geometry": True,
    },
    "annotation_accept": {
        "keys": {"annotation_local_id", "origin"},
        "bbox": (), "zoom": False, "input_kind": False,
        "annotation": True, "no_geometry": True,
    },
    "annotation_reject": None,   # 同 annotation_accept
}
_PAYLOAD_SPECS["zoom_out"] = dict(_PAYLOAD_SPECS["zoom_in"])
_PAYLOAD_SPECS["annotation_update"] = dict(_PAYLOAD_SPECS["annotation_create"])
_PAYLOAD_SPECS["annotation_reject"] = dict(_PAYLOAD_SPECS["annotation_accept"])

#: 标注动作的固定来源（§7.1：human / human_review）
_ANNOTATION_ORIGINS = {
    "annotation_create": "human",
    "annotation_update": "human",
    "annotation_delete": "human",
    "annotation_accept": "human_review",
    "annotation_reject": "human_review",
}


def validate_event(index, event) -> dict:
    """单事件白名单校验；返回规范化后的 (event_id, seq, action, payload)。

    顶层键必须**恰好**是 {event_id, seq, action, schema_version, payload}
    （额外字段整批拒绝，§7.3）；payload 只允许该 action 白名单内的键。
    任何失败抛 EventValidationError（路由层 400，整批不写）。
    """
    if not isinstance(event, dict):
        _err(index, "必须是对象")
    expected_keys = {"event_id", "seq", "action", "schema_version", "payload"}
    if set(event.keys()) != expected_keys:
        extra = sorted(set(event.keys()) - expected_keys)
        missing = sorted(expected_keys - set(event.keys()))
        _err(index, "顶层字段不合法（extra=%s missing=%s）" % (extra, missing))
    event_id = event["event_id"]
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        _err(index, "event_id 必须是 6..80 位 [A-Za-z0-9_-] 字符串")
    seq = event["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        _err(index, "seq 必须是 >=1 的整数")
    action = event["action"]
    if action not in EVENT_ACTIONS:
        _err(index, "未知 action：%r" % (action,))
    if event["schema_version"] != SCHEMA_VERSION:
        _err(index, "schema_version 必须是 %s" % SCHEMA_VERSION)
    payload = event["payload"]
    if not isinstance(payload, dict):
        _err(index, "payload 必须是对象")
    spec = _PAYLOAD_SPECS[action]
    unknown = sorted(set(payload.keys()) - spec["keys"])
    if unknown:
        _err(index, "payload 含非白名单字段 %s（整批拒绝）" % unknown)
    missing = sorted(spec["keys"] - set(payload.keys()))
    if missing:
        _err(index, "payload 缺少必填字段 %s" % missing)
    for field in spec["bbox"]:
        _check_bbox(payload[field], index, field)
    if spec["zoom"]:
        _check_zoom_ratio(payload["image_zoom_ratio"], index)
    if spec["input_kind"]:
        if payload["input_kind"] not in INPUT_KINDS:
            _err(index, "input_kind 必须在 %s 内" % (INPUT_KINDS,))
    if "changed_center" in spec["keys"]:
        if not isinstance(payload["changed_center"], bool):
            _err(index, "changed_center 必须是布尔值")
    if spec.get("evidence"):
        if payload["evidence"] != OBSERVE_EVIDENCE:
            _err(index, "evidence 必须是 %s" % OBSERVE_EVIDENCE)
    if spec.get("annotation"):
        local_id = payload["annotation_local_id"]
        if not isinstance(local_id, str) or not _LOCAL_ID_RE.match(local_id):
            _err(index, "annotation_local_id 必须是 4..64 位匿名局部 ID")
        if payload["origin"] != _ANNOTATION_ORIGINS[action]:
            _err(index, "origin 必须是 %s" % _ANNOTATION_ORIGINS[action])
        if not spec.get("no_geometry"):
            if payload["tool_type"] not in TOOL_TYPES:
                _err(index, "tool_type 必须在 %s 内" % (TOOL_TYPES,))
            if payload["shape_type"] not in TOOL_TYPES:
                _err(index, "shape_type 必须在 %s 内" % (TOOL_TYPES,))
    return {"event_id": event_id, "seq": seq, "action": action,
            "schema_version": event["schema_version"], "payload": payload}


# --------------------------------------------------------------------------- #
# 速率限制（auth_rate_limits 通用桶；allow-then-block 语义）
# --------------------------------------------------------------------------- #
def _record_rate_hit(scope, subject, limit):
    """独立事务记录一次请求并判定是否超限；返回 retry_after 秒（0=放行）。

    **必须独立于主 ingestion 事务提交**：被 403/409/校验失败拒绝的请求同样
    要推进计数/锁定，否则超限阈值永远打不到（回滚连带清零）。复用
    auth_rate_limits 的 (scope, subject_hash) 桶表：窗口内第 1..limit 次放行，
    超过即锁定 lock 秒（锁期内直接拒绝）。语义与登录防爆破计数器独立
    （专用 scope 前缀 research_*），TRUNCATE 隔离与运维口径互不影响。
    """
    if limit <= 0:
        return 0
    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                cur.execute(
                    "SELECT extract(epoch from window_started_at)::float8 AS ws,"
                    " failed_count, "
                    "extract(epoch from locked_until)::float8 AS locked_until "
                    "FROM auth_rate_limits "
                    "WHERE scope=%s AND subject_hash=%s FOR UPDATE",
                    (scope, subject))
                row = cur.fetchone()
                if row is not None and row["locked_until"] is not None \
                        and row["locked_until"] > now:
                    return int(math.ceil(row["locked_until"] - now))
                if row is None or row["ws"] is None \
                        or row["ws"] + _RATE_WINDOW_SECONDS <= now:
                    count, window_start = 1, now
                else:
                    count = int(row["failed_count"]) + 1
                    window_start = row["ws"]
                locked_until = now + _RATE_LOCK_SECONDS if count > limit else None
                cur.execute(
                    "INSERT INTO auth_rate_limits "
                    "(scope, subject_hash, window_started_at, failed_count, "
                    " locked_until, updated_at) VALUES "
                    "(%s,%s, to_timestamp(%s), %s, "
                    " CASE WHEN %s THEN to_timestamp(%s) ELSE NULL END, now()) "
                    "ON CONFLICT (scope, subject_hash) DO UPDATE SET "
                    "window_started_at=to_timestamp(%s), failed_count=%s, "
                    "locked_until=CASE WHEN %s THEN to_timestamp(%s) "
                    "ELSE NULL END, updated_at=now()",
                    (scope, subject, window_start, count,
                     locked_until is not None, locked_until or 0,
                     window_start, count,
                     locked_until is not None, locked_until or 0))
                if locked_until is not None:
                    return int(math.ceil(locked_until - now))
                return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 授权闸（§6.1 唯一权威 research_consent_store + §6.3-3 撤回锁序）
# --------------------------------------------------------------------------- #
_CONSENT_COLUMNS = ("user_id, state, scope_version, document_version, "
                    "document_sha256, epoch, granted_at, withdrawn_at, "
                    "updated_at")


def _lock_consent(cur, user_id):
    """锁 consent 行（与 withdraw 同一行锁：撤回提交后的写入不成功）。"""
    cur.execute(
        "SELECT %s FROM user_research_consents WHERE user_id=%%s FOR UPDATE"
        % _CONSENT_COLUMNS, (user_id,))
    return cur.fetchone()


def _assert_ingest_allowed(cur, user_id, environ=None):
    """ingestion 前的权威判定（在 consent 行锁内执行，读到的是提交后状态）。

    账号 active/非预览/非 demo 由路由层（_require_auth + 预览写闸）保证，
    此处不重复；本函数覆盖 §6.1 的 consent/文档/删除任务/开关四项。
    返回 consent 行（供 epoch 比对）。
    """
    if not research_consent_store.collection_enabled(environ):
        raise NotAuthorizedError("collection_disabled")
    consent = _lock_consent(cur, user_id)
    if consent is None or consent["state"] != "granted":
        raise NotAuthorizedError("not_granted")
    published = agreement_store.current_published(
        research_consent_store.RESEARCH_DOCUMENT_TYPE)
    if published is None:
        raise NotAuthorizedError("document_not_published")
    if consent["document_version"] != published["version"]:
        raise NotAuthorizedError("document_version_stale")
    # 删除义务未了结（completed 之外：pending/running/failed，含达上限、
    # 待人工处置的终态 failed）→ 阻断采集；只有 completed 解除（§6.3-5）
    cur.execute(
        "SELECT 1 FROM research_data_deletion_jobs "
        "WHERE user_id=%%s AND %s"
        % research_consent_store.UNRESOLVED_DELETION_JOBS_SQL,
        (user_id,))
    if cur.fetchone() is not None:
        raise NotAuthorizedError("deletion_pending")
    return consent


def _ensure_subject(cur, user_id) -> str:
    """取/建 user 的研究伪名（每用户一个，稳定复用）。"""
    cur.execute("SELECT subject_id FROM research_subjects WHERE user_id=%s",
                (user_id,))
    row = cur.fetchone()
    if row is not None:
        return row["subject_id"]
    subject_id = "rs_" + secrets.token_urlsafe(18)
    cur.execute(
        "INSERT INTO research_subjects (subject_id, user_id) VALUES (%s,%s) "
        "ON CONFLICT (user_id) DO NOTHING", (subject_id, user_id))
    cur.execute("SELECT subject_id FROM research_subjects WHERE user_id=%s",
                (user_id,))
    return cur.fetchone()["subject_id"]


# --------------------------------------------------------------------------- #
# 研究读片会话（POST /api/research/viewing-sessions）
# --------------------------------------------------------------------------- #
def create_viewing_session(user_id, slide_name, *, environ=None) -> dict:
    """创建研究读片会话（§7.3）：服务端绑定 subject/epoch，无授权不创建。

    - slide_name 由路由层完成 ACL（第一版仅本人拥有的切片，§6.1 资源权利）；
      本函数只收业务切片标识并落**研究伪名**；
    - 事务内顺序：速率桶 → consent 行锁 → 权威判定 → subject → 会话插入，
      与 withdraw 的锁序（consent 行）一致，无死锁环；
    - started_day 取 Asia/Shanghai 自然日（数据库侧换算）；expires_at = 90 天。
    """
    if not isinstance(slide_name, str) or not (1 <= len(slide_name) <= 255):
        raise ResearchStoreError("slide 必须是 1..255 字符的业务切片标识")
    conn = _connect()
    try:
        # 速率限制独立事务先行（拒绝也要推进计数；见 _record_rate_hit）
        retry = _record_rate_hit("research_session_create_user", user_id,
                                 _session_create_limit())
        if retry > 0:
            raise RateLimitedError(retry)
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                consent = _assert_ingest_allowed(cur, user_id, environ)
                subject_id = _ensure_subject(cur, user_id)
                session_id = "rvs_" + secrets.token_urlsafe(18)
                pseudonym = slide_pseudonym(slide_name, cur)
                cur.execute(
                    "INSERT INTO research_viewing_sessions "
                    "(session_id, subject_id, consent_epoch, scope_version, "
                    " schema_version, slide_pseudonym, started_day, "
                    " status, expires_at) VALUES "
                    "(%s,%s,%s,%s,%s,%s,(now() AT TIME ZONE 'Asia/Shanghai')"
                    "::date,'active', now() + make_interval(days=>%s))",
                    (session_id, subject_id, consent["epoch"],
                     consent["scope_version"], SCHEMA_VERSION, pseudonym,
                     SESSION_TTL_DAYS))
                cur.execute(
                    "SELECT session_id, consent_epoch, scope_version, "
                    "schema_version, started_day, status, created_at, "
                    "expires_at FROM research_viewing_sessions "
                    "WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
        return {
            "viewing_session_id": row["session_id"],
            "schema_version": row["schema_version"],
            "consent_epoch": row["consent_epoch"],
            "started_day": row["started_day"].isoformat(),
            "status": row["status"],
            "expires_at": row["expires_at"],
        }
    finally:
        conn.close()


def get_viewing_session(viewing_session_id) -> dict | None:
    """读会话行（研究视图/测试；不回真实文件名——只存伪名）。"""
    if not isinstance(viewing_session_id, str) \
            or not _SESSION_ID_RE.match(viewing_session_id):
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, subject_id, consent_epoch, scope_version, "
                "schema_version, slide_pseudonym, started_day, status, "
                "created_at, expires_at, closed_at "
                "FROM research_viewing_sessions WHERE session_id=%s",
                (viewing_session_id,))
            row = cur.fetchone()
            return dict(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 事件写入（POST /api/research/viewer-events）
# --------------------------------------------------------------------------- #
def append_viewer_events(user_id, viewing_session_id, consent_epoch, events,
                         *, body_bytes=None, environ=None) -> dict:
    """批次写入研究事件（§7.3）。返回 ``{"accepted", "replayed"}``。

    - 批次 ≤50 条、≤64 KiB（body_bytes 由路由层量传，None 时跳过字节检查）；
    - 逐事件白名单校验**先于**任何写入（额外字段整批拒绝，无部分写入）；
    - 主体从 session 解析（session → subject → user），不信任客户端身份；
    - consent_epoch 必须同时等于会话记录值与当前值（旧 grant/离线重传 →
      409 epoch_mismatch；撤回后 → 403，写入被锁序阻断）；
    - (session_id,event_id) 重传幂等：内容一致 → replayed；不同内容 → 409；
      (session_id,seq) 复用 → 409 seq_conflict。
    """
    if not isinstance(events, list) or not events:
        raise EventValidationError("events 必须是非空数组")
    if len(events) > BATCH_MAX_EVENTS:
        raise BatchTooLargeError("单批次最多 %d 条事件" % BATCH_MAX_EVENTS)
    if body_bytes is not None and body_bytes > BATCH_MAX_BYTES:
        raise PayloadTooLargeError(
            "请求体超过 %d 字节上限" % BATCH_MAX_BYTES)
    if not isinstance(viewing_session_id, str) \
            or not _SESSION_ID_RE.match(viewing_session_id):
        raise SessionNotFoundError("会话标识不合法")
    if isinstance(consent_epoch, bool) or not isinstance(consent_epoch, int) \
            or consent_epoch < 1:
        raise EventValidationError("consent_epoch 必须是 >=1 的整数")
    validated = [validate_event(i, ev) for i, ev in enumerate(events)]

    conn = _connect()
    try:
        # 速率限制独立事务先行（拒绝也要推进计数；见 _record_rate_hit）
        retry = _record_rate_hit("research_events_user", user_id,
                                 _events_user_limit())
        if retry > 0:
            raise RateLimitedError(retry)
        retry = _record_rate_hit("research_events_session", viewing_session_id,
                                 _events_session_limit())
        if retry > 0:
            raise RateLimitedError(retry)
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                # §6.3-3：与 withdraw 同一行锁——撤回提交后的写入不成功
                consent = _assert_ingest_allowed(cur, user_id, environ)
                cur.execute(
                    "SELECT s.session_id, s.subject_id, s.consent_epoch, "
                    "s.status, s.expires_at, sub.user_id AS owner_user_id "
                    "FROM research_viewing_sessions s "
                    "JOIN research_subjects sub ON sub.subject_id=s.subject_id "
                    "WHERE s.session_id=%s FOR UPDATE OF s",
                    (viewing_session_id,))
                session = cur.fetchone()
                if session is None or session["owner_user_id"] != user_id:
                    # 伪造/他人会话：不泄露存在性，统一 not found
                    raise SessionNotFoundError("会话不存在")
                if session["expires_at"].timestamp() <= time.time():
                    raise SessionClosedError("会话已过期，需重新创建")
                if session["status"] != "active":
                    raise SessionClosedError("会话已关闭")
                if consent_epoch != session["consent_epoch"]:
                    raise EpochMismatchError(
                        "consent_epoch 与会话记录不一致（session=%s）"
                        % session["consent_epoch"])
                if consent_epoch != consent["epoch"]:
                    raise EpochMismatchError(
                        "consent_epoch 与当前授权不一致（current=%s）"
                        % consent["epoch"])
                accepted = replayed = 0
                for ev in validated:
                    # SAVEPOINT：唯一冲突只回滚本条插入，事务保持可用
                    # （psycopg3 中 UniqueViolation 后必须 ROLLBACK TO 才能继续查询）
                    cur.execute("SAVEPOINT research_event_insert")
                    try:
                        cur.execute(
                            "INSERT INTO research_viewer_events "
                            "(session_id, event_id, seq, action, schema_version,"
                            " consent_epoch, payload, expires_at) VALUES "
                            "(%s,%s,%s,%s,%s,%s,%s, now() + "
                            "make_interval(days=>%s))",
                            (viewing_session_id, ev["event_id"], ev["seq"],
                             ev["action"], ev["schema_version"],
                             consent_epoch,
                             json.dumps(ev["payload"]), RETENTION_DAYS))
                        cur.execute("RELEASE SAVEPOINT research_event_insert")
                        accepted += 1
                        continue
                    except psycopg.errors.UniqueViolation as exc:
                        cur.execute(
                            "ROLLBACK TO SAVEPOINT research_event_insert")
                        # 唯一冲突可能是 PK (session_id,event_id) 或 UNIQUE
                        # (session_id,seq)——PG 报告顺序不定，按 event_id 查询
                        # 区分：有同 id 行 → 比内容（幂等/冲突）；无 → seq 复用
                        cur.execute(
                            "SELECT seq, action, schema_version, payload "
                            "FROM research_viewer_events "
                            "WHERE session_id=%s AND event_id=%s",
                            (viewing_session_id, ev["event_id"]))
                        old = cur.fetchone()
                        if old is None:
                            # (session_id, seq) 唯一：不同事件复用序号
                            raise SeqConflictError(
                                "seq=%s 已被其他事件占用" % ev["seq"]) from exc
                        # 同 event_id 重传：内容一致幂等，不一致冲突
                        if old["seq"] == ev["seq"] \
                                and old["action"] == ev["action"] \
                                and old["schema_version"] == ev["schema_version"] \
                                and old["payload"] == ev["payload"]:
                            replayed += 1
                            continue
                        raise EventIdConflictError(
                            "event_id=%s 已存在且内容不同（不覆盖旧事件）"
                            % ev["event_id"]) from exc
        return {"accepted": accepted, "replayed": replayed}
    finally:
        conn.close()


def count_viewer_events(viewing_session_id) -> int:
    """会话事件数（测试/运维核对；不做研究分析入口）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM research_viewer_events "
                "WHERE session_id=%s", (viewing_session_id,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def subject_for_user(user_id) -> dict | None:
    """user 的研究伪名行（测试/删除链核对）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subject_id, user_id, created_at "
                "FROM research_subjects WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row is not None else None
    finally:
        conn.close()
