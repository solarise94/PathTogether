# -*- coding: utf-8 -*-
"""平台运行时设置存储（platform_settings，docs §7.3）。

目标状态：``platform_settings.registration_mode``（closed|invite_only|public，
P0-B §4.1）为运行时权威值；旧布尔 ``registration_open`` 仅保留做 fail-closed
迁移判定（旧 true 不自动映射为开放模式，降级 closed + 告警）。json/dual 后端
platform_settings 不可写，读取 fallback（模式一律 closed，邀请注册 PG-only）。

后端语义（对齐 platform_features，json/dual fail-closed）：

- ``postgres``：``settings_writable()=True``，读写均落 PG；
- ``json`` / ``dual``：platform_settings 不可写——``settings_writable()=False``，
  读取 fallback 到 env（上层据此 fail-closed 判定，不得假装可写）。

本模块只做数据层；路由与 owner 后台 UI 由 app 层接入。
"""

import logging
import os

import psycopg

import pg_store
import platform_features

#: registration_open 的设置键（value 为 JSONB 布尔；**旧布尔**，已被
#: registration_mode 取代，仅保留做 fail-closed 迁移判定与兼容读取）
REGISTRATION_OPEN_KEY = "registration_open"

#: registration_mode 的设置键（value 为 JSONB 字符串 closed|invite_only|public；
#: P0-B 起 §4.1 的运行时权威值）
REGISTRATION_MODE_KEY = "registration_mode"

# --------------------------------------------------------------------------- #
# Batch B（docs review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
# §Batch B 数据模型 4）：user 消费控制目标 + cutover 维护闸。
# --------------------------------------------------------------------------- #
#: user 消费控制目标（JSONB 字符串，只允许 "window"|"total_allowance"）。
#: 只控制 role=user；demo/owner 的窗口语义不受它影响。0029 幂等 seed
#: "window"（部署初期双目标代码并存但行为不变），受控 cutover 以 CAS 切
#: "total_allowance"；回滚走 rollback-plan CAS 切回。
USER_SPEND_TARGET_KEY = "user_spend_target"
USER_SPEND_TARGETS = ("window", "total_allowance")

#: cutover 维护闸（JSONB bool，0029 seed false）：true 时所有 AI dispatch
#: 端点在创建 hold 前稳定返回 503 ai_dispatch_maintenance（wave 2 app.py
#: 接线）。cutover apply 先 CAS false→true，提交后 CAS true→false。
AI_DISPATCH_MAINTENANCE_KEY = "ai_dispatch_maintenance"

#: 合法模式（public 本阶段路由不支持，仅允许出现在存量值中由路由统一拒绝）
REGISTRATION_MODES = ("closed", "invite_only", "public")

#: registration_open 的 bootstrap env 名（仅 PG 无值时生效，读到后回写 PG）
_REGISTRATION_OPEN_ENV = "REGISTRATION_OPEN"

_log = logging.getLogger("svs.settings")


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def settings_writable() -> bool:
    """platform_settings 当前是否可写：仅 postgres 后端为 True。"""
    return platform_features.current_backend() == "postgres"


def get_setting(key, default=None):
    """读一个设置值（JSONB 反序列化后的 Python 值）；无该键返回 default。

    json/dual 后端没有 PG 权威来源，直接返回 default（读取不抛错，配合
    ``settings_writable()=False`` 供上层 fail-closed 判定）。
    """
    if not settings_writable():
        return default
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s", (key,))
                row = cur.fetchone()
        return row["value"] if row is not None else default
    finally:
        conn.close()


def set_setting(key, value, updated_by=None):
    """写一个设置值（任意 JSON 可序列化值，UPSERT）；返回写入后的值。

    json/dual 后端不可写：抛 ``PgFeatureUnavailable``（fail-closed，不静默丢弃）。
    """
    if not settings_writable():
        platform_features.require_pg_backend("platform_settings")
    if not isinstance(key, str) or not key:
        raise ValueError("settings key 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO platform_settings (key, value, updated_at, "
                    "updated_by) VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                    "updated_at=now(), updated_by=EXCLUDED.updated_by "
                    "RETURNING value",
                    (key, psycopg.types.json.Jsonb(value), updated_by),
                )
                row = cur.fetchone()
        return row["value"]
    finally:
        conn.close()


