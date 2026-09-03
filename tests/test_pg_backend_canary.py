# -*- coding: utf-8 -*-
"""PG backend canary（test-review P0-3）：conftest 恒起内嵌 PG，本 canary 不可跳过。

背景：conftest 在 import 期无条件起 pgserver、设 DATABASE_URL + STORAGE_BACKEND
=postgres 并应用 schema（早于本模块 import，conftest 先加载）。本 canary 反其道而
行：

- **不用** ``importorskip``、**不套** 任何可能触发 skip 的标记；
- 必须真的断言当前 backend 为 postgres 并执行一次最小 SQL，依赖缺失或 env 未生效
  直接 **fail**（红），而非 skip。
"""
import os


def test_backend_is_postgres():
    """实际生效的后端必须是 postgres（证明 conftest 的 PG 路径真实启用）。"""
    from pg_compat import BACKEND
    assert BACKEND == "postgres", (
        "BACKEND=%r：应为 postgres（conftest 未生效？）" % BACKEND)


def test_storage_backend_env_is_postgres():
    assert os.environ.get("STORAGE_BACKEND") == "postgres", (
        "STORAGE_BACKEND=%r（conftest 应已设为 postgres）"
        % os.environ.get("STORAGE_BACKEND"))


def test_minimal_sql_roundtrip():
    """对 conftest 拉起的真实 PG 执行一次最小查询（psycopg 缺失 → 直接报错）。"""
    import psycopg
    uri = os.environ.get("DATABASE_URL") or ""
    assert uri, "conftest 应已在 import 期设置 DATABASE_URL"
    conn = psycopg.connect(uri)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 + 1")
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()
