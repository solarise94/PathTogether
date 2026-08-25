# -*- coding: utf-8 -*-
"""PG backend canary（test-review P0-3）：RUN_PG_TESTS=1 时自身**不可跳过**。

背景：conftest 与多处测试用 ``importorskip("pgserver")`` 兜底「缺依赖则 skip」，
CI 的 PG job 若只设 ``RUN_PG_TESTS=1``，pgserver 缺失/未生效时可能「全部跳过
仍然绿」。本 canary 反其道而行：

- **不用** ``importorskip``、**不套** ``pg_only``/其他可能触发 skip 的标记；
- RUN_PG_TESTS=1 时必须真的断言当前 backend 为 postgres 并执行一次最小 SQL，
  依赖缺失或 env 未生效直接 **fail**（红），而非 skip；
- json 模式（CI 的 json job / 本地默认）用 skipif 跳过，不破坏现有 json 流程。

依赖顺序：conftest 在 RUN_PG_TESTS=1 时于 import 期起 pgserver、设
DATABASE_URL + STORAGE_BACKEND=postgres，且早于本模块 import（conftest 先加载）。
"""
import os

import pytest

_RUN_PG = (os.environ.get("RUN_PG_TESTS") or "").strip() in ("1", "true", "True")

# json 模式整体跳过（唯一允许的 skip 分支：本 canary 只属于 PG job）
pytestmark = pytest.mark.skipif(
    not _RUN_PG,
    reason="PG canary 仅在 RUN_PG_TESTS=1（PG CI job）下有意义",
)


def test_backend_is_postgres():
    """实际生效的后端必须是 postgres（证明 conftest 的 PG 路径真实启用）。"""
    from pg_compat import BACKEND
    assert BACKEND == "postgres", (
        "BACKEND=%r：RUN_PG_TESTS=1 下应为 postgres（conftest 未生效？）" % BACKEND)


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