class SettingsVersionConflictError(Exception):
    """CAS 更新未命中：key 不存在或当前值与 expected 不符（409 语义）。

    仿 spend_store.SpendVersionConflictError 模式：``code`` 稳定，供路由层
    映射 409 错误信封；context 只含 key 与 expected/current 等非敏感标量。
    """

    code = "settings_version_conflict"

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


def compare_and_set_setting(key, expected, value, updated_by=None):
    """CAS 写设置值（Batch B 数据模型 4）：当前值精确等于 expected 才写入。

    单条 ``UPDATE platform_settings SET value=%s::jsonb, updated_at=now(),
    updated_by=%s WHERE key=%s AND value=%s::jsonb RETURNING key``——比较与
    写入原子，无 last-write-wins 窗口；未命中（key 不存在或当前值 != expected）
    抛 :class:`SettingsVersionConflictError`（稳定 409 语义，不做 upsert）。

    用途红线：``user_spend_target`` 与 ``ai_dispatch_maintenance`` 的切换
    **必须**走本函数（cutover 脚本）；无版本的 :func:`set_setting` 是
    last-write-wins，禁止用于 cutover 键（spec §Batch B 数据模型 4）。

    json/dual 后端不可写：抛 ``PgFeatureUnavailable``（与其他写路径一致
    fail-closed）。返回写入后的值。
    """
    if not settings_writable():
        platform_features.require_pg_backend("platform_settings")
    if not isinstance(key, str) or not key:
        raise ValueError("settings key 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE platform_settings SET value=%s::jsonb, "
                    "updated_at=now(), updated_by=%s "
                    "WHERE key=%s AND value=%s::jsonb RETURNING value",
                    (psycopg.types.json.Jsonb(value), updated_by, key,
                     psycopg.types.json.Jsonb(expected)),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT value FROM platform_settings WHERE key=%s",
                        (key,))
                    current_row = cur.fetchone()
                    raise SettingsVersionConflictError(
                        "设置已被他人修改（CAS 未命中，请刷新后重试）",
                        key=key,
                        expected=expected,
                        key_exists=current_row is not None,
                        current=(current_row["value"]
                                 if current_row is not None else None))
        return row["value"]
    finally:
        conn.close()


