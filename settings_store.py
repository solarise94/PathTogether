# -*- coding: utf-8 -*-
"""平台运行时设置存储（platform_settings，docs §7.3）。

目标状态：``platform_settings.registration_open`` 为运行时权威值，env
``REGISTRATION_OPEN`` 只作首次部署的 bootstrap 默认；owner 在用户管理页切换、
多 worker 每次读取一致值，不依赖进程内常量。

后端语义（对齐 platform_features，json/dual fail-closed）：

- ``postgres``：``settings_writable()=True``，读写均落 PG；
- ``json`` / ``dual``：platform_settings 不可写——``settings_writable()=False``，
  读取 fallback 到 env（上层据此 fail-closed 判定，不得假装可写）。

本模块只做数据层；路由与 owner 后台 UI 由后续任务接入。
"""

import os

import psycopg

import pg_store
import platform_features

#: registration_open 的设置键（value 为 JSONB 布尔）
REGISTRATION_OPEN_KEY = "registration_open"

#: registration_open 的 bootstrap env 名（仅 PG 无值时生效，读到后回写 PG）
_REGISTRATION_OPEN_ENV = "REGISTRATION_OPEN"


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
