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