def get_registration_open() -> bool:
    """注册开关权威解析（docs §7.3）。

    解析顺序：
      1. PG platform_settings 有值 → 用 PG（运行时权威）；
      2. 否则 env REGISTRATION_OPEN 作 bootstrap 默认，并 best-effort 回写 PG
         （此后 env 不再参与，做到「env 只作 bootstrap」；回写失败不影响返回）；
      3. json/dual 后端：platform_settings 不可用，直接 fallback env。

    P0-B 起权威开关是 ``registration_mode``（get_registration_mode）；本函数保留
    供旧调用方/测试读取旧布尔键。
    """
    if not settings_writable():
        return platform_features._truthy(os.environ.get(_REGISTRATION_OPEN_ENV))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (REGISTRATION_OPEN_KEY,))
                row = cur.fetchone()
                if row is not None:
                    return bool(row["value"])
                # PG 无值：env 作 bootstrap 默认并回写（同一事务，避免并发双写竞争）
                default = platform_features._truthy(
                    os.environ.get(_REGISTRATION_OPEN_ENV))
                cur.execute(
                    "INSERT INTO platform_settings (key, value, updated_at, "
                    "updated_by) VALUES (%s, %s, now(), 'bootstrap') "
                    "ON CONFLICT (key) DO NOTHING",
                    (REGISTRATION_OPEN_KEY,
                     psycopg.types.json.Jsonb(default)),
                )
                return default
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# registration_mode（P0-B，docs §4.1）：closed | invite_only | public
# --------------------------------------------------------------------------- #
def get_registration_mode() -> str:
    """注册模式权威解析（closed|invite_only|public）。

    - PG：读 ``registration_mode``（JSONB 字符串）；
      * 无该键但旧布尔 ``registration_open=true`` → **fail-closed**：持久化并
        返回 ``closed``（旧 true 绝不自动映射为 invite_only/public，由 owner
        显式切换）+ 告警日志；
      * 无该键且旧布尔缺失/false → bootstrap 为 ``closed`` 并回写；
      * 存量值非法（含手写的 public 之外的乱值）→ 按关闭处理返回 ``closed``；
        合法存量 ``public`` 原样返回（路由层统一 503 public_registration_not_
        supported，本阶段不支持公开注册）。
    - json/dual：一律 ``closed``（邀请注册整体 fail-closed，PG-only）。

    注意：本函数只解析存储值；「invite_only 是否允许生效」的前置条件
    （HTTPS / Secure Cookie / postgres 后端）由 app 层 fail-closed 闸再判定。
    """
    if not settings_writable():
        return "closed"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (REGISTRATION_MODE_KEY,))
                row = cur.fetchone()
                if row is not None:
                    val = row["value"]
                    if isinstance(val, str) and val in REGISTRATION_MODES:
                        return val
                    _log.warning(
                        "platform_settings.%s 存量值非法（%r），按 closed 处理",
                        REGISTRATION_MODE_KEY, val)
                    return "closed"
                # 无 mode 键：检查旧布尔（true 绝不映射为开放模式）
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (REGISTRATION_OPEN_KEY,))
                legacy = cur.fetchone()
                legacy_true = bool(
                    legacy is not None and legacy["value"] is True)
                if legacy_true:
                    _log.warning(
                        "检测到旧 registration_open=true：不自动映射为"
                        " invite_only/public（fail-closed 降级为 closed），"
                        "请 owner 显式切换 registration_mode")
                # bootstrap 写 closed（无论 legacy true/false；幂等）
                cur.execute(
                    "INSERT INTO platform_settings (key, value, updated_at, "
                    "updated_by) VALUES (%s, %s, now(), 'bootstrap') "
                    "ON CONFLICT (key) DO NOTHING",
                    (REGISTRATION_MODE_KEY, psycopg.types.json.Jsonb("closed")),
                )
                return "closed"
    finally:
        conn.close()


def set_registration_mode(mode, updated_by=None) -> str:
    """写注册模式。本阶段只接受 closed / invite_only（public 拒绝）。

    返回写入后的模式。json/dual 后端不可写（PgFeatureUnavailable fail-closed）。
    前置条件（HTTPS / Secure Cookie / PG）由调用方（app 层）先行校验。
    """
    if not settings_writable():
        platform_features.require_pg_backend("platform_settings")
    if mode not in ("closed", "invite_only"):
        raise ValueError(
            "public_registration_not_supported：本阶段仅支持 closed / "
            "invite_only（收到 %r）" % (mode,))
    set_setting(REGISTRATION_MODE_KEY, str(mode), updated_by=updated_by)
    return str(mode)


# --------------------------------------------------------------------------- #
# ai_safety.*：运行时安全参数（批次 F，docs §7.3 阶段 2）
#
# demo_enabled / demo_task_max_steps / platform_task_max_steps /
# own_task_max_steps_limit / demo_max_concurrency 从 ai_budget_periods 列迁居
# platform_settings（0027 backfill 已搬当前周期值）。缺省回落 budget_store
# DEFAULT_* 常量（fail-closed 风格：非法/缺失值不放大权限——demo_enabled
# 回落 False、步数回落保守值）。
# --------------------------------------------------------------------------- #
#: 安全参数 → platform_settings 键（值均为 JSONB 标量）
AI_SAFETY_KEYS = {
    "demo_enabled": "ai_safety.demo_enabled",
    "demo_task_max_steps": "ai_safety.demo_task_max_steps",
    "platform_task_max_steps": "ai_safety.platform_task_max_steps",
    "own_task_max_steps_limit": "ai_safety.own_task_max_steps_limit",
    "demo_max_concurrency": "ai_safety.demo_max_concurrency",
}

#: set_ai_safety_settings 的同事务 audit action（参照 spend_store
#: ENFORCEMENT_MODE_AUDIT_ACTION 模式）
AI_SAFETY_AUDIT_ACTION = "ai_safety.settings_update"


def _ai_safety_defaults() -> dict:
    """缺省值（与 budget_store.DEFAULT_* 常量同源；demo 关闭、步数保守）。"""
    import budget_store
    return {
        "demo_enabled": budget_store.DEFAULT_DEMO_ENABLED,
        "demo_task_max_steps": budget_store.DEFAULT_DEMO_TASK_MAX_STEPS,
        "platform_task_max_steps":
            budget_store.DEFAULT_PLATFORM_TASK_MAX_STEPS,
        "own_task_max_steps_limit":
            budget_store.DEFAULT_OWN_TASK_MAX_STEPS_LIMIT,
        "demo_max_concurrency": budget_store.DEFAULT_DEMO_MAX_CONCURRENCY,
    }


def _read_ai_safety_tx(cur):
    """同事务读五键（缺省/非法值回落默认，get_registration_mode 同款风格）。"""
    out = _ai_safety_defaults()
    cur.execute(
        "SELECT key, value FROM platform_settings WHERE key = ANY(%s)",
        (sorted(AI_SAFETY_KEYS.values()),))
    by_key = {row["key"]: row["value"] for row in cur.fetchall()}
    for field, key in AI_SAFETY_KEYS.items():
        if key not in by_key:
            continue
        raw = by_key[key]
        if field == "demo_enabled":
            if isinstance(raw, bool):
                out[field] = raw
            else:
                _log.warning("platform_settings.%s 存量值非法（%r），按默认"
                             "（False）处理", key, raw)
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            _log.warning("platform_settings.%s 存量值非法（%r），按默认处理",
                         key, raw)
            continue
        if raw < 0:
            _log.warning("platform_settings.%s 存量值为负（%r），按默认处理",
                         key, raw)
            continue
        out[field] = int(raw)
    return out


def get_ai_safety_settings() -> dict:
    """读全部 ai_safety.* 安全参数（单事务五键）。

    - PG：逐键回落默认（缺失/类型非法/负值 → :data:`_ai_safety_defaults`，
      fail-closed：不放大权限）；
    - json/dual：直接返回默认值（platform_settings 不可用，上层按
      ``settings_writable()`` fail-closed）。
    """
    if not settings_writable():
        return _ai_safety_defaults()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _read_ai_safety_tx(cur)
    finally:
        conn.close()


def set_ai_safety_settings(validated, actor_user_id=None, updated_by=None):
    """写 ai_safety.* 安全参数（UPSERT + 同事务 audit，批次 F）。

    ``validated`` 为**已通过 app 层校验**的字段子集 dict（键必须在
    :data:`AI_SAFETY_KEYS` 内；允许部分更新——与 settings.update 逐项提交
    模式对齐）。写入与审计（action=``ai_safety.settings_update``，detail 只
    含字段名与前后值等非敏感标量）**同一事务**，任一失败整体回滚（参照
    spend_store.set_enforcement_mode；CAS 省略——单一 owner 写入口，无并发
    覆盖面）。返回写入后的全量五键快照。
    """
    if not settings_writable():
        platform_features.require_pg_backend("platform_settings")
    if not isinstance(validated, dict) or not validated:
        raise ValueError("validated 需为非空 dict（五安全参数的子集）")
    unknown = set(validated) - set(AI_SAFETY_KEYS)
    if unknown:
        raise ValueError("未知安全参数字段：%s（允许 %s）"
                         % (sorted(unknown), sorted(AI_SAFETY_KEYS)))
    import psycopg.types.json as _pgjson
    import share_store_pg
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                current = _read_ai_safety_tx(cur)
                for field, value in validated.items():
                    cur.execute(
                        "INSERT INTO platform_settings "
                        "(key, value, updated_at, updated_by) "
                        "VALUES (%s, %s, now(), %s) "
                        "ON CONFLICT (key) DO UPDATE SET "
                        "value=EXCLUDED.value, updated_at=now(), "
                        "updated_by=EXCLUDED.updated_by",
                        (AI_SAFETY_KEYS[field], _pgjson.Jsonb(value),
                         updated_by))
                after = _read_ai_safety_tx(cur)
                share_store_pg.record_audit_tx(
                    cur, AI_SAFETY_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="platform_settings",
                    target_id="ai_safety",
                    detail={
                        "fields": sorted(validated),
                        "previous": {k: current[k] for k in validated},
                        "updated": {k: after[k] for k in validated},
                    })
                return after
    finally:
        conn.close()
